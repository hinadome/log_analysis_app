"""Saved pipelines — reusable ordered chains, stored on the server.

Pipelines live in DATA_DIR/pipelines.json as {"<name>": Pipeline} so they
are shared by every browser and both UIs (the React UI previously kept
them in localStorage, per browser, and migrates them here on first load).
Saving under an existing name replaces it — the name is the identity.

The file is small and rewritten whole on every save (atomic tmp + replace,
one process, a lock around read-modify-write), which is plenty for a
handful of user-authored pipelines.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path

from ..config import DATA_DIR
from ..models import Pipeline, PipelineStep

PIPELINES_FILE = DATA_DIR / "pipelines.json"

_lock = threading.Lock()


def _load_all() -> dict[str, Pipeline]:
    try:
        raw = json.loads(PIPELINES_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: dict[str, Pipeline] = {}
    for name, rec in raw.items():
        try:
            out[str(name)] = Pipeline(
                name=str(name),
                steps=[PipelineStep(**s) for s in rec.get("steps") or []],
                created_at=str(rec.get("created_at") or ""),
                updated_at=str(rec.get("updated_at") or ""),
            )
        except (TypeError, ValueError):
            continue  # a corrupt entry is skipped, not fatal
    return out


def _persist(all_: dict[str, Pipeline]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = DATA_DIR / f".pipelines-{uuid.uuid4().hex[:8]}.tmp"
    try:
        tmp.write_text(json.dumps(
            {name: p.model_dump() for name, p in sorted(all_.items())},
            indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, PIPELINES_FILE)
    finally:
        tmp.unlink(missing_ok=True)


def list_pipelines() -> list[Pipeline]:
    """All saved pipelines, sorted by name."""
    with _lock:
        return list(_load_all().values())


def get_pipeline(name: str) -> Pipeline | None:
    with _lock:
        return _load_all().get(name)


def save_pipeline(name: str, steps: list[PipelineStep]) -> Pipeline:
    """Create or replace the pipeline under `name`; returns what was stored."""
    from .job_store import utcnow
    with _lock:
        all_ = _load_all()
        now = utcnow()
        existing = all_.get(name)
        pipeline = Pipeline(name=name, steps=steps,
                            created_at=existing.created_at if existing else now,
                            updated_at=now)
        all_[name] = pipeline
        _persist(all_)
        return pipeline


def delete_pipeline(name: str) -> bool:
    """Remove a pipeline; True when it existed."""
    with _lock:
        all_ = _load_all()
        if name not in all_:
            return False
        del all_[name]
        _persist(all_)
        return True
