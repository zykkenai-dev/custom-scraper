"""Search clients: SerpAPI (paid), Bing/DuckDuckGo/Mojeek (free), ScrapingBee.

Contract: every client implements
    search(query, num) -> list[dict]   # {"url": str, "title": str}
The collector uses `title` to verify engine results are actually relevant
(anti-bot engines sometimes return junk) and falls back to the next engine.
"""

import base64
import logging
import random
import threading
import time
from urllib.parse import parse_qs, unquote, urlparse

import requests
from bs4 import BeautifulSoup

from config.settings import ScraperSettings

logger = logging.getLogger(__name__)

SERPAPI_ENDPOINT = "https://serpapi.com/search.json"
SERPAPI_ACCOUNT_ENDPOINT = "https://serpapi.com/account.json"
SCRAPINGBEE_ENDPOINT = "https://app.scrapingbee.com/api/v1/"

# Engine lookups fail fast: a blocked engine punishes per-request retries,
# so give each attempt a short ceiling (10s) instead of the page-fetch budget.
def _engine_timeout(settings) -> int:
    return min(getattr(settings, "timeout_seconds", 20), 10)

# ---- DuckDuckGo circuit breaker ----
# When DDG gets itself blocked/unreachable, every lookup used to burn a full
# connect timeout (10-20s) *per domain during enrichment*. After the first
# failure we remember it's down for a window and short-circuit all callers.
_DDG_DOWN_UNTIL = 0.0


def _ddg_is_down() -> bool:
    return time.time() < _DDG_DOWN_UNTIL


def _mark_ddg_down(cooldown: int = 600) -> None:
    global _DDG_DOWN_UNTIL
    _DDG_DOWN_UNTIL = time.time() + cooldown


class SerpAPIError(RuntimeError):
    pass


class SearchClient:
    """Wraps SerpAPI organic search results."""

    def __init__(self, settings: ScraperSettings):
        self._settings = settings
        self._api_key = settings.serpapi_key
        self._available = bool(self._api_key)
        self._session = requests.Session()
        self._used_this_run = 0
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return self._available

    def search(self, query: str, num: int = 10, engine: str = "google") -> list:
        """Return organic results while preserving the configured free quota."""
        if not self._available:
            logger.info("SerpAPI key missing; skipping search for %r", query)
            return []
        with self._lock:
            return self._search_with_budget(query, num, engine)

    def _search_with_budget(self, query: str, num: int, engine: str) -> list:
        per_run = max(0, self._settings.serpapi_max_per_run)
        if self._used_this_run >= per_run:
            logger.warning("SerpAPI per-run fallback budget reached (%d); using free engines only", per_run)
            return []
        if not self._quota_available():
            return []
        params = {
            "q": query,
            "engine": engine,
            "api_key": self._api_key,
            "num": min(num, 100),
            "hl": "en",
            "gl": self._settings.search_country,
        }
        try:
            resp = self._session.get(SERPAPI_ENDPOINT, params=params, timeout=30)
            if resp.status_code >= 400:
                raise SerpAPIError(f"HTTP {resp.status_code}")
            data = resp.json()
            if data.get("error"):
                raise SerpAPIError("API returned an error")
        except (requests.RequestException, ValueError, SerpAPIError) as exc:
            # Never include the request URL in logs because it carries the key.
            detail = str(exc) if isinstance(exc, SerpAPIError) else type(exc).__name__
            logger.error("SerpAPI request failed: %s", detail)
            raise SerpAPIError(detail) from exc

        # SerpApi counts successful responses even when organic_results is
        # empty, so account for the call before parsing its payload.
        self._used_this_run += 1

        results: list[dict] = []
        for item in data.get("organic_results", []):
            link = (item.get("link") or "").strip()
            title = (item.get("title") or "").strip()
            if link.startswith("http"):
                results.append({"url": link, "title": title})
        return _dedupe_results(results)

    def _quota_available(self) -> bool:
        """Check SerpApi's free Account API before spending a search credit."""
        try:
            resp = self._session.get(
                SERPAPI_ACCOUNT_ENDPOINT,
                params={"api_key": self._api_key},
                timeout=10,
            )
            if resp.status_code >= 400:
                logger.warning("SerpAPI quota check returned HTTP %s; preserving quota", resp.status_code)
                return False
            data = resp.json()
            remaining = int(data.get("plan_searches_left", data.get("total_searches_left", 0)))
        except (requests.RequestException, ValueError, TypeError):
            logger.warning("SerpAPI quota check failed; preserving quota")
            return False
        reserve = max(0, self._settings.serpapi_reserve)
        if remaining <= reserve:
            logger.warning("SerpAPI monthly reserve reached (%d searches left); using free engines only", remaining)
            return False
        return True


class DuckDuckGoClient:
    """Free search engine client scraping DuckDuckGo's HTML endpoint.

    DuckDuckGo intermittently serves bot-challenge pages (HTTP 202 / animated
    `anon` captcha shells) to datacenter IPs. We detect those and retry once
    with a POST body (which often sneaks past the GET-only check) before
    giving up so the collector can fail over to the next engine.
    """

    SEARCH_URL = "https://html.duckduckgo.com/html/"

    def __init__(self, settings: ScraperSettings, session=None):
        self._settings = settings
        self._session = session

    @property
    def available(self) -> bool:
        return True

    def search(self, query: str, num: int = 10) -> list:
        """Return deduped organic results as [{"url", "title"}, ...]."""
        if _ddg_is_down():
            return []
        params = {
            "q": query,
            "kl": f"{self._settings.search_country}-en",
        }
        first = self._request(self.SEARCH_URL, params=params)
        # Only bother with the POST fallback when DDG actually answered with a
        # challenge page. A connect timeout means the network can't reach DDG
        # at all — POSTing just doubles the delay for nothing.
        if (
            first is None
            or not isinstance(first, requests.Response)
            or not getattr(first, "status_code", 0) == 200
            or _is_bot_page(first.text)
        ):
            if isinstance(first, requests.Response) and _is_bot_page(first.text):
                logger.info("DuckDuckGo GET challenged for %r; retrying with POST", query)
                second = self._request(self.SEARCH_URL, params=params, method="POST")
                if isinstance(second, requests.Response) and not _is_bot_page(second.text):
                    first = second
        if not isinstance(first, requests.Response) or first.status_code != 200:
            # Either the network can't reach DDG or it served a challenge
            # page (202/captcha) — both mean the engine is unusable for a
            # while, so trip the breaker so later lookups skip it instantly.
            _mark_ddg_down()
            logger.warning("DuckDuckGo unavailable for %r", query)
            return []
        return self._parse(first.text, num)

    def _request(self, url: str, *, params: dict, method: str = "GET"):
        """Perform GET (or POST) against DDG. Returns Response or None."""
        try:
            if method.upper() == "POST":
                if self._session is not None:
                    return self._session.post(url, data=params, headers={**_browser_headers(), "Referer": "https://duckduckgo.com/"})
                return requests.post(
                    url,
                    data=params,
                    headers={**_browser_headers(), "Referer": "https://duckduckgo.com/"},
                    timeout=_engine_timeout(self._settings),
                )
            if self._session is not None:
                return self._session.get(url, params=params, headers=_browser_headers())
            return requests.get(
                url,
                params=params,
                headers=_browser_headers(),
                timeout=_engine_timeout(self._settings),
            )
        except requests.RequestException as exc:
            logger.warning("DuckDuckGo request failed for %r: %s", params.get("q"), exc)
            return None

    def _parse(self, html: str, num: int) -> list:
        results: list[dict] = []
        try:
            soup = BeautifulSoup(html, "lxml")
            for elem in soup.select(".result"):
                anchor = elem.select_one("a.result__a")
                snippet = elem.select_one(".result__snippet")
                if not anchor:
                    continue
                href = anchor.get("href", "")
                url = _unwrap_ddg_redirect(href)
                title = anchor.get_text(" ", strip=True)
                if url and url.startswith("http"):
                    results.append(
                        {"url": url, "title": title or (snippet.get_text(" ", strip=True) if snippet else "")}
                    )
        except Exception as exc:  # noqa: BLE001
            logger.warning("DuckDuckGo parse failed: %s", exc)
        return _dedupe_results(results)[:num]


class MojeekClient:
    """Free Mojeek search client; independent index as a fallback engine."""

    SEARCH_URL = "https://www.mojeek.com/search"

    def __init__(self, settings: ScraperSettings, session=None):
        self._settings = settings
        self._session = session

    @property
    def available(self) -> bool:
        return True

    def search(self, query: str, num: int = 10) -> list:
        params = {"q": query}
        try:
            if self._session is not None:
                resp = self._session.get(self.SEARCH_URL, params=params, headers=_browser_headers())
            else:
                resp = requests.get(
                    self.SEARCH_URL,
                    params=params,
                    headers=_browser_headers(),
                    timeout=_engine_timeout(self._settings),
                )
            if resp.status_code != 200 or _is_bot_page(resp.text):
                logger.warning("Mojeek returned %s for %r%s",
                               resp.status_code, query,
                               " (bot-checked)" if resp.status_code == 200 else "")
                return []
        except requests.RequestException as exc:
            logger.warning("Mojeek request failed for %r: %s", query, exc)
            return []

        results: list[dict] = []
        try:
            soup = BeautifulSoup(resp.text, "lxml")
            for elem in soup.select(".results-standard .result, ul.results-standard li"):
                anchor = None
                for sel in ("h2 a", "a.ob", "a.title"):
                    anchor = elem.select_one(sel)
                    if anchor:
                        break
                if not anchor:
                    continue
                href = anchor.get("href", "")
                url = href if href.startswith("http") else None
                title = anchor.get_text(" ", strip=True)
                if url:
                    results.append({"url": url, "title": title})
        except Exception as exc:  # noqa: BLE001
            logger.warning("Mojeek parse failed for %r: %s", query, exc)
        return _dedupe_results(results)[:num]


class BingClient:
    """Free Bing search client; primary free engine.

    Bing wraps result links in a base64-encoded `u` query param we decode.
    """

    SEARCH_URL = "https://www.bing.com/search"
    # Bing flags links to some no-value properties; skip those entirely.

    def __init__(self, settings: ScraperSettings, session=None):
        self._settings = settings
        self._session = session

    @property
    def available(self) -> bool:
        return True

    def search(self, query: str, num: int = 10) -> list:
        params = {
            "q": query,
            "count": min(num, 30),
            "cc": self._settings.search_country,
            "setlang": "en",
        }
        try:
            if self._session is not None:
                resp = self._session.get(self.SEARCH_URL, params=params, headers=_browser_headers())
            else:
                resp = requests.get(
                    self.SEARCH_URL,
                    params=params,
                    headers=_browser_headers(),
                    timeout=_engine_timeout(self._settings),
                )
            if resp.status_code != 200 or _is_bot_page(resp.text):
                logger.warning("Bing returned %s for %r%s",
                               resp.status_code, query,
                               " (bot-checked)" if resp.status_code == 200 else "")
                return []
        except requests.RequestException as exc:
            logger.warning("Bing request failed for %r: %s", query, exc)
            return []

        results: list[dict] = []
        try:
            soup = BeautifulSoup(resp.text, "lxml")
            for elem in soup.select("li.b_algo"):
                anchor = elem.select_one("h2 a")
                if not anchor:
                    continue
                href = anchor.get("href", "")
                url = _unwrap_bing_redirect(href)
                title = anchor.get_text(" ", strip=True)
                if url and url.startswith("http"):
                    results.append({"url": url, "title": title})
        except Exception as exc:  # noqa: BLE001
            logger.warning("Bing parse failed for %r: %s", query, exc)
        return _dedupe_results(results)[:num]


class BingRssClient:
    """Free Bing client using its RSS endpoint (lighter than the HTML page).

    Returns the destination URL directly (RSS links are not wrapped in the
    base64 `ck/a` redirect), which keeps the fallback chain working even when
    the HTML page is bot-checked.
    """

    SEARCH_URL = "https://www.bing.com/search"

    def __init__(self, settings: ScraperSettings, session=None):
        self._settings = settings
        self._session = session

    @property
    def available(self) -> bool:
        return True

    def search(self, query: str, num: int = 10) -> list:
        params = {
            "q": query,
            "format": "rss",
            "count": min(num, 10),
            "cc": self._settings.search_country,
            "setlang": "en",
        }
        try:
            if self._session is not None:
                resp = self._session.get(self.SEARCH_URL, params=params, headers=_browser_headers())
            else:
                resp = requests.get(
                    self.SEARCH_URL,
                    params=params,
                    headers=_browser_headers(),
                    timeout=_engine_timeout(self._settings),
                )
            if resp.status_code != 200 or _is_bot_page(resp.text):
                logger.warning("Bing RSS returned %s for %r%s",
                               resp.status_code, query,
                               " (bot-checked)" if resp.status_code == 200 else "")
                return []
        except requests.RequestException as exc:
            logger.warning("Bing RSS request failed for %r: %s", query, exc)
            return []

        results: list[dict] = []
        try:
            soup = BeautifulSoup(resp.text, "xml")
            for item in soup.find_all("item"):
                title = item.find("title")
                link = item.find("link")
                title_text = title.get_text(" ", strip=True) if title else ""
                url = link.get_text(" ", strip=True) if link else ""
                if url and url.startswith("http") and url not in results:
                    results.append({"url": url, "title": title_text})
        except Exception as exc:  # noqa: BLE001
            logger.warning("Bing RSS parse failed for %r: %s", query, exc)
        return _dedupe_results(results)[:num]


class BraveClient:
    """Free Brave Search client. Independent index, decent bot tolerance."""

    SEARCH_URL = "https://search.brave.com/search"

    def __init__(self, settings: ScraperSettings, session=None):
        self._settings = settings
        self._session = session

    @property
    def available(self) -> bool:
        return True

    def search(self, query: str, num: int = 10) -> list:
        params = {"q": query, "source": "web"}
        try:
            if self._session is not None:
                resp = self._session.get(self.SEARCH_URL, params=params, headers=_browser_headers())
            else:
                resp = requests.get(
                    self.SEARCH_URL,
                    params=params,
                    headers=_browser_headers(),
                    timeout=_engine_timeout(self._settings),
                )
            if resp.status_code != 200 or _is_bot_page(resp.text):
                logger.warning("Brave returned %s for %r%s",
                               resp.status_code, query,
                               " (bot-checked)" if resp.status_code == 200 else "")
                return []
        except requests.RequestException as exc:
            logger.warning("Brave request failed for %r: %s", query, exc)
            return []

        results: list[dict] = []
        try:
            soup = BeautifulSoup(resp.text, "lxml")
            # Brave: .snippet-title anchors. Fall back to any http link in .snippet.
            for elem in soup.select(".snippet"):
                anchor = elem.select_one("a.snippet-title") or elem.select_one("a[href^='http']")
                if not anchor:
                    continue
                href = anchor.get("href", "")
                if not href.startswith("http"):
                    continue
                title = anchor.get_text(" ", strip=True)
                results.append({"url": href, "title": title})
        except Exception as exc:  # noqa: BLE001
            logger.warning("Brave parse failed for %r: %s", query, exc)
        return _dedupe_results(results)[:num]


class EcosiaClient:
    """Free Ecosia client (Bing-backed, ad-free JSON in <script> window var)."""

    SEARCH_URL = "https://www.ecosia.org/search"

    def __init__(self, settings: ScraperSettings, session=None):
        self._settings = settings
        self._session = session

    @property
    def available(self) -> bool:
        return True

    def search(self, query: str, num: int = 10) -> list:
        params = {"q": query, "mkt": self._settings.search_country}
        try:
            if self._session is not None:
                resp = self._session.get(self.SEARCH_URL, params=params, headers=_browser_headers())
            else:
                resp = requests.get(
                    self.SEARCH_URL,
                    params=params,
                    headers=_browser_headers(),
                    timeout=_engine_timeout(self._settings),
                )
            if resp.status_code != 200 or _is_bot_page(resp.text):
                logger.warning("Ecosia returned %s for %r%s",
                               resp.status_code, query,
                               " (bot-checked)" if resp.status_code == 200 else "")
                return []
        except requests.RequestException as exc:
            logger.warning("Ecosia request failed for %r: %s", query, exc)
            return []

        results: list[dict] = []
        try:
            soup = BeautifulSoup(resp.text, "lxml")
            for elem in soup.select(".result"):
                anchor = elem.select_one("a.result__link, a[href^='http']")
                if not anchor:
                    continue
                href = anchor.get("href", "")
                title = anchor.get_text(" ", strip=True)
                url = _unwrap_ecosia_redirect(href)
                if url and url.startswith("http"):
                    results.append({"url": url, "title": title})
        except Exception as exc:  # noqa: BLE001
            logger.warning("Ecosia parse failed for %r: %s", query, exc)
        return _dedupe_results(results)[:num]


class SearxClient:
    """Free SearXNG meta-search client over a pool of public instances.

    SearXNG aggregates Google/Bing/Brave behind a permissive front-end. The
    pool means no single bot-checked IP sinks discovery: we try instances in
    random order until one answers.
    """

    def __init__(self, settings: ScraperSettings, instances: list, session=None):
        self._settings = settings
        self._instances = [i.rstrip("/") for i in instances if i]
        self._session = session

    @property
    def available(self) -> bool:
        return bool(self._instances)

    def search(self, query: str, num: int = 10) -> list:
        pool = list(self._instances)
        import random

        random.shuffle(pool)
        # Cap attempts: instance connect timeouts each cost up to the engine
        # timeout, and if 3 instances won't answer a query the rest won't either.
        for instance in pool[: min(3, len(pool))]:
            results = self._try_instance(instance, query, num)
            if results:
                return results
        return []

    def _try_instance(self, instance: str, query: str, num: int) -> list:
        params = {"q": query}
        try:
            if self._session is not None:
                resp = self._session.get(instance + "/search", params=params, headers=_browser_headers())
            else:
                resp = requests.get(
                    instance + "/search",
                    params=params,
                    headers=_browser_headers(),
                    timeout=_engine_timeout(self._settings),
                )
            if resp.status_code != 200 or _is_bot_page(resp.text):
                return []
        except requests.RequestException:
            return []

        results: list[dict] = []
        try:
            soup = BeautifulSoup(resp.text, "lxml")
            for article in soup.select("article.result"):
                anchor = article.select_one("h3 a") or article.select_one("a.url_header")
                if not anchor:
                    continue
                href = anchor.get("href", "")
                # SearXNG may serve internal redirect links.
                url = _unwrap_searx_url(href)
                title = anchor.get_text(" ", strip=True)
                if url and url.startswith("http"):
                    results.append({"url": url, "title": title})
        except Exception:  # noqa: BLE001
            return []
        return _dedupe_results(results)[:num]


def _browser_headers() -> dict:
    return {
        "User-Agent": _user_agent(),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }


# Markers that reveal a search endpoint served a bot-check page instead of
# real organic results. When detected the engine counts as "failed" so the
# collector can fail over to the next engine rather than scrape junk.
_BOT_PAGE_MARKERS = (
    "captcha",
    "unusual traffic",
    "confirm you're not a robot",
    "verify you are human",
    "please enable javascript",
    "challenge-platform",
    "anomaly detector",
    "access denied",
    "are you a robot",
)


def _is_bot_page(text: str) -> bool:
    """True when a search response is a bot-check / challenge page."""
    if not text:
        return True
    lowered = text.lower()[:20000]
    for marker in _BOT_PAGE_MARKERS:
        if marker in lowered:
            return True
    return False


def _user_agent() -> str:
    from core.network import USER_AGENTS

    return random.choice(USER_AGENTS)


def _unwrap_ddg_redirect(href: str) -> str | None:
    """DuckDuckGo wraps results in /l/?uddg=<encoded-url>&rut=..."""
    try:
        parts = urlparse(href)
        if "uddg" in parts.query:
            uddg = parse_qs(parts.query).get("uddg", [""])[0]
            return unquote(unquote(uddg))
    except ValueError:
        return None
    return None


def _unwrap_bing_redirect(href: str) -> str | None:
    """Bing wraps results in /ck/a?...&u=<a1 + base64(URL)>&ntb=1.

    The `u` param is base64 of the destination URL, prefixed with `a1`
    (protocol marker) and missing its trailing padding.
    """
    if "bing.com/ck/" not in href:
        return href if href.startswith("http") else None
    try:
        parts = urlparse(href)
        raw = parse_qs(parts.query).get("u", [""])[0]
        if not raw:
            return None
        body = raw[2:] if raw[:2] == "a1" else raw
        pad = "=" * ((4 - len(body) % 4) % 4)
        url = base64.b64decode(body + pad).decode("utf-8", "replace")
        return url if url.startswith("http") else None
    except (ValueError, base64.binascii.Error, KeyError):
        return None


def _unwrap_ecosia_redirect(href: str) -> str | None:
    """Ecosia wraps results in an internal redirect carrying the real URL
    either as a ``u``/_escaped_fragment_ query param or in ``data-url``."""
    if href.startswith("http") and "ecosia.org/redirect" not in href:
        return href
    try:
        parts = urlparse(href)
        for key in ("u", "target", "url"):
            vals = parse_qs(parts.query).get(key)
            if vals:
                return unquote(vals[0]) or None
    except ValueError:
        return None
    return None


def _unwrap_searx_url(href: str) -> str | None:
    """SearXNG sometimes links results through its own /search?q= redirect
    that re-URL-encodes the destination. Recover the http target."""
    if href.startswith("http") and "/search?" not in href:
        return href
    try:
        parts = urlparse(href)
        # Rewrite: search?q=<encoded-encoded-url>#...   -> decode twice.
        q = parse_qs(parts.query).get("q", [""])[0]
        if not q:
            return None
        url = unquote(unquote(q))
        return url if url.startswith("http") else None
    except (ValueError, KeyError):
        return None


class RenderClient:
    """Renders JS-heavy pages via ScrapingBee returnHTML.

    When no ScrapingBee key is set, ``render_free`` can fall back to the free
    r.jina.ai reader proxy (rate-limited, no key) so JS-only sites still give
    up their contact info at zero cost.
    """

    def __init__(self, settings: ScraperSettings):
        self._api_key = settings.scrapingbee_key
        self._available = bool(self._api_key)
        self._session = requests.Session()

    @property
    def available(self) -> bool:
        return self._available

    def render(self, url: str, *, wait: int = 1500) -> str | None:
        """Return rendered HTML of a URL, or None on failure/unavailable."""
        if not self._available:
            return None
        params = {
            "api_key": self._api_key,
            "url": url,
            "render_js": "true",
            "wait": wait,
            "premium_proxy": "true",
        }
        try:
            resp = self._session.get(SCRAPINGBEE_ENDPOINT, params=params, timeout=40)
            if resp.status_code != 200:
                logger.warning("ScrapingBee returned %s for %s", resp.status_code, url)
                return None
            return resp.text
        except requests.RequestException as exc:
            logger.debug("ScrapingBee render failed for %s: %s", url, exc)
            return None

    def render_free(self, url: str, timeout: int = 15) -> str | None:
        """Render a URL via the free jina.ai reader proxy (no API key).

        Sends back the page text as markdown; extraction still runs our
        email/phone/social regexes on it. Failures return None quietly.
        """
        reader_url = "https://r.jina.ai/" + url
        try:
            resp = self._session.get(
                reader_url,
                headers={"User-Agent": _user_agent(), "X-Return-Format": "text"},
                timeout=timeout,
            )
            if resp.status_code == 200 and resp.text:
                return resp.text
            logger.debug("jina reader returned %s for %s", resp.status_code, url)
            return None
        except requests.RequestException as exc:
            logger.debug("jina reader failed for %s: %s", url, exc)
            return None


def _dedupe_results(results: list) -> list:
    """Dedupe search results by URL, preserving order."""
    seen: set = set()
    result: list = []
    for r in results:
        url = r["url"] if isinstance(r, dict) else r
        if url in seen:
            continue
        seen.add(url)
        result.append(r)
    return result
