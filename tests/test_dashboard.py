"""Tests for dashboard/server.py read-side helpers (no server, no network)."""

import json
import csv
import io
import threading
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen

import pytest

import dashboard.server as server
from dashboard.auth import AuthStore, provision


class TestQuality:
    def test_email_weight(self):
        assert server.quality_score(["a@x.com"], [], [], [], []) == 17
        assert server.quality_score(["a@x.com"] * 5, [], [], [], []) == 65
        assert server.quality_score(["a@x.com"] * 5, [], [], [], [], "inferred") == 2

    def test_label_bands(self):
        assert server.quality_label(60) == "high"
        assert server.quality_label(59) == "medium"
        assert server.quality_label(30) == "medium"
        assert server.quality_label(29) == "low"


class TestSplitAndNorm:
    def test_split_pipe(self):
        assert server._split(" a | b | c ") == ["a", "b", "c"]

    def test_split_empty(self):
        assert server._split("") == []
        assert server._split(None) == []

    def test_norm_list_string_with_pipes(self):
        assert server._norm_list("a@x.com | b@x.com") == ["a@x.com", "b@x.com"]

    def test_norm_list_single_string(self):
        assert server._norm_list("a@x.com") == ["a@x.com"]

    def test_norm_list_json_list(self):
        assert server._norm_list(["a@x.com", " b@x.com "]) == ["a@x.com", "b@x.com"]

    def test_norm_list_none(self):
        assert server._norm_list(None) == []


class TestLeadFromAny:
    def test_csv_style_row(self):
        item = {
            "business_name": "Acme",
            "niche": "real_estate",
            "website": "https://acme.com",
            "emails": "a@acme.com | b@acme.com",
            "quality_score": "74",
            "quality_label": "high",
            "email_origin": "inferred",
        }
        lead = server._lead_from_any(item)
        assert lead["emails"] == ["a@acme.com", "b@acme.com"]
        assert lead["quality_score"] == 2
        assert lead["quality_label"] == "low"
        assert lead["email_origin"] == "inferred"

    def test_missing_score_recomputed(self):
        lead = server._lead_from_any({
            "business_name": "Acme", "website": "https://acme.com",
            "emails": ["a@acme.com"],
        })
        assert lead["quality_score"] == 17
        assert lead["quality_label"] == "low"

    def test_missing_email_origin_defaults(self):
        lead = server._lead_from_any({"emails": []})
        assert lead["email_origin"] == "scraped"

    def test_bad_quality_values(self):
        lead = server._lead_from_any({"quality_score": "not-a-number"})
        assert lead["quality_score"] == 0
        assert lead["quality_label"] == "low"


class TestHostKey:
    def test_normalises(self):
        assert (
            server._host_key("https://www.Acme.com/path/")
            == "acme.com"
        )

    def test_different_paths_same_host(self):
        assert server._host_key("https://acme.com/x") == server._host_key("https://acme.com/y")

    def test_empty(self):
        assert server._host_key("") == ""


class TestDashboardPaths:
    def test_output_confined_to_data_files(self):
        assert server._output_path("data/leads.csv") == "data/leads.csv"
        assert server._output_path("../outside.csv") is None
        assert server._output_path("config/niches.py") is None
        assert server._output_path("data/../main.py") is None

    def test_remote_binding_rejected(self):
        with pytest.raises(ValueError, match="loopback"):
            server.serve(host="0.0.0.0", port=0)


class TestHostedOrigin:
    def test_vercel_host_requires_vercel_runtime(self, monkeypatch):
        monkeypatch.delenv("VERCEL", raising=False)
        monkeypatch.delenv("VERCEL_ENV", raising=False)
        monkeypatch.delenv("VERCEL_URL", raising=False)
        monkeypatch.delenv("VERCEL_PROJECT_PRODUCTION_URL", raising=False)
        monkeypatch.delenv("DASHBOARD_ALLOWED_HOSTS", raising=False)
        assert server._dashboard_host_allowed("project.vercel.app") is False
        monkeypatch.setenv("VERCEL", "1")
        assert server._dashboard_host_allowed("project.vercel.app") is True
        assert server._dashboard_host_allowed("evil.example") is False

    def test_custom_host_requires_explicit_allowlist(self, monkeypatch):
        monkeypatch.delenv("DASHBOARD_ALLOWED_HOSTS", raising=False)
        assert server._dashboard_host_allowed("dashboard.example.com") is False
        monkeypatch.setenv("DASHBOARD_ALLOWED_HOSTS", "dashboard.example.com")
        assert server._dashboard_host_allowed("dashboard.example.com") is True

    def test_serverless_run_queues_a_real_job(self, monkeypatch):
        monkeypatch.setenv("VERCEL", "1")
        calls = []

        def fake_create_job(options, requested_by):
            calls.append((options, requested_by))
            return "00000000-0000-0000-0000-000000000001"

        monkeypatch.setattr(server, "create_job", fake_create_job)
        ok, info = server.start_run({
            "niche": ["real_estate"], "max": 500,
            "out": "data/leads.csv", "fresh": True,
        }, "tarun")
        assert ok is True
        assert info["queued"] is True
        assert info["job_id"]
        assert calls[0][0]["max_leads"] == 500
        assert calls[0][1] == "tarun"

    def test_serverless_status_maps_supabase_job(self, monkeypatch):
        job = {
            "id": "00000000-0000-0000-0000-000000000001",
            "status": "completed",
            "requested_by": "tarun",
            "options": {"niche": ["real_estate"], "max_leads": 500},
            "log_tail": ["Done"],
            "leads_saved": 12,
            "created_at": "2026-01-01T00:00:00+00:00",
            "finished_at": "2026-01-01T00:01:00+00:00",
        }
        status = server._hosted_status(job, "tarun")
        assert status["job_status"] == "completed"
        assert status["leads_collected_log"] == 12
        assert status["niches"] == ["real_estate"]
        assert status["max"] == 500

    def test_hosted_login_page_and_api_guard_without_local_auth(self, monkeypatch):
        monkeypatch.setenv("VERCEL", "1")
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{httpd.server_port}"
        headers = {"Host": "project.vercel.app"}
        try:
            with urlopen(Request(base + "/login", headers=headers), timeout=3) as response:
                assert response.status == 200
                body = response.read()
                assert b"Sign in to Lead Studio" in body
                assert b'https://custom-scraper-three.vercel.app/share-preview.jpg' in body
            with urlopen(Request(base + "/share-preview.jpg", headers=headers), timeout=3) as response:
                assert response.headers["Content-Type"] == "image/jpeg"
                assert response.read(2) == b"\xff\xd8"
            with urlopen(Request(base + "/brand-logo.png", headers=headers), timeout=3) as response:
                assert response.headers["Content-Type"] == "image/png"
                assert response.read(8) == b"\x89PNG\r\n\x1a\n"
            with urlopen(Request(base + "/share-preview.jpg", headers=headers, method="HEAD"), timeout=3) as response:
                assert response.status == 200
                assert response.headers["Content-Length"] == str((server.DASH / "share-preview.jpg").stat().st_size)
            with pytest.raises(HTTPError) as exc:
                urlopen(Request(base + "/api/status", headers=headers), timeout=3)
            assert exc.value.code == 401
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=3)

    def test_worker_api_requires_token_and_proxies_supabase(self, monkeypatch):
        token = "worker-token-" + "x" * 32
        job_id = "00000000-0000-0000-0000-000000000001"
        updates = []
        monkeypatch.setenv("VERCEL", "1")
        monkeypatch.setenv("WORKER_API_TOKEN", token)
        monkeypatch.setattr(server, "claim_next_job", lambda: {"id": job_id, "status": "running"})
        monkeypatch.setattr(server, "get_job", lambda value: {"id": value, "status": "running"})
        monkeypatch.setattr(server, "save_lead_dicts", lambda rows: len(rows))
        monkeypatch.setattr(server, "update_job", lambda value, fields: updates.append((value, fields)))
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{httpd.server_port}"

        def post(path, payload, supplied=token):
            headers = {"Host": "project.vercel.app", "Content-Type": "application/json"}
            if supplied:
                headers["Authorization"] = f"Bearer {supplied}"
            return urlopen(Request(base + path, data=json.dumps(payload).encode(), headers=headers), timeout=3)

        try:
            with pytest.raises(HTTPError) as exc:
                post("/api/worker/claim", {}, supplied="")
            assert exc.value.code == 401
            with post("/api/worker/claim", {}) as response:
                assert json.load(response)["job"]["id"] == job_id
            with post("/api/worker/status", {"job_id": job_id}) as response:
                assert json.load(response)["status"] == "running"
            with post("/api/worker/leads", {"job_id": job_id, "leads": [{"website": "https://acme.example"}]}) as response:
                assert json.load(response)["saved"] == 1
            with post("/api/worker/finish", {
                "job_id": job_id, "status": "completed", "leads_saved": 1,
                "log_tail": ["done"],
            }) as response:
                assert json.load(response)["ok"] is True
            assert updates[0][0] == job_id
            assert updates[0][1]["status"] == "completed"
            assert updates[0][1]["leads_saved"] == 1
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=3)


class TestParseLog:
    def test_parses_metric_lines(self):
        lines = [
            "2026-09-18 | Searching: '\"real estate\" brokerage \"contact us\"'",
            "2026-09-18 | Probing https://acme.com",
            "2026-09-18 | Probing https://other.com",
            "2026-09-18 | collected 3 qualified leads for real_estate",
        ]
        stats = server.parse_log(lines)
        assert stats["searches"] == 1
        assert stats["candidates_probed"] == 2
        assert stats["leads_collected"] == 3
        assert stats["per_niche"] == {"real_estate": 3}
        assert stats["current_query"] == '"real estate" brokerage "contact us"'
        assert stats["current_url"] == "https://other.com"

    def test_multiple_niches_summed(self):
        lines = [
            "collected 2 qualified leads for real_estate",
            "collected 4 qualified leads for saas",
        ]
        stats = server.parse_log(lines)
        assert stats["leads_collected"] == 6
        assert stats["per_niche"] == {"real_estate": 2, "saas": 4}

    def test_query_dedupe_and_tracking(self):
        lines = [
            'Searching: \'q1\'',
            'Searching: \'q2\'',
            'Searching: \'q1\'',
        ]
        stats = server.parse_log(lines)
        assert stats["queries_seen"] == ["q1", "q2"]


class TestLoadLeads:
    def test_merges_files_richest_wins(self, tmp_path, monkeypatch):
        f1 = tmp_path / "a.json"
        f2 = tmp_path / "b.json"
        f1.write_text(json.dumps({"leads": [
            {"business_name": "Acme", "website": "https://acme.com",
             "emails": ["info@acme.com"], "quality_score": 17},
        ]}), encoding="utf-8")
        f2.write_text(json.dumps({"leads": [
            {"business_name": "Acme", "website": "https://www.acme.com/",
             "emails": ["info@acme.com", "sales@acme.com"], "quality_score": 29},
        ]}), encoding="utf-8")
        monkeypatch.setattr(server, "candidate_files", lambda: [f1, f2])
        leads, src = server.load_leads()
        assert len(leads) == 1
        assert leads[0]["emails"] == ["info@acme.com", "sales@acme.com"]

    def test_merges_equal_contacts_from_newer_file(self, tmp_path, monkeypatch):
        older = tmp_path / "older.json"
        newer = tmp_path / "newer.json"
        older.write_text(json.dumps({"leads": [{
            "business_name": "Old name", "website": "https://acme.com",
            "niche": "coaching", "emails": ["info@acme.com"],
        }]}), encoding="utf-8")
        newer.write_text(json.dumps({"leads": [{
            "business_name": "New name", "website": "https://acme.com",
            "niche": "real_estate", "emails": ["info@acme.com"],
        }]}), encoding="utf-8")
        older.touch()
        newer.touch()
        # Candidate order does not determine precedence; file mtime does.
        import os
        os.utime(older, (1, 1))
        os.utime(newer, (2, 2))
        monkeypatch.setattr(server, "candidate_files", lambda: [older, newer])
        leads, _ = server.load_leads()
        assert leads[0]["business_name"] == "New name"
        assert leads[0]["niche"] == "real_estate"

    def test_empty_when_no_files(self, tmp_path, monkeypatch):
        monkeypatch.setattr(server, "candidate_files", lambda: [])
        leads, src = server.load_leads()
        assert leads == []
        assert src is None

    def test_skips_corrupt_file(self, tmp_path, monkeypatch):
        bad = tmp_path / "bad.csv"
        bad.write_text("garbage,,,", encoding="utf-8")
        monkeypatch.setattr(server, "candidate_files", lambda: [bad])
        leads, _ = server.load_leads()
        assert leads == []


def test_persisted_log_recovers_only_last_run(tmp_path, monkeypatch):
    path = tmp_path / "run.log"
    path.write_text(
        "### START 2026-01-01T10:00:00+00:00 :: python main.py --niche saas --max 5 --out data/leads.csv\n"
        "Probing https://one.test\ncollected 3 qualified leads for saas\n"
        "### EXIT code=0 at 2026-01-01T10:01:00+00:00\n"
        "### START 2026-01-01T11:00:00+00:00 :: python main.py --niche real_estate --max 2 --out data/new.csv --seeds data/seeds.demo.txt\n"
        "Probing https://two.test\ncollected 0 qualified leads for real_estate\n"
        "### EXIT code=0 at 2026-01-01T11:01:00+00:00\n", encoding="utf-8",
    )
    monkeypatch.setattr(server, "LOG_PATH", path)
    before_run, before_log = dict(server._run), list(server._log)
    try:
        server.load_persisted_log()
        assert server.parse_log(list(server._log))["candidates_probed"] == 1
        assert server.parse_log(list(server._log))["leads_collected"] == 0
        assert server._run["niches"] == ["real_estate"]
        assert server._run["out_file"] == "data/new.csv"
        assert server._run["exit_code"] == 0
    finally:
        server._run.clear(); server._run.update(before_run)
        server._log.clear(); server._log.extend(before_log)


def test_dashboard_published_export_excludes_inferred(monkeypatch, tmp_path):
    published = server._lead_from_any({"business_name": "Published", "website": "https://one.test", "emails": ["info@one.test"]})
    guessed = server._lead_from_any({"business_name": "Guessed", "website": "https://two.test", "emails": ["info@two.test"], "email_origin": "inferred"})
    monkeypatch.setattr(server, "load_leads", lambda: ([published, guessed], None))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    store = tmp_path / "users.json"
    provision({"tarun": "test-pass", "prabh": "test-pass", "uttkarsh": "test-pass"}, store)
    httpd.auth = AuthStore(store)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{httpd.server_port}"
        opener = build_opener(HTTPCookieProcessor(CookieJar()))
        opener.open(Request(url + "/api/login", data=json.dumps({"username": "tarun", "password": "test-pass"}).encode(), headers={"Content-Type": "application/json"}), timeout=3).close()
        with opener.open(url + "/api/export.csv?published_only=1", timeout=3) as response:
            rows = list(csv.DictReader(io.StringIO(response.read().decode("utf-8-sig"))))
        assert len(rows) == 1
        assert rows[0]["business_name"] == "Published"
        assert rows[0]["email_origin"] == "scraped"
    finally:
        httpd.shutdown(); httpd.server_close(); thread.join(timeout=3)
