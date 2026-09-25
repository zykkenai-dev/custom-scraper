#!/usr/bin/env python
"""Launch the live dashboard locally and open the browser.

Usage:
    python dashboard/run.py            # 127.0.0.1:8765, browser auto-opens
    python dashboard/run.py 9000       # custom port
    python dashboard/run.py 8765 --no-open

Optional extra: point the dashboard at a specific leads file with --leads path.
"""

import argparse
import os
import sys
import threading
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _venv_python() -> Path:
    return ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _ensure_venv() -> None:
    """Re-exec this launcher with the project venv if it isn't already.

    The scraper's dependencies live in .venv; running the dashboard with the
    system interpreter would spawn a scraper that cannot import requests/bs4.
    """
    venv_py = _venv_python()
    if venv_py.is_file() and os.path.abspath(sys.executable) != os.path.abspath(str(venv_py)):
        os.execv(str(venv_py), [str(venv_py), *sys.argv])


_ensure_venv()

sys.path.insert(0, str(ROOT))

from dashboard.server import serve  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="LeadScraper live dashboard")
    parser.add_argument("port", nargs="?", type=int, default=8765, help="port (default 8765)")
    parser.add_argument("--host", default="127.0.0.1", help="bind host (default 127.0.0.1)")
    parser.add_argument("--no-open", action="store_true", help="do not auto-open a browser")
    parser.add_argument("--leads", default=None, help="override leads source file")
    args = parser.parse_args(argv)

    try:
        srv = serve(host=args.host, port=args.port, leads=args.leads)
    except ValueError as exc:
        parser.error(str(exc))
    url = f"http://{args.host}:{args.port}/"
    print(f"\n  Lead Scraper dashboard  ->  {url}")
    print(f"  interpreter             ->  {sys.executable}")
    print("  Press Ctrl+C to stop.\n", flush=True)

    if not args.no_open:
        threading.Timer(0.4, webbrowser.open, args=(url,)).start()

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
