"""Script discovery and management endpoints.

The editor endpoints write ordinary flat-style scripts (code file + sidecar
manifest) into SCRIPTS_DIR — the same files a user could create by hand.
Writing scripts is within the trust model: scripts are arbitrary code running
as the server user by design (see ARCHITECTURE.md §9), and ids/extensions
are constrained so a request can never escape SCRIPTS_DIR.

When that trust is not shared — other people have the password — set
LOGSMITH_ALLOW_SCRIPT_EDITING=0: the write endpoints below then answer 403,
and reads (list, source view, export, running jobs) keep working.
"""
from __future__ import annotations

import io
import json
import re
import shutil
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from fastapi import APIRouter, File, HTTPException, Response, UploadFile

from ..config import SCRIPTS_DIR, ALLOW_SCRIPT_EDITING
from ..models import ScriptUpsertRequest
from ..services.scanner import DEFAULT_INTERPRETERS, discover_scripts

router = APIRouter(prefix="/scripts", tags=["scripts"])

_SAFE_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")


def _require_editing_enabled() -> None:
    """403 unless script authoring is allowed on this server."""
    if not ALLOW_SCRIPT_EDITING:
        raise HTTPException(
            status_code=403,
            detail="Script editing is disabled on this server "
                   "(LOGSMITH_ALLOW_SCRIPT_EDITING=0) — scripts on disk can be "
                   "run, but not created, changed or deleted",
        )


def _write_code(code_path: Path, code: str) -> None:
    """Write a script's code file, executable.

    A command template may run the file directly (`{script} {log}` without
    an interpreter); write_text's default 644 mode would then fail the job
    with EACCES. Interpreted invocation (python3 {script} …) only needs the
    read bit, so 755 is safe either way.

    CRLF is normalized to LF: text pasted from Windows tools carries
    carriage returns, and one at the end of the shebang line breaks
    direct execution ('bash\\r': No such file or directory).
    """
    code_path.write_text(code.replace("\r\n", "\n"), encoding="utf-8")
    code_path.chmod(0o755)

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
    req.manifest.category = req.manifest.category.strip()
    manifest = req.manifest.model_dump(exclude_none=True)
    if not manifest["category"]:
        del manifest["category"]  # keep manifest files free of empty labels
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
            "category": script.category,
            "command": script.command,
            "timeout": script.timeout,
            "parameters": [p.model_dump(exclude_none=True) for p in script.parameters],
            "output_filename": script.output_filename,
        },
    }


@router.post("")
def create_script(req: ScriptUpsertRequest):
    _require_editing_enabled()
    if _find(req.id) is not None:
        raise HTTPException(status_code=409, detail=f"A script named '{req.id}' already exists")
    manifest = _validate(req)
    code_path = SCRIPTS_DIR / f"{req.id}.{req.extension}"
    manifest_path = SCRIPTS_DIR / f"{req.id}.json"
    try:
        SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
        _write_code(code_path, req.code)
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
    _require_editing_enabled()
    if req.id != script_id:
        raise HTTPException(status_code=422, detail="Body id and path id don't match")
    script = _get_flat_or_404(script_id)
    manifest = _validate(req)
    old_code_path = Path(script.script_path)
    code_path = SCRIPTS_DIR / f"{script_id}.{req.extension}"
    manifest_path = SCRIPTS_DIR / f"{script_id}.json"
    try:
        _write_code(code_path, req.code)
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        if code_path != old_code_path:
            old_code_path.unlink(missing_ok=True)  # extension changed
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to write script: {exc}")
    return {"script": _find(script_id)}


@router.delete("/{script_id}")
def delete_script(script_id: str):
    _require_editing_enabled()
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


# --- import (the export's counterpart) ---------------------------------------
#
# One tar.gz in, one or more scripts out. Two archive shapes, decided by
# the content: a top-level manifest.json means one directory-style script
# (the whole tree lands in scripts/<id>/ — the export flattens the original
# directory name away, so the id comes from ?name= or the file name);
# otherwise the top level must be flat scripts (code + sidecar pairs, a
# pack of several allowed). Everything is validated before anything is
# written: an import either lands whole or answers an error.

_MAX_IMPORT_ARCHIVE_BYTES = 50 * 1024 * 1024   # the uploaded tar.gz itself
_MAX_IMPORT_EXTRACT_BYTES = 100 * 1024 * 1024  # total unpacked content
_MAX_IMPORT_FILES = 2000                       # members in one archive


def _strip_archive_suffix(filename: str) -> str:
    """'backup.tar.gz' -> 'backup' (the id a directory-style import wants)."""
    raw = (filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    return re.sub(r"\.(tar\.gz|tgz|tar)$", "", raw, flags=re.IGNORECASE)


def _safe_member_name(name: str) -> bool:
    """A tar member we are willing to extract: relative, no escape, no weird."""
    if not name or "\\" in name or name.startswith("/"):
        return False
    parts = PurePosixPath(name).parts
    return bool(parts) and all(part not in ("", "..") for part in parts)


def _extract_to_staging(data: bytes) -> Path:
    """Validate every member, unpack the regular files into a temp dir.

    Only regular files are ever extracted (the `data` filter blocks links,
    devices and escapes as a second line of defense); RESTORE.txt — the
    export's note to a human — never lands anywhere.
    """
    try:
        tar = tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")
    except tarfile.TarError:
        raise HTTPException(422, detail="Not a gzip tar archive — expected a .tar.gz from Export (or a pack of flat scripts)")
    staging = Path(tempfile.mkdtemp(prefix="logsmith-import-"))
    try:
        with tar:
            total = 0
            members_seen = 0
            for member in tar:
                if member.name == "RESTORE.txt":
                    continue
                members_seen += 1
                if members_seen > _MAX_IMPORT_FILES:
                    raise HTTPException(413, detail="Too many files in the archive (over 2000)")
                if member.isdir():
                    if not _safe_member_name(member.name):
                        raise HTTPException(422, detail=f"Bad path in archive: {member.name!r}")
                    continue
                if not member.isfile():
                    raise HTTPException(422, detail=f"Only regular files can be imported — {member.name!r} is not one")
                if not _safe_member_name(member.name):
                    raise HTTPException(422, detail=f"Bad path in archive: {member.name!r}")
                if member.size < 0:
                    raise HTTPException(422, detail=f"Bad size in archive for {member.name!r}")
                total += member.size
                if total > _MAX_IMPORT_EXTRACT_BYTES:
                    raise HTTPException(413, detail="The archive unpacks to more than 100 MiB — scripts don't")
                if (staging / member.name).exists():  # duplicate member names
                    raise HTTPException(422, detail=f"Duplicate path in archive: {member.name!r}")
                tar.extract(member, staging, filter="data")
    except HTTPException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    except (tarfile.TarError, OSError) as exc:
        shutil.rmtree(staging, ignore_errors=True)
        raise HTTPException(422, detail=f"Could not read the archive: {exc}")
    return staging


def _flat_scripts_in(staging: Path) -> list[tuple[str, Path, Path]]:
    """(id, code path, sidecar path) for every flat script at the top level.

    Mirrors the scanner's discovery rules: a code file is a script when it
    has a same-stem .json sidecar or a known interpreter extension; every
    sidecar must find its code file, and every stem must be a safe id.
    """
    entries = sorted(staging.iterdir())
    for entry in entries:
        if entry.is_dir():
            raise HTTPException(
                422,
                detail="Directories inside the archive need a top-level manifest.json "
                       "(directory-style script) — flat scripts are code + sidecar pairs",
            )
    code_files = [e for e in entries if e.suffix.lower() != ".json"]
    sidecars = {e.stem: e for e in entries if e.suffix.lower() == ".json"}

    by_stem: dict[str, list[Path]] = {}
    for code in code_files:
        by_stem.setdefault(code.stem, []).append(code)

    scripts: list[tuple[str, Path, Path]] = []
    for stem, codes in by_stem.items():
        if len(codes) > 1:
            raise HTTPException(422, detail=f"Ambiguous script '{stem}': multiple code files ({', '.join(c.name for c in codes)})")
        code = codes[0]
        sidecar = sidecars.get(stem)
        if not sidecar and code.suffix.lower() not in DEFAULT_INTERPRETERS:
            raise HTTPException(422, detail=f"'{code.name}' has no .json sidecar and '{code.suffix}' is not a known script type — it would be invisible to the scanner")
        if sidecar is not None:
            try:
                json.loads(sidecar.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise HTTPException(422, detail=f"Manifest '{sidecar.name}' is not valid JSON: {exc}")
        scripts.append((stem, code, sidecar))
    orphans = [path.name for stem, path in sidecars.items() if stem not in by_stem]
    if orphans:
        raise HTTPException(422, detail=f"Manifest without its script: {', '.join(sorted(orphans))}")
    for stem, _, _ in scripts:
        if not _SAFE_ID.match(stem):
            raise HTTPException(422, detail=f"Script id '{stem}' is not importable — ids start with a letter/digit and use letters, digits, '-' or '_' (rename the files, or use ?name=)")
    return scripts


def _remove_existing(script) -> None:
    """Delete the files an existing script occupies (overwrite path)."""
    code_path = Path(script.script_path)
    if script.style == "directory":
        script_dir = code_path.parent
        # paranoia: only ever rmtree a direct child of SCRIPTS_DIR
        if script_dir.parent == SCRIPTS_DIR and script_dir.name == script.id:
            shutil.rmtree(script_dir, ignore_errors=True)
        return
    code_path.unlink(missing_ok=True)
    code_path.with_suffix(".json").unlink(missing_ok=True)


@router.post("/import")
async def import_scripts(file: UploadFile = File(...), name: str = "",
                         overwrite: bool = False):
    """Import script(s) from a tar.gz — the export format, or a pack of them.

    A top-level manifest.json makes it one directory-style script (id from
    ?name= or the file name — the export flattens the original directory
    name away); otherwise the top level must be flat scripts (code +
    sidecar pairs, several allowed). Existing ids are a 409 unless
    ?overwrite=1 replaces them first. All-or-nothing: nothing is written
    until everything validates.
    """
    _require_editing_enabled()

    data = bytearray()
    while chunk := await file.read(1024 * 1024):
        data.extend(chunk)
        if len(data) > _MAX_IMPORT_ARCHIVE_BYTES:
            raise HTTPException(413, detail="The archive exceeds the 50 MiB import limit")
    if not data:
        raise HTTPException(422, detail="The uploaded file is empty")

    staging = _extract_to_staging(bytes(data))
    try:
        # classify + plan
        written: list[Path] = []
        if (staging / "manifest.json").is_file():
            try:
                json.loads((staging / "manifest.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise HTTPException(422, detail=f"The manifest.json is not valid JSON: {exc}")
            script_id = (name or "").strip() or _strip_archive_suffix(file.filename or "")
            if not _SAFE_ID.match(script_id):
                raise HTTPException(422, detail="A directory-style import needs a valid id — pass ?name=<id> (letters, digits, '-', '_')")
            plan = [("directory", script_id, staging)]
        else:
            plan = [("flat", stem, code) for stem, code, _ in _flat_scripts_in(staging)]
        if not plan:
            raise HTTPException(422, detail="No scripts found in the archive — expected a tar.gz from Export (code + sidecar pairs, or a directory-style script with manifest.json)")

        existing = {s.id: s for s in discover_scripts()}
        conflicts = [sid for kind, sid, _ in plan if sid in existing]
        if conflicts and not overwrite:
            raise HTTPException(409, detail=f"Script(s) already exist: {', '.join(conflicts)} — pass ?overwrite=1 (or the checkbox in the UI) to replace them")

        # write (only after everything above validated)
        try:
            SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
            for kind, sid, source in plan:
                if kind == "directory":
                    target = SCRIPTS_DIR / sid
                    if sid in existing:
                        _remove_existing(existing[sid])
                    if target.exists():
                        # not a discovered script (no manifest.json?) — some
                        # other content owns this name; never bury or mix
                        raise HTTPException(409, detail=f"'{target}' already exists on disk but is not a script the scanner knows — resolve it by hand first")
                    shutil.move(str(source), str(target))
                    written.append(target)
                else:
                    if sid in existing:
                        _remove_existing(existing[sid])
                    code = source.read_text(encoding="utf-8")
                    code_path = SCRIPTS_DIR / source.name
                    _write_code(code_path, code)
                    written.append(code_path)
                    sidecar = source.with_suffix(".json")
                    if sidecar.is_file():
                        manifest_path = code_path.with_suffix(".json")
                        manifest_path.write_text(sidecar.read_text(encoding="utf-8"), encoding="utf-8")
                        written.append(manifest_path)
        except (OSError, UnicodeDecodeError) as exc:
            for path in written:  # never leave a half-written import behind
                if path.is_dir():
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    path.unlink(missing_ok=True)
            raise HTTPException(500, detail=f"Failed to write the imported script: {exc}")

        discovered = [s for s in discover_scripts() if any(sid == s.id for _, sid, _ in plan)]
        for script in discovered:
            # the editor's contract: a script file is executable (direct
            # execution needs it; interpreted invocation only needs read)
            if script.style == "directory":
                try:
                    Path(script.script_path).chmod(0o755)
                except OSError:
                    pass
        return {"imported": [sid for _, sid, _ in plan], "scripts": discovered}
    finally:
        shutil.rmtree(staging, ignore_errors=True)
