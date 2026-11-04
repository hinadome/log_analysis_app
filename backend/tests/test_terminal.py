"""The remote terminal (LOGSMITH_TERMINAL).

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

def test_disabled_by_default(client):
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


def test_lockdown_forces_terminal_off():
    """LOGSMITH_ALLOW_SCRIPT_EDITING=0 means "run the vetted scripts only" —
    a shell is strictly more powerful, so it stays off even if asked for."""
    import app.config as config_module
    saved = {k: os.environ.get(k) for k in
             ("LOGSMITH_TERMINAL", "LOGSMITH_ALLOW_SCRIPT_EDITING")}
    try:
        os.environ["LOGSMITH_TERMINAL"] = "1"
        os.environ["LOGSMITH_ALLOW_SCRIPT_EDITING"] = "0"
        assert importlib.reload(config_module).TERMINAL_ENABLED is False
        os.environ["LOGSMITH_ALLOW_SCRIPT_EDITING"] = "1"
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
    assert "LOGSMITH_TERMINAL" in page.text

    res = client.post("/terminal", data={"command": "echo page-terminal-works"})
    assert res.status_code == 200
    assert "page-terminal-works" in res.text
    assert 'name="command"' in res.text  # the command rides back in the form

    # blank submissions are caught, not run
    res = client.post("/terminal", data={"command": "   "})
    assert "Type a command first" in res.text
