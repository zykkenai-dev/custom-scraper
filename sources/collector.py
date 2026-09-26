"""Orchestrates lead discovery for a niche.

Flow: niche queries -> search engines (SerpAPI / Bing / DuckDuckGo / Brave /
Ecosia / SearXNG) -> fetch pages -> extract contacts -> niche + corporate
tier filter -> crawl contact pages for emails -> social discovery -> email
enrichment -> qualified leads.
"""

import logging
import math
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

from config.niches import Niche
from config.settings import ScraperSettings
from core.extractor import extract_all, extract_emails, extract_from_html
from core.filter import is_valid_candidate_lead, name_is_denied, url_is_denied
from core.models import ContactInfo, Lead
from core.network import build_session
from sources.page import (
    anchor_links,
    contact_links,
    extract_business_name,
    fetch_page_full,
    html_body_text,
    is_page_url,
)
from sources.search import (
    BingClient,
    BingRssClient,
    BraveClient,
    DuckDuckGoClient,
    EcosiaClient,
    MojeekClient,
    RenderClient,
    SearchClient,
    SearxClient,
)
from sources.social import (
    EmailEnricher,
    InstagramSource,
    LinkedInSource,
    merge_contacts,
)

logger = logging.getLogger(__name__)

# Subpages crawled when the homepage yields no emails.
CONTACT_PATHS = (
    "/contact",
    "/contact-us",
    "/contactus",
    "/contact.html",
    "/get-in-touch",
    "/reach-us",
    "/inquiries",
    "/enquiries",
    "/en",
    "/english",
    "/about",
    "/about-us",
    "/our-team",
    "/team",
    "/staff",
    "/people",
    "/impressum",
    "/imprint",
)

_CACHE_DIR = os.path.join("data", "cache")

# Large targets need more than the four hand-written niche queries. These
# markets turn each query into a distinct local-business search while keeping
# the configured SEARCH_COUNTRY as the source of truth. Unknown countries use
# broad market labels rather than silently falling back to the United States.
_DISCOVERY_MARKETS = {
    "us": (
        "New York NY", "Los Angeles CA", "Chicago IL", "Houston TX",
        "Phoenix AZ", "Philadelphia PA", "San Antonio TX", "San Diego CA",
        "Dallas TX", "Austin TX", "Jacksonville FL", "San Jose CA",
        "Fort Worth TX", "Columbus OH", "Charlotte NC", "Indianapolis IN",
        "Seattle WA", "Denver CO", "Washington DC", "Nashville TN",
        "Boston MA", "Las Vegas NV", "Portland OR", "Detroit MI",
        "Atlanta GA", "Miami FL", "Orlando FL", "Tampa FL",
        "Raleigh NC", "Minneapolis MN", "Salt Lake City UT", "Kansas City MO",
        "Sacramento CA", "Cleveland OH", "Pittsburgh PA", "Cincinnati OH",
        "St Louis MO", "Baltimore MD", "Milwaukee WI", "New Orleans LA",
        "Richmond VA", "Oklahoma City OK", "Louisville KY", "Memphis TN",
        "Birmingham AL", "Charleston SC", "Boise ID", "Omaha NE",
        "Albuquerque NM", "Honolulu HI",
    ),
    "in": (
        "Mumbai", "Delhi", "Bengaluru", "Hyderabad", "Chennai", "Kolkata",
        "Pune", "Ahmedabad", "Jaipur", "Surat", "Lucknow", "Chandigarh",
        "Gurugram", "Noida", "Indore", "Kochi", "Nagpur", "Vadodara",
        "Bhopal", "Coimbatore", "Visakhapatnam", "Nashik", "Bhubaneswar",
        "Patna", "Ludhiana", "Rajkot", "Thane", "Dehradun", "Mysuru",
        "Mangaluru", "Goa", "Raipur", "Ranchi", "Guwahati", "Jodhpur",
    ),
    "gb": (
        "London", "Manchester", "Birmingham", "Leeds", "Glasgow", "Liverpool",
        "Bristol", "Edinburgh", "Sheffield", "Newcastle", "Nottingham",
        "Cardiff", "Belfast", "Leicester", "Brighton", "Cambridge", "Oxford",
    ),
    "ca": (
        "Toronto ON", "Vancouver BC", "Montreal QC", "Calgary AB", "Ottawa ON",
        "Edmonton AB", "Winnipeg MB", "Quebec City QC", "Hamilton ON",
        "Halifax NS", "Victoria BC", "Kitchener ON", "London ON", "Saskatoon SK",
    ),
    "au": (
        "Sydney NSW", "Melbourne VIC", "Brisbane QLD", "Perth WA", "Adelaide SA",
        "Gold Coast QLD", "Canberra ACT", "Newcastle NSW", "Hobart TAS",
        "Darwin NT", "Geelong VIC", "Wollongong NSW",
    ),
    "ae": (
        "Dubai", "Abu Dhabi", "Sharjah", "Ajman", "Ras Al Khaimah",
        "Fujairah", "Al Ain",
    ),
}

_GENERIC_MARKETS = (
    "capital city", "largest city", "north region", "south region",
    "east region", "west region", "central region", "coastal region",
    "business district", "metropolitan area", "near me",
)

_DISCOVERY_QUALIFIERS = (
    "independent", "local", "boutique", "established", "top rated",
)


def _discovery_queries(niche: Niche, max_leads: int, country: str = "us") -> list[str]:
    """Build enough distinct queries for the requested lead volume.

    Four static queries can reasonably feed a small run, but they cannot feed
    a 100-lead target. Historical production yield is roughly two qualified
    leads per query, so large runs get up to one query per two requested leads
    (capped to protect free providers and runtime). Every base query is used
    before location variants are added.
    """
    base = list(dict.fromkeys(q.strip() for q in niche.search_queries if q.strip()))
    if not base:
        return []
    requested = max(1, int(max_leads))
    query_budget = min(60, max(len(base), math.ceil(requested / 2)))
    if query_budget <= len(base):
        return base

    markets = _DISCOVERY_MARKETS.get((country or "").strip().lower(), _GENERIC_MARKETS)
    expanded = list(base)
    # Rotate the base query while advancing through markets. This ensures the
    # first paid fallbacks cover different cities instead of spending the
    # whole per-run allowance on four equivalent searches for one city.
    for round_index in range(len(base)):
        for market_index, market in enumerate(markets):
            query = base[(market_index + round_index) % len(base)]
            expanded.append(f'{query} "{market}"')
            if len(expanded) >= query_budget:
                return expanded
    for qualifier in _DISCOVERY_QUALIFIERS:
        for market in markets:
            for query in base:
                expanded.append(f'{query} "{market}" {qualifier}')
                if len(expanded) >= query_budget:
                    return expanded
    return expanded[:query_budget]


def _results_per_query(max_leads: int) -> int:
    """Ask broad searches for deeper result pages on high-volume runs.

    SerpAPI charges by search request rather than organic row count. Requesting
    up to 100 rows lets the existing four-call fallback budget supply a large
    candidate bank without spending additional free-plan searches.
    """
    return 100 if int(max_leads) >= 50 else 20


def _maps_query(niche: Niche, query: str) -> str | None:
    """Return a simple local-business query for an expanded market search."""
    for base in niche.search_queries:
        prefix = base.strip() + " "
        if not query.startswith(prefix):
            continue
        match = re.fullmatch(r'"([^"]+)"(?:\s+(.+))?', query[len(prefix):].strip())
        if not match:
            continue
        market, qualifier = match.groups()
        business_type = niche.label.split("&", 1)[0].strip()
        return " ".join(part for part in (business_type, market, qualifier or "") if part)
    return None


class SearchSource:
    """Discover business websites via search and harvest their contacts.

    Engine selection: cached results, then free engines, then SerpAPI as a
    quota-protected last resort when every free source is blocked or junk.
    """

    def __init__(self, settings: ScraperSettings, *, enrich: bool = True):
        self.settings = settings
        self.enrich = enrich
        self.session = build_session(settings)
        self.search = SearchClient(settings)
        # Engine clients intentionally use *plain* requests (short timeout,
        # no 3x/backoff retries): a 429/403 from an engine is a deterministic
        # block, so retrying just wastes minutes. Only target-page fetches get
        # the careful rate-limited/retrying session.
        self.ddg = DuckDuckGoClient(settings)
        self.bing_rss = BingRssClient(settings)
        self.bing = BingClient(settings)
        self.mojeek = MojeekClient(settings)
        self.brave = BraveClient(settings)
        self.ecosia = EcosiaClient(settings)
        self.searx = SearxClient(settings, settings.searx_instances)
        self.renderer = RenderClient(settings)
        # Free-engine chain ordered by observed reliability on this network.
        # Brave answers consistently with on-topic results; Bing serves
        # off-topic spam under heavy bot pressures and Mojeek/Ecosia/DDG are
        # frequently captcha'd here. The relevance gate in _discover fails
        # over any engine that returns junk, so the order only matters for
        # speed.
        self.free_engines = [
            self.brave, self.bing_rss, self.bing,
            self.mojeek, self.ecosia, self.ddg, self.searx,
        ]
        # Social enrichment sources use plain requests (short engine timeout +
        # circuit breaker): a blocked DDG lookup must fail in ~10s, not hang
        # on 3x retries via the careful target-page session.
        self.ig_source = InstagramSource(settings)
        self.li_source = LinkedInSource(settings)
        self.email_enricher = EmailEnricher(settings)
        self._worker_ctx = threading.local()

    def _discover(
        self,
        query: str,
        num: int = 10,
        niche: Niche | None = None,
        maps_query: str | None = None,
        allow_paid: bool = True,
    ) -> list:
        """Try cache and free engines before the quota-protected paid fallback.

        Search results are cached to disk (per query). A *fresh* cache (a
        week old at most) means we only probe a couple of engines for new
        URLs and reuse the banked results — never burn the full engine chain
        again in the same week.

        When *niche* is given, engine results are checked for niche relevance
        before being trusted. This is the anti-junk bank: bots sometimes serve
        plausible-looking but totally off-topic pages (dictionary entries, a
        football club, "real" word matches), and we refuse to cache or probe
        those.
        """
        cached = self._clean_candidates(self._load_cached(query)) if self.settings.cache_search else []
        cached_fresh = bool(cached and self._is_cache_fresh(query))

        if cached and cached_fresh:
            # Fresh cache: reuse banked results without touching engines at all.
            # Engines are bot-blocked or connect-blocked from this network, and
            # probing them fresh costs 20-80s of timeouts per query for zero
            # new URLs inside the cache window.
            if niche is None or _results_relevant(cached, niche):
                logger.info("Using cached results for %r (%d urls)", query, len(cached))
                return list(cached)
            logger.info("Cached results for %r fail niche relevance; re-probing", query)
            cached = []
        live = []
        for engine in self.free_engines:
            try:
                urls = engine.search(query, num=num)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Engine %s failed: %s", type(engine).__name__, exc)
                continue
            if not urls:
                continue
            clean = self._clean_candidates(urls)
            if niche is None or _results_relevant(clean, niche):
                live = urls
                logger.debug("Engine %s served %d relevant results for %r",
                             type(engine).__name__, len(clean), query)
                break
            logger.warning("Engine %s returned off-topic results for %r; failing over",
                           type(engine).__name__, query)

        use_paid = self.search.available and allow_paid and (not live or bool(maps_query))
        if use_paid:
            if maps_query and live:
                logger.info(
                    "Supplementing free results for %r with quota-protected SerpAPI Maps",
                    query,
                )
            else:
                logger.info("Free search engines failed for %r; trying quota-protected SerpAPI", query)
            try:
                paid_query = maps_query or query
                paid_engine = "google_maps" if maps_query else "google"
                paid = self._clean_candidates(
                    self.search.search(paid_query, num=num, engine=paid_engine)
                )
                if paid and (niche is None or _results_relevant(paid, niche)):
                    live = _dedupe_urls(live + paid)
                elif paid:
                    logger.warning("SerpAPI returned off-topic results for %r", query)
            except Exception as exc:  # noqa: BLE001
                logger.error("SerpAPI fallback failed: %s", exc)
        merged = self._clean_candidates(_dedupe_urls(cached + live))
        if live or cached:
            self._save_cache(query, merged)
            return merged
        return []

    def _clean_candidates(self, items: list) -> list:
        """Drop URLs that could never be a lead page before they are cached.

        Content pages (blog/news/guides) are first normalised to the site
        root — the homepage is the actual lead page — then junk hosts and
        file paths are rejected.
        """
        cleaned = []
        for item in items:
            url = item["url"] if isinstance(item, dict) else item
            norm = _normalize_candidate_url(url)
            if is_page_url(norm) and not url_is_denied(norm):
                if isinstance(item, dict):
                    item = dict(item)
                    item["url"] = norm
                else:
                    item = norm
                cleaned.append(item)
        return cleaned

    # --- search result cache -------------------------------------------------
    def _cache_path(self, query: str):
        import hashlib

        digest = hashlib.sha256(query.encode("utf-8", "replace")).hexdigest()[:16]
        return os.path.join(_CACHE_DIR, f"{digest}.json")

    def _is_cache_fresh(self, query: str) -> bool:
        path = self._cache_path(query)
        try:
            fresh_until = time.time() - self.settings.cache_search_hours * 3600
            return os.path.exists(path) and os.path.getmtime(path) > fresh_until
        except OSError:
            return False

    def _load_cached(self, query: str) -> list:
        import json

        path = self._cache_path(query)
        try:
            if not os.path.exists(path):
                return []
            if time.time() - os.path.getmtime(path) > self.settings.cache_search_hours * 3600:
                return []
            data = json.loads(open(path, encoding="utf-8").read())
            return data.get("urls", [])
        except (OSError, ValueError, json.JSONDecodeError):
            return []

    def _save_cache(self, query: str, urls: list) -> None:
        import json

        try:
            os.makedirs(_CACHE_DIR, exist_ok=True)
            path = self._cache_path(query)
            payload = {
                "query": query,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "urls": urls,
            }
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
        except OSError as exc:
            logger.debug("Search cache write failed: %s", exc)

    def collect(self, niche: Niche, max_leads: int = 20, skip_domains: set | None = None) -> list:
        """Collect up to max_leads qualified leads for a niche.

        *skip_domains*: set of lowercase domains already captured in prior
        runs; candidates on those domains are skipped so reruns find new
        prospects instead of re-scraping the same ones.

        Candidate URLs are processed with a small thread pool (each thread
        has its own rate-limited session) for real parallel throughput.
        """
        leads: list = []
        seen_urls: set = set()
        candidate_urls: list = []
        prior_domains = skip_domains or set()
        discovered_any = False

        queries = _discovery_queries(
            niche,
            max_leads,
            getattr(self.settings, "search_country", "us"),
        )
        minimum_goal = max(1, math.ceil(max_leads / 2))
        logger.info(
            "Discovery plan for %s: %d queries for target %d (minimum goal %d)",
            niche.id,
            len(queries),
            max_leads,
            minimum_goal,
        )
        results_per_query = _results_per_query(max_leads)
        for query in queries:
            logger.info("Searching: %r", query)
            maps_query = _maps_query(niche, query)
            urls = self._discover(
                query,
                num=results_per_query,
                niche=niche,
                maps_query=maps_query,
                # For large jobs, preserve the paid allowance for Maps
                # searches. Organic fallback calls on the four generic base
                # queries produce far fewer unique business websites.
                allow_paid=max_leads < 50 or maps_query is not None,
            )
            discovered_any = discovered_any or bool(urls)
            for item in urls:
                url = item["url"] if isinstance(item, dict) else item
                title = item.get("title", "") if isinstance(item, dict) else ""
                if (
                    is_page_url(url)
                    and not url_is_denied(url)
                    and url not in seen_urls
                ):
                    seen_urls.add(url)
                    domain = _extract_domain(url)
                    if domain and domain in prior_domains:
                        logger.debug("Already scraped (%s), skipping", domain)
                        continue
                    candidate_urls.append((url, query, title))
            time.sleep(self.settings.delay_between_requests)

        if not discovered_any:
            raise RuntimeError(
                f"No search results for {niche.id}; free engines may be blocked. "
                "Use --seeds or configure SERPAPI_KEY."
            )

        if candidate_urls:
            # Rank candidates by niche relevance so the best matches are
            # probed first, and loudly-junk titles (wrong industry, portals,
            # sign-in pages) are never probed at all. The probe budget below
            # means resources go to real prospects instead of keyword noise.
            ranked = []
            for url, query, title in candidate_urls:
                score = _candidate_score(url, title, niche)
                if score < _CANDIDATE_MIN_SCORE:
                    logger.debug("Low-relevance candidate (%d), skipping: %s", score, url)
                    continue
                ranked.append((score, url, query))
            ranked.sort(key=lambda r: r[0], reverse=True)
            # Search results include blocked sites and pages with no public
            # contact details. A 4x candidate pool gives the collector room
            # to reach at least half of a large requested target in ordinary
            # conditions without probing an unbounded number of websites.
            probe_budget = max(15, max_leads * 4)
            ranked = ranked[:probe_budget]
            logger.info("Ranked %d relevant candidates for %s (probe budget %d)",
                        len(ranked), niche.id, probe_budget)
            candidate_urls = [(url, query) for _s, url, query in ranked]

        logger.info("Found %d new candidate URLs for %s", len(candidate_urls), niche.id)

        workers = max(1, min(self.settings.max_concurrent_requests, len(candidate_urls) or 1))
        if workers > 1:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                future_to_query = {
                    pool.submit(self._process_url_worker, url, niche): (url, query)
                    for url, query in candidate_urls
                }
                for future in as_completed(future_to_query):
                    if len(leads) >= max_leads:
                        break
                    url, query = future_to_query[future]
                    try:
                        contact, name, final_url = future.result()
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("Worker failed: %s", exc)
                        continue
                    if contact is None:
                        continue
                    lead = self._make_lead(name, niche, final_url, contact, query)
                    if lead.has_contact:
                        leads.append(lead)
                        logger.debug("Lead: %s (%s contact points)",
                                     name, _contact_count(contact))
        else:
            for url, query in candidate_urls:
                if len(leads) >= max_leads:
                    break
                contact, name, final_url = self._process_url(url, niche)
                if contact is None:
                    continue
                lead = self._make_lead(name, niche, final_url, contact, query)
                if lead.has_contact:
                    leads.append(lead)
                    logger.debug("Lead: %s (%s contact points)", name, _contact_count(contact))

        logger.info(
            "Target summary for %s: collected %d; minimum %d; requested %d",
            niche.id,
            len(leads),
            minimum_goal,
            max_leads,
        )
        if len(leads) < minimum_goal:
            logger.warning(
                "Minimum result goal not reached for %s: collected %d of %d; "
                "discovery exhausted %d queries and %d candidates",
                niche.id,
                len(leads),
                minimum_goal,
                len(queries),
                len(candidate_urls),
            )
        return leads

    def _make_lead(self, name, niche, url, contact, query) -> Lead:
        return Lead.from_contact(
            business_name=name or "Unknown",
            niche=niche.id,
            website=url,
            contact=contact,
            source_query=query,
        )

    def collect_seeds(self, niche: Niche, urls: list, max_leads: int = 20) -> list:
        """Harvest leads from a caller-provided list of business URLs."""
        pending = []
        seen: set = set()
        for raw in urls:
            url = (raw or "").strip()
            if not url:
                continue
            if not url.startswith("http"):
                url = "https://" + url
            if not is_page_url(url) or url_is_denied(url) or url in seen:
                continue
            seen.add(url)
            pending.append(url)

        leads: list = []
        workers = max(1, min(self.settings.max_concurrent_requests, len(pending) or 1))
        if workers > 1:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                future_to_url = {
                    pool.submit(self._process_url_worker, url, niche): url
                    for url in pending
                }
                for future in as_completed(future_to_url):
                    if len(leads) >= max_leads:
                        break
                    url = future_to_url[future]
                    try:
                        contact, name, final_url = future.result()
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("Seed worker failed: %s", exc)
                        continue
                    if contact is None:
                        continue
                    lead = Lead.from_contact(
                        business_name=name or "Unknown",
                        niche=niche.id,
                        website=final_url,
                        contact=contact,
                        source_query="seed",
                    )
                    if lead.has_contact:
                        leads.append(lead)
                        logger.debug("Seed lead: %s (%s contact points)",
                                     name, _contact_count(contact))
        else:
            for url in pending:
                if len(leads) >= max_leads:
                    break
                contact, name, final_url = self._process_url(url, niche)
                if contact is None:
                    continue
                lead = Lead.from_contact(
                    business_name=name or "Unknown",
                    niche=niche.id,
                    website=final_url,
                    contact=contact,
                    source_query="seed",
                )
                if lead.has_contact:
                    leads.append(lead)
                    logger.debug("Seed lead: %s (%s contact points)", name, _contact_count(contact))
        return leads

    def _process_url(self, url: str, niche: Niche, *, ctx=None):
        """Fetch + extract one URL. Returns (ContactInfo|None, name, final_url).

        *ctx* is an optional per-worker context (thread-local) carrying its
        own rate-limited session so concurrent workers never share mutable
        request state. When None, the collector's own session is used.
        """
        ctx = ctx or self
        logger.info("Probing %s", url)
        try:
            html, final_url = fetch_page_full(ctx.session, ctx.renderer, url)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Skipping %s: %s", url, exc)
            return None, None, url

        if not html or not is_page_url(final_url) or url_is_denied(final_url):
            logger.debug("Empty page: %s", url)
            return None, None, final_url

        body = html_body_text(html)
        rendered = None
        if not is_valid_candidate_lead(body, niche) and self.settings.free_js_render and len(body) < 500:
            rendered = ctx.renderer.render_free(final_url)
            if rendered and is_valid_candidate_lead(rendered, niche):
                body = rendered
        if not is_valid_candidate_lead(body, niche):
            logger.debug("Out of niche scope, skipping: %s", url)
            return None, None, final_url
        links = anchor_links(html)
        contact = extract_all(text=body, links=links)
        # Emails hidden from visible text: Cloudflare shields, %40 encoding.
        html_emails = extract_from_html(html)
        if html_emails.emails:
            contact = merge_contacts(contact, html_emails)
        name = extract_business_name(html, fallback_url=final_url)
        if name_is_denied(name):
            logger.debug("Mega brand, skipping: %s", url)
            return None, None, final_url

        # Crawl contact pages when homepage has no emails.
        if not contact.emails:
            crawler = self._crawl_contact_pages(final_url, niche, homepage_html=html, ctx=ctx)
            contact = merge_contacts(contact, crawler)

        # Free JS render fallback: JS-only sites yield nothing so far, so run
        # the page through the free jina.ai reader and pull emails from it.
        if self.settings.free_js_render and not contact.emails:
            rendered = rendered or ctx.renderer.render_free(final_url)
            if rendered:
                extra = ContactInfo(emails=extract_emails(rendered))
                if extra.emails:
                    contact = merge_contacts(contact, extra)
                    logger.debug("Free-rendered %s found %d emails", final_url, len(extra.emails))

        # Social discovery: search for IG/LinkedIn when not found on-page.
        domain = _extract_domain(final_url)
        if self.enrich and domain:
            if not contact.instagram_handles:
                ig_handles = self._discover_instagram(domain, ctx)
                if ig_handles:
                    extra = ContactInfo(instagram_handles=ig_handles)
                    contact = merge_contacts(contact, extra)
            if not contact.linkedin_urls:
                li_urls = self._discover_linkedin(domain, ctx)
                if li_urls:
                    extra = ContactInfo(linkedin_urls=li_urls)
                    contact = merge_contacts(contact, extra)

        # Email enrichment: validate MX records, drop invalids.
        if self.enrich and contact.emails:
            contact = ctx.email_enricher.enrich(contact)

        # Optional guesses, clearly marked as inferred. An MX record only
        # proves the domain accepts mail, not that any mailbox exists.
        if self.enrich and self.settings.infer_emails and not contact.emails and domain:
            inferred = ctx.email_enricher.infer_emails(domain)
            if inferred:
                contact = merge_contacts(
                    contact, ContactInfo(emails=inferred, email_origin="inferred")
                )
                logger.debug("Inferred unverified mailboxes for %s: %s", domain, inferred)

        return contact, name, final_url

    def _crawl_contact_pages(self, base_url: str, niche: Niche,
                             homepage_html: str = "", ctx=None) -> ContactInfo:
        """Crawl discovered contact/about links, then guessed paths, then merge.

        *homepage_html* (already fetched in the caller) is scanned for real
        contact/about/team links so we hunt the exact URLs a site uses, not
        just the guessed CONTACT_PATHS.

        Only sites whose homepage shows contact signals trigger a crawl, and
        we cap the number of subpage fetches so un-contactable sites can't
        burn our whole rate budget.
        """
        ctx = ctx or self
        discovered = contact_links(homepage_html or "") or []
        if not discovered and not _looks_contactable(homepage_html):
            logger.debug("No contact signals on %s; skipping subpage crawl", base_url)
            return ContactInfo()

        urls = _ordered_contact_urls(base_url, discovered)
        aggregated = ContactInfo()
        misses = 0
        visited_final: set = set()
        for sub_url in urls[:5]:
            if misses >= 3:
                break  # 3 stale probes in a row: stop wasting the budget
            try:
                html, final_url = fetch_page_full(ctx.session, ctx.renderer, sub_url)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Subpage crawl skipped %s: %s", sub_url, exc)
                misses += 1
                continue
            if urlparse(final_url).hostname != urlparse(base_url).hostname:
                misses += 1
                continue
            # Skip pages that redirect onto an already-crawled URL — this
            # kills the /contact -> /contact-us/ re-fetch loop.
            if final_url in visited_final:
                logger.debug("Redirected to already-crawled %s, skipping", final_url)
                continue
            visited_final.add(final_url)
            if not html:
                misses += 1
                continue
            # The homepage already established niche fit. Contact pages often
            # contain only an address and email, with no niche keywords.
            body = html_body_text(html)
            links = anchor_links(html)
            sub_contact = extract_all(text=body, links=links)
            html_emails = extract_from_html(html)
            # Also pull mailto: emails directly from anchor hrefs.
            mailto_emails = _extract_mailto_emails(html)
            if mailto_emails:
                sub_contact.emails = sorted(set(sub_contact.emails) | mailto_emails)
            if html_emails.emails:
                sub_contact = merge_contacts(sub_contact, html_emails)
            if sub_contact.has_anything:
                misses = 0
                aggregated = merge_contacts(aggregated, sub_contact)
                if aggregated.emails:
                    logger.debug("Found emails on %s", sub_url)
                    break
            else:
                misses += 1
        return aggregated

    def _worker_ctx_get(self):
        """Return this thread's isolated session + enrichment stack."""
        ctx = getattr(self._worker_ctx, "ctx", None)
        if ctx is None:
            ctx = _WorkerContext(self.settings)
            self._worker_ctx.ctx = ctx
        return ctx

    def _process_url_worker(self, url: str, niche: Niche):
        """Thread-pool entry point: run _process_url in a worker context."""
        return self._process_url(url, niche, ctx=self._worker_ctx_get())

    def _discover_instagram(self, domain: str, ctx=None) -> list:
        """Use search engines to find IG handles for a domain."""
        ctx = ctx or self
        try:
            handles = ctx.ig_source.find_from_domain(domain)
            if handles:
                logger.debug("Social discovery: IG handles for %s: %s", domain, handles)
            return handles
        except Exception as exc:  # noqa: BLE001
            logger.debug("Instagram discovery failed for %s: %s", domain, exc)
            return []

    def _discover_linkedin(self, domain: str, ctx=None) -> list:
        """Use search engines to find LinkedIn company pages for a domain."""
        ctx = ctx or self
        try:
            urls = ctx.li_source.find_from_domain(domain)
            if urls:
                logger.debug("Social discovery: LinkedIn for %s: %s", domain, urls)
            return urls
        except Exception as exc:  # noqa: BLE001
            logger.debug("LinkedIn discovery failed for %s: %s", domain, exc)
            return []


def _extract_domain(url: str) -> str | None:
    """Extract the bare domain from a URL (no scheme, no www)."""
    try:
        host = urlparse(url).hostname or ""
        return host.lower().removeprefix("www.") or None
    except Exception:  # noqa: BLE001
        return None


# Cutoff below which a search candidate is not worth probing at all.
_CANDIDATE_MIN_SCORE = -2


def _results_relevant(items: list, niche: Niche, min_ratio: float = 0.3) -> bool:
    """True when enough of an engine's results look like the target niche.

    Anti-bot engines occasionally serve an identical off-topic page bank
    (dictionary entries, a football club, single-word matches). We refuse
    to treat such a response as search answers and fail over to the next
    engine instead.
    """
    if not niche or not niche.scope_keywords or not items:
        return True
    hits = 0
    for it in items:
        url = it["url"] if isinstance(it, dict) else it
        title = it.get("title", "") if isinstance(it, dict) else ""
        hay = f"{url} {title or ''}".lower()
        if any(kw in hay for kw in niche.scope_keywords):
            hits += 1
    required = max(1, int(len(items) * min_ratio))
    return hits >= required


# Title fragments that scream "not a prospect we can win" regardless of the
# niche query that surfaced them.
_JUNK_TITLE_SIGNALS = (
    "sign in", "log in", "login", "download", "wikipedia",
    "dictionary", "lyrics", "watch online", "streaming", "live score",
    "highlights", "fixtures", "official website", "news",
    "products for sale", "shop online", "job openings", "careers",
    "free download", "check-in", "track order", "coupon",
)

# A search result pointing at one of these first path segments is almost
# always a content/blog page, not the business homepage we want to scrape.
# We normalise those to the site root before caching/probing.
_CONTENT_PATH_SEGMENTS = (
    "blog", "news", "articles", "insights", "resources", "guides",
    "press", "events", "webinars", "faq", "portfolio", "case-studies",
    "testimonials", "reviews", "media",
)


def _normalize_candidate_url(url: str) -> str:
    """Point known content pages at the site root.

    Searching for "estate agency <contact>" keeps surfacing blog posts like
    ``/blog/7-emails-for-your-estate-agency``. Those pages are never a lead;
    the business lives at the homepage, so rewrite them to ``scheme://host/``.
    """
    try:
        parsed = urlparse(url)
        path = parsed.path.lower().strip("/")
    except ValueError:
        return url
    if not path:
        return url
    first = path.split("/")[0]
    if first in _CONTENT_PATH_SEGMENTS:
        return f"{parsed.scheme}://{parsed.netloc}/"
    return url


def _candidate_score(url: str, title: str, niche: Niche) -> int:
    """Heuristic relevance of a search result to the target niche.

    Positive weight for niche scope keywords appearing in the URL host or
    result title; heavy penalties for exclusion keywords and generic
    non-lead signals. Lower-scoring candidates are dropped first, so noise
    never eats the probe budget.
    """
    if name_is_denied(title):
        return -100
    hay = f"{url} {title or ''}".lower()
    score = 0
    for kw in niche.scope_keywords:
        if kw in hay:
            score += 2
    for kw in niche.exclusion_keywords:
        if kw in hay:
            score -= 4
    for sig in _JUNK_TITLE_SIGNALS:
        if sig in hay:
            score -= 3
    try:
        depth = len(urlparse(url).path.strip("/").split("/")) if urlparse(url).path.strip("/") else 0
    except ValueError:
        depth = 0
    if depth <= 1:
        score += 1  # homepage-ish pages are the real lead targets
    return score


def _dedupe_urls(urls: list) -> list:
    """Dedupe a list of URL strings (or {url,...} dicts), preserving order."""
    seen: set = set()
    out: list = []
    for item in urls:
        url = item["url"] if isinstance(item, dict) else item
        if url in seen:
            continue
        seen.add(url)
        out.append(item)
    return out


class _WorkerContext:
    """Per-thread, self-contained scraping context for concurrent workers.

    Each worker gets its own rate-limited session, renderer, and enrichment
    stack so nothing mutable is shared between threads (requests.Session and
    our rate limiter are not thread-safe).
    """

    def __init__(self, settings: ScraperSettings):
        self.settings = settings
        self.session = build_session(settings)
        self.renderer = RenderClient(settings)
        # Plain requests + short timeouts (circuit breaker in DDG client).
        self.ig_source = InstagramSource(settings)
        self.li_source = LinkedInSource(settings)
        self.email_enricher = EmailEnricher(settings)


def _ordered_contact_urls(base_url: str, discovered: list) -> list:
    """Order contact pages to crawl: discovered first, then guessed paths,
    de-duplicated."""
    urls = []
    seen = set()
    for rel in list(discovered) + list(CONTACT_PATHS):
        abs_url = urljoin(base_url, rel)
        if urlparse(abs_url).hostname != urlparse(base_url).hostname or not is_page_url(abs_url):
            continue
        if abs_url in seen or abs_url == base_url.rstrip("/") + "/":
            continue
        seen.add(abs_url)
        urls.append(abs_url)
    return urls


# Words that signal a homepage is likely to have a crawlable contact page.
_CONTACTABLE_HINTS = (
    "contact", "email us", "get in touch", "reach us", "inquiries",
    "enquiries", "write to us", "talk to us", "our team", "our staff",
    "about us", "we would love to hear", "send us a message",
    "mailto", "phone", "call us",
)


def _looks_contactable(html: str) -> bool:
    """True when the homepage text hints at real contact/team pages."""
    if not html:
        return False
    lowered = html_body_text(html).lower()[:20000]
    return any(hint in lowered for hint in _CONTACTABLE_HINTS)


def _extract_mailto_emails(html: str) -> set:
    """Pull email addresses from mailto: anchor hrefs in raw HTML."""
    import re

    emails: set = set()
    # Match href="mailto:someone@example.com" and variants
    for match in re.finditer(r'href\s*=\s*["\']mailto:([^"\'>\s]+)', html, re.IGNORECASE):
        email = match.group(1).strip().lower().split("?")[0]
        if "@" in email and "." in email.split("@")[-1]:
            emails.add(email)
    return emails


def _contact_count(info: ContactInfo) -> int:
    return (
        len(info.emails)
        + len(info.whatsapp_numbers)
        + len(info.instagram_handles)
        + len(info.linkedin_urls)
        + len(info.phones)
    )
