"""Tests for optional Supabase lead persistence (no network access)."""

import pytest

from core.models import Lead
from output import supabase_store


class Response:
    def __init__(self, payload=None):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def test_not_configured_is_a_noop(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SECRET_KEY", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    lead = Lead("Acme", "saas", "https://acme.com", emails=["hello@acme.com"])
    assert supabase_store.is_configured() is False
    assert supabase_store.save_leads([lead]) == 0


def test_partial_configuration_is_reported(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://project.supabase.co")
    monkeypatch.delenv("SUPABASE_SECRET_KEY", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    assert supabase_store.is_configured() is False
    with pytest.raises(supabase_store.SupabaseStoreError, match="SUPABASE_SECRET_KEY"):
        supabase_store.save_leads([])


def test_payload_normalizes_website_key():
    lead = Lead(
        "Acme",
        "saas",
        "https://www.Acme.com/contact",
        emails=["hello@acme.com"],
        source_query="saas",
    )
    payload = supabase_store.lead_payload(lead)
    assert payload["website_key"] == "acme.com"
    assert payload["emails"] == ["hello@acme.com"]
    assert payload["quality_score"] == lead.quality_score


def test_save_upserts_rows(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://project.supabase.co/")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "sb_secret_test")
    lead = Lead("Acme", "saas", "https://acme.com", emails=["hello@acme.com"])
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return Response()

    monkeypatch.setattr(supabase_store.requests, "post", fake_post)
    assert supabase_store.save_leads([lead]) == 1
    assert calls[0][0] == "https://project.supabase.co/rest/v1/leads"
    assert calls[0][1]["params"] == {"on_conflict": "website_key"}
    assert calls[0][1]["headers"]["apikey"] == "sb_secret_test"
    assert calls[0][1]["json"][0]["website_key"] == "acme.com"


def test_worker_dicts_are_validated_through_lead_model(monkeypatch):
    captured = []

    def fake_save(leads, *, timeout=20.0):
        captured.extend(leads)
        return len(leads)

    monkeypatch.setattr(supabase_store, "save_leads", fake_save)
    count = supabase_store.save_lead_dicts([
        {
            "business_name": "Acme",
            "niche": "saas",
            "website": "https://acme.example/contact",
            "emails": ["hello@acme.example"],
            "quality_score": 100,
            "quality_label": "high",
            "unexpected": "ignored",
        },
        {"business_name": "No website"},
    ])
    assert count == 1
    assert captured[0].business_name == "Acme"
    assert captured[0].quality_score == 17
    assert not hasattr(captured[0], "unexpected")


def test_purge_denied_leads(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://project.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "sb_secret_test")
    calls = []

    def fake_get(url, **kwargs):
        calls.append(("get", url, kwargs))
        return Response([
            {"id": 10, "website": "https://www.estatesale.com/companies/WA/Seattle"},
            {"id": 11, "website": "https://acmerealty.example"},
        ])

    def fake_delete(url, **kwargs):
        calls.append(("delete", url, kwargs))
        return Response()

    monkeypatch.setattr(supabase_store.requests, "get", fake_get)
    monkeypatch.setattr(supabase_store.requests, "delete", fake_delete)
    assert supabase_store.purge_denied_leads() == 1
    assert calls[1][2]["params"] == {"id": "in.(10)"}
