"""Tests for the remote GitHub Actions worker transport."""

from core.models import Lead
from output.exporter import export_leads
from scripts import supabase_worker


def test_remote_upload_batches_local_export(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKER_API_URL", "https://dashboard.example")
    monkeypatch.setenv("WORKER_API_TOKEN", "t" * 40)
    out = tmp_path / "leads.json"
    export_leads([
        Lead("Acme", "saas", "https://acme.example", emails=["hello@acme.example"]),
        Lead("Beta", "saas", "https://beta.example", phones=["+1 555 0100"]),
    ], str(out), merge=False)
    calls = []

    def fake_post(path, payload):
        calls.append((path, payload))
        return {"saved": len(payload["leads"])}

    monkeypatch.setattr(supabase_worker, "_remote_post", fake_post)
    assert supabase_worker._upload_leads("job-1", str(out)) == 2
    assert calls[0][0] == "/api/worker/leads"
    assert calls[0][1]["job_id"] == "job-1"
    assert calls[0][1]["leads"][0]["emails"] == ["hello@acme.example"]
