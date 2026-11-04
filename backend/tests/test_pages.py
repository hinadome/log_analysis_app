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
import re
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
    # the submit bar is sticky: Run stays reachable past long fieldset stacks
    assert 'class="runbar"' in body
    assert "▶ Run" in body


def test_run_page_without_a_log(client):
    """A missing selected log no longer blocks the page — log-free scripts
    are configured without one, and the missing one is still flagged."""
    res = client.get("/run", params={"log": "ghost.log"})
    assert res.status_code == 200
    assert "no longer available" in res.text
    # the page renders the full configuration, with a no-log hint instead
    # of the old dead-end card
    assert "No log selected" in res.text
    assert "log-free" in res.text
    assert 'name="scripts"' not in res.text  # nothing chosen yet


def test_run_page_accepts_typed_absolute_path(client, tmp_path):
    # the home page's "use a file by path" box: any existing file on the
    # machine, not just what the logs listing knows
    outside = tmp_path / "system.log"
    outside.write_text("2026-10-30 ERROR typed absolute path\n", encoding="utf-8")
    res = client.get("/run", params={"log": str(outside)})
    assert res.status_code == 200
    assert "no longer available" not in res.text
    assert "No log selected" not in res.text
    # carried through as a chip and as the form's hidden log value
    assert str(outside) in res.text


def test_script_category_filter(client):
    """The shipped samples carry categories; both script tables narrow to
    one category via ?cat=, chosen scripts survive the filter, and an
    unknown category falls back to everything."""
    # home: chip row + filtering
    res = client.get("/")
    assert "category:" in res.text
    assert 'href="/?cat=web"' in res.text
    res = client.get("/", params={"cat": "web"})
    assert "Top IPs" in res.text
    assert "Regex Search" not in res.text  # search, not web

    # unknown category: everything, not a broken empty table
    res = client.get("/", params={"cat": "bogus"})
    assert "Regex Search" in res.text

    # run page: same table filter, chosen steps stay submitted via their
    # hidden inputs even when their category is filtered out of view
    res = client.get("/run", params={
        "log": LOG, "scripts": ["grep_search"], "cat": "web"})
    assert "Top IPs" in res.text                       # a web script in the table
    assert '<input type="hidden" name="scripts" value="grep_search">' in res.text
    assert '<input type="hidden" name="pick" value="grep_search">' in res.text  # stays checked through an Update
    assert 'name="cat" value="web"' in res.text        # the filter persists through Update


def _hidden_script_order(html: str) -> list[str]:
    """Order of the chosen steps as the page carries them (GET + POST forms
    both repeat the hidden inputs, so dedupe preserving order)."""
    return list(dict.fromkeys(re.findall(r'name="scripts" value="(\w+)"', html)))


def test_chain_step_order_visible_and_reorderable(client):
    """Chain mode numbers the parameter fieldsets (Step N) and gives each
    legend ↑/↓ links that swap it with a neighbor; the run summary shows the
    pipeline. In parallel mode order is irrelevant, so no numbering appears.
    The script table's checkboxes merge into the carried order on Update —
    a reordered chain survives an Update instead of snapping back to the
    table's alphabetical order."""
    res = client.get("/run", params=[
        ("log", LOG), ("scripts", "grep_search"), ("scripts", "top_ips"),
        ("mode", "chain")])
    assert "Step 1" in res.text and "Step 2" in res.text
    assert "Regex Search → Top IPs" in res.text         # runbar pipeline, in order
    # the ↓ on step 1 / ↑ on step 2 both swap the pair (hrefs are HTML-escaped)
    assert "scripts=top_ips&amp;scripts=grep_search" in res.text

    # following a swap link re-numbers the steps
    res = client.get("/run", params=[
        ("log", LOG), ("scripts", "top_ips"), ("scripts", "grep_search"),
        ("mode", "chain")])
    assert "Top IPs → Regex Search" in res.text
    assert _hidden_script_order(res.text) == ["top_ips", "grep_search"]

    # an Update keeps that custom order, appends newly checked scripts in
    # table order, and drops unchecked ones
    res = client.get("/run", params=[
        ("log", LOG), ("scripts", "top_ips"), ("scripts", "grep_search"),
        ("pick", "top_ips"), ("pick", "error_counter"), ("mode", "chain")])
    assert _hidden_script_order(res.text) == ["top_ips", "error_counter"]

    # parallel mode: no step numbering, no reorder controls
    res = client.get("/run", params=[
        ("log", LOG), ("scripts", "grep_search"), ("scripts", "top_ips")])
    assert "Step 1" not in res.text
    assert "Run this step" not in res.text


# --- saved pipelines -------------------------------------------------------

def test_save_pipeline_via_runbar_and_load(client):
    """The runbar's 💾 button posts the /submit form's fields to
    /pipelines/save; success redirects to the same fully-configured /run
    URL (steps, params, msg flash) — which is also the shareable link."""
    res = client.post("/pipelines/save", data={
        "logs": LOG,
        "scripts": ["grep_search", "error_counter"],
        "mode": "chain",
        "pipeline_name": "my chain",
        "p::grep_search::pattern": "timeout",
        "p::grep_search::ignore_case": "true",
        "p::grep_search::context": "2",
        "p::error_counter::level": "WARN",
    }, follow_redirects=False)
    assert res.status_code == 303
    loc = res.headers["location"]
    assert loc.startswith("/run?")
    assert "logs=app.log" in loc
    assert "scripts=grep_search&scripts=error_counter" in loc  # order kept
    assert "p::grep_search::pattern=timeout" in loc
    assert "p::error_counter::level=WARN" in loc
    assert "msg=" in loc

    # the API serves what was saved — typed params, in order
    pipes = client.get("/api/pipelines").json()["pipelines"]
    assert [p["name"] for p in pipes] == ["my chain"]
    assert pipes[0]["steps"][0]["params"] == {
        "pattern": "timeout", "ignore_case": True, "context": 2, "max_count": 0}
    assert pipes[0]["steps"][1]["params"] == {"level": "WARN", "json": False}

    # the redirect target shows the flash and keeps the configuration
    page = client.get(loc)
    assert "Pipeline &#39;my chain&#39; saved" in page.text  # msg flash
    assert 'name="p::grep_search::pattern" value="timeout"' in page.text

    # loading it via ?pipeline= replaces the steps, forces chain mode and
    # prefills every parameter — a ready-to-run page
    page = client.get("/run", params={"log": LOG, "pipeline": "my chain"})
    body = page.text
    assert "Step 1" in body and "Step 2" in body
    assert _hidden_script_order(body) == ["grep_search", "error_counter"]
    assert 'name="p::grep_search::pattern" value="timeout"' in body
    assert 'name="p::grep_search::ignore_case" value="true" checked' in body
    assert re.search(r'name="p::grep_search::context"[^>]*value="2"', body, re.S)
    assert '<option value="WARN" selected>WARN</option>' in body
    assert 'value="chain" checked' in body                       # mode forced
    assert 'value="my chain">my chain</option>' in body           # Load select
    assert 'formaction="/pipelines/save"' in body                # save row
    assert 'formaction="/pipelines/delete" formmethod="post"' in body  # 🗑 button


def test_run_page_param_values_survive_links_and_update(client):
    """`p::` query params prefill the form, ride along the GET form's
    hidden inputs (Update) and the reorder/chip links — a configured run
    never falls back to defaults by navigating."""
    res = client.get("/run", params=[
        ("log", LOG), ("scripts", "grep_search"), ("mode", "chain"),
        ("p::grep_search::pattern", "timeout"),
        ("p::grep_search::ignore_case", "true"),
    ])
    body = res.text
    assert 'name="p::grep_search::pattern" value="timeout"' in body
    assert 'name="p::grep_search::ignore_case" value="true" checked' in body
    # the GET form carries them — an Update keeps the configuration
    assert '<input type="hidden" name="p::grep_search::pattern" value="timeout">' in body
    # so do the reorder links
    assert "p::grep_search::pattern=timeout" in body


def test_run_page_unknown_pipeline_flashes_error(client):
    res = client.get("/run", params={"log": LOG, "pipeline": "ghost"})
    assert res.status_code == 200
    assert "No pipeline named &#39;ghost&#39;" in res.text  # msg is escaped
    assert 'class="flash bad"' in res.text


def test_save_pipeline_requires_a_name(client):
    res = client.post("/pipelines/save", data={
        "logs": LOG, "scripts": "grep_search", "mode": "chain",
        "p::grep_search::pattern": "x"})
    assert res.status_code == 422
    assert "Pipeline name is empty" in res.text
    assert client.get("/api/pipelines").json()["pipelines"] == []


def test_save_pipeline_unknown_script_renders_error(client):
    res = client.post("/pipelines/save", data={
        "logs": LOG, "scripts": "ghost", "mode": "chain", "pipeline_name": "x"})
    assert res.status_code == 404
    assert "Unknown script: ghost" in res.text


def test_save_pipeline_validates_params_like_submission(client):
    # grep_search's pattern is required — saving without it is a 422 page
    res = client.post("/pipelines/save", data={
        "logs": LOG, "scripts": "grep_search", "mode": "chain",
        "pipeline_name": "bad"})
    assert res.status_code == 422
    assert "required" in res.text
    assert client.get("/api/pipelines").json()["pipelines"] == []


def test_delete_pipeline_via_load_card(client):
    """The Load card's 🗑 button shares the Load form (formaction +
    formmethod overrides): the selected name and hidden logs POST to
    /pipelines/delete; success redirects to /run with a flash, and the
    card disappears once no pipelines remain."""
    client.post("/pipelines/save", data={
        "logs": LOG, "scripts": "grep_search", "mode": "chain",
        "pipeline_name": "my chain", "p::grep_search::pattern": "ERROR"})

    res = client.post("/pipelines/delete", data={"pipeline": "my chain", "logs": LOG},
                      follow_redirects=False)
    assert res.status_code == 303
    loc = res.headers["location"]
    assert loc.startswith("/run?") and "logs=app.log" in loc and "msg=" in loc

    assert client.get("/api/pipelines").json()["pipelines"] == []
    page = client.get(loc)
    assert "Pipeline &#39;my chain&#39; deleted" in page.text  # msg flash
    assert "Saved pipelines" not in page.text                  # card gone

    # deleting an unknown name (e.g. a stale page) renders the 404 page
    res = client.post("/pipelines/delete", data={"pipeline": "ghost", "logs": LOG})
    assert res.status_code == 404
    assert "No pipeline named" in res.text


# --- schedules (cron-fired pipelines) ---------------------------------------

def _make_pipeline(client):
    assert client.post("/api/pipelines", json={
        "name": "my chain",
        "steps": [{"script_id": "grep_search", "params": {"pattern": "ERROR"}}],
    }).status_code == 200


def test_schedules_page_create_and_list(client):
    """The cron view renders with the nav link, and the create form saves
    a schedule that appears with its clock and plain-language label."""
    _make_pipeline(client)
    res = client.get("/schedules")
    assert res.status_code == 200
    assert 'href="/schedules"' in res.text          # nav link (base.html)
    assert "No schedules yet" in res.text

    res = client.post("/schedules/save", data={
        "name": "every five", "cron": "*/5 * * * *",
        "pipeline": "my chain", "log": LOG, "label": "", "enabled": "on",
    }, follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"].startswith("/schedules?")

    page = client.get(res.headers["location"])
    assert "Schedule &#39;every five&#39; saved" in page.text  # msg flash
    assert "every five" in page.text
    assert "*/5 * * * *" in page.text
    assert "every 5 minutes" in page.text            # cron_desc
    assert "never run" in page.text

    schedules = client.get("/api/schedules").json()["schedules"]
    assert [s["name"] for s in schedules] == ["every five"]


def test_schedules_page_form_validation(client):
    _make_pipeline(client)
    res = client.post("/schedules/save", data={
        "name": "bad", "cron": "0 8 * *", "pipeline": "my chain", "log": LOG})
    assert res.status_code == 422
    assert "5 fields" in res.text

    res = client.post("/schedules/save", data={
        "name": "bad", "cron": "0 8 * * *", "pipeline": "ghost", "log": LOG})
    assert res.status_code == 404


def test_schedules_edit_toggle_run_delete(client):
    _make_pipeline(client)
    client.post("/schedules/save", data={
        "name": "nightly", "cron": "0 8 * * *",
        "pipeline": "my chain", "log": LOG, "enabled": "on"})

    # ?schedule= prefills the form with the stored configuration
    page = client.get("/schedules", params={"schedule": "nightly"})
    assert 'value="nightly"' in page.text
    assert 'value="0 8 * * *"' in page.text
    assert "Edit a schedule" in page.text

    # a preset chip (?cron=) prefills a fresh form instead
    page = client.get("/schedules", params={"cron": "*/15 * * * *"})
    assert 'value="*/15 * * * *"' in page.text
    assert "Create a schedule" in page.text

    # toggle → paused, visibly
    res = client.post("/schedules/toggle", data={"name": "nightly"}, follow_redirects=False)
    assert res.status_code == 303
    page = client.get(res.headers["location"])
    assert "Schedule &#39;nightly&#39; paused" in page.text
    assert '<span class="badge">paused</span>' in page.text

    # run now: a real submission labelled with the schedule's name
    res = client.post("/schedules/run", data={"name": "nightly"}, follow_redirects=False)
    assert res.status_code == 303
    page = client.get(res.headers["location"])
    assert "nightly: ok: 1 job(s) submitted" in page.text
    jobs = client.get("/api/jobs").json()["jobs"]
    assert [j["label"] for j in jobs] == ["nightly"]

    # delete
    res = client.post("/schedules/delete", data={"name": "nightly"}, follow_redirects=False)
    assert res.status_code == 303
    assert client.get("/api/schedules").json()["schedules"] == []


def test_schedules_page_script_mode(client):
    """The form schedules a single script: the selects default to (none),
    the Configure-parameters GET wizard re-renders with the fieldset, and
    saving stores inline steps — runnable without a log."""
    res = client.get("/schedules")
    assert 'name="script"' in res.text
    assert '<option value="">— none —</option>' in res.text
    assert '<option value="">— none (log-free) —</option>' in res.text

    # the wizard: pick a script, press Configure parameters (GET submit)
    page = client.get("/schedules", params={
        "name": "disk hourly", "cron": "0 * * * *", "script": "disk_report",
        "log": "", "enabled": "true"})
    assert 'value="disk hourly"' in page.text          # typed values survive
    assert 'value="0 * * * *"' in page.text
    assert 'value="disk_report" selected' in page.text
    assert "No parameters." in page.text               # disk_report has none

    # a parameterful script shows its fields, and its needs-log badge
    page = client.get("/schedules", params={
        "name": "errors", "cron": "0 8 * * *", "script": "error_counter", "log": ""})
    assert 'name="p::error_counter::level"' in page.text
    assert "needs log" in page.text

    # save a log-free single-script schedule, then run it — log-free
    res = client.post("/schedules/save", data={
        "name": "disk hourly", "cron": "0 * * * *", "script": "disk_report",
        "pipeline": "", "log": "", "label": "", "enabled": "true",
    }, follow_redirects=False)
    assert res.status_code == 303
    schedules = client.get("/api/schedules").json()["schedules"]
    assert schedules[0]["steps"] == [{"script_id": "disk_report", "params": {}}]
    assert schedules[0]["target"] == "disk_report"
    assert schedules[0]["log"] == ""

    res = client.post("/schedules/run", data={"name": "disk hourly"}, follow_redirects=False)
    assert res.status_code == 303
    jobs = client.get("/api/jobs").json()["jobs"]
    assert [j["script_id"] for j in jobs] == ["disk_report"]
    assert jobs[0]["log_path"] == ""
    assert jobs[0]["label"] == "disk hourly"

    # ?schedule= edit prefill selects the script and shows its fieldset
    page = client.get("/schedules", params={"schedule": "disk hourly"})
    assert 'value="disk_report" selected' in page.text
    assert "No parameters." in page.text


def test_schedules_page_script_needs_log(client):
    """A needs-log script with no log is rejected, naming the script."""
    res = client.post("/schedules/save", data={
        "name": "no log", "cron": "0 8 * * *", "script": "error_counter",
        "pipeline": "", "log": "", "label": "", "enabled": "true"})
    assert res.status_code == 422
    assert "error_counter" in res.text


def test_schedules_page_without_pipelines(client):
    """No saved pipelines — the form still renders (a schedule can be a
    single script), with empty selects and a hint."""
    page = client.get("/schedules")
    assert page.status_code == 200
    assert "single script" in page.text
    assert '<option value="">— none —</option>' in page.text


def test_run_page_flags_missing_typed_path(client):
    res = client.get("/run", params={"log": "/no/such/file.log"})
    assert res.status_code == 200
    assert "no longer available" in res.text
    assert "No log selected" in res.text


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


def test_submit_without_a_log_runs_log_free(client):
    """No logs checked: the run page still configures, and submitting runs
    the log-free scripts without one — a needs-log script is named in a
    422 instead."""
    # the run page renders fully with no log selected
    page = client.get("/run", params={"scripts": "disk_report"})
    assert "No log selected" in page.text
    assert "log-free" in page.text
    assert "no log" in page.text                      # the runbar summary

    # submit: no logs fields at all — one log-free submission
    res = client.post("/submit", data={
        "scripts": "disk_report", "mode": "parallel", "label": ""}, follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"].startswith("/jobs/")
    job_id = res.headers["location"].split("/")[-1]
    job = wait_done(client, job_id)
    assert job["status"] == "completed"
    assert job["log_path"] == "" and job["log_name"] == ""
    # the input tab says so instead of pretending a file was lost
    input_page = client.get(f"/jobs/{job_id}", params={"tab": "input"})
    assert "No input" in input_page.text
    assert "command has no" in input_page.text and "{log}" in input_page.text

    # a needs-log script without a log is rejected with the named error
    res = client.post("/submit", data={
        "scripts": "error_counter", "mode": "parallel"})
    assert res.status_code == 422
    assert "error_counter" in res.text
    assert "{log}" in res.text


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
