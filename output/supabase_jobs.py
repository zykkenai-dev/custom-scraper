"""Supabase REST helpers for durable dashboard scrape jobs and lead reads."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from typing import Any

import requests


class SupabaseJobError(RuntimeError):
    """Raised when a Supabase job/lead request cannot be completed."""


def _config() -> tuple[str, str] | None:
    url = (os.getenv("SUPABASE_URL") or "").strip().rstrip("/")
    key = (
        os.getenv("SUPABASE_SECRET_KEY")
        or os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        or ""
    ).strip()
    if not url and not key:
        return None
    if not url or not key:
        raise SupabaseJobError(
            "SUPABASE_URL and SUPABASE_SECRET_KEY must both be configured"
        )
    return url, key


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _request(method: str, path: str, **kwargs) -> requests.Response:
    config = _config()
    if config is None:
        raise SupabaseJobError("Supabase is not configured")
    url, key = config
    headers = dict(kwargs.pop("headers", {}))
    headers.setdefault("apikey", key)
    headers.setdefault("Content-Type", "application/json")
    if key.startswith("eyJ"):
        headers.setdefault("Authorization", f"Bearer {key}")
    try:
        response = requests.request(
            method, f"{url}/rest/v1/{path}", headers=headers,
            timeout=kwargs.pop("timeout", 20), **kwargs,
        )
        response.raise_for_status()
        return response
    except requests.RequestException as exc:
        raise SupabaseJobError(f"Supabase request failed: {exc}") from exc


def _json(response: requests.Response) -> Any:
    if not response.content:
        return []
    try:
        return response.json()
    except ValueError as exc:
        raise SupabaseJobError("Supabase returned invalid JSON") from exc


def create_job(options: dict[str, Any], requested_by: str) -> str:
    """Insert a queued job and return its UUID."""
    response = _request(
        "POST", "scrape_jobs",
        params={"select": "id"},
        headers={"Prefer": "return=representation"},
        json={
            "status": "queued",
            "requested_by": (requested_by or "")[:200],
            "options": options,
        },
    )
    rows = _json(response)
    if not isinstance(rows, list) or not rows or "id" not in rows[0]:
        raise SupabaseJobError("Supabase did not return a scrape job id")
    return str(rows[0]["id"])


def _valid_id(job_id: str) -> str:
    try:
        return str(uuid.UUID(str(job_id)))
    except (ValueError, AttributeError, TypeError) as exc:
        raise SupabaseJobError("invalid scrape job id") from exc


def get_job(job_id: str) -> dict[str, Any] | None:
    """Fetch one job by UUID."""
    job_id = _valid_id(job_id)
    rows = _json(_request(
        "GET", "scrape_jobs",
        params={"select": "*", "id": f"eq.{job_id}", "limit": "1"},
    ))
    return rows[0] if isinstance(rows, list) and rows else None


def latest_job(requested_by: str | None = None) -> dict[str, Any] | None:
    """Fetch the newest job, optionally scoped to a dashboard user."""
    params: dict[str, str] = {
        "select": "*",
        "order": "created_at.desc",
        "limit": "1",
    }
    if requested_by:
        params["requested_by"] = f"eq.{requested_by}"
    rows = _json(_request("GET", "scrape_jobs", params=params))
    return rows[0] if isinstance(rows, list) and rows else None


def claim_next_job() -> dict[str, Any] | None:
    """Atomically claim the oldest queued job, if one exists."""
    rows = _json(_request(
        "GET", "scrape_jobs",
        params={
            "select": "*",
            "status": "eq.queued",
            "order": "created_at.asc",
            "limit": "1",
        },
    ))
    if not isinstance(rows, list) or not rows:
        return None
    job_id = _valid_id(rows[0]["id"])
    claimed = _json(_request(
        "PATCH", "scrape_jobs",
        params={"id": f"eq.{job_id}", "status": "eq.queued", "select": "*"},
        headers={"Prefer": "return=representation"},
        json={"status": "running", "started_at": _now()},
    ))
    return claimed[0] if isinstance(claimed, list) and claimed else None


def update_job(job_id: str, values: dict[str, Any]) -> None:
    """Update a job's durable state."""
    job_id = _valid_id(job_id)
    _request(
        "PATCH", "scrape_jobs",
        params={"id": f"eq.{job_id}"},
        headers={"Prefer": "return=minimal"},
        json=values,
    )


def cancel_job(job_id: str) -> None:
    update_job(job_id, {
        "status": "cancelled",
        "finished_at": _now(),
        "error": "Cancelled from dashboard",
    })


def fetch_leads(limit: int = 1000) -> list[dict[str, Any]]:
    """Read saved leads for the hosted dashboard."""
    limit = max(1, min(int(limit), 5000))
    rows = _json(_request(
        "GET", "leads",
        params={
            "select": "*",
            "order": "quality_score.desc,scraped_at.desc",
            "limit": str(limit),
        },
    ))
    return rows if isinstance(rows, list) else []
