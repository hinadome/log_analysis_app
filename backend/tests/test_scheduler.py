"""The ticker engine: due-minute firing and its bounds.

The clock is injected so tests step through minutes deterministically;
job submission is stubbed except where the real path matters (that is
test_schedules_api.py's run-now test). What is pinned here:
- fires when the current minute matches, exactly once per minute
- a minute missed while "down" stays missed — no catch-up
- overlap with a still-active previous run is skipped, visibly
- a schedule's failure lands in last_result, never in the ticker's face
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.models import PipelineStep
from app.services import pipeline_store, schedule_store
from app.services.scheduler import Scheduler

T = datetime(2026, 11, 3, 14, 7, 5)  # a Tuesday


@pytest.fixture(autouse=True)
def clean_stores():
    schedule_store.SCHEDULES_FILE.unlink(missing_ok=True)
    yield
    schedule_store.SCHEDULES_FILE.unlink(missing_ok=True)


@pytest.fixture(autouse=True)
def a_pipeline():
    """fire() resolves the pipeline at fire time, not from the schedule."""
    pipeline_store.save_pipeline(
        "p", [PipelineStep(script_id="grep_search", params={"pattern": "x"})])
    yield
    pipeline_store.PIPELINES_FILE.unlink(missing_ok=True)


def put(name="s", cron="* * * * *", **over):
    fields = dict(cron=cron, pipeline="p", log="app.log", label=None, enabled=True)
    fields.update(over)
    schedule_store.save_schedule(name, **fields)
    return schedule_store.get_schedule(name)


def put_inline(name="d", cron="* * * * *", **over):
    fields = dict(cron=cron, pipeline=None, log="", label=None, enabled=True,
                  steps=[PipelineStep(script_id="disk_report", params={})])
    fields.update(over)
    schedule_store.save_schedule(name, **fields)
    return schedule_store.get_schedule(name)


def stub_submission(monkeypatch, jobs=None, error=None):
    """Redirect the jobs API's create path; records what would have run."""
    calls = []

    async def fake_create_jobs(req):
        calls.append(req)
        if error:
            raise error
        return {"jobs": [SimpleNamespace(id=f"job-{i}")
                         for i in range(len(req.jobs))]}

    monkeypatch.setattr("app.routers.jobs.create_jobs", fake_create_jobs)
    monkeypatch.setattr("app.routers.jobs.store",
                        SimpleNamespace(get=lambda job_id: (jobs or {}).get(job_id)))
    return calls


def tick_at(now, **scheduler_over):
    sched = Scheduler(now_fn=lambda: now, **scheduler_over)
    return asyncio.run(sched.tick())


def test_fires_once_per_matching_minute(monkeypatch):
    calls = stub_submission(monkeypatch)
    put()

    assert tick_at(T) == ["s"]            # 14:07 matches "* * * * *"
    assert tick_at(datetime(2026, 11, 3, 14, 7, 41)) == []  # same minute — guarded

    schedule = schedule_store.get_schedule("s")
    assert schedule.last_run_at == "2026-11-03T14:07:00"
    assert schedule.last_result == "ok: 1 job(s) submitted"
    assert schedule.last_job_ids == ["job-0"]
    assert len(calls) == 1
    assert calls[0].label == "s"           # the schedule's name
    assert calls[0].mode == "chain"        # a pipeline fires as a chain
    assert calls[0].log_path == "app.log"


def test_next_matching_minute_fires_again(monkeypatch):
    stub_submission(monkeypatch)
    put()

    assert tick_at(T) == ["s"]
    assert tick_at(datetime(2026, 11, 3, 14, 8, 2)) == ["s"]  # a new minute


def test_inline_steps_fire_as_a_chain_without_a_log(monkeypatch):
    """A schedule's own script(s) fire like a pipeline does — mode=chain,
    the schedule's name as label, and log_path empty when saved so."""
    calls = stub_submission(monkeypatch)
    put_inline()

    assert tick_at(T) == ["d"]
    req = calls[0]
    assert req.mode == "chain"
    assert req.log_path == ""
    assert req.label == "d"
    assert [j.script_id for j in req.jobs] == ["disk_report"]
    assert req.jobs[0].params == {}


def test_inline_schedule_survives_pipeline_deletion(monkeypatch):
    """Inline steps do not resolve anything at fire time — deleting the
    pipeline (or never having one) does not touch them."""
    calls = stub_submission(monkeypatch)
    put_inline()
    pipeline_store.PIPELINES_FILE.unlink(missing_ok=True)

    assert tick_at(T) == ["d"]
    assert len(calls) == 1


def test_schedule_with_neither_pipeline_nor_steps_records_error(monkeypatch):
    """A hand-edited store entry with nothing to run fails into the
    bookkeeping, not into the ticker's face."""
    stub_submission(monkeypatch)
    put(pipeline=None, steps=[])

    assert tick_at(T) == ["s"]
    schedule = schedule_store.get_schedule("s")
    assert schedule.last_result == ("error: schedule has nothing to run "
                                    "(neither pipeline nor steps)")


def test_non_matching_and_matching_minutes(monkeypatch):
    stub_submission(monkeypatch)
    put(cron="0 8 * * *")  # daily at 08:00

    assert tick_at(T) == []                              # 14:07 — no match
    assert tick_at(datetime(2026, 11, 4, 8, 0, 12)) == ["s"]   # the due minute
    assert tick_at(datetime(2026, 11, 4, 8, 0, 59)) == []     # still that minute
    assert tick_at(datetime(2026, 11, 4, 8, 1, 0)) == []      # past it — missed forever


def test_no_catchup_for_minutes_lost_while_down(monkeypatch):
    """The 14:07 fire was missed (server down); starting at 14:09 does
    not reach back for it — that is the Airflow line we do not cross."""
    stub_submission(monkeypatch)
    put(cron="7 14 * * *")

    assert tick_at(datetime(2026, 11, 3, 14, 9, 0)) == []
    assert schedule_store.get_schedule("s").last_run_at is None


def test_disabled_schedule_never_fires(monkeypatch):
    calls = stub_submission(monkeypatch)
    put(enabled=False)

    assert tick_at(T) == []
    assert calls == []


def test_overlap_with_active_run_is_skipped(monkeypatch):
    """The previous fire's job is still queued/running — skip, visibly."""
    calls = stub_submission(
        monkeypatch,
        jobs={"job-0": SimpleNamespace(status="running")})
    put()
    schedule_store.record_run("s", run_at="2026-11-03T14:06:00",
                              result="ok: 1 job(s) submitted", job_ids=["job-0"])

    assert tick_at(T) == ["s"]
    assert calls == []  # nothing submitted
    schedule = schedule_store.get_schedule("s")
    assert schedule.last_result == "skipped: previous run still active (1 job(s))"
    assert schedule.last_job_ids == []
    assert schedule.last_run_at == "2026-11-03T14:07:00"  # the skip counts as the run


def test_finished_previous_run_does_not_block(monkeypatch):
    calls = stub_submission(
        monkeypatch,
        jobs={"job-0": SimpleNamespace(status="completed")})
    put()
    schedule_store.record_run("s", run_at="2026-11-03T14:06:00",
                              result="ok: 1 job(s) submitted", job_ids=["job-0"])

    assert tick_at(T) == ["s"]
    assert len(calls) == 1


def test_missing_pipeline_recorded_not_raised(monkeypatch):
    stub_submission(monkeypatch)
    put(pipeline="gone")

    assert tick_at(T) == ["s"]  # fired (and failed) — the ticker survives
    assert schedule_store.get_schedule("s").last_result == \
        "error: pipeline 'gone' not found"


def test_submission_error_recorded_not_raised(monkeypatch):
    from fastapi import HTTPException

    stub_submission(monkeypatch, error=HTTPException(404, "Log file not found"))
    put()

    assert tick_at(T) == ["s"]
    assert schedule_store.get_schedule("s").last_result == \
        "error: Log file not found"


def test_fires_multiple_schedules_in_one_minute(monkeypatch):
    stub_submission(monkeypatch)
    put("one")
    put("two", cron="7 14 * * *")
    put("three", cron="0 8 * * *")          # not due
    put("four", enabled=False)               # paused

    assert tick_at(T) == ["one", "two"]      # sorted by name, from the store
