"""Saved schedules — pipelines set to run at cron-defined times.

Schedules live in DATA_DIR/schedules.json as {"<name>": Schedule},
mirroring pipeline_store: same identity rule (the name), same atomic
whole-file rewrite, same lock around read-modify-write. The scheduler
owns the bookkeeping fields (last_run_at / last_result / last_job_ids)
via `record_run`; the save API only writes the configuration.
"""
from __future__ import annotations

import json
import os
import threading
import uuid

from ..config import DATA_DIR
from ..models import Schedule

SCHEDULES_FILE = DATA_DIR / "schedules.json"

_lock = threading.Lock()


def _load_all() -> dict[str, Schedule]:
    try:
        raw = json.loads(SCHEDULES_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: dict[str, Schedule] = {}
    for name, rec in raw.items():
        try:
            out[str(name)] = Schedule(**{
                k: v for k, v in rec.items() if k in Schedule.model_fields})
        except (TypeError, ValueError):
            continue  # a corrupt entry is skipped, not fatal
    return out


def _persist(all_: dict[str, Schedule]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = DATA_DIR / f".schedules-{uuid.uuid4().hex[:8]}.tmp"
    try:
        tmp.write_text(json.dumps(
            {name: s.model_dump() for name, s in sorted(all_.items())},
            indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, SCHEDULES_FILE)
    finally:
        tmp.unlink(missing_ok=True)


def list_schedules() -> list[Schedule]:
    """All saved schedules, sorted by name."""
    with _lock:
        return list(_load_all().values())


def get_schedule(name: str) -> Schedule | None:
    with _lock:
        return _load_all().get(name)


def save_schedule(name: str, **fields) -> Schedule:
    """Create or replace the schedule under `name`; returns what was stored.

    Replacing keeps the bookkeeping (last run/result/jobs) — an edit
    mid-flight still sees the previous run's job ids for the overlap
    check, and a re-save does not rewind history.
    """
    from .job_store import utcnow
    with _lock:
        all_ = _load_all()
        now = utcnow()
        existing = all_.get(name)
        schedule = Schedule(
            name=name, **fields,
            created_at=existing.created_at if existing else now,
            updated_at=now,
            last_run_at=existing.last_run_at if existing else None,
            last_result=existing.last_result if existing else None,
            last_job_ids=existing.last_job_ids if existing else [])
        all_[name] = schedule
        _persist(all_)
        return schedule


def delete_schedule(name: str) -> bool:
    """Remove a schedule; True when it existed."""
    with _lock:
        all_ = _load_all()
        if name not in all_:
            return False
        del all_[name]
        _persist(all_)
        return True


def record_run(name: str, *, run_at: str, result: str, job_ids: list[str]) -> None:
    """The scheduler's bookkeeping write after a fire (or a skip/error)."""
    with _lock:
        all_ = _load_all()
        schedule = all_.get(name)
        if schedule is None:  # deleted while firing — nothing to update
            return
        all_[name] = schedule.model_copy(update={
            "last_run_at": run_at, "last_result": result,
            "last_job_ids": job_ids})
        _persist(all_)
