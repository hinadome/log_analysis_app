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
