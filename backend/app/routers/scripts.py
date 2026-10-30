"""Script discovery and management endpoints.

The editor endpoints write ordinary flat-style scripts (code file + sidecar
manifest) into SCRIPTS_DIR — the same files a user could create by hand.
Writing scripts is within the trust model: scripts are arbitrary code running
as the server user by design (see ARCHITECTURE.md §9), and ids/extensions
are constrained so a request can never escape SCRIPTS_DIR.
"""
from __future__ import annotations

import io
import json
import re
import tarfile
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Response

from ..config import SCRIPTS_DIR
from ..models import ScriptUpsertRequest
from ..services.scanner import discover_scripts

router = APIRouter(prefix="/scripts", tags=["scripts"])

_SAFE_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")

# Default command template per extension when the manifest leaves it blank.
_DEFAULT_COMMAND = {
    "py": "python3 {script} {log} {args}",
    "sh": "bash {script} {log} {args}",
    "bash": "bash {script} {log} {args}",
    "zsh": "zsh {script} {log} {args}",
    "js": "node {script} {log} {args}",
    "rb": "ruby {script} {log} {args}",
    "pl": "perl {script} {log} {args}",
}


@router.get("")
def get_scripts():
    return {"scripts": discover_scripts()}


def _find(script_id: str):
    for script in discover_scripts():
        if script.id == script_id:
            return script
    return None


def _get_flat_or_404(script_id: str):
    script = _find(script_id)
    if script is None:
        raise HTTPException(status_code=404, detail=f"Script not found: {script_id}")
    if script.style != "flat":
        raise HTTPException(
            status_code=400,
            detail="Directory-style scripts can't be edited in the UI — edit the files directly",
        )
    # A broken manifest is exactly what the editor is for: the manifest
    # reported below is the parsed default, and saving rewrites a clean one.
    return script


def _validate(req: ScriptUpsertRequest) -> dict:
    """Shared checks for create/update; returns the manifest dict to persist."""
    if not _SAFE_ID.match(req.id):
        raise HTTPException(status_code=422, detail=(
            "Script id must start with a letter/digit and contain only "
            "letters, digits, '-' or '_' (max 64 chars)"))
    if not req.code.strip():
        raise HTTPException(status_code=422, detail="Script code is empty")
    if not (1 <= req.manifest.timeout <= 86400):
        raise HTTPException(status_code=422, detail="timeout must be 1–86400 seconds")
    manifest = req.manifest.model_dump(exclude_none=True)
    if not manifest.get("command", "").strip():
        manifest["command"] = _DEFAULT_COMMAND[req.extension]
    return manifest


@router.get("/{script_id}")
def get_script_source(script_id: str):
    """Code + manifest for the UI editor (flat scripts only)."""
    script = _get_flat_or_404(script_id)
    code_path = Path(script.script_path)
    try:
        code = code_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to read script: {exc}")
    return {
        "id": script_id,
        "extension": code_path.suffix.lstrip("."),
        "code": code,
        # the manifest is reported as-discovered (parsed + defaults), so a
        # broken sidecar still opens in the editor and saving fixes it
        "manifest": {
            "name": script.name,
            "description": script.description,
            "command": script.command,
            "timeout": script.timeout,
            "parameters": [p.model_dump(exclude_none=True) for p in script.parameters],
            "output_filename": script.output_filename,
        },
    }


@router.post("")
def create_script(req: ScriptUpsertRequest):
    if _find(req.id) is not None:
        raise HTTPException(status_code=409, detail=f"A script named '{req.id}' already exists")
    manifest = _validate(req)
    code_path = SCRIPTS_DIR / f"{req.id}.{req.extension}"
    manifest_path = SCRIPTS_DIR / f"{req.id}.json"
    try:
        SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
        code_path.write_text(req.code, encoding="utf-8")
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        # never leave a half-written pair behind
        for path in (code_path, manifest_path):
            try:
                path.unlink()
            except OSError:
                pass
        raise HTTPException(status_code=500, detail=f"Failed to write script: {exc}")
    return {"script": _find(req.id)}


@router.put("/{script_id}")
def update_script(script_id: str, req: ScriptUpsertRequest):
    if req.id != script_id:
        raise HTTPException(status_code=422, detail="Body id and path id don't match")
    script = _get_flat_or_404(script_id)
    manifest = _validate(req)
    old_code_path = Path(script.script_path)
    code_path = SCRIPTS_DIR / f"{script_id}.{req.extension}"
    manifest_path = SCRIPTS_DIR / f"{script_id}.json"
    try:
        code_path.write_text(req.code, encoding="utf-8")
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        if code_path != old_code_path:
            old_code_path.unlink(missing_ok=True)  # extension changed
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to write script: {exc}")
    return {"script": _find(script_id)}


@router.delete("/{script_id}")
def delete_script(script_id: str):
    script = _get_flat_or_404(script_id)
    code_path = Path(script.script_path)
    try:
        code_path.unlink(missing_ok=True)
        code_path.with_suffix(".json").unlink(missing_ok=True)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to delete script: {exc}")
    return {"ok": True}


@router.get("/{script_id}/export")
def export_script(script_id: str):
    """Download a script as a tar.gz — code + manifest (+ support files)."""
    script = _find(script_id)
    if script is None:
        raise HTTPException(status_code=404, detail=f"Script not found: {script_id}")

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        code_path = Path(script.script_path)
        if script.style == "flat":
            tar.add(code_path, arcname=code_path.name)
            manifest_path = code_path.with_suffix(".json")
            if manifest_path.is_file():
                tar.add(manifest_path, arcname=manifest_path.name)
            how_to = (
                f"Flat-style script: place {code_path.name} and "
                f"{manifest_path.name} side by side in a scripts/ directory.\n"
                f"The script is discovered on the next API request (no restart needed).\n"
            )
        else:
            script_dir = code_path.parent
            for member in sorted(script_dir.rglob("*")):
                if member.is_file():
                    tar.add(member, arcname=str(member.relative_to(script_dir)))
            how_to = (
                "Directory-style script: place the whole extracted directory "
                "inside a scripts/ directory.\n"
                "The script is discovered on the next API request (no restart needed).\n"
            )
        readme = (
            f"LogSmith script export — {script.name} ({script.id})\n"
            f"Exported {datetime.now(timezone.utc).isoformat()}\n\n"
            f"{how_to}"
        ).encode("utf-8")
        info = tarfile.TarInfo("RESTORE.txt")
        info.size = len(readme)
        info.mtime = int(datetime.now(timezone.utc).timestamp())
        tar.addfile(info, io.BytesIO(readme))

    return Response(
        content=buf.getvalue(),
        media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{script_id}.tar.gz"'},
    )
