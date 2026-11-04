"""Schedule endpoints: list, save, delete, toggle, run now.

A schedule binds a saved pipeline to one log and a cron expression; the
in-process ticker (services/scheduler.py) fires it. Everything the save
path checks — cron parses (and matches something), pipeline exists and
is still runnable, log resolves — is checked before anything is stored,
so a stored schedule is valid as saved. What can still go wrong at fire
time (log deleted, script removed) lands in the schedule's last_result
where the UIs show it.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException

from ..models import PipelineStep, Schedule, ScheduleSaveRequest
from ..services import pipeline_store, schedule_store
from ..services.cron import CronError, describe, parse_cron
from ..services.scheduler import next_due, scheduler
from ..services.scanner import discover_scripts
from .jobs import _resolve_log
from .pipelines import _validated_steps

router = APIRouter(prefix="/schedules", tags=["schedules"])


def _enriched(schedule: Schedule) -> dict:
    """A schedule plus what the UIs want beside it: the plain-language
    cron label, when it fires next, and what it runs (a pipeline's name
    or the inline script)."""
    out = schedule.model_dump()
    due = next_due(schedule.cron)
    out["next_due"] = due.isoformat() if due else None
    out["cron_desc"] = describe(schedule.cron)
    out["target"] = (schedule.pipeline
                     or (schedule.steps[0].script_id if schedule.steps else ""))
    return out


@router.get("")
def list_schedules():
    return {"schedules": [_enriched(s) for s in schedule_store.list_schedules()]}


@router.post("")
def save_schedule(req: ScheduleSaveRequest):
    name = (req.name or "").strip()[:80]
    if not name:
        raise HTTPException(status_code=422, detail="Schedule name is empty")
    cron = " ".join((req.cron or "").split())
    try:
        parse_cron(cron)
    except CronError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    scripts = {s.id: s for s in discover_scripts()}
    pipeline_name: str | None = None
    if (req.pipeline or "").strip() and req.steps:
        raise HTTPException(status_code=422, detail=(
            "A schedule runs either a saved pipeline or its own script(s), not both"))
    if (req.pipeline or "").strip():
        pipeline = pipeline_store.get_pipeline((req.pipeline or "").strip())
        if pipeline is None:
            raise HTTPException(status_code=404,
                                detail=f"No pipeline named '{req.pipeline}'")
        # the pipeline's scripts must still exist and take their parameters —
        # a schedule pointing at a broken pipeline is noise, not a schedule
        _validated_steps(pipeline.steps, scripts)
        steps = pipeline.steps          # resolved again at every fire
    elif req.steps:
        steps = [PipelineStep(script_id=sid, params=params)
                 for sid, params in _validated_steps(req.steps, scripts)]
    else:
        raise HTTPException(status_code=422, detail=(
            "A schedule needs a pipeline or a script to run"))

    # A log is required iff the first step reads {log} — schedules fire in
    # chain mode, where only position 0 ever sees the selected log.
    log_path = (req.log or "").strip()
    if scripts[steps[0].script_id].needs_log and not log_path:
        raise HTTPException(status_code=422, detail=(
            f"The first step '{steps[0].script_id}' reads its input through "
            "{log} — this schedule needs a log"))
    # a given log must resolve (404/400 otherwise) but is stored as given,
    # the same way an interactive submission names it
    if log_path:
        _resolve_log(log_path)
    saved = schedule_store.save_schedule(
        name, cron=cron, pipeline=pipeline_name,
        steps=[] if pipeline_name else steps,
        log=log_path,
        label=(req.label or "").strip()[:80] or None, enabled=req.enabled)
    return {"schedule": _enriched(saved)}


@router.delete("/{name}")
def delete_schedule(name: str):
    if not schedule_store.delete_schedule(name):
        raise HTTPException(status_code=404, detail=f"No schedule named '{name}'")
    return {"deleted": name}


@router.post("/{name}/toggle")
def toggle_schedule(name: str):
    schedule = schedule_store.get_schedule(name)
    if schedule is None:
        raise HTTPException(status_code=404, detail=f"No schedule named '{name}'")
    updated = schedule_store.save_schedule(
        name, cron=schedule.cron, pipeline=schedule.pipeline,
        steps=schedule.steps, log=schedule.log, label=schedule.label,
        enabled=not schedule.enabled)
    return {"schedule": _enriched(updated)}


@router.post("/{name}/run")
async def run_schedule(name: str):
    """Fire now, ignoring the cron clock (enabled or not).

    Shares the ticker's fire(): overlap with the previous run is skipped
    and recorded, and the run lands in the schedule's bookkeeping.
    """
    schedule = schedule_store.get_schedule(name)
    if schedule is None:
        raise HTTPException(status_code=404, detail=f"No schedule named '{name}'")
    result, job_ids = await scheduler.fire(
        schedule, datetime.now().replace(second=0, microsecond=0))
    return {"result": result, "job_ids": job_ids}
