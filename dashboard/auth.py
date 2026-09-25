"""Local dashboard accounts and short-lived, server-side sessions."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import tempfile
import threading
import time
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
USERS_PATH = ROOT / "data" / "dashboard-users.json"
USERS = ("tarun", "prabh", "uttkarsh")
ITERATIONS = 600_000
SESSION_SECONDS = 12 * 60 * 60
FAILED_LIMIT = 5
FAILED_WINDOW = 10 * 60


def _record(password: str) -> dict[str, str | int]:
    salt = secrets.token_bytes(32)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS)
    return {"salt": salt.hex(), "hash": digest.hex(), "iterations": ITERATIONS}


def _write_users(path: Path, users: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".dashboard-users-", dir=path.parent)
    try:
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump({"users": users}, out, indent=2)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def provision(passwords: dict[str, str], path: Path = USERS_PATH) -> None:
    """Create the exact three accounts once; never overwrite an existing store."""
    if set(passwords) != set(USERS) or not all(isinstance(p, str) and p for p in passwords.values()):
        raise ValueError("provide a nonempty password for tarun, prabh, and uttkarsh")
    if path.exists():
        raise FileExistsError(f"credential store already exists: {path}")
    _write_users(path, {user: _record(passwords[user]) for user in USERS})


def load_users(path: Path = USERS_PATH) -> dict:
    try:
        if os.name != "nt" and path.stat().st_mode & 0o077:
            raise ValueError(f"credential store must be owner-only (chmod 600): {path}")
        users = json.loads(path.read_text(encoding="utf-8"))["users"]
        if set(users) != set(USERS):
            raise ValueError("credential store must contain exactly tarun, prabh, and uttkarsh")
        for record in users.values():
            if record["iterations"] != ITERATIONS or len(bytes.fromhex(record["salt"])) != 32 or len(bytes.fromhex(record["hash"])) != 32:
                raise ValueError("invalid password record")
        return users
    except FileNotFoundError as exc:
        raise ValueError(f"dashboard accounts are not configured; run python dashboard/manage_users.py init ({path})") from exc
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid credential store: {path}") from exc


def set_password(username: str, password: str, path: Path = USERS_PATH) -> None:
    if username not in USERS or not password:
        raise ValueError("choose tarun, prabh, or uttkarsh and a nonempty password")
    users = load_users(path)
    users[username] = _record(password)
    _write_users(path, users)


class AuthStore:
    def __init__(self, path: Path = USERS_PATH):
        self.path = path
        self.users = load_users(path)
        self.mtime = path.stat().st_mtime_ns
        self.lock = threading.RLock()
        self.sessions: dict[str, dict] = {}
        self.failures: dict[str, deque[float]] = {}

    def _reload(self) -> None:
        mtime = self.path.stat().st_mtime_ns
        if mtime != self.mtime:
            self.users = load_users(self.path)
            self.mtime = mtime
            self.sessions.clear()  # password changes revoke every active session

    def login(self, username: str, password: str, peer: str) -> tuple[str, str] | None:
        username = username.strip().lower()
        if not isinstance(password, str) or len(password) > 1024:
            return None
        now = time.monotonic()
        key = f"{peer}:{username}"
        with self.lock:
            self._reload()
            attempts = self.failures.setdefault(key, deque())
            while attempts and now - attempts[0] > FAILED_WINDOW:
                attempts.popleft()
            if len(attempts) >= FAILED_LIMIT:
                raise PermissionError("too many attempts; try again later")
            record = self.users.get(username)
            salt = bytes.fromhex(record["salt"]) if record else bytes(32)
            expected = bytes.fromhex(record["hash"]) if record else bytes(32)
            candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS)
            if not record or not hmac.compare_digest(candidate, expected):
                attempts.append(now)
                return None
            self.failures.pop(key, None)
            token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            self.sessions[token] = {"username": username, "csrf": csrf, "expires": time.time() + SESSION_SECONDS}
            return token, csrf

    def session(self, token: str) -> dict | None:
        with self.lock:
            self._reload()
            session = self.sessions.get(token)
            if not session:
                return None
            if session["expires"] <= time.time():
                self.sessions.pop(token, None)
                return None
            return session.copy()

    def revoke(self, token: str) -> None:
        with self.lock:
            self.sessions.pop(token, None)


class HostedAuthStore:
    """Stateless serverless auth backed by Vercel environment variables.

    The shared password is never written to the repository or local user file.
    A signed, expiring cookie keeps sessions valid across function instances.
    """

    def __init__(self, password: str, session_secret: str):
        if not password:
            raise ValueError("DASHBOARD_PASSWORD must not be empty")
        if len(session_secret.encode("utf-8")) < 32:
            raise ValueError("DASHBOARD_SESSION_SECRET must be at least 32 characters")
        self.password = password.encode("utf-8")
        self.session_secret = session_secret.encode("utf-8")
        # Bind cookies to both secrets so rotating either one immediately
        # revokes sessions issued with the previous dashboard password.
        self.signing_key = hmac.new(
            self.session_secret, self.password, hashlib.sha256
        ).digest()

    @classmethod
    def from_env(cls) -> "HostedAuthStore | None":
        password = os.getenv("DASHBOARD_PASSWORD", "")
        if not password:
            return None
        session_secret = (
            os.getenv("DASHBOARD_SESSION_SECRET", "")
            or os.getenv("SUPABASE_SECRET_KEY", "")
            or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
        )
        if not session_secret:
            raise ValueError("DASHBOARD_SESSION_SECRET or SUPABASE_SECRET_KEY must be set")
        return cls(password, session_secret)

    @staticmethod
    def _encode(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

    @staticmethod
    def _decode(value: str) -> bytes:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))

    def _signature(self, payload: str) -> str:
        digest = hmac.new(
            self.signing_key, payload.encode("ascii"), hashlib.sha256
        ).digest()
        return self._encode(digest)

    def _issue(self, username: str) -> tuple[str, str]:
        csrf = secrets.token_urlsafe(32)
        payload = self._encode(json.dumps({
            "username": username,
            "csrf": csrf,
            "expires": int(time.time() + SESSION_SECONDS),
        }, separators=(",", ":")).encode("utf-8"))
        return f"{payload}.{self._signature(payload)}", csrf

    def _read(self, token: str) -> dict | None:
        try:
            payload, signature = token.split(".", 1)
            if not hmac.compare_digest(signature, self._signature(payload)):
                return None
            data = json.loads(self._decode(payload))
            if (
                data.get("username") not in USERS
                or not isinstance(data.get("csrf"), str)
                or not isinstance(data.get("expires"), int)
                or data["expires"] <= int(time.time())
            ):
                return None
            return data
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def login(self, username: str, password: str, peer: str) -> tuple[str, str] | None:
        del peer  # Vercel instances do not share a durable rate-limit store.
        username = username.strip().lower()
        if username not in USERS or not isinstance(password, str):
            return None
        if not hmac.compare_digest(password.encode("utf-8"), self.password):
            return None
        return self._issue(username)

    def session(self, token: str) -> dict | None:
        return self._read(token) if token else None

    def revoke(self, token: str) -> None:
        # Stateless cookies are invalidated by clearing the cookie. Rotating
        # DASHBOARD_PASSWORD or DASHBOARD_SESSION_SECRET invalidates all tokens.
        del token
