"""Exercise the complete seed-to-export path against a local HTTP fixture."""

import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from core.models import Lead
from main import _serpapi_budget_for_target
from output.exporter import export_leads, load_leads

ROOT = Path(__file__).resolve().parent.parent


def test_serpapi_budget_scales_with_large_target():
    assert _serpapi_budget_for_target(4, 20) == 4
    assert _serpapi_budget_for_target(4, 50) == 4
    assert _serpapi_budget_for_target(4, 100) == 6
    assert _serpapi_budget_for_target(4, 200) == 11
    assert _serpapi_budget_for_target(4, 500) == 12


def test_seed_cli_crawls_contact_page_and_fresh_replaces_prior(tmp_path):
    class Site(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/contact":
                html = "<html><body>Email: hello@local-realty.com</body></html>"
            else:
                html = ('<html><head><title>Local Realty</title></head><body>'
                        'Independent real estate brokerage '
                        '<a href="/contact">Contact us</a></body></html>')
            raw = html.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Site)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        seeds = tmp_path / "seeds.txt"
        seeds.write_text(f"http://127.0.0.1:{server.server_port}/\n", encoding="utf-8")
        output = tmp_path / "leads.csv"
        export_leads([Lead("Old Realty", "real_estate", "https://old.example", emails=["old@old.example"])],
                     str(output), merge=False)
        env = dict(os.environ, REQUEST_DELAY_MIN="0", REQUEST_DELAY_MAX="0",
                   MAX_CONCURRENT_REQUESTS="1", FREE_JS_RENDER="false", VERIFY_EMAILS="false")
        result = subprocess.run(
            [sys.executable, "main.py", "--niche", "real_estate", "--seeds", str(seeds),
             "--max", "1", "--out", str(output), "--fresh", "--no-enrich"],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=20,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        leads = load_leads(str(output))
        assert len(leads) == 1
        assert leads[0].business_name == "Local Realty"
        assert leads[0].emails == ["hello@local-realty.com"]

        json_output = tmp_path / "leads.json"
        export_leads([Lead("Old Realty", "real_estate", "https://old.example", emails=["old@old.example"])],
                     str(json_output), merge=False)
        result = subprocess.run(
            [sys.executable, "main.py", "--niche", "real_estate", "--seeds", str(seeds),
             "--max", "1", "--out", str(output), "--json", "--no-enrich"],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=20,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "Loaded 1 prior leads from" in result.stdout
        assert len(load_leads(str(json_output))) == 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
