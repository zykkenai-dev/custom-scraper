"""Tests for the remote GitHub Actions worker transport."""

import sys

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


def test_watch_keeps_polling_until_deadline(monkeypatch):
    results = iter([-1, 0, -1])
    clock = iter([0.0, 0.0, 1.0, 2.0])
    sleeps = []
    monkeypatch.setattr(supabase_worker, "run_one", lambda idle_code=0: next(results))
    monkeypatch.setattr(supabase_worker.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(supabase_worker.time, "sleep", sleeps.append)

    assert supabase_worker.run_watch(2, 1) == 0
    assert sleeps == [1]


def test_remote_cancel_status(monkeypatch):
    monkeypatch.setattr(
        supabase_worker,
        "_remote_post",
        lambda path, payload: {"status": "cancelled"},
    )
    monkeypatch.setattr(
        supabase_worker,
        "_remote_config",
        lambda: ("https://dashboard.example", "x" * 32),
    )
    assert supabase_worker._job_cancelled("job-1") is True


def test_running_scraper_stops_when_cancelled(monkeypatch):
    monkeypatch.setattr(supabase_worker, "CANCEL_POLL_SECONDS", 0.05)
    monkeypatch.setattr(supabase_worker, "_job_cancelled", lambda _job_id: True)
    code, output, error, cancelled = supabase_worker._run_scraper(
        [sys.executable, "-c", "import time; print('started', flush=True); time.sleep(60)"],
        "job-1",
    )
    assert code == 130
    assert "started" in output
    assert error == "Cancelled from dashboard"
    assert cancelled is True
