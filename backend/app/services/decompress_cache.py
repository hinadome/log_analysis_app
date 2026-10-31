"""Shared decompressed copies of compressed inputs (.gz/.bz2/.xz).

A compressed log used to cost one decompressed copy *per job*: a fan-out
of N scripts on one .gz wrote N copies into the job directories. The
cache keeps exactly one copy per unique source version — (resolved path,
size, mtime_ns) — under DATA_DIR/cache/decompressed/, and every job,
rerun and the Input tab read that same file.

Entries are self-describing via a `<key>.json` sidecar (source path,
size, mtime). prune() drops entries whose source is gone or has changed
underneath them; anything else is kept because a rerun on the unchanged
source should stay free.
"""
from __future__ import annotations

import gzip
import bz2
import lzma
import hashlib
import json
import os
import shutil
import uuid
from pathlib import Path

from ..config import DATA_DIR

CACHE_DIR = DATA_DIR / "cache" / "decompressed"

DECOMPRESSORS = {
    ".gz": gzip.open,
    ".bz2": bz2.open,
    ".xz": lzma.open,
}


def _key(source: Path, size: int, mtime_ns: int) -> str:
    return hashlib.sha256(
        f"{source.resolve()}|{size}|{mtime_ns}".encode("utf-8")
    ).hexdigest()[:24]


def cache_target(source: Path) -> Path:
    """Stable cache path for this exact source version.

    Keyed by resolved path + size + mtime, so an edited or replaced log
    gets a fresh entry instead of silently serving stale bytes.
    """
    st = source.stat()
    return CACHE_DIR / f"{_key(source, st.st_size, st.st_mtime_ns)}.log"


def decompress(source: Path, opener, target: Path) -> None:
    """Materialize `target` from `source`, atomically and idempotently.

    The sidecar is written *before* the data file is renamed into place,
    so a visible `<key>.log` always has a matching `<key>.json`. Crashes
    leave at most orphan temps, which the next prune() removes.
    """
    if target.exists():
        return
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    data_tmp = CACHE_DIR / f".tmp-{uuid.uuid4().hex[:12]}"
    meta_tmp = CACHE_DIR / f".tmp-{uuid.uuid4().hex[:12]}"
    try:
        with opener(source, "rb") as src, data_tmp.open("wb") as dst:
            shutil.copyfileobj(src, dst, 1 << 20)
        st = source.stat()
        meta_tmp.write_text(json.dumps({
            "source": str(source.resolve()),
            "size": st.st_size,
            "mtime_ns": st.st_mtime_ns,
        }), encoding="utf-8")
        os.replace(meta_tmp, target.with_suffix(".json"))
        os.replace(data_tmp, target)
    finally:
        data_tmp.unlink(missing_ok=True)
        meta_tmp.unlink(missing_ok=True)


def prune() -> int:
    """Drop cache entries whose source is gone or changed; returns files removed."""
    if not CACHE_DIR.is_dir():
        return 0
    valid: set[str] = set()
    doomed: list[Path] = []
    for entry in CACHE_DIR.iterdir():
        if not entry.is_file():
            continue
        if entry.name.startswith(".tmp-"):  # crashed materialization
            doomed.append(entry)
            continue
        if entry.suffix != ".json":
            continue
        try:
            meta = json.loads(entry.read_text(encoding="utf-8"))
            st = Path(meta["source"]).stat()
            if st.st_size == meta["size"] and st.st_mtime_ns == meta["mtime_ns"]:
                valid.add(entry.stem)
                continue
        except (OSError, ValueError, KeyError):
            pass
        doomed.append(entry)  # source gone, changed, or unreadable sidecar
    for entry in CACHE_DIR.iterdir():
        if entry.suffix == ".log" and entry.stem not in valid:
            doomed.append(entry)  # orphan data without a valid sidecar
    for path in doomed:
        path.unlink(missing_ok=True)
    return len(doomed)
