"""Authentication and route-gating tests for the local dashboard."""

import json
import stat
import threading
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen

import pytest

import dashboard.auth as auth
import dashboard.server as server


@pytest.fixture
def credentials(tmp_path, monkeypatch):
    # Production uses 600,000 rounds; lower only this test process's cost.
    monkeypatch.setattr(auth, "ITERATIONS", 1000)
    path = tmp_path / "dashboard-users.json"
    auth.provision({"tarun": "test-pass", "prabh": "test-pass", "uttkarsh": "test-pass"}, path)
    return path


def test_provision_exact_users_and_private_file(credentials):
    assert set(auth.load_users(credentials)) == set(auth.USERS)
    assert stat.S_IMODE(credentials.stat().st_mode) == 0o600
    assert "test-pass" not in credentials.read_text()
    with pytest.raises(FileExistsError):
        auth.provision({user: "other" for user in auth.USERS}, credentials)
    with pytest.raises(ValueError):
        auth.provision({"tarun": "only one"}, credentials.with_name("bad.json"))


def test_password_rotation_revokes_sessions(credentials):
    store = auth.AuthStore(credentials)
    token, _ = store.login("tarun", "test-pass", "127.0.0.1")
    assert store.session(token)["username"] == "tarun"
    auth.set_password("tarun", "new-secret", credentials)
    assert store.session(token) is None
    assert store.login("tarun", "test-pass", "127.0.0.1") is None
    assert store.login("tarun", "new-secret", "127.0.0.1")


def test_login_rate_limit(credentials):
    store = auth.AuthStore(credentials)
    for _ in range(auth.FAILED_LIMIT):
        assert store.login("tarun", "wrong", "127.0.0.1") is None
    with pytest.raises(PermissionError):
        store.login("tarun", "test-pass", "127.0.0.1")


def test_hosted_auth_uses_environment_password_and_signed_session(monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "test-pass")
    monkeypatch.setenv("DASHBOARD_SESSION_SECRET", "s" * 48)
    store = auth.HostedAuthStore.from_env()
    assert store is not None
    token, csrf = store.login("Tarun", "test-pass", "203.0.113.10")
    session = store.session(token)
    assert session["username"] == "tarun"
    assert session["csrf"] == csrf
    assert store.login("tarun", "wrong", "203.0.113.10") is None
    assert store.session(token + "tampered") is None
    store.revoke(token)  # stateless logout is handled by clearing the cookie


def _post(opener, url, path, data, csrf=None):
    headers = {"Content-Type": "application/json"}
    if csrf:
        headers["X-CSRF-Token"] = csrf
    return opener.open(Request(url + path, data=json.dumps(data).encode(), headers=headers), timeout=3)


def test_routes_require_one_of_three_sessions_and_csrf(credentials, monkeypatch):
    calls = []
    monkeypatch.setattr(server, "start_run", lambda opts: (calls.append(opts) or True, {"ok": True}))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    httpd.auth = auth.AuthStore(credentials)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{httpd.server_port}"
    try:
        with urlopen(url + "/login", timeout=3) as response:
            assert b"Sign in to Lead Studio" in response.read()
        with urlopen(url + "/", timeout=3) as response:
            assert response.url.endswith("/login")
            assert b"New scrape" not in response.read()
        with urlopen(url + "/app.js", timeout=3) as response:
            assert response.url.endswith("/login")
        for path in ("/api/status", "/api/leads", "/api/export.csv", "/api/session"):
            with pytest.raises(HTTPError) as exc:
                urlopen(url + path, timeout=3)
            assert exc.value.code == 401
        with pytest.raises(HTTPError) as exc:
            _post(build_opener(), url, "/api/run", {})
        assert exc.value.code == 401
        with pytest.raises(HTTPError) as exc:
            _post(build_opener(), url, "/api/login", {"username": "other", "password": "test-pass"})
        assert exc.value.code == 401
        for username in auth.USERS:
            opener = build_opener(HTTPCookieProcessor(CookieJar()))
            with _post(opener, url, "/api/login", {"username": username, "password": "test-pass"}) as response:
                assert response.status == 200
                assert "HttpOnly" in response.headers["Set-Cookie"]
                assert "SameSite=Strict" in response.headers["Set-Cookie"]
            with opener.open(url + "/api/session", timeout=3) as response:
                session = json.load(response)
            assert session["username"] == username
            with opener.open(url + "/", timeout=3) as response:
                assert b"Lead Studio" in response.read()
            with pytest.raises(HTTPError) as exc:
                _post(opener, url, "/api/run", {"niche": ["real_estate"]})
            assert exc.value.code == 403
            with _post(opener, url, "/api/run", {"niche": ["real_estate"]}, session["csrf"]) as response:
                assert response.status == 200
            with _post(opener, url, "/api/logout", {}, session["csrf"]) as response:
                assert response.status == 200
            with pytest.raises(HTTPError) as exc:
                opener.open(url + "/api/leads", timeout=3)
            assert exc.value.code == 401
        assert len(calls) == 3
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)
