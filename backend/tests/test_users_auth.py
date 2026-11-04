"""Multi-user mode: accounts, sessions, and the login contract.

The store singletons are pointed at per-test files (the same trick the
scripts_dir fixture uses) so a test creating accounts never leaks the
multi-user mode into other tests.
"""
from __future__ import annotations

import json

import pytest

import app.auth as auth_module
from app.services import session_store as session_module
from app.services import user_store as user_module
from app.services.user_store import verify_password
from test_auth import auth_on  # noqa: F401 -- fixture: token mode, rate-limit slate


# --- the store ---------------------------------------------------------------

def test_user_store_roundtrip(users_file):
    users_file.create("alice", "secret-pw", role="admin")
    users_file.create("bob", "another-one")

    alice = users_file.get("alice")
    assert alice.role == "admin"
    assert verify_password(alice, "secret-pw")
    assert not verify_password(alice, "wrong")
    assert users_file.get("bob").role == "user"
    assert users_file.get("nobody") is None

    # persisted: usernames visible, password hashes not in the clear
    raw = json.loads(users_file._path.read_text(encoding="utf-8"))
    assert [u["username"] for u in raw["users"]] == ["alice", "bob"]
    assert all("secret-pw" not in u["password_hash"] for u in raw["users"])
    assert all(u["password_hash"].startswith("scrypt$") for u in raw["users"])


def test_user_store_validation(users_file):
    users_file.create("alice", "pw-1234")
    with pytest.raises(ValueError, match="already exists"):
        users_file.create("alice", "pw-5678")
    for bad in ("", "-dash", "with space", "x" * 33, "önce"):
        with pytest.raises(ValueError):
            users_file.create(bad, "pw-1234")
    # surrounding whitespace is forgiven, not fatal (form input)
    assert users_file.create("  carol ", "pw-1234").username == "carol"
    with pytest.raises(ValueError):
        users_file.create("carol", "abc")   # too short
    with pytest.raises(ValueError):
        users_file.create("dave", "good-pw", role="root")


def test_user_store_reloads_on_external_edit(users_file):
    users_file.create("alice", "pw-1234")
    # a hand-edit (admin fixing the file by hand) is picked up live
    users_file._path.write_text(json.dumps(
        {"users": [{"id": "x", "username": "dave", "password_hash":
                    "scrypt$16384$8$1$00$00", "role": "user"}]}), encoding="utf-8")
    assert users_file.get("alice") is None
    assert users_file.get("dave").id == "x"
    assert users_file.has_users()


def test_password_reset_and_disable(users_file, sessions_file):
    users_file.create("alice", "old-pw")
    sid = sessions_file.create("alice")
    assert sessions_file.user_for(sid) == "alice"

    # a reset revokes live sessions — the sid must not survive the new password
    users_file.set_password(users_file.get("alice"), "new-pw")
    sessions_file.drop_user("alice")
    assert sessions_file.user_for(sid) is None
    assert verify_password(users_file.get("alice"), "new-pw")

    # a disabled user: valid login pair, but no entry
    users_file.set_disabled(users_file.get("alice"), True)
    assert sessions_file.valid(sessions_file.create("alice")) is False
    users_file.set_disabled(users_file.get("alice"), False)
    assert sessions_file.valid(sessions_file.create("alice")) is True


# --- sessions ------------------------------------------------------------------

def test_session_lifecycle(sessions_file, users_file):
    users_file.create("alice", "pw-1234")
    sid = sessions_file.create("alice")
    assert sessions_file.user_for(sid) == "alice"
    assert sessions_file.user_for(None) is None
    assert sessions_file.user_for("forged") is None

    sessions_file.drop(sid)
    assert sessions_file.user_for(sid) is None

    # drop_user sweeps every session of that user and only that user
    other = sessions_file.create("alice")
    users_file.create("bob", "pw-5678")
    bob_sid = sessions_file.create("bob")
    assert sessions_file.drop_user("alice") == 1
    assert sessions_file.user_for(other) is None
    assert sessions_file.user_for(bob_sid) == "bob"


def test_sessions_expire_with_age(sessions_file, users_file):
    users_file.create("alice", "pw-1234")
    sid = sessions_file.create("alice")
    # age it beyond the window, as time passing would
    sessions_file._sessions[sid]["created_at"] -= session_module.SESSION_MAX_AGE + 1
    assert sessions_file.user_for(sid) is None


# --- mode: anonymous (no users, no token) — today's behavior -------------------

def test_anonymous_mode_status_is_exactly_the_old_shape(client, users_file, sessions_file):
    assert auth_module.multi_user_enabled() is False
    assert client.get("/api/auth/status").json() == {"auth_required": False}
    assert client.get("/api/jobs").status_code == 200


# --- mode: legacy shared password ----------------------------------------------

def test_legacy_token_mode_unchanged(client, users_file, sessions_file,
                                     auth_on, fresh_logins):
    from test_auth import TOKEN
    status = client.get("/api/auth/status").json()
    assert status == {"auth_required": True}

    # the username field is accepted and ignored in legacy mode
    assert client.post("/api/auth/login",
                       json={"username": "ignored", "password": TOKEN}).status_code == 200
    assert client.get("/api/jobs").status_code == 200
    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/jobs").status_code == 401


# --- mode: multi-user ----------------------------------------------------------

@pytest.fixture()
def multi_user(client, users_file, sessions_file, fresh_logins):
    """Two accounts: an admin and a regular user; mode flips on."""
    users_file.create("admin", "admin-pw", role="admin")
    users_file.create("alice", "alice-pw")
    assert auth_module.multi_user_enabled() is True
    return users_file


def login(client, username, password):
    return client.post("/api/auth/login",
                       json={"username": username, "password": password})


def test_multi_user_login_flow(client, multi_user, sessions_file):
    assert client.get("/api/jobs").status_code == 401

    status = client.get("/api/auth/status").json()
    assert status == {"auth_required": True, "multi_user": True, "user": None}

    # wrong pair: one message, no account confirmation
    res = login(client, "alice", "nope")
    assert res.status_code == 401
    assert res.json()["detail"] == "Incorrect username or password"
    assert login(client, "ghost", "nope").status_code == 401

    res = login(client, "alice", "alice-pw")
    assert res.status_code == 200
    assert "logsmith_session=" in res.headers["set-cookie"]
    assert "alice-pw" not in res.headers["set-cookie"]  # only the opaque sid

    status = client.get("/api/auth/status").json()
    assert status["user"] == {"username": "alice", "role": "user"}
    assert client.get("/api/jobs").status_code == 200

    # logout revokes server-side: the sid is dead even if the cookie lingers
    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/jobs").status_code == 401


def test_multi_user_disabled_account_cannot_login(client, multi_user, sessions_file):
    # a live session dies when the account is disabled
    sid = sessions_file.create("alice")
    multi_user.set_disabled(multi_user.get("alice"), True)
    assert sessions_file.valid(sid) is False
    assert login(client, "alice", "alice-pw").status_code == 401


def test_multi_user_rate_limit(client, multi_user, fresh_logins):
    for _ in range(auth_module.LOGIN_MAX_FAILURES):
        assert login(client, "alice", "wrong").status_code == 401
    blocked = login(client, "alice", "alice-pw")  # even the right password
    assert blocked.status_code == 429
    assert "Retry-After" in blocked.headers


def test_multi_user_python_ui_login_form(client, multi_user, sessions_file):
    page = client.get("/login")
    assert page.status_code == 200
    assert 'name="username"' in page.text  # the form grew the username field

    res = client.post("/login", data={"username": "alice", "password": "alice-pw",
                                      "next": "/"},
                      follow_redirects=False)
    assert res.status_code == 303
    assert client.get("/api/jobs").status_code == 200

    client.post("/api/auth/logout")
    res = client.post("/login", data={"username": "alice", "password": "wrong",
                                      "next": "/"})
    assert res.status_code == 401
    assert "Incorrect username or password" in res.text
