"""Shared fixtures for the ScriptEngine test suite.

Environment variables are pointed at temporary directories BEFORE any
`app.*` import happens, so tests never touch the real `logs/`, `data/` or
auth settings. Scripts are the repository's own sample scripts (stable
fixtures); logs are generated into a session tmp dir.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
sys.path.insert(0, str(BACKEND))

# --- isolated environment (must be set before importing app.*) ---------
_TMP = Path(tempfile.mkdtemp(prefix="scriptengine-tests-"))
LOGS_TMP = _TMP / "logs"
DATA_TMP = _TMP / "data"
LOGS_TMP.mkdir(parents=True)
DATA_TMP.mkdir(parents=True)

os.environ["LOGS_DIR"] = str(LOGS_TMP)
os.environ["DATA_DIR"] = str(DATA_TMP)
os.environ["SCRIPTS_DIR"] = str(REPO / "scripts")
os.environ["SCRIPTENGINE_TOKEN"] = ""
os.environ["JOB_RETENTION_DAYS"] = "0"
# Test the python frontend: pages router mounted, no SPA catch-all. The API
# routes are identical either way, so the rest of the suite is unaffected.
os.environ["SCRIPTENGINE_UI"] = "python"
# The terminal ships ON by default now (admins see it without config), but
# the suite pins it off: every page/nav assertion elsewhere was written
# against a terminal-less UI, and test_terminal.py exercises both states
# explicitly anyway.
os.environ["SCRIPTENGINE_TERMINAL"] = "0"

SAMPLE_LOG = LOGS_TMP / "app.log"
SAMPLE_LOG.write_text(
    "2026-10-29 10:00:01 INFO  app started\n"
    "2026-10-29 10:00:05 ERROR db connection lost: SQLException timeout\n"
    "2026-10-29 10:00:06 WARN  retrying\n"
    "2026-10-29 10:00:07 ERROR db connection lost: SQLException deadlock\n"
    "2026-10-29 10:00:07 ERROR client 203.0.113.7 hit /api timeout\n"
    "2026-10-29 10:01:00 INFO  shutdown\n",
    encoding="utf-8",
)

# Common-format access log, for the access-log-oriented sample scripts.
SAMPLE_ACCESS_LOG = LOGS_TMP / "access.log"
SAMPLE_ACCESS_LOG.write_text(
    '10.0.1.5 - - [28/Oct/2026:14:00:00 +0000] "GET /health HTTP/1.1" 200 512\n'
    '10.0.2.4 - - [28/Oct/2026:14:00:01 +0000] "GET /static/app.js HTTP/1.1" 200 4096\n'
    '10.0.1.10 - - [28/Oct/2026:14:00:04 +0000] "POST /api/orders HTTP/1.1" 400 768\n'
    '203.0.113.7 - - [28/Oct/2026:14:00:05 +0000] "GET /admin HTTP/1.1" 404 256\n'
    '10.0.3.9 - - [28/Oct/2026:14:01:00 +0000] "GET /health HTTP/1.1" 503 128\n'
    '10.0.4.2 - - [28/Oct/2026:14:01:02 +0000] "GET /static/style.css HTTP/1.1" 200 8192\n',
    encoding="utf-8",
)


def wipe_jobs() -> None:
    """Remove every persisted job (module store rescans from disk)."""
    jobs_dir = DATA_TMP / "jobs"
    if jobs_dir.is_dir():
        for entry in jobs_dir.iterdir():
            shutil.rmtree(entry, ignore_errors=True)


@pytest.fixture()
def store(tmp_path: Path):
    from app.services.job_store import JobStore

    return JobStore(tmp_path)


@pytest.fixture()
def users_file(tmp_path, monkeypatch):
    """The user-store singleton, backed by an empty per-test file."""
    from app.services import user_store as user_module
    path = tmp_path / "users.json"
    monkeypatch.setattr(user_module.store, "_path", path)
    user_module.store._users, user_module.store._stamp = [], None
    yield user_module.store


@pytest.fixture()
def sessions_file(tmp_path, monkeypatch):
    from app.services import session_store as session_module
    path = tmp_path / "sessions.json"
    monkeypatch.setattr(session_module.store, "_path", path)
    session_module.store._sessions, session_module.store._stamp = {}, None
    yield session_module.store


@pytest.fixture()
def fresh_logins(monkeypatch):
    import app.auth as auth_module
    monkeypatch.setattr(auth_module, "_LOGIN_FAILURES", {})


@pytest.fixture()
def executor(store):
    from app.services.executor import Executor

    return Executor(store)


@pytest.fixture()
def client():
    """FastAPI TestClient with lifespan (startup reconciles, shutdown kills)."""
    from fastapi.testclient import TestClient

    from app.main import app
    from app.services import pipeline_store, schedule_store

    pipeline_store.PIPELINES_FILE.unlink(missing_ok=True)  # no leakage between tests
    schedule_store.SCHEDULES_FILE.unlink(missing_ok=True)
    wipe_jobs()
    with TestClient(app) as test_client:
        yield test_client
    pipeline_store.PIPELINES_FILE.unlink(missing_ok=True)
    schedule_store.SCHEDULES_FILE.unlink(missing_ok=True)
    # leaving the context manager runs executor.shutdown() — jobs created
    # by the test are stopped and persisted as cancelled/failed.


# --- helpers used by the executor-level tests ---------------------------

def make_script(script_id="s", command="cat {log}", timeout=30, output_filename=None):
    """A fabricated script — no file on disk needed, the command is real."""
    from app.models import ScriptInfo

    return ScriptInfo(
        id=script_id,
        name=script_id,
        description="",
        command=command,
        timeout=timeout,
        parameters=[],
        script_path="/bin/true",
        style="flat",
        output_filename=output_filename,
    )


def make_job(store, script, log_path, label=None):
    return store.create(
        script_id=script.id,
        script_name=script.name,
        log_path=log_path,
        log_name=Path(log_path).name,
        params={},
        label=label,
    )


def patch_script_lookup(monkeypatch, scripts):
    """Redirect executor's script lookup to a test's fake scripts.

    _advance_chain re-resolves the next step's script through
    discover_scripts() (the real scripts/ dir); chain unit tests use
    fabricated ScriptInfo objects that would not be found there.
    """
    import app.services.executor as executor_module

    by_id = {s.id: s for s in scripts}
    monkeypatch.setattr(executor_module, "_find_script",
                        lambda script_id: by_id.get(script_id))
