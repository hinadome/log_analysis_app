"""Saved-pipeline endpoints: list, save, delete.

A pipeline is a reusable ordered chain — scripts plus their parameters —
saved under a name and loadable on any log in either UI. Validation at
save time matches submission: unknown scripts, broken manifests and bad
parameter values are rejected with 422/404 before anything is stored,
so a saved pipeline always loads cleanly (or names exactly what went
missing, at load time).
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..models import PipelineSaveRequest, PipelineStep, ScriptInfo
from ..services import pipeline_store
from ..services.scanner import discover_scripts
from .jobs import _validate_params

router = APIRouter(prefix="/pipelines", tags=["pipelines"])


def _validated_steps(req: PipelineSaveRequest, scripts: dict[str, ScriptInfo]):
    """Steps with existing scripts and submission-grade parameter checks."""
    seen: list[tuple[str, dict]] = []
    for step in req.steps:
        script = scripts.get(step.script_id)
        if script is None:
            raise HTTPException(status_code=404, detail=f"Unknown script: {step.script_id}")
        if script.error:
            raise HTTPException(status_code=422, detail=(
                f"Script '{script.id}' has a broken manifest: {script.error}"))
        seen.append((step.script_id, _validate_params(script, step.params)))
    return seen


@router.get("")
def list_pipelines():
    return {"pipelines": [p.model_dump() for p in pipeline_store.list_pipelines()]}


@router.post("")
def save_pipeline(req: PipelineSaveRequest):
    name = (req.name or "").strip()[:80]
    if not name:
        raise HTTPException(status_code=422, detail="Pipeline name is empty")
    if not req.steps:
        raise HTTPException(status_code=422, detail="A pipeline needs at least one step")
    scripts = {s.id: s for s in discover_scripts()}
    steps = _validated_steps(req, scripts)
    saved = pipeline_store.save_pipeline(
        name, [PipelineStep(script_id=sid, params=params) for sid, params in steps])
    return {"pipeline": saved.model_dump()}


@router.delete("/{name}")
def delete_pipeline(name: str):
    if not pipeline_store.delete_pipeline(name):
        raise HTTPException(status_code=404, detail=f"No pipeline named '{name}'")
    return {"deleted": name}
