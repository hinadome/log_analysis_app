"""The cron ticker — an in-process asyncio loop that fires schedules.

Every 30 seconds (machine-local clock, minute granularity) each enabled
schedule whose cron expression matches the current minute is fired
through the normal job-submission path: scheduled runs are ordinary
jobs with the schedule's name as their label, visible in both UIs and
subject to the same queueing, retention and concurrency as manual ones.

Deliberate bounds (this is a workbench, not Airflow):
- Overlap: if a schedule's previous run is still active, the fire is
  skipped and recorded — never two of the same schedule at once.
- No catch-up: a minute lost to a server restart is lost. `last_run_at`
  guards against double-firing within one minute; it never triggers a
  fire for a minute already gone.

`now_fn` is injectable so the engine tests drive the clock; `tick()`
is public for the same reason.
"""
from __future__ import annotations

import asyncio
from datetime import datetime

from ..models import JobCreateRequest, JobSpecIn, Schedule
from . import pipeline_store, schedule_store
from .cron import CronError, next_after, parse_cron

TICK_SECONDS = 30
ACTIVE_STATUSES = {"queued", "waiting", "running"}


def _minute(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso)
    except ValueError:
        return None


class Scheduler:
    def __init__(self, now_fn=None, interval: float = TICK_SECONDS):
        self._now_fn = now_fn or datetime.now
        self.interval = interval
        self._task: asyncio.Task | None = None

    # --- lifecycle (main.py lifespan) ---------------------------------

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def shutdown(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # one bad tick must not kill the loop
                print(f"scheduler: tick failed: {exc!r}")
            await asyncio.sleep(self.interval)

    # --- the engine ----------------------------------------------------

    async def tick(self) -> list[str]:
        """Fire every enabled schedule due in the current minute.

        Returns the names fired — the ticker loop ignores them, tests
        and logs find them handy.
        """
        now = self._now_fn()
        minute = now.replace(second=0, microsecond=0)
        fired: list[str] = []
        for schedule in schedule_store.list_schedules():
            if not schedule.enabled:
                continue
            try:
                spec = parse_cron(schedule.cron)
            except CronError:
                continue  # cannot happen for stored schedules (validated at save)
            last = _minute(schedule.last_run_at)
            if last is not None and last >= minute:
                continue  # already fired this minute — the double-fire guard
            if not spec.matches(minute):
                continue
            await self.fire(schedule, minute)
            fired.append(schedule.name)
        return fired

    async def fire(self, schedule: Schedule, minute: datetime) -> tuple[str, list[str]]:
        """Run a schedule's pipeline now (ticker or "run now" button).

        Records the outcome in the schedule's bookkeeping either way and
        returns (result text, job ids). Raises nothing: a schedule that
        fails must not take the ticker (or the button) down with it.
        """
        from ..routers import jobs  # late import: routers import services first

        def record(result: str, job_ids: list[str]) -> tuple[str, list[str]]:
            schedule_store.record_run(schedule.name, run_at=minute.isoformat(),
                                      result=result, job_ids=job_ids)
            return result, job_ids

        active = [job_id for job_id in schedule.last_job_ids
                  if (job := jobs.store.get(job_id)) is not None
                  and job.status in ACTIVE_STATUSES]
        if active:
            return record(f"skipped: previous run still active "
                          f"({len(active)} job(s))", [])

        pipeline = pipeline_store.get_pipeline(schedule.pipeline)
        if pipeline is None:
            return record(f"error: pipeline '{schedule.pipeline}' not found", [])

        label = (schedule.label or "").strip()[:80] or schedule.name
        try:
            response = await jobs.create_jobs(JobCreateRequest(
                log_path=schedule.log,
                mode="chain",
                label=label,
                jobs=[JobSpecIn(script_id=s.script_id, params=s.params)
                      for s in pipeline.steps]))
        except Exception as exc:  # HTTPException carries the 4xx detail
            detail = getattr(exc, "detail", None) or exc
            return record(f"error: {detail}", [])

        job_ids = [j.id for j in response["jobs"]]
        return record(f"ok: {len(job_ids)} job(s) submitted", job_ids)


# The app-wide instance (main.py lifespan starts/stops it; the API's
# "run now" shares fire() with the ticker).
scheduler = Scheduler()


def next_due(cron: str) -> datetime | None:
    """The next local time this expression fires, for display."""
    try:
        return next_after(parse_cron(cron), datetime.now())
    except CronError:
        return None
