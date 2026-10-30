"""Filesystem-backed job persistence.

Each job lives in its own directory: data/jobs/<job_id>/ containing
job.json (metadata), stdout.txt, stderr.txt and outputs/ (artifacts).
"""
from __future__ import annotations

import re
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ..config import JOBS_DIR, STDOUT_TAIL_BYTES
from ..models import Job

_SAFE_ID = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{8}$")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobStore:
    def __init__(self, root: Path = JOBS_DIR) -> None:
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)
        # Parsed-job cache: job_id -> (mtime_ns, size, Job). The directory
        # listing in list() stays the source of truth, so deleted or wiped
        # jobs can never leak from the cache; a changed mtime or size (every
        # save() rewrites job.json) forces a re-parse.
        self._cache: dict[str, tuple[int, int, Job]] = {}

    # -- paths ---------------------------------------------------------
    def job_dir(self, job_id: str) -> Path:
        return self._root / job_id

    def stdout_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "stdout.txt"

    def stderr_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "stderr.txt"

    def outputs_dir(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "outputs"

    # -- lifecycle ------------------------------------------------------
    def new_id(self) -> str:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        return f"{stamp}-{uuid.uuid4().hex[:8]}"

    def create(self, *, script_id: str, script_name: str, log_path: str,
               log_name: str, params: dict, label: str | None = None) -> Job:
        job = Job(
            id=self.new_id(),
            script_id=script_id,
            script_name=script_name,
            log_path=log_path,
            log_name=log_name,
            label=label,
            params=params,
            created_at=utcnow(),
        )
        self.outputs_dir(job.id).mkdir(parents=True, exist_ok=True)
        self.save(job)
        return job

    def save(self, job: Job) -> None:
        try:
            (self.job_dir(job.id) / "job.json").write_text(job.model_dump_json(indent=2))
        except OSError:
            pass  # job directory deleted underneath us

    def get(self, job_id: str) -> Job | None:
        if not _SAFE_ID.match(job_id):
            return None
        meta = self.job_dir(job_id) / "job.json"
        if not meta.is_file():
            return None
        try:
            return Job.model_validate_json(meta.read_text(encoding="utf-8"))
        except Exception:
            return None

    def list(self) -> list[Job]:
        """All jobs, newest first.

        Parsed job.json files are cached by (mtime, size); polling costs one
        directory scan plus stat() per job instead of N JSON parses.
        """
        jobs: list[Job] = []
        seen: set[str] = set()
        if self._root.is_dir():
            for directory in self._root.iterdir():
                if not directory.is_dir():
                    continue
                job_id = directory.name
                if not _SAFE_ID.match(job_id):
                    continue
                meta = directory / "job.json"
                try:
                    stat = meta.stat()
                except OSError:
                    continue  # disappeared between iterdir and stat
                cached = self._cache.get(job_id)
                if cached and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
                    job = cached[2]
                else:
                    job = self.get(job_id)
                    if job is None:
                        continue  # unreadable/corrupt job.json — same as before
                    self._cache[job_id] = (stat.st_mtime_ns, stat.st_size, job)
                # copy-on-return: callers may mutate freely (like a fresh
                # parse) without corrupting what the cache holds
                jobs.append(job.model_copy())
                seen.add(job_id)
        if len(seen) != len(self._cache):
            self._cache = {k: v for k, v in self._cache.items() if k in seen}
        jobs.sort(key=lambda j: j.id, reverse=True)
        return jobs

    def delete(self, job_id: str) -> bool:
        if not _SAFE_ID.match(job_id):
            return False
        directory = self.job_dir(job_id)
        if not directory.exists():
            return False
        shutil.rmtree(directory, ignore_errors=True)
        return True

    # -- chains -----------------------------------------------------------
    def list_chain(self, chain_id: str) -> list[Job]:
        """All jobs of a chain, ordered by step index."""
        steps = [j for j in self.list() if j.chain_id == chain_id]
        steps.sort(key=lambda j: j.chain_index)
        return steps

    # -- content ---------------------------------------------------------
    def tail(self, job_id: str, which: str, max_bytes: int = STDOUT_TAIL_BYTES) -> str:
        path = self.stdout_path(job_id) if which == "stdout" else self.stderr_path(job_id)
        if not path.is_file():
            return ""
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
            return fh.read().decode(errors="replace")

    def read(self, job_id: str, which: str, max_bytes: int = 20 * 1024 * 1024) -> str:
        path = self.stdout_path(job_id) if which == "stdout" else self.stderr_path(job_id)
        return self.read_path(path, max_bytes) or ""

    @staticmethod
    def read_path(path: Path, max_bytes: int = 20 * 1024 * 1024) -> str | None:
        """Read a text file, capped; None when it does not exist."""
        if not path.is_file():
            return None
        with path.open("rb") as fh:
            data = fh.read(max_bytes + 1)
        if len(data) > max_bytes:
            return data[:max_bytes].decode(errors="replace") + f"\n… truncated at {max_bytes} bytes"
        return data.decode(errors="replace")

    # -- maintenance ------------------------------------------------------
    def reconcile(self) -> None:
        """Mark jobs that could not finish (server stopped) as failed."""
        for job in self.list():
            if job.status in ("queued", "waiting", "running"):
                job.status = "failed"
                job.error = "Interrupted by server restart"
                job.finished_at = utcnow()
                self.save(job)

    def prune(self, retention_days: int) -> int:
        """Delete jobs older than retention_days; returns number removed."""
        if retention_days <= 0:
            return 0
        cutoff = time.time() - retention_days * 86400
        removed = 0
        if not self._root.is_dir():
            return 0
        for directory in self._root.iterdir():
            if not directory.is_dir():
                continue
            try:
                created = datetime.strptime(directory.name[:15], "%Y%m%d-%H%M%S").timestamp()
            except ValueError:
                created = directory.stat().st_mtime
            if created < cutoff:
                shutil.rmtree(directory, ignore_errors=True)
                removed += 1
        return removed
