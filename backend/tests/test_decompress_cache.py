"""The shared decompressed-input cache: one copy per source version.

Covers the two contracts the executor relies on — idempotent material
creation (a fan-out decompresses once) and GC that keeps entries only
while their source file is unchanged on disk.
"""
from __future__ import annotations

import gzip
import json

from app.services import decompress_cache
from app.services.decompress_cache import cache_target, decompress, prune


def _source(tmp_path, monkeypatch, name="in.log.gz", payload=b"one\ntwo\n"):
    """A gz source plus a throwaway cache dir; returns the source path."""
    monkeypatch.setattr(decompress_cache, "CACHE_DIR", tmp_path / "cache")
    src = tmp_path / name
    src.write_bytes(gzip.compress(payload))
    return src


def test_decompress_is_idempotent_and_writes_sidecar(tmp_path, monkeypatch):
    src = _source(tmp_path, monkeypatch)
    target = cache_target(src)
    decompress(src, gzip.open, target)
    first, mtime = target.read_bytes(), target.stat().st_mtime_ns

    # a second materialization is a no-op — not a rewrite
    decompress(src, gzip.open, target)
    assert target.read_bytes() == first
    assert target.stat().st_mtime_ns == mtime

    meta = json.loads(target.with_suffix(".json").read_text(encoding="utf-8"))
    assert meta["source"] == str(src.resolve())
    assert meta["size"] == src.stat().st_size
    assert meta["mtime_ns"] == src.stat().st_mtime_ns


def test_prune_keeps_valid_and_drops_gone_or_changed(tmp_path, monkeypatch):
    keep = _source(tmp_path, monkeypatch, "keep.log.gz")
    keep_target = cache_target(keep)
    decompress(keep, gzip.open, keep_target)

    gone = _source(tmp_path, monkeypatch, "gone.log.gz")
    gone_target = cache_target(gone)
    decompress(gone, gzip.open, gone_target)
    gone.unlink()  # source deleted

    changed = _source(tmp_path, monkeypatch, "changed.log.gz")
    changed_target = cache_target(changed)
    decompress(changed, gzip.open, changed_target)
    # source replaced underneath: same path, different size → new identity
    changed.write_bytes(gzip.compress(b"replaced with more content\n"))

    # nothing prunable yet besides the two stale pairs (data + sidecar each)
    assert prune() == 4
    assert keep_target.is_file() and keep_target.with_suffix(".json").is_file()
    assert not gone_target.exists() and not changed_target.exists()


def test_prune_removes_orphan_junk(tmp_path, monkeypatch):
    src = _source(tmp_path, monkeypatch)
    target = cache_target(src)
    decompress(src, gzip.open, target)

    # a crash between sidecar and data replace leaves orphans behind
    orphan_data = decompress_cache.CACHE_DIR / "deadbeef.log"
    orphan_data.write_bytes(b"no sidecar")
    crashed_tmp = decompress_cache.CACHE_DIR / ".tmp-leftover"
    crashed_tmp.write_bytes(b"partial")

    assert prune() == 2  # orphan data + crashed temp; the valid pair stays
    assert target.is_file()
    assert not orphan_data.exists() and not crashed_tmp.exists()


def test_prune_on_missing_dir_is_a_no_op(tmp_path, monkeypatch):
    monkeypatch.setattr(decompress_cache, "CACHE_DIR", tmp_path / "nope")
    assert prune() == 0
