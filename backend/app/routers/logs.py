"""Log file listing and preview endpoints."""
from __future__ import annotations

import bz2
import gzip
import lzma
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

from ..config import LOGS_DIR, LOG_PREVIEW_LINES
from ..services.scanner import list_logs

router = APIRouter(prefix="/logs", tags=["logs"])

_DECOMPRESSORS = {
    ".gz": gzip.open,
    ".bz2": bz2.open,
    ".xz": lzma.open,
}


def _resolve_log(user_path: str) -> Path:
    """Resolve a user-supplied path and ensure it stays inside LOGS_DIR."""
    p = Path(user_path)
    if not p.is_absolute():
        p = LOGS_DIR / p
    p = p.resolve()
    base = LOGS_DIR.resolve()
    if p != base and base not in p.parents:
        raise HTTPException(status_code=400, detail="Path escapes the logs directory")
    if not p.is_file():
        raise HTTPException(status_code=404, detail="Log file not found")
    return p


@router.get("")
def get_logs():
    return {"logs": list_logs()}


@router.get("/preview")
def preview_log(path: str, lines: int = Query(LOG_PREVIEW_LINES, ge=1, le=2000)):
    p = _resolve_log(path)
    collected: list[str] = []
    truncated = False
    total_chars = 0
    opener = _DECOMPRESSORS.get(p.suffix.lower())
    fh = opener(p, "rt", encoding="utf-8", errors="replace") if opener \
        else p.open(encoding="utf-8", errors="replace")
    with fh:
        for line in fh:
            if len(collected) >= lines or total_chars > 2_000_000:
                truncated = True
                break
            collected.append(line.rstrip("\r\n"))
            total_chars += len(line)
    return {
        "name": p.name,
        "path": str(p),
        "size": p.stat().st_size,
        "compressed": opener is not None,
        "lines": collected,
        "truncated": truncated,
    }
