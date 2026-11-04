"""Schedule API: save (upsert), list with next-due, toggle, run-now, delete.

A schedule binds a saved pipeline to a log and a cron expression, so the
fixtures build a real pipeline first (submission-grade validation runs on
the pipeline's steps at schedule-save time too). The run-now test goes
through the real submission path — scheduled runs are ordinary jobs.
"""
from __future__ import annotations

PIPELINE = {
    "name": "errors then count",
    "steps": [
        {"script_id": "grep_search", "params": {"pattern": "ERROR"}},
        {"script_id": "error_counter", "params": {"level": "ERROR"}},
    ],
}

SCHEDULE = {
    "name": "nightly errors",
    "cron": "0 8 * * *",
    "pipeline": "errors then count",
    "log": "app.log",
    "label": None,
    "enabled": True,
}


def _pipeline(client):
    assert client.post("/api/pipelines", json=PIPELINE).status_code == 200


def _save(client, **over):
    return client.post("/api/schedules", json={**SCHEDULE, **over})


def test_save_list_roundtrip(client):
    _pipeline(client)
    res = _save(client)
    assert res.status_code == 200
    saved = res.json()["schedule"]
    assert saved["name"] == "nightly errors"
    assert saved["cron"] == "0 8 * * *"
    assert saved["cron_desc"] == "daily at 08:00"
    assert saved["next_due"]  # an ISO timestamp — it fires tomorrow at 08:00
    assert saved["enabled"] is True
    assert saved["last_run_at"] is None and saved["last_result"] is None

    listed = client.get("/api/schedules").json()["schedules"]
    assert [s["name"] for s in listed] == ["nightly errors"]
    assert listed[0]["next_due"]


def test_save_upserts_by_name_keeping_bookkeeping(client):
    _pipeline(client)
    _save(client)
    first = client.get("/api/schedules").json()["schedules"][0]

    res = _save(client, cron="*/15 * * * *")  # same name — replace
    assert res.status_code == 200
    listed = client.get("/api/schedules").json()["schedules"]
    assert len(listed) == 1
    assert listed[0]["cron"] == "*/15 * * * *"
    assert listed[0]["created_at"] == first["created_at"]  # identity kept


def test_save_validation(client):
    _pipeline(client)
    assert _save(client, name="  ").status_code == 422          # empty name
    assert _save(client, cron="0 8 * *").status_code == 422     # four fields
    assert _save(client, cron="61 * * * *").status_code == 422   # out of range
    assert _save(client, cron="0 0 30 2 *").status_code == 422   # never matches
    assert _save(client, pipeline="nope").status_code == 404     # unknown pipeline
    assert _save(client, log="missing.log").status_code == 404  # unknown log
    assert _save(client, log="../../etc/passwd").status_code == 400  # escapes logs/


def test_save_rejects_schedule_for_broken_pipeline(client):
    """A pipeline whose script vanished cannot be scheduled — that is
    noise, not a schedule (checked at save time, like the pipeline API)."""
    from app.models import PipelineStep
    from app.services import pipeline_store

    pipeline_store.save_pipeline(
        "ghost", [PipelineStep(script_id="gone_script", params={})])
    assert _save(client, pipeline="ghost").status_code == 404


def _save_inline(client, name="disk hourly", script_id="disk_report",
                 cron="0 * * * *", log=""):
    return client.post("/api/schedules", json={
        "name": name, "cron": cron, "pipeline": None,
        "steps": [{"script_id": script_id, "params": {}}],
        "log": log, "label": None, "enabled": True})


def test_save_inline_steps(client):
    """A schedule can be a single script with its own parameters — no
    pipeline needed. The steps are a validated snapshot owned by the
    schedule; `target` names the script for the UIs."""
    res = _save_inline(client)
    assert res.status_code == 200
    saved = res.json()["schedule"]
    assert saved["pipeline"] is None
    assert saved["steps"] == [{"script_id": "disk_report", "params": {}}]
    assert saved["target"] == "disk_report"
    assert saved["log"] == ""


def test_save_rejects_both_pipeline_and_steps(client):
    """Pipeline and steps are mutually exclusive — both set, or neither."""
    _pipeline(client)
    both = {**SCHEDULE, "steps": [{"script_id": "disk_report", "params": {}}]}
    assert _save(client, **both).status_code == 422

    neither = {k: v for k, v in SCHEDULE.items() if k != "pipeline"}
    assert client.post("/api/schedules", json=neither).status_code == 422


def test_save_rejects_unknown_inline_script(client):
    res = client.post("/api/schedules", json={
        "name": "ghost", "cron": "0 8 * * *", "pipeline": None,
        "steps": [{"script_id": "gone_script", "params": {}}],
        "log": "", "label": None, "enabled": True})
    assert res.status_code == 404


def test_save_requires_log_iff_first_step_reads_log(client):
    """Schedules fire in chain mode, where only position 0 sees the log —
    so the log requirement narrows to the first step, inline or pipeline."""
    # inline: a needs-log script with no log
    res = _save_inline(client, name="no log", script_id="error_counter")
    assert res.status_code == 422
    assert "error_counter" in res.json()["detail"]

    # ... and with a log it saves
    assert _save_inline(client, name="with log", script_id="error_counter",
                        log="app.log").status_code == 200

    # a log-free first step needs no log
    assert _save_inline(client, name="disk").status_code == 200

    # same rule through a pipeline: its first step (grep_search) decides
    _pipeline(client)
    assert _save(client, log="").status_code == 422


def test_toggle_round_trips_steps(client):
    """Pause/resume keeps an inline schedule's steps intact."""
    _save_inline(client)
    res = client.post("/api/schedules/disk hourly/toggle")
    assert res.status_code == 200
    paused = res.json()["schedule"]
    assert paused["enabled"] is False
    assert paused["pipeline"] is None
    assert paused["steps"] == [{"script_id": "disk_report", "params": {}}]

    resumed = client.post("/api/schedules/disk hourly/toggle").json()["schedule"]
    assert resumed["enabled"] is True
    assert resumed["steps"] == paused["steps"]


def test_run_now_inline_steps(client):
    """Run-now on an inline schedule fires its own steps — as a chain
    submission, labelled with the schedule's name, log-free here."""
    _save_inline(client)
    res = client.post("/api/schedules/disk hourly/run")
    assert res.status_code == 200
    out = res.json()
    assert out["result"] == "ok: 1 job(s) submitted"

    jobs = client.get("/api/jobs").json()["jobs"]
    assert [j["id"] for j in jobs] == out["job_ids"]
    assert jobs[0]["script_id"] == "disk_report"
    assert jobs[0]["log_path"] == ""
    assert jobs[0]["label"] == "disk hourly"


def test_toggle_and_delete(client):
    _pipeline(client)
    _save(client)

    res = client.post("/api/schedules/nightly errors/toggle")
    assert res.status_code == 200
    assert res.json()["schedule"]["enabled"] is False
    assert client.get("/api/schedules").json()["schedules"][0]["enabled"] is False

    res = client.post("/api/schedules/nightly errors/toggle")
    assert res.json()["schedule"]["enabled"] is True

    assert client.delete("/api/schedules/nightly errors").status_code == 200
    assert client.get("/api/schedules").json()["schedules"] == []
    assert client.delete("/api/schedules/nightly errors").status_code == 404
    assert client.post("/api/schedules/nope/toggle").status_code == 404
    assert client.post("/api/schedules/nope/run").status_code == 404


def test_run_now_submits_real_jobs(client):
    """Run-now shares the ticker's fire(): a normal chain submission with
    the schedule's name as the label, recorded in the bookkeeping."""
    _pipeline(client)
    _save(client)

    res = client.post("/api/schedules/nightly errors/run")
    assert res.status_code == 200
    out = res.json()
    assert out["result"] == "ok: 2 job(s) submitted"
    assert len(out["job_ids"]) == 2

    jobs = client.get("/api/jobs").json()["jobs"]
    assert {j["id"] for j in jobs} == set(out["job_ids"])
    assert all(j["label"] == "nightly errors" for j in jobs)  # schedule name
    assert all(j["chain_id"] == jobs[0]["chain_id"] for j in jobs)  # a chain

    schedule = client.get("/api/schedules").json()["schedules"][0]
    assert schedule["last_result"] == "ok: 2 job(s) submitted"
    assert schedule["last_job_ids"] == out["job_ids"]
    assert schedule["last_run_at"]


def test_run_now_custom_label(client):
    _pipeline(client)
    _save(client, label="morning report")
    client.post("/api/schedules/nightly errors/run")
    jobs = client.get("/api/jobs").json()["jobs"]
    assert all(j["label"] == "morning report" for j in jobs)


def test_run_now_missing_log_records_error(client):
    """A schedule whose log vanished fires into a recorded error, never a
    500 — the ticker must survive its schedules' failures."""
    _pipeline(client)
    _save(client)
    from app.services import schedule_store
    # simulate the log disappearing after the schedule was saved
    schedule_store.save_schedule(
        "nightly errors", cron="0 8 * * *", pipeline="errors then count",
        log="deleted.log", label=None, enabled=True)

    res = client.post("/api/schedules/nightly errors/run")
    assert res.status_code == 200
    assert res.json()["result"].startswith("error:")
    schedule = client.get("/api/schedules").json()["schedules"][0]
    assert schedule["last_result"].startswith("error:")
