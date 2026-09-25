#!/usr/bin/env python3
"""Claim one queued Supabase scrape job and run the real scraper.

The GitHub Actions workflow invokes this script on a schedule. Keeping the
worker outside Vercel gives long-running scrapes a durable process and lets the
hosted dashboard remain a small API/UI function.
"""

from __future__ import annotations

import re
import os
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from output.supabase_jobs import SupabaseJobError, claim_next_job, update_job  # noqa: E402
from output.exporter import load_leads  # noqa: E402

import requests  # noqa: E402

LOG_LIMIT = 80
RUN_TIMEOUT_SECONDS = 18_000
UPLOAD_BATCH_SIZE = 50


def _as_list(value) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str) and value:
        return [value]
    return []


def _int(value, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(int(value), high))
    except (TypeError, ValueError):
        return default


def _command(job: dict, out: str | None = None) -> list[str]:
    options = job.get("options") if isinstance(job.get("options"), dict) else {}
    niches = _as_list(options.get("niche")) or ["real_estate"]
    max_leads = _int(options.get("max_leads"), 6, 1, 500)
    workers = _int(options.get("workers"), 2, 1, 8)
    out = out or f"/tmp/lead-worker-{uuid.uuid4().hex}.json"
    cmd = [
        sys.executable, "-u", "main.py", "--niche", *niches,
        "--max", str(max_leads), "--out", out, "--workers", str(workers),
    ]
    seeds = options.get("seeds")
    if seeds:
        cmd += ["--seeds", str(seeds)]
    if options.get("emails_only"):
        cmd += ["--emails-only"]
    min_quality = _int(options.get("min_quality"), 0, 0, 100)
    max_quality = _int(options.get("max_quality"), 100, 0, 100)
    if min_quality > 0:
        cmd += ["--min-quality", str(min_quality)]
    if max_quality < 100:
        cmd += ["--max-quality", str(max_quality)]
    if options.get("no_enrich"):
        cmd += ["--no-enrich"]
    if options.get("fresh"):
        cmd += ["--fresh"]
    elif options.get("no_merge"):
        cmd += ["--no-merge"]
    return cmd


def _remote_config() -> tuple[str, str] | None:
    url = (os.getenv("WORKER_API_URL") or "").strip().rstrip("/")
    token = (os.getenv("WORKER_API_TOKEN") or "").strip()
    if not url and not token:
        return None
    if not url.startswith("https://") or len(token) < 32:
        raise RuntimeError("WORKER_API_URL and WORKER_API_TOKEN must both be configured")
    return url, token


def _remote_post(path: str, payload: dict) -> dict:
    config = _remote_config()
    if config is None:
        raise RuntimeError("remote worker API is not configured")
    url, token = config
    response = requests.post(
        url + path,
        headers={"Authorization": f"Bearer {token}"},
        json=payload,
        timeout=60,
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError("worker API returned invalid JSON")
    return data


def _claim_job() -> dict | None:
    if _remote_config() is not None:
        return _remote_post("/api/worker/claim", {}).get("job")
    return claim_next_job()


def _finish_job(job_id: str, values: dict) -> None:
    if _remote_config() is not None:
        _remote_post("/api/worker/finish", {"job_id": job_id, **values})
        return
    update_job(job_id, values)


def _upload_leads(job_id: str, out: str) -> int:
    if _remote_config() is None:
        return _saved_count("")
    leads = [lead.to_dict() for lead in load_leads(out, strict=True)]
    saved = 0
    for start in range(0, len(leads), UPLOAD_BATCH_SIZE):
        result = _remote_post("/api/worker/leads", {
            "job_id": job_id,
            "leads": leads[start:start + UPLOAD_BATCH_SIZE],
        })
        saved += int(result.get("saved") or 0)
    return saved


def _saved_count(output: str) -> int:
    matches = re.findall(r"Saved\s+(\d+)\s+leads?\s+to\s+Supabase", output)
    return int(matches[-1]) if matches else 0


def run_one(idle_code: int = 0) -> int:
    try:
        job = _claim_job()
    except (SupabaseJobError, RuntimeError, requests.RequestException) as exc:
        print(f"Could not claim job: {exc}", file=sys.stderr)
        return 1
    if not job:
        print("No queued scrape job.")
        return idle_code

    job_id = str(job["id"])
    with tempfile.TemporaryDirectory(prefix="lead-worker-") as temp_dir:
        out = str(Path(temp_dir) / "leads.json")
        command = _command(job, out)
        print(f"Claimed job {job_id}: {' '.join(command)}", flush=True)
        try:
            completed = subprocess.run(
                command, cwd=str(ROOT), stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, timeout=RUN_TIMEOUT_SECONDS,
                check=False,
            )
            output = completed.stdout or ""
            code = int(completed.returncode)
            error = None if code == 0 else f"scraper exited with code {code}"
        except subprocess.TimeoutExpired as exc:
            output = exc.stdout if isinstance(exc.stdout, str) else ""
            code = 124
            error = "scraper timed out"
        except OSError as exc:
            output = str(exc)
            code = 127
            error = f"could not launch scraper: {exc}"

        saved = 0
        if code == 0 and _remote_config() is not None:
            try:
                saved = _upload_leads(job_id, out)
            except (OSError, ValueError, RuntimeError, requests.RequestException) as exc:
                code = 1
                error = f"could not upload leads: {exc}"
        elif code == 0:
            saved = _saved_count(output)

    lines = output.splitlines()
    values = {
        "status": "completed" if code == 0 else "failed",
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "log_tail": lines[-LOG_LIMIT:],
        "leads_saved": saved,
        "error": error,
    }
    try:
        _finish_job(job_id, values)
    except (SupabaseJobError, RuntimeError, requests.RequestException) as exc:
        print(f"Job {job_id} finished but status update failed: {exc}", file=sys.stderr)
        return 1
    print(f"Finished job {job_id} with exit code {code}; saved {values['leads_saved']} leads.")
    return 0 if code == 0 else 1


def run_watch(watch_seconds: int, poll_seconds: int) -> int:
    """Keep a hosted worker ready so dashboard jobs start without cron latency."""
    watch_seconds = max(1, min(int(watch_seconds), RUN_TIMEOUT_SECONDS))
    poll_seconds = max(1, min(int(poll_seconds), 60))
    deadline = time.monotonic() + watch_seconds
    failures = 0
    print(
        f"Worker ready for {watch_seconds}s; checking every {poll_seconds}s.",
        flush=True,
    )
    while True:
        code = run_one(idle_code=-1)
        if code > 0:
            failures += 1
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if code != 0:
            time.sleep(min(poll_seconds, remaining))
    print("Worker watch window finished.", flush=True)
    return 1 if failures else 0


def main() -> int:
    try:
        watch_seconds = int(os.getenv("WORKER_WATCH_SECONDS", "0") or 0)
        poll_seconds = int(os.getenv("WORKER_POLL_SECONDS", "10") or 10)
    except ValueError:
        print("WORKER_WATCH_SECONDS and WORKER_POLL_SECONDS must be integers.", file=sys.stderr)
        return 1
    return run_watch(watch_seconds, poll_seconds) if watch_seconds > 0 else run_one()


if __name__ == "__main__":
    raise SystemExit(main())
