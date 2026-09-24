"""Tests for durable Supabase job and hosted lead helpers."""

import pytest

from output import supabase_jobs


class Response:
    def __init__(self, payload):
        self.payload = payload
        self.content = b"x"

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def test_create_job(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://project.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "sb_secret_test")
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return Response([{"id": "00000000-0000-0000-0000-000000000001"}])

    monkeypatch.setattr(supabase_jobs.requests, "request", fake_request)
    job_id = supabase_jobs.create_job({"max_leads": 500}, "tarun")
    assert job_id == "00000000-0000-0000-0000-000000000001"
    assert calls[0][0] == "POST"
    assert calls[0][1].endswith("/rest/v1/scrape_jobs")
    assert calls[0][2]["json"]["options"]["max_leads"] == 500


def test_claim_next_job_uses_status_filter(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://project.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "sb_secret_test")
    responses = iter([
        Response([{"id": "00000000-0000-0000-0000-000000000001"}]),
        Response([{"id": "00000000-0000-0000-0000-000000000001", "status": "running"}]),
    ])
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return next(responses)

    monkeypatch.setattr(supabase_jobs.requests, "request", fake_request)
    job = supabase_jobs.claim_next_job()
    assert job["status"] == "running"
    assert calls[1][2]["params"]["status"] == "eq.queued"


def test_fetch_leads(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://project.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "sb_secret_test")
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return Response([{"website_key": "acme.com", "emails": ["a@acme.com"]}])

    monkeypatch.setattr(supabase_jobs.requests, "request", fake_request)
    rows = supabase_jobs.fetch_leads(limit=25)
    assert rows[0]["website_key"] == "acme.com"
    assert calls[0][2]["params"]["limit"] == "25"


def test_partial_config_is_rejected(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://project.supabase.co")
    monkeypatch.delenv("SUPABASE_SECRET_KEY", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    with pytest.raises(supabase_jobs.SupabaseJobError, match="SUPABASE_SECRET_KEY"):
        supabase_jobs.latest_job()
