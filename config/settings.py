"""Central settings loader. Reads .env and exposes typed config."""

import os
import random
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _env_bool(key: str, default: bool = False) -> bool:
    val = os.getenv(key)
    if val is None or not val.strip():
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, default))
    except (TypeError, ValueError):
        return default


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, default))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class ScraperSettings:
    """Runtime settings for the scraper engine."""

    serpapi_key: str = os.getenv("SERPAPI_KEY", "") or ""
    # SerpApi is a last-resort search provider. Keep a monthly reserve and a
    # per-process ceiling so the free allowance cannot be drained by one job.
    serpapi_reserve: int = _env_int("SERPAPI_RESERVE", 25)
    serpapi_max_per_run: int = _env_int("SERPAPI_MAX_PER_RUN", 4)
    scrapingbee_key: str = os.getenv("SCRAPINGBEE_API_KEY", "") or ""
    proxies: list = field(
        default_factory=lambda: [
            p.strip()
            for p in os.getenv("PROXY_LIST", "").split(",")
            if p.strip()
        ]
    )

    request_delay_min: float = _env_float("REQUEST_DELAY_MIN", 1.5)
    request_delay_max: float = _env_float("REQUEST_DELAY_MAX", 3.5)
    timeout_seconds: int = _env_int("TIMEOUT_SECONDS", 20)
    max_concurrent_requests: int = _env_int("MAX_CONCURRENT_REQUESTS", 4)
    user_agent_rotate: bool = _env_bool("USER_AGENT_ROTATE", True)
    max_retries: int = _env_int("MAX_RETRIES", 2)
    search_country: str = (os.getenv("SEARCH_COUNTRY") or "us").strip().lower() or "us"
    verify_emails: bool = _env_bool("VERIFY_EMAILS", True)
    respect_robots: bool = _env_bool("RESPECT_ROBOTS", True)
    # MX records cannot prove that a specific mailbox exists. Guessing is
    # therefore opt-in and always marked as inferred in exported leads.
    infer_emails: bool = _env_bool("INFER_EMAILS", False)
    # Free-lite options (all default-on so it works with zero keys).
    free_js_render: bool = _env_bool("FREE_JS_RENDER", True)
    cache_search: bool = _env_bool("CACHE_SEARCH", True)
    cache_search_hours: int = _env_int("CACHE_SEARCH_HOURS", 168)
    searx_instances: list = field(
        default_factory=lambda: [
            i.strip()
            for i in (
                os.getenv("SEARX_INSTANCES")
                or "https://searx.be,https://priv.au,https://search.bus-hit.me,"
                "https://searx.tiekoetter.com,https://paulgo.io,https://search.hbubli.cc"
            ).split(",")
            if i.strip()
        ]
    )

    @property
    def has_serpapi(self) -> bool:
        return bool(self.serpapi_key)

    @property
    def has_scrapingbee(self) -> bool:
        return bool(self.scrapingbee_key)

    @property
    def has_proxies(self) -> bool:
        return len(self.proxies) > 0

    @property
    def delay_between_requests(self) -> float:
        return round(
            random.uniform(self.request_delay_min, self.request_delay_max), 2
        )


SETTINGS = ScraperSettings()


def get_settings() -> ScraperSettings:
    return SETTINGS
