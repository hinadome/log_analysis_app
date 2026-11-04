"""Job lifecycle endpoints: create, list, inspect, cancel, delete, rerun."""
from __future__ import annotations

import asyncio
import json
import mimetypes
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse

from ..config import LOGS_DIR, STDOUT_TAIL_BYTES, JOB_RETENTION_DAYS
from ..models import Job, JobCreateRequest, ScriptInfo
from ..services import decompress_cache
from ..services.executor import Executor
from ..services.job_store import JobStore
from ..services.scanner import discover_scripts

router = APIRouter(prefix="/jobs", tags=["jobs"])

store = JobStore()
executor = Executor(store)


def _validate_params(script: ScriptInfo, params: dict) -> dict:
    """Type-check user parameters against the script manifest."""
    specs = {p.name: p for p in script.parameters}
    for name in params:
        if name not in specs:
            raise HTTPException(status_code=422,
                                detail=f"Unknown parameter '{name}' for script '{script.id}'")

    clean: dict = {}
    for spec in script.parameters:
        value = params.get(spec.name, spec.default)
        if spec.required and (value is None or value == ""):
            raise HTTPException(status_code=422,
                                detail=f"Parameter '{spec.name}' is required for script '{script.id}'")
        if value is None or value == "":
            continue
        try:
            if spec.type == "boolean":
                if not isinstance(value, bool):
                    raise ValueError("expected true/false")
                clean[spec.name] = value
            elif spec.type == "integer":
                if isinstance(value, bool):
                    raise ValueError("expected an integer")
                clean[spec.name] = int(value)
            elif spec.type == "float":
                if isinstance(value, bool):
                    raise ValueError("expected a number")
                clean[spec.name] = float(value)
            elif spec.type == "enum":
                if str(value) not in spec.choices:
                    raise ValueError(f"must be one of {spec.choices}")
                clean[spec.name] = str(value)
            elif spec.type == "multi":
                if isinstance(value, str):
                    value = [value]
                if not isinstance(value, list):
                    raise ValueError("expected a list")
                clean[spec.name] = [str(v) for v in value]
            else:  # string
                clean[spec.name] = str(value)
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid value for parameter '{spec.name}' ({spec.type}): {exc}",
            )
    return clean


def _resolve_log(user_path: str) -> Path:
    """Resolve a user-supplied input path to an existing file.

    Relative names resolve under LOGS_DIR and must not escape it. An
    absolute path may point anywhere on this machine: scripts already run
    as this user with full filesystem access, so an arbitrary input file
    grants no new capability — it just has to exist.
    """
    p = Path(user_path)
    if p.is_absolute():
        p = p.resolve()
        if not p.is_file():
            raise HTTPException(status_code=404, detail=f"Log file not found: {user_path}")
        return p
    p = (LOGS_DIR / p).resolve()
    base = LOGS_DIR.resolve()
    if p != base and base not in p.parents:
        raise HTTPException(status_code=400, detail="Path escapes the logs directory")
    if not p.is_file():
        raise HTTPException(status_code=404, detail=f"Log file not found: {user_path}")
    return p


def _display_log_name(p: Path) -> str:
    """The label a job carries for its input: base name for a file in
    logs/, the full path for one reached by absolute path from elsewhere
    on the machine (its base name alone would identify nothing)."""
    return p.name if LOGS_DIR.resolve() in p.parents else str(p)


def _get_job_or_404(job_id: str) -> Job:
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


def _job_detail(job: Job) -> dict:
    detail = {
        **job.model_dump(),
        "stdout_tail": store.tail(job.id, "stdout"),
        "stderr_tail": store.tail(job.id, "stderr"),
    }
    if job.chain_id:
        detail["chain_steps"] = [
            {
                "id": s.id,
                "index": s.chain_index,
                "script_name": s.script_name,
                "status": s.status,
                "error": s.error,
            }
            for s in store.list_chain(job.chain_id)
        ]
    return detail


@router.post("")
async def create_jobs(req: JobCreateRequest):
    if not req.jobs:
        raise HTTPException(status_code=422, detail="No jobs requested")
    log = _resolve_log(req.log_path) if (req.log_path or "").strip() else None

    # Pass 1: validate every spec up front. A bad spec must fail the whole
    # request rather than leave half-created jobs that are never submitted.
    scripts = {s.id: s for s in discover_scripts()}
    plan: list[tuple[ScriptInfo, dict]] = []
    for spec in req.jobs:
        script = scripts.get(spec.script_id)
        if script is None:
            raise HTTPException(status_code=404,
                                detail=f"Unknown script: {spec.script_id}")
        if script.error:
            raise HTTPException(status_code=422,
                                detail=f"Script '{script.id}' has a broken manifest: {script.error}")
        plan.append((script, _validate_params(script, spec.params)))

    # Without a log, only scripts that don't read {log} can run. In chain
    # mode only the first step reads the log (the rest read the previous
    # step's output), so the check narrows to position 0.
    if log is None:
        for script, _ in (plan[:1] if req.mode == "chain" else plan):
            if script.needs_log:
                raise HTTPException(status_code=422, detail=(
                    f"Script '{script.id}' reads its input through {{log}} — "
                    "select a log, or drop this script"))

    # Pass 2: create the jobs (validation can no longer fail here).
    label = (req.label or "").strip()[:80] or None
    steps: list[tuple[Job, ScriptInfo]] = [
        (store.create(
            script_id=script.id,
            script_name=script.name,
            log_path=str(log) if log else "",
            log_name=_display_log_name(log) if log else "",
            params=params,
            label=label,
        ), script)
        for script, params in plan
    ]

    if req.mode == "chain" and len(steps) > 1:
        chain_id = f"chain-{uuid.uuid4().hex[:10]}"
        executor.submit_chain(steps, chain_id)
    else:
        for job, script in steps:
            if log:
                job.input_source = "log"
            store.save(job)
            executor.submit(job, script)
    return {"jobs": [job for job, _script in steps]}


@router.get("")
def list_jobs(
    status: str | None = Query(None),
    q: str | None = Query(None),
    limit: int = Query(200, ge=1, le=5000),
    offset: int = Query(0, ge=0),
):
    jobs = store.list()
    if status:
        wanted = {s.strip() for s in status.split(",") if s.strip()}
        jobs = [j for j in jobs if j.status in wanted]
    if q and q.strip():
        needle = q.strip().lower()
        jobs = [j for j in jobs if needle in (
            f"{j.label or ''} {j.script_name} {j.script_id} {j.log_name} "
            f"{j.id} {j.chain_id or ''}"
        ).lower()]
    # total is post-filter so "x of y" stays honest while paging a search
    total = len(jobs)
    return {"jobs": jobs[offset:offset + limit], "total": total}


@router.get("/{job_id}")
def get_job(job_id: str):
    return _job_detail(_get_job_or_404(job_id))


@router.get("/{job_id}/stdout", response_class=PlainTextResponse)
def get_job_stdout(job_id: str):
    _get_job_or_404(job_id)
    return PlainTextResponse(store.read(job_id, "stdout"), media_type="text/plain")


@router.get("/{job_id}/stderr", response_class=PlainTextResponse)
def get_job_stderr(job_id: str):
    _get_job_or_404(job_id)
    return PlainTextResponse(store.read(job_id, "stderr"), media_type="text/plain")


@router.get("/{job_id}/input", response_class=PlainTextResponse)
def get_job_input(job_id: str):
    """What the script actually received: the log, or the previous chain step's output.

    Capped like stdout (20 MB); 404 when the source file is gone (deleted log,
    pruned job data).
    """
    job = _get_job_or_404(job_id)
    if job.status == "waiting":
        # A waiting chain step has no input yet — and the shared decompressed
        # cache may already hold the *log's* copy from a sibling job, which
        # must not leak in as this step's input (it will read the previous
        # step's output, not the log).
        raise HTTPException(status_code=404, detail="Input file is not available")
    path = executor.input_path(job)
    text = store.read_path(path) if path else None
    if text is None:
        # waiting steps have no input yet; a deleted log or pruned data is gone forever
        raise HTTPException(status_code=404, detail="Input file is not available")
    return PlainTextResponse(text, media_type="text/plain")


@router.get("/{job_id}/files/{filename}")
def get_job_file(job_id: str, filename: str):
    _get_job_or_404(job_id)
    if "/" in filename or "\\" in filename or filename in (".", ".."):
        raise HTTPException(status_code=400, detail="Invalid file name")
    path = store.outputs_dir(job_id) / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return FileResponse(path, media_type=media_type, filename=filename)


@router.post("/{job_id}/cancel")
async def cancel_job(job_id: str):
    job = _get_job_or_404(job_id)
    if job.chain_id:
        ok = await executor.cancel_chain(job.chain_id)
    else:
        ok = await executor.cancel(job_id)
    if not ok:
        raise HTTPException(status_code=409, detail="Job is not running or queued")
    return {"ok": True}


@router.delete("/{job_id}")
async def delete_job(job_id: str):
    job = _get_job_or_404(job_id)
    if job.chain_id:
        # Deleting a step of a still-live chain cancels the whole chain:
        # later steps may be consuming (or waiting on) this step's output.
        alive = any(s.status in ("queued", "waiting", "running")
                    for s in store.list_chain(job.chain_id))
        if alive:
            await executor.cancel_chain(job.chain_id)
    elif job.status in ("queued", "running"):
        await executor.cancel(job_id)
    store.delete(job_id)
    return {"ok": True}


@router.post("/{job_id}/rerun")
async def rerun_job(job_id: str, from_step: int | None = Query(None, ge=1)):
    """Re-run a job. For chains: the whole chain, or only from `from_step` on.

    `from_step` (1-based step number) re-runs only that step and the ones
    after it, seeding its input with the persisted output of the preceding
    completed step — earlier steps are not executed again.
    """
    job = _get_job_or_404(job_id)

    if job.chain_id:
        old_steps = store.list_chain(job.chain_id)
        log = _resolve_log(old_steps[0].log_path) if old_steps[0].log_path else None
        scripts = {s.id: s for s in discover_scripts()}
        # Validate every step before creating anything — a missing script
        # must not leave half-created jobs behind.
        plan: list[tuple[ScriptInfo, dict]] = []
        for step in old_steps:
            script = scripts.get(step.script_id)
            if script is None or script.error:
                raise HTTPException(
                    status_code=404,
                    detail=f"Script no longer available: {step.script_id}")
            plan.append((script, _validate_params(script, step.params)))

        original_size = old_steps[0].chain_size or len(old_steps)
        selected = list(zip(plan, old_steps))  # (script+params, old step)
        seed: tuple[str, str] | None = None   # (input_path, input_source)

        if from_step is not None and from_step > 1:
            if from_step > original_size:
                raise HTTPException(status_code=400, detail=(
                    f"from_step={from_step} is beyond the chain size "
                    f"({original_size})"))
            target = from_step - 1  # 0-based chain_index to restart from
            selected = [(p, s) for p, s in selected if s.chain_index >= target]
            if not selected:
                raise HTTPException(status_code=400, detail=(
                    f"Step {from_step} no longer exists in this chain"))
            preceding = [s for s in old_steps if s.chain_index < target]
            if preceding:
                prev = preceding[-1]  # nearest surviving earlier step
                if prev.status != "completed":
                    raise HTTPException(status_code=409, detail=(
                        f"Cannot continue from step {from_step}: step "
                        f"{prev.chain_index + 1} is {prev.status}. Only a "
                        "completed step can seed a partial rerun — rerun the "
                        "full chain instead."))
                input_path, source = executor.chain_output(prev)
                if input_path is None:
                    raise HTTPException(status_code=409, detail=(
                        f"Cannot continue from step {from_step}: step "
                        f"{prev.chain_index + 1} produced no output to reuse."))
                seed = (str(input_path), source)
            # No surviving earlier step (deleted): start from the log.

        # A re-run replaces the chain: stop the old one if still alive.
        if any(s.status in ("queued", "waiting", "running") for s in old_steps):
            await executor.cancel_chain(job.chain_id)

        new_steps: list[tuple[Job, ScriptInfo]] = [
            (store.create(
                script_id=script.id,
                script_name=script.name,
                log_path=str(log) if log else "",
                log_name=_display_log_name(log) if log else "",
                params=params,
                label=job.label,
            ), script)
            for script, params in [p for p, _s in selected]
        ]
        first = new_steps[0][0]
        if seed:
            first.input_path, first.input_source = seed
        executor.submit_chain(
            new_steps, f"chain-{uuid.uuid4().hex[:10]}",
            start_index=selected[0][1].chain_index, total=original_size)
        return {"job": first}

    log = _resolve_log(job.log_path) if job.log_path else None
    scripts = {s.id: s for s in discover_scripts()}
    script = scripts.get(job.script_id)
    if script is None or script.error:
        raise HTTPException(status_code=404, detail=f"Script no longer available: {job.script_id}")
    params = _validate_params(script, job.params)
    new_job = store.create(
        script_id=script.id,
        script_name=script.name,
        log_path=str(log) if log else "",
        log_name=_display_log_name(log) if log else "",
        params=params,
        label=job.label,
    )
    executor.submit(new_job, script)
    return {"job": new_job}


@router.post("/prune")
def prune_jobs(days: int | None = Query(None, ge=1, le=3650)):
    """Manually delete jobs older than `days` (default: JOB_RETENTION_DAYS)."""
    effective = days if days is not None else JOB_RETENTION_DAYS
    if effective <= 0:
        raise HTTPException(status_code=400, detail="Retention is disabled (JOB_RETENTION_DAYS=0); pass ?days=N")
    removed = store.prune(effective)
    cache_removed = decompress_cache.prune()
    return {"removed": removed, "retention_days": effective,
            "cache_removed": cache_removed}


@router.get("/{job_id}/events")
async def job_events(job_id: str):
    """Server-sent events stream for a job: status changes + stdout deltas."""
    _get_job_or_404(job_id)

    async def generator():
        last_status = None
        # Catch up with the last 64 KB, then stream only new bytes.
        path = store.stdout_path(job_id)
        try:
            size = path.stat().st_size if path.exists() else 0
        except OSError:
            size = 0
        offset = max(0, size - 65536)

        while True:
            job = store.get(job_id)
            if job is None:
                yield 'data: {"type": "gone"}\n\n'
                return

            # Stream any stdout we have not sent yet.
            if path.exists():
                try:
                    with path.open("rb") as fh:
                        fh.seek(offset)
                        chunk = fh.read()
                    if chunk:
                        offset += len(chunk)
                        text = chunk.decode(errors="replace")
                        # "data: " prefix is required — without it EventSource
                        # silently drops the frame and the UI never sees stdout
                        yield f"data: {json.dumps({'type': 'stdout', 'text': text})}\n\n"
                except OSError:
                    pass

            if job.status != last_status:
                last_status = job.status
                payload = {
                    "type": "status",
                    "status": job.status,
                    "error": job.error,
                    "exit_code": job.exit_code,
                    "started_at": job.started_at,
                    "finished_at": job.finished_at,
                    "stdout_size": job.stdout_size,
                    "stderr_size": job.stderr_size,
                    "output_files": job.output_files,
                    "stderr_tail": store.tail(job_id, "stderr", 4096),
                }
                yield f"data: {json.dumps(payload)}\n\n"
                if job.status not in ("queued", "running", "waiting"):
                    return

            await asyncio.sleep(0.4)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
