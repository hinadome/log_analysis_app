"""Discovery of log files and analysis scripts.

Script conventions
------------------
Flat style:  scripts/foo.py + optional sidecar manifest scripts/foo.json.
             Without a sidecar, a sensible default command is derived from the
             file extension.

Directory style: scripts/foo/ with a manifest.json inside. The manifest may
             name the entry file via "entry"; otherwise the first non-manifest
             file in the directory is used.

Manifest fields: name, description, command, timeout, parameters,
             output_filename. See README.md for the full reference.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from ..config import LOGS_DIR, SCRIPTS_DIR, DEFAULT_TIMEOUT
from ..models import LogFile, ParamSpec, ScriptInfo, ScriptManifest

# Interpreter used for the default command template when no manifest exists.
DEFAULT_INTERPRETERS = {
    ".py": "python3",
    ".sh": "bash",
    ".bash": "bash",
    ".zsh": "zsh",
    ".js": "node",
    ".rb": "ruby",
    ".pl": "perl",
}

_HIDDEN = {".", "_"}


def _default_command(script_path: Path) -> str:
    interpreter = DEFAULT_INTERPRETERS.get(script_path.suffix.lower())
    if interpreter:
        return f"{interpreter} {{script}} {{log}} {{args}}"
    return "{script} {log} {args}"


def _parse_manifest(data: dict) -> ScriptManifest:
    params = [ParamSpec(**p) for p in data.get("parameters") or []]
    return ScriptManifest(
        name=str(data.get("name") or ""),
        description=str(data.get("description") or ""),
        command=str(data.get("command") or ""),
        timeout=int(data.get("timeout") or DEFAULT_TIMEOUT),
        parameters=params,
        entry=data.get("entry"),
        output_filename=data.get("output_filename"),
    )


def _load_manifest(path: Path) -> ScriptManifest:
    return _parse_manifest(json.loads(path.read_text(encoding="utf-8")))


def _to_info(script_id: str, manifest: ScriptManifest, script_path: Path, style: str,
             error: str | None = None) -> ScriptInfo:
    return ScriptInfo(
        id=script_id,
        name=manifest.name or script_id,
        description=manifest.description,
        command=manifest.command or _default_command(script_path),
        timeout=manifest.timeout,
        parameters=manifest.parameters,
        script_path=str(script_path),
        style=style,
        output_filename=manifest.output_filename,
        error=error,
    )


def discover_scripts() -> list[ScriptInfo]:
    """Find every usable script in SCRIPTS_DIR."""
    if not SCRIPTS_DIR.is_dir():
        return []

    infos: list[ScriptInfo] = []
    for entry in sorted(SCRIPTS_DIR.iterdir()):
        if entry.name.startswith(tuple(_HIDDEN)):
            continue

        if entry.is_file() and entry.suffix.lower() != ".json":
            sidecar = entry.with_suffix(".json")
            if sidecar.exists():
                try:
                    manifest = _load_manifest(sidecar)
                except Exception as exc:
                    infos.append(_to_info(entry.stem, ScriptManifest(name=entry.stem),
                                          entry, "flat", error=f"bad manifest: {exc}"))
                    continue
            elif entry.suffix.lower() in DEFAULT_INTERPRETERS:
                manifest = ScriptManifest(name=entry.stem, timeout=DEFAULT_TIMEOUT)
            else:
                continue  # unknown type without a manifest -> ignore
            infos.append(_to_info(entry.stem, manifest, entry, "flat"))

        elif entry.is_dir():
            manifest_file = entry / "manifest.json"
            if not manifest_file.exists():
                continue
            try:
                manifest = _load_manifest(manifest_file)
            except Exception as exc:
                infos.append(_to_info(entry.name, ScriptManifest(name=entry.name),
                                      entry, "directory", error=f"bad manifest: {exc}"))
                continue
            entry_file = entry / manifest.entry if manifest.entry else _first_entry(entry)
            if entry_file is None or not entry_file.is_file():
                infos.append(_to_info(entry.name, manifest, entry, "directory",
                                      error="manifest has no usable entry file"))
                continue
            infos.append(_to_info(entry.name, manifest, entry_file, "directory"))

    return infos


def _first_entry(directory: Path) -> Path | None:
    for child in sorted(directory.iterdir()):
        if child.is_file() and not child.name.startswith(tuple(_HIDDEN)) and child.name != "manifest.json":
            return child
    return None


def list_logs() -> list[LogFile]:
    """List log files (recursively) under LOGS_DIR, newest first."""
    if not LOGS_DIR.is_dir():
        return []

    files: list[Path] = []
    for path in LOGS_DIR.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(LOGS_DIR)
        if any(part.startswith(tuple(_HIDDEN)) for part in rel.parts):
            continue
        files.append(path)

    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [
        LogFile(
            name=str(p.relative_to(LOGS_DIR)),
            path=str(p),
            size=p.stat().st_size,
            modified=datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc).isoformat(),
        )
        for p in files
    ]
