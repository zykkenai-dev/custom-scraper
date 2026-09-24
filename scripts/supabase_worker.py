#!/usr/bin/env python3
"""Claim one queued Supabase scrape job and run the real scraper.

The GitHub Actions workflow invokes this script on a schedule. Keeping the
worker outside Vercel gives long-running scrapes a durable process and lets the
hosted dashboard remain a small API/UI function.
"""

from __future__ import annotations

import re
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from output.supabase_jobs import SupabaseJobError, claim_next_job, update_job  # noqa: E402

LOG_LIMIT = 80
RUN_TIMEOUT_SECONDS = 18_000


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


def _command(job: dict) -> list[str]:
    options = job.get("options") if isinstance(job.get("options"), dict) else {}
    niches = _as_list(options.get("niche")) or ["real_estate"]
    max_leads = _int(options.get("max_leads"), 6, 1, 500)
    workers = _int(options.get("workers"), 2, 1, 8)
    out = f"/tmp/lead-worker-{uuid.uuid4().hex}.json"
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


def _saved_count(output: str) -> int:
    matches = re.findall(r"Saved\s+(\d+)\s+leads?\s+to\s+Supabase", output)
    return int(matches[-1]) if matches else 0


def run_one() -> int:
    try:
        job = claim_next_job()
    except SupabaseJobError as exc:
        print(f"Could not claim job: {exc}", file=sys.stderr)
        return 1
    if not job:
        print("No queued scrape job.")
        return 0

    job_id = str(job["id"])
    command = _command(job)
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

    lines = output.splitlines()
    values = {
        "status": "completed" if code == 0 else "failed",
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "log_tail": lines[-LOG_LIMIT:],
        "leads_saved": _saved_count(output),
        "error": error,
    }
    try:
        update_job(job_id, values)
    except SupabaseJobError as exc:
        print(f"Job {job_id} finished but status update failed: {exc}", file=sys.stderr)
        return 1
    print(f"Finished job {job_id} with exit code {code}; saved {values['leads_saved']} leads.")
    return 0 if code == 0 else 1


if __name__ == "__main__":
    raise SystemExit(run_one())
