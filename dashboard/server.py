"""Live dashboard server (stdlib only).

Read-only view over the scraper:
  - serves dashboard/index.html + style.css + app.js
  - GET  /api/status     -> running?, niche/queries, probed, leads, elapsed, log tail
  - GET  /api/leads      -> parsed leads from newest data file (JSON or CSV)
  - GET  /api/export.csv -> CSV download of current leads
  - POST /api/run        -> spawn `python -u main.py ...` (never edits scraper)
  - POST /api/stop       -> stop the spawned run

Run via:  python dashboard/run.py
"""

from __future__ import annotations

import csv
import hmac
import ipaddress
import json
import os
import re
import shlex
import subprocess
import sys
import threading
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from dashboard.auth import AuthStore, HostedAuthStore, SESSION_SECONDS
from output.supabase_jobs import (
    SupabaseJobError,
    cancel_job,
    claim_next_job,
    create_job,
    fetch_leads,
    get_job,
    latest_job,
    update_job,
)
from output.supabase_store import SupabaseStoreError, purge_denied_leads, save_lead_dicts

ROOT = Path(__file__).resolve().parent.parent
DASH = Path(__file__).resolve().parent
LOG_PATH = DASH / ".last_run.log"

KNOWN_NICHES = [
    "real_estate", "finance", "healthcare", "legal", "saas",
    "ecommerce", "coaching", "automotive", "hospitality",
]

WORKER_BODY_LIMIT = 1024 * 1024
WORKER_BATCH_LIMIT = 100

# ---------------------------------------------------------------- state

_lock = threading.Lock()
_log = deque(maxlen=500)  # in-memory tail of current/prior run
_run = {
    "proc": None,          # subprocess.Popen | None
    "cmd": [],             # list[str]
    "cmd_str": "",
    "niches": [],
    "max": None,
    "out_file": "data/leads.csv",
    "seeds": "",
    "started_at": None,    # datetime | None
    "ended_at": None,      # datetime | None
    "exit_code": None,
    "queries_seen": [],
}
_leads_override = {"path": None}  # set via --leads
_hosted_auth: HostedAuthStore | None = None
_hosted_auth_lock = threading.Lock()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


# ---------------------------------------------------------------- quality (mirrors core/models.py, standalone copy)

def quality_score(emails, wa, ig, li, phones, email_origin="scraped") -> int:
    s = 0
    if email_origin == "inferred":
        s += 2 if emails else 0
    elif email_origin == "mixed":
        s += 12 if emails else 0
    else:
        s += min(len(emails), 5) * 12
    s += min(len(wa), 3) * 8
    s += min(len(ig), 3) * 5
    s += min(len(li), 3) * 4
    s += min(len(phones), 3) * 2
    if emails and email_origin != "inferred":
        s += 5
    if wa:
        s += 5
    return min(s, 100)


def quality_label(score: int) -> str:
    if score >= 60:
        return "high"
    if score >= 30:
        return "medium"
    return "low"


# ---------------------------------------------------------------- leads loading (read-only)

def _split(v: str) -> list:
    if not v:
        return []
    return [p.strip() for p in str(v).split(" | ") if p.strip()]


def _norm_list(v) -> list:
    if v is None:
        return []
    if isinstance(v, str):
        # JSON sometimes stores a joined string; CSV always does.
        return _split(v) if " | " in v else ([v] if v.strip() else [])
    return [str(x).strip() for x in list(v) if str(x).strip()]


def _lead_from_any(item: dict) -> dict:
    emails = _norm_list(item.get("emails"))
    wa = _norm_list(item.get("whatsapp_numbers"))
    ig = _norm_list(item.get("instagram_handles"))
    li = _norm_list(item.get("linkedin_urls"))
    ph = _norm_list(item.get("phones"))
    origin = (item.get("email_origin") or "scraped").strip().lower()
    # Recalculate legacy scores: older exports counted MX-backed guesses as
    # verified mailboxes, so their stored quality labels are misleading.
    score = quality_score(emails, wa, ig, li, ph, origin)
    label = quality_label(score)
    return {
        "business_name": item.get("business_name") or "Unknown",
        "niche": item.get("niche") or "",
        "website": item.get("website") or "",
        "emails": emails,
        "whatsapp_numbers": wa,
        "instagram_handles": ig,
        "linkedin_urls": li,
        "phones": ph,
        "source_query": item.get("source_query") or "",
        "quality_label": label,
        "quality_score": score,
        "email_origin": origin,
        "scraped_at": item.get("scraped_at") or "",
    }


def _host_key(url: str) -> str:
    """Stable merge key: bare hostname (no scheme/www/path)."""
    try:
        host = (urlparse(url or "").hostname or "").lower().removeprefix("www.")
    except ValueError:
        return (url or "").lower()
    return host or (url or "").lower()


def candidate_files() -> list[Path]:
    cands: list[Path] = []
    if _leads_override["path"]:
        cands.append(Path(_leads_override["path"]))
    with _lock:
        out = _run.get("out_file") or ""
    if out:
        cands.append(ROOT / out if not Path(out).is_absolute() else Path(out))
    cands += [ROOT / "data" / "leads.json", ROOT / "data" / "leads.csv"]
    # Support the doc-mentioned `output/` CSV dir if a user points --out there.
    outdir = ROOT / "output"
    if outdir.is_dir():
        try:
            cands += sorted(
                (p for p in outdir.glob("*.csv") if p.is_file()),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )[:3]
        except OSError:
            pass
    # de-dupe, keep order
    seen, uniq = set(), []
    for p in cands:
        try:
            rp = p.resolve()
        except OSError:
            rp = p
        if rp not in seen:
            seen.add(rp)
            uniq.append(p)
    return uniq


def _read_one(src: Path) -> list[dict]:
    if src.suffix.lower() == ".json":
        data = json.loads(src.read_text(encoding="utf-8"))
        items = data.get("leads", data) if isinstance(data, dict) else data
        return [_lead_from_any(x) for x in items if isinstance(x, dict)]
    with src.open(newline="", encoding="utf-8-sig") as fh:
        return [_lead_from_any(r) for r in csv.DictReader(fh)]


def _contacts_n(l: dict) -> int:
    return len(l["emails"]) + len(l["whatsapp_numbers"]) + len(l["instagram_handles"]) + len(l["linkedin_urls"]) + len(l["phones"])


def _lead_rank(lead: dict) -> tuple[int, int]:
    # Prefer a published email to a long list of speculative mailboxes.
    origin = lead["email_origin"]
    trust = 2 if lead["emails"] and origin == "scraped" else 1 if origin == "mixed" else 0
    return trust, _contacts_n(lead)


def load_leads() -> tuple[list[dict], Path | None]:
    """Return (leads, newest_source). Merges all candidates in memory
    (display only, never writes) so idle state shows *all* last results
    even when data/leads.json and data/leads.csv diverge."""
    existing = []
    for p in candidate_files():
        try:
            ap = p if p.is_absolute() else (ROOT / p)
            if ap.is_file() and ap.stat().st_size > 0:
                existing.append(ap)
        except OSError:
            continue
    if not existing:
        return [], None
    existing.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    merged: dict[str, dict] = {}
    for src in existing:
        try:
            for l in _read_one(src):
                key = _host_key(l["website"] or l["business_name"])
                if not key:
                    continue
                prev = merged.get(key)
                # Files are processed newest first. Keep that record on ties.
                if prev is None or _lead_rank(l) > _lead_rank(prev):
                    merged[key] = l
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    leads = sorted(merged.values(), key=lambda l: (l["quality_score"], l["scraped_at"]), reverse=True)
    return leads, existing[0]


# ---------------------------------------------------------------- log parsing

RE_SEARCH = re.compile(r"Searching:\s*(.+?)\s*$")
RE_PROBE = re.compile(r"Probing\s+(https?://\S+)")
RE_CACHED = re.compile(r"Using cached results for\s+(.+?)\s+\((\d+)\s+urls\)")
RE_COLLECTED = re.compile(r"collected\s+(\d+)\s+qualified leads for\s+(\S+)")
RE_EXPORT = re.compile(r"Exported\s+(\d+)\s+leads to\s+(CSV|JSON):\s*(\S+)")


def parse_log(lines: list[str]) -> dict:
    probed = 0
    searches = 0
    collected = 0
    per_niche: dict[str, int] = {}
    queries: list[str] = []
    cur_q, cur_u = "", ""
    for ln in lines:
        m = RE_SEARCH.search(ln)
        if m:
            searches += 1
            q = m.group(1).strip().strip("'")
            cur_q = q
            if q not in queries:
                queries.append(q)
        m = RE_PROBE.search(ln)
        if m:
            probed += 1
            cur_u = m.group(1)
        m = RE_COLLECTED.search(ln)
        if m:
            n = int(m.group(1))
            per_niche[m.group(2)] = n
    collected = sum(per_niche.values())
    return {
        "candidates_probed": probed,
        "searches": searches,
        "leads_collected": collected,
        "per_niche": per_niche,
        "current_query": cur_q,
        "current_url": cur_u,
        "queries_seen": queries[-12:],
    }


def is_running() -> bool:
    with _lock:
        p = _run["proc"]
        return p is not None and p.poll() is None


def build_status() -> dict:
    with _lock:
        snapshot_lines = list(_log)
        snap = dict(_run)
        proc = snap["proc"]
        running = proc is not None and proc.poll() is None
    stats = parse_log(snapshot_lines)
    leads, src = load_leads()
    now = utcnow()
    started = snap["started_at"]
    ended = snap["ended_at"]
    if running and started:
        elapsed = (now - started).total_seconds()
    elif started and ended:
        elapsed = (ended - started).total_seconds()
    elif started:
        elapsed = (now - started).total_seconds()
    else:
        elapsed = 0.0
    # live leads_found: during a run show log-collected, else file total
    file_total = len(leads)
    leads_found = stats["leads_collected"] if running else file_total
    if running:
        leads_found = max(leads_found, 0)
    niches = list(snap["niches"]) or sorted({l["niche"] for l in leads if l["niche"]})
    try:
        leads_mtime = (
            datetime.fromtimestamp(src.stat().st_mtime, tz=timezone.utc).isoformat()
            if src and src.exists() else None
        )
    except OSError:
        leads_mtime = None
    return {
        "running": running,
        "hosted": _serverless_runtime(),
        "hosted_test_mode": _serverless_runtime(),
        "run_supported": True,
        "pid": proc.pid if proc else None,
        "niche": ", ".join(snap["niches"]) if snap["niches"] else ("-" if not niches else ", ".join(niches)),
        "niches": snap["niches"] or niches,
        "max": snap["max"],
        "out_file": snap["out_file"],
        "seeds": snap["seeds"],
        "cmd": snap["cmd_str"],
        "started_at": iso(started),
        "ended_at": iso(ended),
        "exit_code": None if running else snap["exit_code"],
        "elapsed_s": round(elapsed, 1),
        "candidates_probed": stats["candidates_probed"],
        "searches": stats["searches"],
        "leads_found": leads_found,
        "leads_total": file_total,
        "leads_collected_log": stats["leads_collected"],
        "per_niche": stats["per_niche"],
        "current_query": stats["current_query"],
        "current_url": stats["current_url"],
        "queries_seen": stats["queries_seen"] or snap["queries_seen"],
        "log_tail": snapshot_lines[-80:],
        "log_lines": len(snapshot_lines),
        "source_file": str(src.relative_to(ROOT)) if src and _is_under(src, ROOT) else (str(src) if src else None),
        "leads_mtime": leads_mtime,
        "updated_at": now.isoformat(),
    }


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _hosted_status(job: dict | None, requested_by: str = "") -> dict:
    """Translate a Supabase job row into the dashboard status shape."""
    if not job:
        return {
            "running": False,
            "hosted": True,
            "hosted_test_mode": True,
            "run_supported": True,
            "pid": None,
            "niche": "-",
            "niches": [],
            "max": None,
            "out_file": "Supabase",
            "seeds": "",
            "cmd": "",
            "started_at": None,
            "ended_at": None,
            "exit_code": None,
            "elapsed_s": 0.0,
            "candidates_probed": 0,
            "searches": 0,
            "leads_found": 0,
            "leads_total": 0,
            "leads_collected_log": 0,
            "per_niche": {},
            "current_query": "",
            "current_url": "",
            "queries_seen": [],
            "log_tail": [],
            "log_lines": 0,
            "source_file": "Supabase public.leads",
            "leads_mtime": None,
            "updated_at": utcnow().isoformat(),
            "job_id": None,
            "job_status": "idle",
            "requested_by": requested_by,
        }
    options = job.get("options") if isinstance(job.get("options"), dict) else {}
    status = str(job.get("status") or "queued")
    logs = job.get("log_tail") if isinstance(job.get("log_tail"), list) else []
    logs = [str(line) for line in logs][-500:]
    started = _parse_timestamp(job.get("started_at") or job.get("created_at"))
    ended = _parse_timestamp(job.get("finished_at"))
    now = utcnow()
    elapsed = 0.0
    if started:
        elapsed = ((ended or now) - started).total_seconds()
    found = int(job.get("leads_saved") or 0)
    running = status in {"queued", "running"}
    failed = status in {"failed", "cancelled"}
    completed = status == "completed"
    niches = [str(n) for n in options.get("niche", []) if str(n)]
    return {
        "running": running,
        "hosted": True,
        "hosted_test_mode": True,
        "run_supported": True,
        "pid": None,
        "niche": ", ".join(niches) or "-",
        "niches": niches,
        "max": options.get("max_leads"),
        "out_file": "Supabase",
        "seeds": options.get("seeds") or "",
        "cmd": "Supabase worker",
        "started_at": started.isoformat() if started else None,
        "ended_at": ended.isoformat() if ended else None,
        "exit_code": 0 if completed else 1 if failed else None,
        "elapsed_s": round(max(0.0, elapsed), 1),
        "candidates_probed": 0,
        "searches": 0,
        "leads_found": found,
        "leads_total": found,
        "leads_collected_log": found,
        "per_niche": {},
        "current_query": "",
        "current_url": "",
        "queries_seen": [],
        "log_tail": logs,
        "log_lines": len(logs),
        "source_file": "Supabase public.leads",
        "leads_mtime": ended.isoformat() if ended else None,
        "updated_at": now.isoformat(),
        "job_id": job.get("id"),
        "job_status": status,
        "requested_by": job.get("requested_by") or requested_by,
        "error": job.get("error") or "",
    }


def build_hosted_status(job_id: str | None = None, requested_by: str = "") -> dict:
    job = get_job(job_id) if job_id else latest_job(requested_by or None)
    if job and requested_by and job.get("requested_by") not in ("", requested_by):
        job = latest_job(requested_by)
    return _hosted_status(job, requested_by)


def _hosted_leads() -> list[dict]:
    return [_lead_from_any(row) for row in fetch_leads(limit=5000)]


def _is_under(p: Path, root: Path) -> bool:
    try:
        p.relative_to(root)
        return True
    except ValueError:
        return False


def _local_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _host_from_value(value: str) -> str:
    """Return a hostname from either ``host`` or a full URL value."""
    raw = (value or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlparse(raw if "://" in raw else "//" + raw)
        return (parsed.hostname or "").lower()
    except ValueError:
        return ""


def _serverless_runtime() -> bool:
    """Whether this handler is running inside a Vercel-style function."""
    return any(
        os.getenv(name)
        for name in ("VERCEL", "VERCEL_ENV", "VERCEL_URL", "VERCEL_PROJECT_PRODUCTION_URL")
    )


def _dashboard_host_allowed(host: str) -> bool:
    """Allow loopback locally and explicitly trusted hosts when deployed.

    Vercel function invocations do not pass through ``dashboard.serve()`` and
    use a public ``*.vercel.app`` Host. Custom domains must be listed in
    ``DASHBOARD_ALLOWED_HOSTS`` rather than allowing arbitrary public hosts.
    """
    normalized = (host or "").lower().rstrip(".")
    if not normalized:
        return False
    if _local_host(normalized):
        return True

    configured = {
        _host_from_value(item)
        for item in os.getenv("DASHBOARD_ALLOWED_HOSTS", "").split(",")
        if item.strip()
    }
    if normalized in configured:
        return True

    vercel_runtime = _serverless_runtime()
    vercel_hosts = {
        _host_from_value(os.getenv(name, ""))
        for name in ("VERCEL_URL", "VERCEL_PROJECT_PRODUCTION_URL")
    }
    return vercel_runtime and (
        normalized.endswith(".vercel.app") or normalized in vercel_hosts
    )


def _get_hosted_auth() -> HostedAuthStore | None:
    """Build the stateless Vercel auth store once per warm function instance."""
    if not _serverless_runtime():
        return None
    global _hosted_auth
    with _hosted_auth_lock:
        if _hosted_auth is None:
            try:
                _hosted_auth = HostedAuthStore.from_env()
            except ValueError:
                return None
        return _hosted_auth


def _output_path(raw: str) -> str | None:
    """Dashboard runs may write only CSV/JSON files in data or output."""
    path = Path(raw)
    if path.is_absolute() or path.suffix.lower() not in {".csv", ".json"}:
        return None
    resolved = (ROOT / path).resolve()
    if not any(_is_under(resolved, (ROOT / directory).resolve()) for directory in ("data", "output")):
        return None
    return str(path)


# ---------------------------------------------------------------- run spawning (display plumbing only)

def _append_log(line: str) -> None:
    with _lock:
        if line.startswith("### START"):
            _log.clear()
        _log.append(line)
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def _reader(proc: subprocess.Popen) -> None:
    try:
        assert proc.stdout is not None
        for raw in iter(proc.stdout.readline, ""):
            if raw == "":
                break
            _append_log(raw.rstrip("\n"))
    finally:
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        code = proc.poll()
        with _lock:
            _run["ended_at"] = utcnow()
            _run["exit_code"] = code
        _append_log(f"### EXIT code={code} at {utcnow().isoformat()}")
        try:
            if proc.stdout:
                proc.stdout.close()
        except Exception:
            pass


def _os_windows() -> bool:
    import os
    return os.name == "nt"


def _resolve_python() -> str:
    """Best interpreter to spawn the scraper with.

    Prefer the project virtualenv so the dashboard "Run" button works no
    matter how the server itself was started. Falls back to the current
    interpreter (e.g. when there is no .venv).
    """
    import sys
    rel = Path("Scripts") / "python.exe" if _os_windows() else Path("bin") / "python"
    venv_py = ROOT / ".venv" / rel
    if venv_py.is_file():
        return str(venv_py)
    return sys.executable


def _resolve_niche_ids() -> list:
    """Niche ids from the scraper config when importable, else builtin list."""
    try:
        from config.niches import all_niche_ids
        return all_niche_ids()
    except Exception:  # noqa: BLE001
        return KNOWN_NICHES


def _prepare_run(opts: dict) -> tuple[dict | None, str | None]:
    """Validate dashboard options and build a safe scraper command."""
    niche_opts = _resolve_niche_ids()
    niche = opts.get("niche") or [niche_opts[0]]
    if isinstance(niche, str):
        niche = [n.strip() for n in niche.replace(",", " ").split() if n.strip()]
    niche = [n for n in niche if n in niche_opts] or [niche_opts[0]]
    try:
        max_leads = max(1, min(int(opts.get("max", 6)), 500))
    except (TypeError, ValueError):
        max_leads = 6

    requested_out = str(opts.get("out") or "data/leads.csv").strip()
    out = _output_path(requested_out)
    if out is None:
        return None, "output must be a .csv or .json file under data/ or output/"

    seeds = str(opts.get("seeds") or "").strip()
    if seeds:
        seed_path = (ROOT / seeds).resolve()
        if not _is_under(seed_path, (ROOT / "data").resolve()) or not seed_path.is_file():
            return None, "seeds must be an existing file under data/"
    try:
        workers = int(opts.get("workers") or 2)
        workers = max(1, min(workers, 8))
    except (TypeError, ValueError):
        workers = 2

    py = _resolve_python()
    cmd = [py, "-u", "main.py", "--niche", *niche, "--max", str(max_leads),
           "--out", out, "--workers", str(workers)]
    if seeds:
        cmd += ["--seeds", seeds]
    if opts.get("emails_only"):
        cmd += ["--emails-only"]
    try:
        mq = int(opts.get("min_quality") or 0)
        if mq > 0:
            cmd += ["--min-quality", str(max(1, min(mq, 100)))]
    except (TypeError, ValueError):
        pass
    try:
        mx = int(opts.get("max_quality") or 100)
        if mx < 100:
            cmd += ["--max-quality", str(max(0, min(mx, 100)))]
    except (TypeError, ValueError):
        pass
    if opts.get("no_enrich"):
        cmd += ["--no-enrich"]
    if opts.get("fresh"):
        cmd += ["--fresh"]
    elif opts.get("no_merge"):
        cmd += ["--no-merge"]

    def q(a: str) -> str:
        return f'"{a}"' if " " in a else a

    return {
        "niche": niche,
        "max": max_leads,
        "out": out,
        "seeds": seeds,
        "workers": workers,
        "cmd": cmd,
        "cmd_str": " ".join(q(a) for a in cmd),
    }, None


def _bounded_int(value, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(int(value), high))
    except (TypeError, ValueError):
        return default


def _enqueue_hosted_run(opts: dict, requested_by: str = "") -> tuple[bool, dict]:
    """Persist a hosted dashboard request for the external worker."""
    config, error = _prepare_run(opts)
    if error:
        return False, {"error": error}
    assert config is not None
    options = {
        "niche": config["niche"],
        "max_leads": config["max"],
        "seeds": config["seeds"],
        "workers": config["workers"],
        "emails_only": bool(opts.get("emails_only")),
        "min_quality": _bounded_int(opts.get("min_quality"), 0, 0, 100),
        "max_quality": _bounded_int(opts.get("max_quality"), 100, 0, 100),
        "no_enrich": bool(opts.get("no_enrich")),
        "fresh": bool(opts.get("fresh")),
        "no_merge": bool(opts.get("no_merge")),
    }
    try:
        job_id = create_job(options, requested_by)
    except SupabaseJobError as exc:
        return False, {"error": str(exc)}
    return True, {
        "queued": True,
        "job_id": job_id,
        "message": "Scrape started. Leads will appear automatically as they are found.",
    }


def start_run(opts: dict, requested_by: str = "") -> tuple[bool, dict]:
    if _serverless_runtime():
        return _enqueue_hosted_run(opts, requested_by)
    config, error = _prepare_run(opts)
    if error:
        return False, {"error": error}
    assert config is not None
    with _lock:
        p = _run["proc"]
        if p is not None and p.poll() is None:
            return False, {"error": "a scrape is already running", "pid": p.pid}
        try:
            proc = subprocess.Popen(
                config["cmd"], cwd=str(ROOT), stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, bufsize=1,
            )
        except OSError as exc:
            return False, {"error": f"failed to launch scraper: {exc}"}
        _run.update(
            proc=proc, cmd=config["cmd"], cmd_str=config["cmd_str"],
            niches=config["niche"], max=config["max"], out_file=config["out"],
            seeds=config["seeds"], started_at=utcnow(), ended_at=None,
            exit_code=None, queries_seen=[],
        )
    _append_log(f"### START {utcnow().isoformat()} :: {config['cmd_str']}")
    t = threading.Thread(target=_reader, args=(proc,), daemon=True)
    t.start()
    return True, {"pid": proc.pid, "cmd": config["cmd_str"]}


def stop_run(job_id: str | None = None) -> dict:
    if _serverless_runtime():
        if not job_id:
            return {"ok": False, "error": "No hosted scrape job is available to cancel."}
        try:
            cancel_job(job_id)
        except SupabaseJobError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "running": False, "cancelled": True}
    with _lock:
        p = _run["proc"]
    if p is None or p.poll() is not None:
        return {"ok": True, "running": False}
    try:
        p.terminate()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    _append_log("### STOP requested by dashboard")
    return {"ok": True, "running": True}


def load_persisted_log() -> None:
    try:
        if LOG_PATH.is_file():
            lines = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
            last_start = next((i for i in range(len(lines) - 1, -1, -1)
                               if lines[i].startswith("### START ")), None)
            tail = lines[last_start:][-500:] if last_start is not None else []
            with _lock:
                _log.clear()
                _log.extend(tail)
                if last_start is None:
                    return
                parts = lines[last_start].split("::", 1)
                if len(parts) != 2:
                    return
                _run["cmd_str"] = parts[1].strip()
                try:
                    _run["started_at"] = datetime.fromisoformat(parts[0].removeprefix("### START ").strip())
                    cmd = shlex.split(_run["cmd_str"])
                    _run["cmd"] = cmd
                    for flag, field in (("--max", "max"), ("--out", "out_file"), ("--seeds", "seeds")):
                        if flag in cmd:
                            value = cmd[cmd.index(flag) + 1]
                            _run[field] = int(value) if flag == "--max" else value
                    if "--niche" in cmd:
                        start = cmd.index("--niche") + 1
                        _run["niches"] = []
                        for arg in cmd[start:]:
                            if arg.startswith("--"):
                                break
                            _run["niches"].append(arg)
                except (ValueError, IndexError):
                    pass
                exit_line = next((ln for ln in reversed(tail) if ln.startswith("### EXIT code=")), None)
                if exit_line:
                    match = re.match(r"### EXIT code=(-?\d+) at (\S+)", exit_line)
                    if match:
                        _run["exit_code"] = int(match.group(1))
                        try:
                            _run["ended_at"] = datetime.fromisoformat(match.group(2))
                        except ValueError:
                            pass
                else:
                    _run["exit_code"] = 1  # prior process was interrupted
    except OSError:
        pass


# ---------------------------------------------------------------- HTTP

MIME = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
        ".js": "text/javascript; charset=utf-8", ".json": "application/json",
        ".csv": "text/csv; charset=utf-8", ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg"}


class Handler(BaseHTTPRequestHandler):
    server_version = "LeadDash/1.0"

    def log_message(self, *a):  # quiet; dashboard has its own log view
        pass

    def _same_origin(self) -> bool:
        host = self.headers.get("Host", "")
        try:
            host_name = urlparse("//" + host).hostname or ""
        except ValueError:
            return False
        if not _dashboard_host_allowed(host_name):
            return False
        origin = self.headers.get("Origin")
        if origin:
            try:
                parsed = urlparse(origin)
                return (
                    parsed.scheme in {"http", "https"}
                    and parsed.netloc.lower() == host.lower()
                )
            except ValueError:
                return False
        return True

    # -- helpers
    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200, extra: dict | None = None):
        self._send(code, json.dumps(obj).encode(), "application/json", extra)

    def _static(self, name: str):
        target = (DASH / name).resolve()
        if not str(target).startswith(str(DASH.resolve())) or not target.is_file():
            self._json({"error": "not found"}, 404)
            return
        self._send(200, target.read_bytes(), MIME.get(target.suffix, "application/octet-stream"))

    def _cookie_token(self) -> str:
        try:
            cookies = SimpleCookie()
            cookies.load(self.headers.get("Cookie", ""))
            return cookies["lead_session"].value if "lead_session" in cookies else ""
        except Exception:
            return ""

    def _job_id(self) -> str:
        try:
            cookies = SimpleCookie()
            cookies.load(self.headers.get("Cookie", ""))
            return cookies["lead_job_id"].value if "lead_job_id" in cookies else ""
        except Exception:
            return ""

    def _auth(self):
        """Return the local auth store when the stdlib launcher attached one.

        Vercel's BaseHTTPRequestHandler adapter does not call ``serve()`` and
        therefore has no local auth attribute. When DASHBOARD_PASSWORD (and a
        session secret) are configured, use the stateless hosted store;
        otherwise protected routes remain safely unavailable.
        """
        server_auth = getattr(getattr(self, "server", None), "auth", None)
        return server_auth if server_auth is not None else _get_hosted_auth()

    def _session(self) -> dict | None:
        auth = self._auth()
        return auth.session(self._cookie_token()) if auth is not None else None

    def _require_session(self, api: bool) -> dict | None:
        session = self._session()
        if session:
            return session
        if api:
            self._json({"error": "login required"}, 401)
        else:
            self._send(303, b"", "text/plain", {"Location": "/login"})
        return None

    def _read_json(self, max_bytes: int = 4096) -> dict | None:
        if self.headers.get("Content-Type", "").split(";", 1)[0].lower() != "application/json":
            self._json({"error": "JSON content type required"}, 415)
            return None
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._json({"error": "invalid content length"}, 400)
            return None
        if length < 1 or length > max_bytes:
            self._json({"error": "invalid request body length"}, 413)
            return None
        try:
            opts = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._json({"error": "invalid JSON body"}, 400)
            return None
        if not isinstance(opts, dict):
            self._json({"error": "JSON object required"}, 400)
            return None
        return opts

    def _worker_authorized(self) -> bool:
        expected = os.getenv("WORKER_API_TOKEN", "")
        supplied = self.headers.get("Authorization", "")
        if len(expected) < 32 or not supplied.startswith("Bearer "):
            return False
        return hmac.compare_digest(supplied.removeprefix("Bearer "), expected)

    def _worker_request(self, path: str) -> bool:
        """Serve the token-protected GitHub worker API.

        The worker never receives the Supabase key. It claims jobs and uploads
        validated JSON batches through this Vercel function instead.
        """
        if path not in {
            "/api/worker/claim", "/api/worker/status",
            "/api/worker/leads", "/api/worker/finish",
        }:
            return False
        if not self._worker_authorized():
            self._json({"error": "worker authorization required"}, 401)
            return True
        opts = self._read_json(WORKER_BODY_LIMIT)
        if opts is None:
            return True
        try:
            if path == "/api/worker/claim":
                purged = purge_denied_leads()
                return self._json({"job": claim_next_job(), "purged": purged}) or True

            job_id = opts.get("job_id")
            job = get_job(job_id)
            if not job:
                self._json({"error": "scrape job was not found"}, 404)
                return True
            if path == "/api/worker/status":
                self._json({"status": str(job.get("status") or "")})
                return True
            if job.get("status") != "running":
                self._json({"error": "scrape job is not running"}, 409)
                return True

            if path == "/api/worker/leads":
                leads = opts.get("leads")
                if not isinstance(leads, list) or len(leads) > WORKER_BATCH_LIMIT:
                    self._json({"error": "invalid lead batch"}, 400)
                    return True
                saved = save_lead_dicts(leads)
                self._json({"saved": saved})
                return True

            status = opts.get("status")
            if status not in {"completed", "failed"}:
                self._json({"error": "invalid completion status"}, 400)
                return True
            raw_lines = opts.get("log_tail")
            lines = raw_lines if isinstance(raw_lines, list) else []
            values = {
                "status": status,
                "finished_at": utcnow().isoformat(),
                "log_tail": [str(line)[:2000] for line in lines[-80:]],
                "leads_saved": _bounded_int(opts.get("leads_saved"), 0, 0, 5000),
                "error": str(opts.get("error") or "")[:2000] or None,
            }
            update_job(str(job_id), values)
            self._json({"ok": True})
            return True
        except (SupabaseJobError, SupabaseStoreError) as exc:
            self._json({"error": str(exc)}, 503)
            return True

    # -- routes
    def do_GET(self):
        if not self._same_origin():
            return self._json({"error": "request origin is not allowed"}, 403)
        path = urlparse(self.path).path
        if path == "/login":
            if self._session():
                return self._send(303, b"", "text/plain", {"Location": "/"})
            return self._static("login.html")
        if path in ("/login.css", "/login.js", "/theme.js", "/share-preview.jpg"):
            return self._static(path.lstrip("/"))
        session = self._require_session(path.startswith("/api/") or path == "/export.csv")
        if not session:
            return
        if path in ("/", "/index.html"):
            return self._static("index.html")
        if path in ("/style.css", "/app.js"):
            return self._static(path.lstrip("/"))
        if path == "/api/session":
            session = self._session()
            return self._json({"username": session["username"], "csrf": session["csrf"]})
        if path == "/api/status":
            if _serverless_runtime():
                try:
                    return self._json(build_hosted_status(self._job_id(), session["username"]))
                except SupabaseJobError as exc:
                    return self._json({"error": str(exc)}, 503)
            return self._json(build_status())
        if path == "/api/leads":
            if _serverless_runtime():
                try:
                    leads = _hosted_leads()
                except SupabaseJobError as exc:
                    return self._json({"error": str(exc)}, 503)
                src = None
            else:
                leads, src = load_leads()
            niches = sorted({l["niche"] for l in leads if l["niche"]})
            return self._json({
                "leads": leads, "total": len(leads),
                "source_file": "Supabase public.leads" if _serverless_runtime()
                else (str(src.relative_to(ROOT)) if src and _is_under(src, ROOT)
                      else (str(src) if src else None)),
                "niches": niches,
                "updated_at": utcnow().isoformat(),
            })
        if path in ("/api/export.csv", "/export.csv"):
            if _serverless_runtime():
                try:
                    leads = _hosted_leads()
                except SupabaseJobError as exc:
                    return self._json({"error": str(exc)}, 503)
            else:
                leads, _ = load_leads()
            only_published = parse_qs(urlparse(self.path).query).get("published_only") == ["1"]
            if only_published:
                leads = [lead for lead in leads if lead["emails"] and lead["email_origin"] == "scraped"]
            import io
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["business_name", "niche", "website", "emails", "whatsapp_numbers",
                        "instagram_handles", "linkedin_urls", "phones", "source_query",
                        "quality_label", "quality_score", "email_origin", "scraped_at"])
            for l in leads:
                w.writerow([l["business_name"], l["niche"], l["website"],
                            " | ".join(l["emails"]), " | ".join(l["whatsapp_numbers"]),
                            " | ".join(l["instagram_handles"]), " | ".join(l["linkedin_urls"]),
                            " | ".join(l["phones"]), l["source_query"],
                            l["quality_label"], l["quality_score"], l["email_origin"], l["scraped_at"]])
            raw = ("\ufeff" + buf.getvalue()).encode("utf-8")
            return self._send(200, raw, "text/csv; charset=utf-8",
                              {"Content-Disposition": 'attachment; filename="leads.csv"'})
        return self._json({"error": "not found"}, 404)

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        if not self._same_origin():
            return self._json({"error": "request origin is not allowed"}, 403)
        path = urlparse(self.path).path
        if path.startswith("/api/worker/"):
            self._worker_request(path)
            return
        if path not in ("/api/login", "/api/logout", "/api/run", "/api/stop"):
            return self._json({"error": "not found"}, 404)
        if path == "/api/login":
            auth = self._auth()
            if auth is None:
                return self._json({"error": "hosted dashboard authentication is not configured"}, 503)
            opts = self._read_json()
            if opts is None:
                return
            username, password = opts.get("username"), opts.get("password")
            if not isinstance(username, str) or not isinstance(password, str):
                return self._json({"error": "invalid credentials"}, 401)
            try:
                result = auth.login(username, password, self.client_address[0])
            except PermissionError:
                return self._json({"error": "too many attempts; try again in 10 minutes"}, 429)
            if not result:
                return self._json({"error": "invalid credentials"}, 401)
            token, _ = result
            secure = "; Secure" if isinstance(auth, HostedAuthStore) else ""
            return self._json(
                {"username": username.strip().lower()},
                200,
                {"Set-Cookie": f"lead_session={token}; Path=/; HttpOnly{secure}; SameSite=Strict"},
            )
        session = self._require_session(True)
        if not session:
            return
        if not hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), session["csrf"]):
            return self._json({"error": "invalid request token"}, 403)
        if path == "/api/logout":
            auth = self._auth()
            if auth is not None:
                auth.revoke(self._cookie_token())
            secure = "; Secure" if isinstance(auth, HostedAuthStore) else ""
            return self._json(
                {"ok": True},
                200,
                {"Set-Cookie": f"lead_session=; Path=/; HttpOnly{secure}; SameSite=Strict; Max-Age=0"},
            )
        opts = self._read_json()
        if opts is None:
            return
        if path == "/api/run":
            ok, info = start_run(opts, session["username"])
            extra = {}
            if ok and info.get("job_id"):
                secure = "; Secure" if _serverless_runtime() else ""
                extra["Set-Cookie"] = (
                    f"lead_job_id={info['job_id']}; Path=/; HttpOnly{secure}; "
                    "SameSite=Strict"
                )
            return self._json(info, 200 if ok else 409, extra)
        if path == "/api/stop":
            return self._json(stop_run(self._job_id() if _serverless_runtime() else None))
        return self._json({"error": "not found"}, 404)


def serve(host="127.0.0.1", port=8765, leads: str | None = None):
    if not _local_host(host):
        raise ValueError("dashboard must bind to localhost or a loopback address")
    if leads:
        _leads_override["path"] = leads
    load_persisted_log()
    auth = AuthStore()
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.auth = auth
    srv.daemon_threads = True
    return srv
