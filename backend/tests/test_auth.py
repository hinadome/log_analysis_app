"""Authentication: token gate, cookie login, and brute-force protection.

conftest pins LOGSMITH_TOKEN="" before import, so AUTH_TOKEN defaults to
disabled; these tests monkeypatch the module attribute to switch it on.
"""
from __future__ import annotations

import pytest

import app.auth as auth_module

TOKEN = "correct-horse-battery"


@pytest.fixture()
def auth_on(monkeypatch):
    monkeypatch.setattr(auth_module, "AUTH_TOKEN", TOKEN)
    auth_module._LOGIN_FAILURES.clear()
    yield
    auth_module._LOGIN_FAILURES.clear()


def test_api_requires_auth_when_enabled(client, auth_on):
    assert client.get("/api/jobs").status_code == 401
    assert client.get("/api/logs").status_code == 401

    # the auth endpoints themselves stay reachable
    status = client.get("/api/auth/status").json()
    assert status == {"auth_required": True}

    # Bearer token authenticates (automation path)
    assert client.get("/api/jobs",
                      headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200
    assert client.get("/api/jobs",
                      headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_cookie_login_and_logout(client, auth_on):
    assert client.post("/api/auth/login", json={"password": "nope"}).status_code == 401

    res = client.post("/api/auth/login", json={"password": TOKEN})
    assert res.status_code == 200 and res.json()["ok"] is True
    set_cookie = res.headers["set-cookie"]
    assert "logsmith_session=" in set_cookie
    assert TOKEN not in set_cookie  # the cookie carries a hash, never the token
    # the session expires — it must not live forever in the browser
    assert f"Max-Age={auth_module.SESSION_MAX_AGE}" in set_cookie

    # the session cookie authenticates (browser path)
    assert client.get("/api/jobs").status_code == 200

    # logout revokes it
    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/jobs").status_code == 401


def test_login_rate_limit(client, auth_on):
    for _ in range(auth_module.LOGIN_MAX_FAILURES):
        res = client.post("/api/auth/login", json={"password": "wrong"})
        assert res.status_code == 401

    # locked out — even the correct password is refused while hot
    blocked = client.post("/api/auth/login", json={"password": "wrong"})
    assert blocked.status_code == 429
    assert "Retry-After" in blocked.headers
    assert client.post("/api/auth/login", json={"password": TOKEN}).status_code == 429


def test_successful_login_clears_failure_count(client, auth_on):
    # failures close to the limit, then a success…
    for _ in range(auth_module.LOGIN_MAX_FAILURES - 1):
        assert client.post("/api/auth/login", json={"password": "wrong"}).status_code == 401
    assert client.post("/api/auth/login", json={"password": TOKEN}).status_code == 200

    # …wipes the slate: the next burst is judged from zero, not from before
    for _ in range(auth_module.LOGIN_MAX_FAILURES):
        res = client.post("/api/auth/login", json={"password": "wrong"})
        assert res.status_code == 401
    assert client.post("/api/auth/login", json={"password": "wrong"}).status_code == 429


def test_login_always_open_when_auth_disabled(client):
    """No token configured: login is a no-op and nothing is rate-limited."""
    assert client.get("/api/auth/status").json() == {"auth_required": False}
    for _ in range(10):
        res = client.post("/api/auth/login", json={"password": "whatever"})
        assert res.status_code == 200
    assert client.get("/api/jobs").status_code == 200
