"""Log file listing and preview endpoints."""
from __future__ import annotations

import bz2
import gzip
import lzma
import os
from collections import deque
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

# Same character budget in both directions so a preview can never blow up
# the browser, regardless of line lengths.
_MAX_PREVIEW_CHARS = 2_000_000


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


def _head_lines(p: Path, opener, max_lines: int):
    """First lines — stops reading as soon as the budget is satisfied."""
    collected: list[str] = []
    truncated = False
    total_chars = 0
    fh = opener(p, "rt", encoding="utf-8", errors="replace") if opener \
        else p.open(encoding="utf-8", errors="replace")
    with fh:
        for line in fh:
            if len(collected) >= max_lines or total_chars > _MAX_PREVIEW_CHARS:
                truncated = True
                break
            collected.append(line.rstrip("\r\n"))
            total_chars += len(line)
    return collected, truncated


def _tail_plain(p: Path, max_lines: int):
    """Last lines of an uncompressed file, by seeking backwards from EOF.

    Only the bytes actually needed are read — a multi-gigabyte log costs
    the same as a small one.
    """
    block_size = 64 * 1024
    truncated = False
    with p.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        remaining = fh.tell()
        buf = b""
        while remaining > 0:
            read_size = min(block_size, remaining)
            remaining -= read_size
            fh.seek(remaining)
            buf = fh.read(read_size) + buf
            # max_lines + 1 newlines guarantees the top retained line is whole
            if buf.count(b"\n") > max_lines or len(buf) > _MAX_PREVIEW_CHARS:
                truncated = True
                break
        text = buf.decode("utf-8", errors="replace")
        if truncated:
            # the buffer starts mid-line: drop that partial line
            nl = text.find("\n")
            text = text[nl + 1:] if nl != -1 else ""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()  # trailing newline, not a real line
    # e.g. 5 lines without a trailing newline carry only 4 "\n" — the loop
    # above can't see that the 5th line exists until the slice drops it
    if len(lines) > max_lines:
        truncated = True
    return [line.rstrip("\r") for line in lines][-max_lines:], truncated


def _tail_compressed(p: Path, opener, max_lines: int):
    """Last lines of a compressed file — streamed, bounded memory.

    Compressed streams can't be seeked, so the file is decompressed
    sequentially but only the final window of lines is kept.
    """
    window: deque[str] = deque(maxlen=max_lines)
    seen = 0
    with opener(p, "rt", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            seen += 1
            window.append(line.rstrip("\r\n"))
    # enforce the character budget from the tail side
    kept: list[str] = []
    used = 0
    truncated = seen > len(window)
    for line in reversed(window):
        if used + len(line) > _MAX_PREVIEW_CHARS:
            truncated = True
            break
        kept.append(line)
        used += len(line)
    kept.reverse()
    return kept, truncated


@router.get("/preview")
def preview_log(
    path: str,
    lines: int = Query(LOG_PREVIEW_LINES, ge=1, le=2000),
    tail: bool = Query(False, description="show the last lines instead of the first"),
):
    p = _resolve_log(path)
    opener = _DECOMPRESSORS.get(p.suffix.lower())
    if tail:
        if opener is None:
            collected, truncated = _tail_plain(p, lines)
        else:
            collected, truncated = _tail_compressed(p, opener, lines)
    else:
        collected, truncated = _head_lines(p, opener, lines)
    return {
        "name": p.name,
        "path": str(p),
        "size": p.stat().st_size,
        "compressed": opener is not None,
        "lines": collected,
        "truncated": truncated,
    }
