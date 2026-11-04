"""User management: the admin API and the /admin/users page."""
from __future__ import annotations

import pytest

import app.auth as auth_module
from app.services import session_store as session_module
from app.services import user_store as user_module

from test_users_auth import login  # the login helper


@pytest.fixture()
def admin(client, users_file, sessions_file, fresh_logins):
    users_file.create("admin", "admin-pw", role="admin")
    users_file.create("alice", "alice-pw")
    users_file.create("bob", "bob-pw")
    login(client, "admin", "admin-pw")
    return users_file


def as_user(client, username, password):
    """A second client sharing the same app, logged in as someone else."""
    from fastapi.testclient import TestClient
    from app.main import app
    other = TestClient(app)
    assert login(other, username, password).status_code == 200
    return other


# --- permissions ---------------------------------------------------------------

def test_non_admin_gets_403(client, admin):
    alice = as_user(client, "alice", "alice-pw")
    assert alice.get("/api/users").status_code == 403
    assert alice.post("/api/users",
                      json={"username": "mallory", "password": "pw-1234"}).status_code == 403
    assert alice.post("/api/users/bob/disable").status_code == 403
    assert alice.get("/admin/users").status_code == 403  # the page too


def test_admin_can_list_and_create(client, admin):
    res = client.get("/api/users")
    assert res.status_code == 200
    listed = {u["username"]: u for u in res.json()["users"]}
    assert set(listed) == {"admin", "alice", "bob"}
    assert listed["admin"]["role"] == "admin"
    assert "password_hash" not in listed["admin"]  # hashes never leave the store

    res = client.post("/api/users",
                      json={"username": "carol", "password": "carol-pw"})
    assert res.status_code == 200
    assert res.json()["user"]["role"] == "user"
    assert user_store_entry("carol") is not None


def user_store_entry(username):
    from app.services.user_store import store
    return store.get(username)


# --- the guards ----------------------------------------------------------------

def test_password_reset_revokes_sessions(client, admin, sessions_file):
    alice = as_user(client, "alice", "alice-pw")
    assert alice.get("/api/jobs").status_code == 200

    res = client.post("/api/users/alice/password", json={"password": "new-pw"})
    assert res.status_code == 200
    assert alice.get("/api/jobs").status_code == 401  # her session died
    assert login(client, "alice", "new-pw").status_code == 200  # new pw works
    client.post("/api/auth/logout")
    login(client, "admin", "admin-pw")


def test_disable_enable_flow(client, admin, sessions_file):
    alice = as_user(client, "alice", "alice-pw")
    assert client.post("/api/users/alice/disable").status_code == 200
    assert alice.get("/api/jobs").status_code == 401      # session revoked
    assert login(client, "alice", "alice-pw").status_code == 401  # login blocked

    assert client.post("/api/users/alice/enable").status_code == 200
    assert login(client, "alice", "alice-pw").status_code == 200
    client.post("/api/auth/logout")


def test_role_changes_and_the_last_admin(client, admin):
    assert client.post("/api/users/alice/role", json={"role": "admin"}).status_code == 200
    assert user_store_entry("alice").role == "admin"

    # two admins: the first may step down — alice remains
    assert client.post("/api/users/admin/role", json={"role": "user"}).status_code == 200

    # …but the last one may not demote or disable themselves (the acting
    # session is alice's now — the admin stepped down above)
    alice = as_user(client, "alice", "alice-pw")
    res = alice.post("/api/users/alice/role", json={"role": "user"})
    assert res.status_code == 422
    assert "last admin" in res.json()["detail"]
    res = alice.post("/api/users/alice/disable")
    assert res.status_code == 422
    assert "last admin" in res.json()["detail"]
    assert user_store_entry("alice").disabled is False


def test_self_disable_is_allowed_with_a_backup_admin(client, admin):
    admin.create("backup-admin", "pw-1234", role="admin")
    res = client.post("/api/users/admin/disable")
    assert res.status_code == 200
    assert admin.get("admin").disabled is True


def test_unknown_user_is_404(client, admin):
    assert client.post("/api/users/ghost/password",
                       json={"password": "pw-1234"}).status_code == 404
    assert client.post("/api/users/ghost/disable").status_code == 404
    assert client.post("/api/users/ghost/role", json={"role": "admin"}).status_code == 404


# --- the bootstrap: first account from a single-user server ----------------------

def test_operator_creates_first_admin_and_stays_logged_in(client, users_file,
                                                          sessions_file, fresh_logins):
    assert auth_module.multi_user_enabled() is False

    # the local operator is the admin before any account exists
    assert client.get("/api/users").status_code == 200
    assert client.get("/api/users").json() == {"users": []}

    # but the first account must be an admin
    res = client.post("/api/users", json={"username": "root", "password": "root-pw",
                                          "role": "user"})
    assert res.status_code == 422
    assert "first account must be an admin" in res.json()["detail"]

    res = client.post("/api/users", json={"username": "root", "password": "root-pw",
                                          "role": "admin"})
    assert res.status_code == 200
    assert auth_module.multi_user_enabled() is True

    # the response carries the minted session: no bounce to /login mid-click
    assert "logsmith_session=" in res.headers["set-cookie"]
    status = client.get("/api/auth/status").json()
    assert status["user"] == {"username": "root", "role": "admin"}

    # and the operator's old powers are gone without that session
    from fastapi.testclient import TestClient
    from app.main import app
    fresh = TestClient(app)
    assert fresh.get("/api/users").status_code == 401


def test_bootstrap_via_python_ui_form(client, users_file, sessions_file, fresh_logins):
    page = client.get("/admin/users")  # single-user mode: operator is admin
    assert page.status_code == 200
    assert "Create a user" in page.text

    res = client.post("/admin/users",
                      data={"username": "root", "password": "root-pw", "role": "admin"},
                      follow_redirects=False)
    assert res.status_code == 303
    assert "logsmith_session=" in res.headers["set-cookie"]  # carried over as admin
    assert client.get("/api/auth/status").json()["user"]["username"] == "root"


# --- the admin page (python UI) --------------------------------------------------

def test_admin_page_lists_and_acts(client, admin):
    page = client.get("/admin/users")
    assert page.status_code == 200
    for name in ("admin", "alice", "bob"):
        assert name in page.text
    assert "admin-pw" not in page.text  # never a hash or password on the page

    res = client.post("/admin/users/alice/disable", follow_redirects=False)
    assert res.status_code == 303
    from urllib.parse import unquote_plus
    assert "Disabled alice" in unquote_plus(res.headers["location"])
    assert user_store_entry("alice").disabled is True

    # the nav link is admin-only
    assert '/admin/users"' in client.get("/").text
    bob = as_user(client, "bob", "bob-pw")  # alice is disabled above
    assert '/admin/users"' not in bob.get("/").text
