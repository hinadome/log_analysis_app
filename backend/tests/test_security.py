"""Security hardening: the script authoring lock.

LOGSMITH_ALLOW_SCRIPT_EDITING=0 turns the server into "run the vetted
scripts only": every write path — the API editor endpoints and both UIs —
answers 403, while reads (list, source, export) and job submission keep
working. The flag is monkeypatched per module, exactly how test_auth.py
handles AUTH_TOKEN; the launcher's password auto-generation is shell-level
and verified by running run.sh, not by pytest.
"""
from __future__ import annotations

import pytest

import app.main as main_module
import app.pages as pages_module
import app.routers.scripts as scripts_router
from app.config import SCRIPTS_DIR

from test_api import submit, wait_status

LOG = "app.log"


@pytest.fixture()
def editing_locked(monkeypatch):
    for module in (scripts_router, pages_module, main_module):
        monkeypatch.setattr(module, "ALLOW_SCRIPT_EDITING", False)


def _json_body(script_id="locktest"):
    """A valid create payload — so a 403 proves the lock, not a 422."""
    return {
        "id": script_id,
        "extension": "py",
        "code": "print('nope')",
        "manifest": {
            "name": "Lock Test",
            "description": "must never be written",
            "command": "python3 {script} {log} {args}",
            "timeout": 30,
            "parameters": [],
        },
    }


def _form(script_id="locktest"):
    """The same script as the editor form posts it."""
    return {
        "original_id": "", "id": script_id, "extension": "py",
        "name": "Lock Test", "description": "must never be written",
        "command": "", "timeout": "30", "output_filename": "",
        "parameters": "[]", "code": "print('nope')",
    }


# --- health flag -----------------------------------------------------------

def test_health_reports_the_flag(client):
    assert client.get("/api/health").json()["script_editing"] is True


# --- API: writes rejected, reads and runs fine ------------------------------

def test_api_writes_are_403_when_locked(editing_locked, client):
    res = client.post("/api/scripts", json=_json_body())
    assert res.status_code == 403
    assert "disabled" in res.json()["detail"]
    assert not (SCRIPTS_DIR / "locktest.py").exists()   # nothing landed on disk

    res = client.put("/api/scripts/error_counter", json=_json_body("error_counter"))
    assert res.status_code == 403

    res = client.delete("/api/scripts/error_counter")
    assert res.status_code == 403
    assert (SCRIPTS_DIR / "error_counter.py").is_file()  # existing scripts untouched


def test_api_reads_and_jobs_still_work_when_locked(editing_locked, client):
    assert client.get("/api/scripts").status_code == 200
    assert client.get("/api/scripts/error_counter").status_code == 200       # source
    assert client.get("/api/scripts/error_counter/export").status_code == 200

    jobs = submit(client, [{"script_id": "error_counter", "params": {}}])
    wait_status(client, [jobs[0]["id"]], ("completed",))


# --- python UI: editor pages locked ------------------------------------------

def test_pages_reject_writes_when_locked(editing_locked, client):
    # editor pages bounce back to the list with an error banner
    res = client.get("/scripts/new", follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"].startswith("/scripts?err=")
    res = client.get("/scripts/error_counter/edit", follow_redirects=False)
    assert res.status_code == 303

    # direct posts get the 403 error page
    assert client.post("/scripts/save", data=_form()).status_code == 403
    assert client.post("/scripts/test-run",
                       data={**_form(), "log": LOG}).status_code == 403
    assert client.post("/scripts/error_counter/delete").status_code == 403
    assert not (SCRIPTS_DIR / "locktest.py").exists()

    # the list page hides the entry points and explains why, but keeps export
    body = client.get("/scripts").text
    assert "/scripts/new" not in body
    assert "/scripts/error_counter/edit" not in body
    assert "/scripts/error_counter/delete" not in body
    assert "editing disabled" in body
    assert "/api/scripts/error_counter/export" in body


# --- default: the lock is opt-in ----------------------------------------------

def test_editor_still_works_when_not_locked(client):
    assert client.get("/scripts/new").status_code == 200
    res = client.get("/scripts/error_counter/edit")
    assert res.status_code == 200
    assert 'action="/scripts/save"' in res.text
    body = client.get("/scripts").text
    assert "/scripts/new" in body
    assert "/scripts/error_counter/edit" in body
