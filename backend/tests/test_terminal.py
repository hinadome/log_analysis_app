"""The remote terminal (SCRIPTENGINE_TERMINAL).

The flag is module-level state read at import, so tests monkeypatch each
consumer's copy (router, pages, main) — the same pattern test_sandbox.py
uses for DOCKER_SANDBOX. Sessions hold real ptys and shells: the fixture
teardown shuts the service down so nothing leaks between tests, and the
TestClient's own lifespan shutdown afterwards is a no-op by then.
"""
from __future__ import annotations

import asyncio
import base64
import importlib
import json
import os
import time

import pytest

import app.main as main_module
import app.pages as pages_module
import app.routers.terminal as terminal_router
import app.services.terminal as terminal_service
from test_users_admin import as_user


@pytest.fixture
def terminal_on(monkeypatch):
    """Turn every consumer of the flag on, with a deterministic shell."""
    monkeypatch.setenv("SHELL", "/bin/sh")
    monkeypatch.setattr(terminal_router, "TERMINAL_ENABLED", True)
    monkeypatch.setattr(pages_module, "TERMINAL_ENABLED", True)
    monkeypatch.setattr(main_module, "TERMINAL_ENABLED", True)
    yield
    asyncio.run(terminal_service.terminal.shutdown())


# --- flag off: the feature does not exist ---------------------------------

def test_disabled_flag_hides_the_feature(client):
    """SCRIPTENGINE_TERMINAL=0 (pinned in conftest): the feature does not
    exist — every endpoint 404s and both UIs drop it."""
    assert client.get("/api/health").json()["terminal"] is False
    for method, path in (("post", "/api/terminal/sessions"),
                         ("get", "/api/terminal/sessions/abc/output"),
                         ("post", "/api/terminal/sessions/abc/input"),
                         ("delete", "/api/terminal/sessions/abc")):
        assert getattr(client, method)(path).status_code == 404, path
    # a valid body too — the gate must beat any validation nitpicks
    res = client.post("/api/terminal/sessions/abc/size", json={"cols": 80, "rows": 24})
    assert res.status_code == 404
    # the python UI hides the nav link and 404s the page, like the API
    assert 'href="/terminal"' not in client.get("/").text
    assert client.get("/terminal").status_code == 404
    assert client.post("/terminal", data={"command": "echo hi"}).status_code == 404


def test_enabled_by_default():
    """No configuration, no accounts: the terminal is simply there — the
    single-user operator (the admin when no accounts exist) sees it."""
    import app.config as config_module
    saved = os.environ.pop("SCRIPTENGINE_TERMINAL", None)
    saved_editing = os.environ.get("SCRIPTENGINE_ALLOW_SCRIPT_EDITING")
    os.environ.pop("SCRIPTENGINE_ALLOW_SCRIPT_EDITING", None)
    try:
        assert importlib.reload(config_module).TERMINAL_ENABLED is True
    finally:
        if saved is not None:
            os.environ["SCRIPTENGINE_TERMINAL"] = saved
        if saved_editing is not None:
            os.environ["SCRIPTENGINE_ALLOW_SCRIPT_EDITING"] = saved_editing
        importlib.reload(config_module)


def test_lockdown_forces_terminal_off():
    """SCRIPTENGINE_ALLOW_SCRIPT_EDITING=0 means "run the vetted scripts only" —
    a shell is strictly more powerful, so it stays off even if asked for."""
    import app.config as config_module
    saved = {k: os.environ.get(k) for k in
             ("SCRIPTENGINE_TERMINAL", "SCRIPTENGINE_ALLOW_SCRIPT_EDITING")}
    try:
        os.environ["SCRIPTENGINE_TERMINAL"] = "1"
        os.environ["SCRIPTENGINE_ALLOW_SCRIPT_EDITING"] = "0"
        assert importlib.reload(config_module).TERMINAL_ENABLED is False
        os.environ["SCRIPTENGINE_ALLOW_SCRIPT_EDITING"] = "1"
        assert importlib.reload(config_module).TERMINAL_ENABLED is True
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        importlib.reload(config_module)


# --- the interactive session -----------------------------------------------

def test_session_lifecycle(client, terminal_on):
    """create → feed a command + exit → one SSE stream carries the output
    that accumulated while nobody was listening, then the exit frame."""
    assert client.get("/api/health").json()["terminal"] is True

    res = client.post("/api/terminal/sessions")
    assert res.status_code == 200
    body = res.json()
    assert body["id"] and body["shell"]
    sid = body["id"]

    # keystrokes as the raw body, typed before anyone attaches — the
    # queue holds them for the stream that comes later
    r = client.post(f"/api/terminal/sessions/{sid}/input",
                    content="echo hello-terminal-$((20+3))\nexit\n")
    assert r.status_code == 200

    events = []
    with client.stream("GET", f"/api/terminal/sessions/{sid}/output") as stream:
        assert stream.status_code == 200
        assert stream.headers["content-type"].startswith("text/event-stream")
        for line in stream.iter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[len("data: "):]))

    out = b"".join(base64.b64decode(e["data"])
                  for e in events if e["type"] == "output")
    assert b"hello-terminal-23" in out
    exits = [e for e in events if e["type"] == "exit"]
    assert len(exits) == 1 and exits[0]["code"] == 0


def test_unknown_session_404s(client, terminal_on):
    for method, path in (("get", "/api/terminal/sessions/nope/output"),
                         ("post", "/api/terminal/sessions/nope/input"),
                         ("delete", "/api/terminal/sessions/nope")):
        assert getattr(client, method)(path).status_code == 404, path
    assert client.post("/api/terminal/sessions/nope/size",
                       json={"cols": 80, "rows": 24}).status_code == 404


def test_resize_and_kill(client, terminal_on):
    sid = client.post("/api/terminal/sessions").json()["id"]

    res = client.post(f"/api/terminal/sessions/{sid}/size",
                     json={"cols": 120, "rows": 40})
    assert res.status_code == 200 and res.json()["ok"] is True
    # out-of-range sizes are rejected before touching the pty
    assert client.post(f"/api/terminal/sessions/{sid}/size",
                       json={"cols": 1, "rows": 40}).status_code == 422
    assert client.post(f"/api/terminal/sessions/{sid}/size",
                       json={"cols": 120, "rows": 9999}).status_code == 422

    assert client.delete(f"/api/terminal/sessions/{sid}").status_code == 200
    assert client.delete(f"/api/terminal/sessions/{sid}").status_code == 404


def test_input_too_large(client, terminal_on):
    sid = client.post("/api/terminal/sessions").json()["id"]
    res = client.post(f"/api/terminal/sessions/{sid}/input",
                      content=b"x" * (256 * 1024 + 1))
    assert res.status_code == 413


def test_input_after_exit_is_410(client, terminal_on):
    sid = client.post("/api/terminal/sessions").json()["id"]
    client.post(f"/api/terminal/sessions/{sid}/input", content="exit\n")
    session = terminal_service.terminal.get(sid)
    for _ in range(50):  # the reader notices EOF asynchronously
        if session is None or session.closed:
            break
        time.sleep(0.1)
    assert session is not None and session.closed
    res = client.post(f"/api/terminal/sessions/{sid}/input", content="x")
    assert res.status_code == 410


def test_session_cap(client, terminal_on, monkeypatch):
    monkeypatch.setattr(terminal_service, "TERMINAL_MAX_SESSIONS", 1)
    assert client.post("/api/terminal/sessions").status_code == 200
    res = client.post("/api/terminal/sessions")
    assert res.status_code == 429
    assert "terminal sessions are in use" in res.json()["detail"]


def test_session_survives_disconnect(client, terminal_on):
    """A dropped stream does not end the session: output produced while
    nobody was listening is waiting in the queue when the next one attaches."""
    sid = client.post("/api/terminal/sessions").json()["id"]

    # attach, read whatever the shell said first, disconnect mid-stream
    with client.stream("GET", f"/api/terminal/sessions/{sid}/output") as stream:
        for line in stream.iter_lines():
            if line.startswith("data: "):
                break  # got one frame — that's enough, drop the connection

    client.post(f"/api/terminal/sessions/{sid}/input",
                content="echo still-alive-xyz\nexit\n")

    events = []
    with client.stream("GET", f"/api/terminal/sessions/{sid}/output") as stream:
        for line in stream.iter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[len("data: "):]))
    out = b"".join(base64.b64decode(e["data"])
                  for e in events if e["type"] == "output")
    assert b"still-alive-xyz" in out
    assert [e for e in events if e["type"] == "exit"]


# --- multi-user: the shell is the admin's -----------------------------------

def test_multi_user_regular_user_gets_403s(client, terminal_on,
                                           users_file, sessions_file, fresh_logins):
    """A terminal is a shell as the server user — the one surface no
    regular account should reach. Every endpoint answers 403 (the feature
    exists, it just isn't theirs), the page an error, the nav link hides."""
    users_file.create("admin", "admin-pw", role="admin")
    users_file.create("alice", "alice-pw")
    alice = as_user(client, "alice", "alice-pw")

    for method, path in (("post", "/api/terminal/sessions"),
                         ("get", "/api/terminal/sessions/abc/output"),
                         ("post", "/api/terminal/sessions/abc/input"),
                         ("delete", "/api/terminal/sessions/abc")):
        assert getattr(alice, method)(path).status_code == 403, path
    # a valid body too — the gate beats any validation nitpicks
    res = alice.post("/api/terminal/sessions/abc/size", json={"cols": 80, "rows": 24})
    assert res.status_code == 403

    # the python UI: no nav link, the page and its runner refuse
    assert 'href="/terminal"' not in alice.get("/").text
    assert alice.get("/terminal").status_code == 403
    assert alice.post("/terminal", data={"command": "echo hi"}).status_code == 403


def test_multi_user_flag_off_beats_rights(client, users_file, sessions_file,
                                          fresh_logins):
    """With the flag off the feature does not exist — a regular user gets
    the same 404 as everyone else, never a 403 that reveals it's compiled
    in but withheld."""
    users_file.create("admin", "admin-pw", role="admin")
    users_file.create("alice", "alice-pw")
    alice = as_user(client, "alice", "alice-pw")

    assert alice.post("/api/terminal/sessions").status_code == 404
    assert alice.get("/terminal").status_code == 404


def test_multi_user_admin_keeps_the_terminal(client, terminal_on,
                                             users_file, sessions_file,
                                             fresh_logins):
    users_file.create("admin", "admin-pw", role="admin")
    users_file.create("alice", "alice-pw")
    admin = as_user(client, "admin", "admin-pw")

    # the full lifecycle over the API
    res = admin.post("/api/terminal/sessions")
    assert res.status_code == 200
    sid = res.json()["id"]
    assert admin.post(f"/api/terminal/sessions/{sid}/input",
                      content="echo admin-only\nexit\n").status_code == 200
    assert admin.delete(f"/api/terminal/sessions/{sid}").status_code == 200

    # and the python UI page + nav link
    assert 'href="/terminal"' in admin.get("/").text
    res = admin.post("/terminal", data={"command": "echo page-as-admin"})
    assert res.status_code == 200
    assert "page-as-admin" in res.text


# --- the one-shot command box (python UI) -----------------------------------

def test_run_command():
    result = asyncio.run(terminal_service.terminal.run_command("echo one-shot"))
    assert result["ok"] is True
    assert "one-shot" in result["output"]
    assert result["exit_code"] == 0

    # stderr is captured, nonzero exit reported
    result = asyncio.run(terminal_service.terminal.run_command("echo err >&2; exit 3"))
    assert result["ok"] is False and result["exit_code"] == 3
    assert "err" in result["output"]

    # a command that never finishes is killed at the timeout
    result = asyncio.run(terminal_service.terminal.run_command("sleep 10", timeout=0.5))
    assert result["ok"] is False
    assert "timed out" in result["error"]


def test_terminal_page_runs_commands(client, terminal_on):
    assert 'href="/terminal"' in client.get("/").text  # nav link appears
    page = client.get("/terminal")
    assert page.status_code == 200
    assert "SCRIPTENGINE_TERMINAL" in page.text

    res = client.post("/terminal", data={"command": "echo page-terminal-works"})
    assert res.status_code == 200
    assert "page-terminal-works" in res.text
    assert 'name="command"' in res.text  # the command rides back in the form

    # blank submissions are caught, not run
    res = client.post("/terminal", data={"command": "   "})
    assert "Type a command first" in res.text


def test_terminal_page_has_interactive_shell(client, terminal_on):
    """The python UI's terminal is progressive enhancement: the page ships
    an xterm mount (interactive shell) AND keeps the no-JS command box."""
    page = client.get("/terminal").text
    assert 'id="term-mount"' in page
    assert 'id="term-status"' in page
    assert 'id="term-restart"' in page
    assert 'id="term-fallback"' in page  # the command box stays as fallback
    assert 'name="command"' in page
    for asset in ("xterm.js", "addon-fit.js", "terminal.js", "xterm.css"):
        assert f"/terminal/assets/{asset}" in page

    # the vendored assets actually serve, as javascript/css
    for asset in ("xterm.js", "addon-fit.js", "terminal.js"):
        res = client.get(f"/terminal/assets/{asset}")
        assert res.status_code == 200
        assert res.headers["content-type"].startswith("text/javascript")
        assert len(res.content) > 1000
    res = client.get("/terminal/assets/xterm.css")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/css")

    # only the vendored set serves — nothing else, no traversal
    assert client.get("/terminal/assets/nope.js").status_code == 404
    assert client.get("/terminal/assets/..%2fconfig.py").status_code == 404


def test_terminal_assets_hidden_when_off(client):
    """Flag off = nothing terminal-related answers, assets included."""
    for asset in ("xterm.js", "addon-fit.js", "terminal.js", "xterm.css"):
        assert client.get(f"/terminal/assets/{asset}").status_code == 404
