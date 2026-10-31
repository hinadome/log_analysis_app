"""Tests for the server-rendered frontend (the LOGSMITH_UI=python pages).

The conftest pins LOGSMITH_UI=python, so the app under test serves the
Jinja2 pages instead of the React SPA. These tests drive the full page
flow — home, run configuration, submission, job monitoring, compare,
upload and the script editor — the way a browser would (forms, redirects,
cookie login), while the API tests keep covering the /api routes both
frontends share.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

import app.auth as auth_module
from app.config import SCRIPTS_DIR

LOG = "app.log"
ACCESS = "access.log"
TOKEN = "page-test-token"


def wait_until(predicate, timeout=30.0, interval=0.25, message="condition not met"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError(message)


def wait_done(client, job_id, timeout=30.0):
    wait_until(
        lambda: client.get(f"/api/jobs/{job_id}").json()["status"]
        in ("completed", "failed", "cancelled", "skipped"),
        timeout=timeout,
        message=f"job {job_id} never finished",
    )
    return client.get(f"/api/jobs/{job_id}").json()


# --- home ----------------------------------------------------------------

def test_home_lists_logs_scripts_and_upload_form(client):
    res = client.get("/")
    assert res.status_code == 200
    body = res.text
    assert "Run scripts on logs" in body
    assert LOG in body and ACCESS in body          # both sample logs
    assert "Regex Search" in body                   # grep_search
    assert 'action="/upload"' in body               # upload form
    assert 'action="/run"' in body                 # main GET form
    assert 'name="logs"' in body and 'name="scripts"' in body


def test_run_page_renders_param_forms(client):
    res = client.get("/run", params={"log": LOG,
                                     "scripts": ["grep_search", "error_counter"]})
    assert res.status_code == 200
    body = res.text
    # the required regex field, a boolean (hidden+checkbox), an integer input
    assert 'name="p::grep_search::pattern"' in body
    assert 'name="p::grep_search::ignore_case"' in body
    assert 'name="p::grep_search::context"' in body
    # an enum select and its choices
    assert "p::error_counter::level" in body and "<select" in body
    assert "FATAL" in body
    # script checkboxes reflect the selection (deep link works)
    assert 'value="grep_search" checked' in body
    assert 'value="error_counter" checked' in body
    assert 'value="slow_scan"' in body and "checked" not in body.split('value="slow_scan"')[1].split(">")[0]


def test_run_page_flags_missing_log(client):
    res = client.get("/run", params={"log": "ghost.log"})
    assert res.status_code == 200
    assert "no longer available" in res.text
    assert "No logs selected" in res.text


def test_run_page_accepts_typed_absolute_path(client, tmp_path):
    # the home page's "use a file by path" box: any existing file on the
    # machine, not just what the logs listing knows
    outside = tmp_path / "system.log"
    outside.write_text("2026-10-30 ERROR typed absolute path\n", encoding="utf-8")
    res = client.get("/run", params={"log": str(outside)})
    assert res.status_code == 200
    assert "no longer available" not in res.text
    assert "No logs selected" not in res.text
    # carried through as a chip and as the form's hidden log value
    assert str(outside) in res.text


def test_run_page_flags_missing_typed_path(client):
    res = client.get("/run", params={"log": "/no/such/file.log"})
    assert res.status_code == 200
    assert "no longer available" in res.text
    assert "No logs selected" in res.text


# --- submission -----------------------------------------------------------

def test_submit_single_job_redirects_to_detail(client):
    res = client.post("/submit", data={
        "logs": LOG, "scripts": "error_counter", "mode": "parallel", "label": "page-flow",
        "p::error_counter::level": "WARN", "p::error_counter::json": "true",
    }, follow_redirects=False)
    assert res.status_code == 303
    job_id = res.headers["location"].split("/")[-1]
    assert res.headers["location"] == f"/jobs/{job_id}"

    job = wait_done(client, job_id)
    assert job["status"] == "completed"
    assert job["label"] == "page-flow"
    # form values were typed: enum string kept, boolean checkbox -> True
    assert job["params"]["level"] == "WARN"
    assert job["params"]["json"] is True

    page = client.get(f"/jobs/{job_id}")
    assert page.status_code == 200
    assert "Error Counter" in page.text
    # the input tab shows what the script actually received
    input_page = client.get(f"/jobs/{job_id}", params={"tab": "input"})
    assert "app started" in input_page.text
    # details tab shows the command
    details_page = client.get(f"/jobs/{job_id}", params={"tab": "details"})
    assert "error_counter.py" in details_page.text


def test_submit_typed_absolute_path_runs_on_that_file(client, tmp_path):
    """The run page carries the typed path in its hidden inputs; the API
    resolver accepts it, so the form flow needs no special casing."""
    outside = tmp_path / "system.log"
    outside.write_text("2026-10-30 ERROR typed absolute path\n", encoding="utf-8")
    res = client.post("/submit", data={
        "logs": str(outside), "scripts": "grep_search", "mode": "parallel",
        "p::grep_search::pattern": "typed absolute path",
    }, follow_redirects=False)
    assert res.status_code == 303
    job_id = res.headers["location"].split("/")[-1]

    job = wait_done(client, job_id)
    assert job["status"] == "completed"
    assert Path(job["log_path"]) == outside.resolve()
    assert job["log_name"] == str(outside.resolve())
    # the script really read the file outside the logs directory
    assert "typed absolute path" in job["stdout_tail"]


def test_submit_fanout_creates_one_job_per_log(client):
    before = {j["id"] for j in client.get("/api/jobs").json()["jobs"]}
    res = client.post("/submit", data={
        "logs": [LOG, ACCESS], "scripts": "error_counter", "mode": "parallel",
    }, follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"].startswith("/jobs?msg=")

    created = {j["id"] for j in client.get("/api/jobs").json()["jobs"]} - before
    assert len(created) == 2
    for job_id in created:
        assert wait_done(client, job_id)["status"] == "completed"

    page = client.get("/jobs")
    assert page.status_code == 200
    for job_id in created:
        assert f"/jobs/{job_id}" in page.text


def test_submit_chain_runs_steps_in_sequence(client):
    res = client.post("/submit", data={
        "logs": LOG, "scripts": ["grep_search", "error_counter"],
        "mode": "chain", "p::grep_search::pattern": "ERROR",
    }, follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"].startswith("/jobs?msg=")

    jobs = client.get("/api/jobs").json()["jobs"]
    ids = [j["id"] for j in jobs if j["chain_id"]]
    assert len(ids) == 2
    for job_id in ids:
        wait_done(client, job_id)
    # fresh records: input_source is set once a step starts consuming
    steps = sorted((client.get(f"/api/jobs/{j}").json() for j in ids),
                   key=lambda j: j["chain_index"])
    assert steps[0]["script_id"] == "grep_search"
    assert steps[1]["script_id"] == "error_counter"
    # step 2 consumed step 1's stdout (grep declares no output artifact)
    assert steps[1]["input_source"] == "prev_stdout"

    page = client.get(f"/jobs/{steps[0]['id']}")
    assert "Chain" in page.text and "step 1 of 2" in page.text


def test_submit_unknown_script_renders_error_page(client):
    res = client.post("/submit", data={"logs": LOG, "scripts": "ghost"},
                      follow_redirects=False)
    assert res.status_code == 404
    assert "Unknown script: ghost" in res.text
    assert 'href="/run?' in res.text  # back link to the configuration


def test_submit_boolean_false_is_explicit(client):
    # only the hidden "false" arrives (checkbox unchecked) -> typed to False
    res = client.post("/submit", data={
        "logs": LOG, "scripts": "grep_search", "mode": "parallel",
        "p::grep_search::pattern": "WARN",
        "p::grep_search::ignore_case": "false",
    }, follow_redirects=False)
    assert res.status_code == 303
    job_id = res.headers["location"].split("/")[-1]
    job = wait_done(client, job_id)
    assert job["params"]["ignore_case"] is False


# --- jobs list -------------------------------------------------------------

def test_jobs_page_search_filter_and_sort(client):
    def submit(script, label, params=None):
        data = {"logs": LOG, "scripts": script, "mode": "parallel", "label": label}
        data.update({f"p::{script}::{k}": v for k, v in (params or {}).items()})
        res = client.post("/submit", data=data, follow_redirects=False)
        return res.headers["location"].split("/")[-1]

    a = submit("error_counter", "alpha")
    b = submit("grep_search", "beta", {"pattern": "ERROR"})
    wait_done(client, a)
    wait_done(client, b)

    # search narrows to one job
    res = client.get("/jobs", params={"q": "alpha"})
    assert a in res.text and b not in res.text
    res = client.get("/jobs", params={"q": "nothing-matches-this"})
    assert "No jobs match" in res.text

    # status filter: everything finished completed
    res = client.get("/jobs", params={"status": "completed"})
    assert a in res.text and b in res.text
    res = client.get("/jobs", params={"status": "failed"})
    assert "No jobs match" in res.text

    # server-side sorting by script name, ascending
    res = client.get("/jobs", params={"sort": "script", "dir": "asc"})
    assert res.text.index("Error Counter") < res.text.index("Regex Search")
    # and descending flips it
    res = client.get("/jobs", params={"sort": "script", "dir": "desc"})
    assert res.text.index("Regex Search") < res.text.index("Error Counter")


def test_cancel_and_delete_from_jobs_page(client):
    res = client.post("/submit", data={
        "logs": LOG, "scripts": "slow_scan", "mode": "parallel",
        "p::slow_scan::seconds": "30",
    }, follow_redirects=False)
    job_id = res.headers["location"].split("/")[-1]
    wait_until(lambda: client.get(f"/api/jobs/{job_id}").json()["status"] == "running",
               message="slow_scan never started")

    res = client.post(f"/jobs/{job_id}/cancel", follow_redirects=False)
    assert res.status_code == 303
    assert wait_done(client, job_id)["status"] == "cancelled"

    res = client.post(f"/jobs/{job_id}/delete", follow_redirects=False)
    assert res.status_code == 303
    assert client.get(f"/api/jobs/{job_id}").status_code == 404


def test_clear_finished_from_jobs_page(client):
    def submit(label):
        res = client.post("/submit", data={
            "logs": LOG, "scripts": "error_counter", "mode": "parallel",
            "label": label, "p::error_counter::level": "ERROR",
        }, follow_redirects=False)
        job_id = res.headers["location"].split("/")[-1]
        wait_done(client, job_id)
        return job_id

    a = submit("keep-1")
    b = submit("keep-2")
    res = client.post("/jobs/clear-finished", follow_redirects=False)
    assert res.status_code == 303
    assert client.get(f"/api/jobs/{a}").status_code == 404
    assert client.get(f"/api/jobs/{b}").status_code == 404


# --- compare ---------------------------------------------------------------

def test_compare_page_diffs_two_runs(client):
    def submit(level):
        res = client.post("/submit", data={
            "logs": LOG, "scripts": "error_counter", "mode": "parallel",
            "p::error_counter::level": level,
        }, follow_redirects=False)
        job_id = res.headers["location"].split("/")[-1]
        return wait_done(client, job_id)

    first = submit("ERROR")
    second = submit("WARN")

    # auto-picks the sibling run of the same script
    res = client.get(f"/jobs/{second['id']}/compare")
    assert res.status_code == 200
    assert first["id"] in res.text
    assert "d-same" in res.text

    # explicit ?other= selects that job
    res = client.get(f"/jobs/{second['id']}/compare",
                     params={"other": first["id"]})
    assert res.status_code == 200
    assert "d-add" in res.text or "d-del" in res.text or "d-same" in res.text


# --- upload ----------------------------------------------------------------

def test_upload_page_flow(client):
    name = f"pageui-upload-{os.getpid()}.log"
    try:
        res = client.post("/upload",
                          files={"file": (name, b"2026-01-01 ERROR upload test\n", "text/plain")},
                          follow_redirects=False)
        assert res.status_code == 303
        assert "Uploaded" in res.headers["location"]
        # the log now appears on the home page and via the API
        assert name in client.get("/").text
        assert any(l["name"] == name for l in client.get("/api/logs").json()["logs"])
    finally:
        from app.config import LOGS_DIR
        (Path(LOGS_DIR) / name).unlink(missing_ok=True)


def test_upload_page_rejects_duplicate(client):
    res = client.post("/upload",
                      files={"file": (LOG, b"2026-01-01 ERROR dup\n", "text/plain")},
                      follow_redirects=False)
    assert res.status_code == 303
    assert "already+exists" in res.headers["location"]


# --- log preview -------------------------------------------------------------

def test_log_preview_page(client):
    res = client.get("/logs/preview", params={"path": LOG})
    assert res.status_code == 200
    assert "app started" in res.text
    assert 'href="/logs/preview?path=app.log&amp;' in res.text  # head/tail toggles


# --- scripts pages -----------------------------------------------------------

@pytest.fixture()
def unique_script_id():
    sid = f"pageui{os.getpid()}"
    yield sid
    for suffix in (".py", ".sh", ".json"):
        (SCRIPTS_DIR / f"{sid}{suffix}").unlink(missing_ok=True)


def test_scripts_pages_render(client):
    res = client.get("/scripts")
    assert res.status_code == 200
    assert "Regex Search" in res.text
    assert 'href="/scripts/grep_search/edit"' in res.text
    assert 'href="/scripts/new"' in res.text

    res = client.get("/scripts/grep_search/edit")
    assert res.status_code == 200
    assert 'name="code"' in res.text
    assert "Regex Search" in res.text  # manifest name prefilled

    res = client.get("/scripts/new")
    assert res.status_code == 200
    assert "New script" in res.text


def test_script_editor_save_test_run_and_delete(client, unique_script_id):
    sid = unique_script_id
    form = {
        "original_id": "", "id": sid, "extension": "py",
        "name": "Page UI Test", "description": "made by the page tests",
        "command": "", "timeout": "30", "output_filename": "",
        "parameters": '[{"name": "who", "flag": "--who", "type": "string", "default": "", "required": false}]',
        "code": "#!/usr/bin/env python3\nimport sys\nprint('hello from page ui')\n",
    }

    # save -> redirect to the edit page of the saved script
    res = client.post("/scripts/save", data=form, follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"].startswith(f"/scripts/{sid}/edit")
    assert (SCRIPTS_DIR / f"{sid}.py").is_file()
    assert (SCRIPTS_DIR / f"{sid}.json").is_file()

    res = client.get(f"/scripts/{sid}/edit")
    assert "Page UI Test" in res.text

    # test-run saves silently and submits one job on the chosen log
    res = client.post("/scripts/test-run", data={**form, "original_id": sid, "log": LOG},
                      follow_redirects=False)
    assert res.status_code == 303
    job_id = res.headers["location"].split("/")[-1]
    job = wait_done(client, job_id)
    assert job["status"] == "completed"
    assert job["script_id"] == sid

    # delete removes it from disk and the editor 404s afterwards
    res = client.post(f"/scripts/{sid}/delete", follow_redirects=False)
    assert res.status_code == 303
    assert not (SCRIPTS_DIR / f"{sid}.py").exists()
    assert client.get(f"/scripts/{sid}/edit").status_code == 404


def test_script_editor_error_keeps_form_values(client):
    res = client.post("/scripts/save", data={
        "original_id": "", "id": "bad id with spaces", "extension": "py",
        "name": "x", "description": "", "command": "", "timeout": "30",
        "output_filename": "", "parameters": "[]", "code": "print(1)",
    }, follow_redirects=False)
    assert res.status_code == 422
    # the form is re-rendered with the entered values instead of losing them
    assert "bad id with spaces" in res.text
    assert "print(1)" in res.text


def test_script_editor_rejects_bad_params_json(client):
    res = client.post("/scripts/save", data={
        "original_id": "", "id": "whatever", "extension": "py",
        "name": "x", "description": "", "command": "", "timeout": "30",
        "output_filename": "", "parameters": "{not json", "code": "print(1)",
    }, follow_redirects=False)
    assert res.status_code == 422
    assert "JSON array" in res.text


# --- auth: login page, cookie, logout ----------------------------------------

@pytest.fixture()
def auth_on(monkeypatch):
    monkeypatch.setattr(auth_module, "AUTH_TOKEN", TOKEN)


def test_pages_require_login(auth_on, client):
    res = client.get("/", follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"] == "/login?next=%2F"

    res = client.get("/jobs", follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"] == "/login?next=%2Fjobs"

    # the login page renders and mentions nothing secret
    res = client.get("/login")
    assert res.status_code == 200
    assert "requires a password" in res.text


def test_login_flow_and_logout(auth_on, client):
    # wrong password -> 401 rendered on the login page
    res = client.post("/login", data={"password": "wrong", "next": "/"},
                      follow_redirects=False)
    assert res.status_code == 401
    assert "Incorrect password" in res.text

    # correct password -> redirect with the session cookie
    res = client.post("/login", data={"password": TOKEN, "next": "/jobs"},
                      follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"] == "/jobs"
    assert auth_module.COOKIE_NAME in res.cookies

    # pages open with the cookie, api too
    assert client.get("/").status_code == 200
    assert client.get("/api/jobs").status_code == 200

    # logout clears the cookie and pages guard again
    res = client.post("/logout", follow_redirects=False)
    assert res.status_code == 303
    assert client.get("/", follow_redirects=False).status_code == 303


def test_api_bearer_still_works_for_pages(auth_on, client):
    """Automation headers work on page routes too (they share is_authenticated)."""
    res = client.get("/", headers={"Authorization": f"Bearer {TOKEN}"},
                     follow_redirects=False)
    assert res.status_code == 200


# --- misc ---------------------------------------------------------------------

def test_output_table_sorting_and_csv(client, unique_script_id):
    sid = unique_script_id
    form = {
        "original_id": "", "id": sid, "extension": "py",
        "name": "JSON Table Test", "description": "", "command": "",
        "timeout": "30", "output_filename": "", "parameters": "[]",
        "code": ("#!/usr/bin/env python3\n"
                 "import json\n"
                 "print(json.dumps([{\"host\": \"b.example\", \"count\": 2},\n"
                 "                  {\"host\": \"a.example\", \"count\": 30},\n"
                 "                  {\"host\": \"c.example\", \"count\": 1}]))\n"),
    }
    res = client.post("/scripts/test-run", data={**form, "log": LOG},
                      follow_redirects=False)
    assert res.status_code == 303
    job_id = res.headers["location"].split("/")[-1]
    assert wait_done(client, job_id)["status"] == "completed"

    # default view: table with all columns, natural row order
    page = client.get(f"/jobs/{job_id}")
    assert "3 rows · 2 columns" in page.text
    assert page.text.index("b.example") < page.text.index("a.example")

    # server-side sort by count desc puts the 30 first
    page = client.get(f"/jobs/{job_id}", params={"sortcol": "count", "sortdir": "-1"})
    assert page.text.index("a.example") < page.text.index("b.example")

    # raw toggle shows the JSON text instead of the table
    page = client.get(f"/jobs/{job_id}", params={"view": "raw"})
    assert "[{" in page.text

    # CSV download returns the table as text/csv
    res = client.get(f"/jobs/{job_id}/result.csv")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/csv")
    assert 'filename="output.csv"' in res.headers["content-disposition"]
    lines = res.text.strip().split("\n")
    assert lines[0] == "host,count"
    assert len(lines) == 4

    # sorted-as-shown CSV honors the sort params
    res = client.get(f"/jobs/{job_id}/result.csv",
                     params={"sortcol": "count", "sortdir": "-1"})
    assert res.text.strip().split("\n")[1] == "a.example,30"

    # non-table output refuses CSV politely
    res = client.post("/submit", data={
        "logs": LOG, "scripts": "error_counter", "mode": "parallel",
    }, follow_redirects=False)
    plain_id = res.headers["location"].split("/")[-1]
    wait_done(client, plain_id)
    assert client.get(f"/jobs/{plain_id}/result.csv").status_code == 422


def test_unknown_page_renders_404(client):
    res = client.get("/no/such/page")
    assert res.status_code == 404
    assert "Page not found: /no/such/page" in res.text


def test_static_css_served(client):
    res = client.get("/static/style.css")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/css")
