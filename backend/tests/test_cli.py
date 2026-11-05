"""The command-line client (cli/logsmith.py) against a live server.

The CLI is exercised in-process (main(argv) → exit code, capsys for the
output) against a real uvicorn server on a free port, started once per
module in a background thread. It shares the suite's isolated stores —
conftest points the env at temp dirs before any app import — so the
tests run scripts from the repo's scripts/ against the sample logs.

Every test gets a fresh config file (LOGSMITH_CONFIG), a wiped job
history and single-user mode; the multi-user lifecycle test flips the
server into accounts mode and restores single-user mode on exit.
"""
from __future__ import annotations

import json
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

from conftest import DATA_TMP, REPO, wipe_jobs

sys.path.insert(0, str(REPO / "cli"))
import logsmith  # noqa: E402


def reset_single_user() -> None:
    """Delete account/session state and clear the store caches — the
    module-scoped server starts (and the suite continues) in single-user
    mode no matter what an earlier test left behind."""
    from app.services import session_store, user_store

    (DATA_TMP / "users.json").unlink(missing_ok=True)
    (DATA_TMP / "sessions.json").unlink(missing_ok=True)
    user_store.store._users, user_store.store._stamp = [], None
    session_store.store._sessions, session_store.store._stamp = {}, None


@pytest.fixture(scope="module")
def server_url():
    reset_single_user()
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    import uvicorn

    from app.main import app

    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started, "the CLI test server did not come up"
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)
    reset_single_user()


@pytest.fixture()
def scripts_sandbox(tmp_path, monkeypatch):
    """A throwaway copy of the repo's scripts/ the live server is pointed
    at — tests may create, share and delete scripts without touching the
    repository (scanner and router hold the only request-time bindings)."""
    import shutil

    import app.routers.scripts as scripts_router
    import app.services.scanner as scanner_module

    target = tmp_path / "scripts"
    shutil.copytree(REPO / "scripts", target,
                    ignore=shutil.ignore_patterns("users"))
    monkeypatch.setattr(scanner_module, "SCRIPTS_DIR", target)
    monkeypatch.setattr(scripts_router, "SCRIPTS_DIR", target)
    return target


@pytest.fixture()
def cli(server_url, tmp_path, monkeypatch, scripts_sandbox):
    """A main(argv) runner on a fresh config, wiped stores, single-user."""
    from app.services import pipeline_store, schedule_store

    reset_single_user()
    wipe_jobs()
    pipeline_store.PIPELINES_FILE.unlink(missing_ok=True)
    schedule_store.SCHEDULES_FILE.unlink(missing_ok=True)
    monkeypatch.setenv("LOGSMITH_CONFIG", str(tmp_path / "cli-config.json"))
    monkeypatch.setenv("LOGSMITH_URL", server_url)

    def run(*argv) -> int:
        return logsmith.main([str(a) for a in argv])

    return run


def latest_job_ids(cli, capsys, count=1) -> list[str]:
    cli("jobs", "list", "--json")
    jobs = json.loads(capsys.readouterr().out)["jobs"]
    return [j["id"] for j in jobs[:count]]


# --- step-spec parsing (pure client logic) ---------------------------------

def test_parse_step_decodes_json_values():
    sid, params = logsmith.parse_step(
        "grep_search:pattern=ERROR,context=2,ignore_case=true")
    assert sid == "grep_search"
    assert params == {"pattern": "ERROR", "context": 2, "ignore_case": True}


def test_parse_step_lists_and_quoting():
    _, params = logsmith.parse_step('t:hosts=["a","b"],note="x,y"')
    assert params["hosts"] == ["a", "b"]
    assert params["note"] == "x,y"          # the comma survived the split
    _, params = logsmith.parse_step("t:hosts=a,hosts=b")
    assert params["hosts"] == ["a", "b"]    # repetition collects a list
    _, params = logsmith.parse_step("plain")
    assert params == {}


def test_parse_step_rejects_garbage():
    with pytest.raises(ValueError):
        logsmith.parse_step("s:justakey")
    with pytest.raises(ValueError):
        logsmith.parse_step(":x=1")


# --- unauthenticated, single-user -------------------------------------------

def test_status_anonymous(cli, capsys):
    assert cli("status") == 0
    out = capsys.readouterr().out
    assert "single-user" in out
    assert "none (anonymous)" in out


def test_logs_list_and_preview(cli, capsys):
    assert cli("logs", "list") == 0
    out = capsys.readouterr().out
    assert "app.log" in out and "access.log" in out

    assert cli("logs", "preview", "app.log", "--lines", "2") == 0
    out = capsys.readouterr().out
    assert "INFO  app started" in out
    assert "shutdown" not in out            # only the first lines

    assert cli("logs", "preview", "app.log", "--lines", "2", "--tail") == 0
    out = capsys.readouterr().out
    assert "shutdown" in out
    assert "app started" not in out


def test_logs_upload_and_duplicate_rejected(cli, capsys, tmp_path):
    f = tmp_path / "cli-upload.log"
    f.write_text("2026-11-05 ERROR from the cli\n")
    assert cli("logs", "upload", str(f)) == 0
    assert "cli-upload.log" in capsys.readouterr().out

    assert cli("logs", "upload", str(f)) == 2          # 409
    assert "already exists" in capsys.readouterr().err


# --- scripts and the run loop ----------------------------------------------

def test_scripts_list_and_show(cli, capsys):
    assert cli("scripts", "list") == 0
    out = capsys.readouterr().out
    assert "grep_search" in out and "error_counter" in out

    assert cli("scripts", "show", "grep_search") == 0
    code = capsys.readouterr().out
    assert code.startswith("#!/usr/bin/env bash")
    assert "--pattern" in code


def test_run_streams_stdout_and_exits_zero(cli, capsys):
    assert cli("run", "error_counter:level=ERROR", "--log", "app.log",
               "--interval", "0.05") == 0
    out = capsys.readouterr().out
    assert "level=ERROR" in out


def test_run_failed_job_exits_one(cli, capsys):
    # an invalid regex makes grep (and so the script) exit 2
    assert cli("run", "grep_search:pattern=[", "--log", "app.log",
               "--interval", "0.05") == 1
    err = capsys.readouterr().err
    assert "failed" in err


def test_run_chain_streams_every_step(cli, capsys):
    assert cli("run", "--mode", "chain",
               "grep_search:pattern=ERROR", "error_counter:level=ERROR",
               "--log", "app.log", "--interval", "0.05") == 0
    captured = capsys.readouterr()
    assert "[Regex Search]" in captured.out
    assert "[Error Counter]" in captured.out
    assert "2 jobs: 2 completed" in captured.err


def test_run_no_wait_and_job_history(cli, capsys):
    assert cli("run", "error_counter", "--log", "app.log", "--no-wait") == 0
    out = capsys.readouterr().out
    assert "error_counter" in out
    assert "queued" in out

    time.sleep(1.0)                       # let the quick job finish
    assert cli("jobs", "list") == 0
    out = capsys.readouterr().out
    assert "Error Counter" in out and "completed" in out


def test_jobs_show_output_files_and_get(cli, capsys, tmp_path):
    assert cli("run", "access_report", "--log", "access.log",
               "--interval", "0.05") == 0
    capsys.readouterr()
    (job_id,) = latest_job_ids(cli, capsys)

    assert cli("jobs", "show", job_id) == 0
    out = capsys.readouterr().out
    assert "access_report" in out and "completed" in out

    assert cli("jobs", "output", job_id, "--stderr") == 0
    assert capsys.readouterr().out == ""      # a clean run has no stderr

    assert cli("jobs", "files", job_id) == 0
    assert "report.md" in capsys.readouterr().out

    target = tmp_path / "fetched.md"
    assert cli("jobs", "get", job_id, "report.md", "--out", str(target)) == 0
    assert "Access log report" in target.read_text()


def test_jobs_rerun_from_step_reuses_output(cli, capsys):
    assert cli("run", "--mode", "chain",
               "grep_search:pattern=ERROR", "error_counter:level=ERROR",
               "--log", "app.log", "--interval", "0.05") == 0
    capsys.readouterr()
    cli("jobs", "list", "--json")
    jobs = json.loads(capsys.readouterr().out)["jobs"]
    first = next(j["id"] for j in jobs if j["chain_index"] == 0)

    # rerun only step 2, seeded with step 1's persisted output: it reads
    # grep's output (3 ERROR lines + its "-- N matching line(s) for
    # pattern 'ERROR' --" summary), so ERROR is 4 of 5 lines — 80%, not
    # the 50% a fresh run on app.log would report
    assert cli("jobs", "rerun", first, "--from-step", "2",
               "--interval", "0.05") == 0
    out = capsys.readouterr().out
    assert "80.0% of file" in out


def test_jobs_cancel_running(cli, capsys):
    # slow_scan defaults to 15s — plenty of time to cancel it
    assert cli("run", "slow_scan", "--log", "app.log", "--no-wait",
               "--json") == 0
    jobs = json.loads(capsys.readouterr().out)
    job_id = jobs[0]["id"]

    assert cli("jobs", "cancel", job_id) == 0
    time.sleep(0.5)                       # let the cancellation land on disk
    assert cli("jobs", "show", job_id) == 0
    assert "cancelled" in capsys.readouterr().out


def test_jobs_delete(cli, capsys):
    assert cli("run", "error_counter", "--log", "app.log",
               "--interval", "0.05") == 0
    capsys.readouterr()
    (job_id,) = latest_job_ids(cli, capsys)

    assert cli("jobs", "delete", job_id) == 0
    assert cli("jobs", "show", job_id) == 2
    assert "not" in capsys.readouterr().err.lower()


def test_jobs_prune(cli, capsys):
    assert cli("run", "error_counter", "--log", "app.log",
               "--interval", "0.05") == 0
    capsys.readouterr()
    assert cli("jobs", "prune", "--days", "30") == 0
    out = capsys.readouterr().out
    assert "removed 0 job(s)" in out       # nothing is 30 days old yet


# --- pipelines and schedules -----------------------------------------------

def test_pipelines_save_run_delete(cli, capsys):
    assert cli("pipelines", "save", "errors", "grep_search:pattern=ERROR",
              "error_counter:level=ERROR") == 0
    assert "saved pipeline 'errors'" in capsys.readouterr().out

    assert cli("pipelines", "list") == 0
    out = capsys.readouterr().out
    assert "errors" in out
    assert "grep_search → error_counter" in out

    assert cli("pipelines", "run", "errors", "--log", "app.log",
               "--interval", "0.05") == 0
    captured = capsys.readouterr()
    assert "Regex Search" in captured.out and "Error Counter" in captured.out
    assert "2 jobs: 2 completed" in captured.err

    assert cli("pipelines", "delete", "errors") == 0
    assert cli("pipelines", "run", "errors", "--log", "app.log") == 2
    assert "no pipeline named 'errors'" in capsys.readouterr().err


def test_schedules_save_toggle_run_delete(cli, capsys):
    assert cli("schedules", "save", "every-hour", "--cron", "0 * * * *",
               "--script", "error_counter:level=ERROR",
               "--log", "app.log") == 0
    capsys.readouterr()

    assert cli("schedules", "list") == 0
    out = capsys.readouterr().out
    assert "every-hour" in out and "error_counter" in out and "yes" in out

    assert cli("schedules", "toggle", "every-hour") == 0
    assert "disabled" in capsys.readouterr().out

    assert cli("schedules", "run", "every-hour") == 0   # fires regardless
    out = capsys.readouterr().out
    assert "ok: 1 job(s) submitted" in out

    assert cli("schedules", "delete", "every-hour") == 0
    capsys.readouterr()
    assert cli("schedules", "list") == 0
    assert "every-hour" not in capsys.readouterr().out


def test_schedules_save_requires_pipeline_or_script(cli, capsys):
    assert cli("schedules", "save", "bad", "--cron", "0 * * * *") == 2
    assert "either" in capsys.readouterr().err
    assert cli("schedules", "save", "bad", "--cron", "0 * * * *",
               "--script", "error_counter",
               "--pipeline", "nope") == 2


# --- script authoring through the CLI --------------------------------------

def test_scripts_create_update_delete(cli, capsys, tmp_path):
    code = tmp_path / "hello.py"
    code.write_text("import sys\nprint('hello from', sys.argv[1])\n")
    assert cli("scripts", "create", "hello", "--file", str(code)) == 0
    assert "created hello" in capsys.readouterr().out

    assert cli("run", "hello", "--log", "app.log", "--interval", "0.05") == 0
    assert "hello from" in capsys.readouterr().out

    code.write_text("import sys\nprint('updated code ran')\n")
    assert cli("scripts", "update", "hello", "--file", str(code)) == 0
    capsys.readouterr()
    assert cli("run", "hello", "--log", "app.log", "--interval", "0.05") == 0
    assert "updated code ran" in capsys.readouterr().out

    assert cli("scripts", "delete", "hello") == 0
    assert cli("run", "hello", "--log", "app.log") == 2
    assert "Unknown script" in capsys.readouterr().err


def test_scripts_export_import_roundtrip(cli, capsys, tmp_path):
    code = tmp_path / "tool.py"
    code.write_text("print('the tool ran')\n")
    assert cli("scripts", "create", "cli_tool", "--file", str(code)) == 0
    capsys.readouterr()

    archive = tmp_path / "cli_tool.tar.gz"
    assert cli("scripts", "export", "cli_tool", "--out", str(archive)) == 0
    assert "wrote" in capsys.readouterr().out
    assert archive.is_file()

    assert cli("scripts", "delete", "cli_tool") == 0
    assert cli("scripts", "import", str(archive)) == 0
    assert "cli_tool" in capsys.readouterr().out

    assert cli("run", "cli_tool", "--log", "app.log",
               "--interval", "0.05") == 0
    assert "the tool ran" in capsys.readouterr().out


# --- the multi-user lifecycle (defined last: it flips server mode) ---------

def test_multi_user_lifecycle(server_url, tmp_path, monkeypatch, capsys,
                              scripts_sandbox):
    """Bootstrap → accounts → private scripts → sharing → disabling."""
    try:
        configs = {"root": tmp_path / "root.json",
                   "alice": tmp_path / "alice.json",
                   "bob": tmp_path / "bob.json"}
        monkeypatch.setenv("LOGSMITH_URL", server_url)

        def run(user, *argv) -> int:
            monkeypatch.setenv("LOGSMITH_CONFIG", str(configs[user]))
            return logsmith.main([str(a) for a in argv])

        # the server starts single-user; the first account must be an admin
        assert run("root", "status") == 0
        assert "single-user" in capsys.readouterr().out
        assert run("root", "users", "create", "root",
                   "--password", "root-pw") == 2
        assert "must be an admin" in capsys.readouterr().err

        # bootstrap: creating it adopts the minted session
        assert run("root", "users", "create", "root", "--password", "root-pw",
                   "--role", "admin") == 0
        assert "adopted the bootstrap session" in capsys.readouterr().out
        assert run("root", "status") == 0
        out = capsys.readouterr().out
        assert "multi-user" in out and "logged in as root (admin)" in out

        assert run("root", "users", "create", "alice",
                   "--password", "alice-pw") == 0
        assert run("root", "users", "create", "bob",
                   "--password", "bob-pw") == 0
        assert run("root", "users", "list") == 0
        out = capsys.readouterr().out
        assert "root" in out and "alice" in out and "bob" in out

        # alice signs in and writes a private script
        assert run("alice", "login", "alice", "--password", "alice-pw") == 0
        assert "Logged in as alice (user)" in capsys.readouterr().out
        code = tmp_path / "tool.py"
        code.write_text("print('alice was here')\n")
        assert run("alice", "scripts", "create", "alices_tool",
                   "--file", str(code)) == 0
        assert run("alice", "scripts", "list") == 0
        row = next(line for line in capsys.readouterr().out.splitlines()
                   if "alices_tool" in line)
        assert "alice" in row                       # the OWNER column

        # bob: neither sees nor can run it (404, never a confirmation)
        assert run("bob", "login", "bob", "--password", "bob-pw") == 0
        capsys.readouterr()
        assert run("bob", "scripts", "list") == 0
        assert "alices_tool" not in capsys.readouterr().out
        assert run("bob", "run", "alices_tool", "--log", "app.log") == 2
        assert "Unknown script" in capsys.readouterr().err

        # sharing publishes it to everyone
        assert run("alice", "scripts", "share", "alices_tool") == 0
        capsys.readouterr()
        assert run("bob", "scripts", "list") == 0
        assert "alices_tool" in capsys.readouterr().out
        assert run("bob", "run", "alices_tool", "--log", "app.log",
                   "--interval", "0.05") == 0
        assert "alice was here" in capsys.readouterr().out

        # unauthenticated in an accounts world → 401 with the login hint
        assert run("alice", "logout") == 0
        assert run("alice", "logs", "list") == 2
        assert "run `logsmith login`" in capsys.readouterr().err

        # disabling an account kills its live session and blocks logins
        assert run("alice", "login", "alice", "--password", "alice-pw") == 0
        capsys.readouterr()
        assert run("root", "users", "disable", "alice") == 0
        capsys.readouterr()
        assert run("alice", "logs", "list") == 2
        assert "Not authenticated" in capsys.readouterr().err
        assert run("alice", "login", "alice", "--password", "alice-pw") == 2
        assert "Incorrect username or password" in capsys.readouterr().err
    finally:
        reset_single_user()
