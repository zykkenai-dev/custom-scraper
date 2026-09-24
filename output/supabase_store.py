"""Optional Supabase persistence for exported leads.

The scraper remains fully local by default. When both ``SUPABASE_URL`` and
``SUPABASE_SECRET_KEY`` are configured, each successful CLI run also upserts
its leads into the public ``leads`` table through Supabase's REST API.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlparse

import requests

from core.models import Lead


class SupabaseStoreError(RuntimeError):
    """Raised when Supabase is partially configured or cannot save leads."""


def _secret_key() -> str:
    # Accept the legacy variable as a compatibility fallback for older projects.
    return (os.getenv("SUPABASE_SECRET_KEY") or os.getenv("SUPABASE_SERVICE_ROLE_KEY") or "").strip()


def _config() -> tuple[str, str] | None:
    url = (os.getenv("SUPABASE_URL") or "").strip().rstrip("/")
    key = _secret_key()
    if not url and not key:
        return None
    if not url or not key:
        missing = "SUPABASE_URL" if not url else "SUPABASE_SECRET_KEY"
        raise SupabaseStoreError(f"{missing} must be set when Supabase persistence is enabled")
    return url, key


def is_configured() -> bool:
    """Return whether both required Supabase settings are present."""
    try:
        return _config() is not None
    except SupabaseStoreError:
        return False


def _website_key(lead: Lead) -> str:
    website = (lead.website or "").strip()
    if website:
        try:
            host = (urlparse(website).hostname or "").lower().removeprefix("www.")
        except ValueError:
            host = ""
        if host:
            return host
        return website.lower()
    # The CLI normally produces website-backed leads, but this keeps malformed
    # or seed records from collapsing into one row when they have no website.
    return f"name:{(lead.business_name or '').strip().lower()}|{(lead.niche or '').strip().lower()}"


def lead_payload(lead: Lead) -> dict[str, Any]:
    """Convert a Lead to the JSON shape expected by the Supabase table."""
    return {
        "website_key": _website_key(lead),
        "business_name": lead.business_name or "Unknown",
        "niche": lead.niche or "",
        "website": lead.website or "",
        "emails": list(lead.emails),
        "whatsapp_numbers": list(lead.whatsapp_numbers),
        "instagram_handles": list(lead.instagram_handles),
        "linkedin_urls": list(lead.linkedin_urls),
        "phones": list(lead.phones),
        "source_query": lead.source_query or "",
        "quality_label": lead.quality_label,
        "quality_score": lead.quality_score,
        "email_origin": lead.email_origin or "scraped",
        "scraped_at": lead.scraped_at or None,
    }


def save_leads(leads: Iterable[Lead], *, timeout: float = 20.0) -> int:
    """Upsert leads into Supabase and return the number sent.

    No network request is made when Supabase is not configured, preserving the
    original local-only behavior. A configured-but-failed request raises
    ``SupabaseStoreError`` so a CLI run does not claim its data was saved.
    """
    config = _config()
    rows = [lead_payload(lead) for lead in leads]
    if config is None or not rows:
        return 0

    url, key = config
    headers = {
        "apikey": key,
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }
    # Legacy service-role JWTs also accept the Authorization header. New
    # Supabase secret keys are short-lived-format keys and should use apikey
    # alone, so only add this for the legacy JWT form.
    if key.startswith("eyJ"):
        headers["Authorization"] = f"Bearer {key}"

    try:
        response = requests.post(
            f"{url}/rest/v1/leads",
            params={"on_conflict": "website_key"},
            headers=headers,
            json=rows,
            timeout=timeout,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise SupabaseStoreError(f"Could not save leads to Supabase: {exc}") from exc
    return len(rows)
