"""Executor: single-job lifecycle, timeouts, cancellation, decompression.

Each test drives a fresh JobStore + Executor inside one asyncio.run() so
all tasks share a single event loop (the semaphore binds to it).
"""
from __future__ import annotations

import asyncio
import bz2
import gzip
import lzma
import time

import pytest

from conftest import make_job, make_script


def write_compressed(path, payload: str) -> None:
    """Write payload through the compressor matching the file suffix."""
    if path.suffix == ".gz":
        opener = gzip.open
    elif path.suffix == ".bz2":
        opener = bz2.open
    else:
        opener = lzma.open
    with opener(path, "wt") as fh:
        fh.write(payload)


async def wait_for(store, job_id, statuses, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = store.get(job_id)
        if job is not None and job.status in statuses:
            return job
        await asyncio.sleep(0.05)
    job = store.get(job_id)
    raise AssertionError(f"job {job_id} never reached {statuses}: "
                        f"{job.status if job else None} / {job.error if job else None}")


def test_single_job_completes(store, executor, tmp_path):
    async def main():
        log = tmp_path / "in.log"
        log.write_text("alpha\nbeta\n")
        script = make_script("cat", "cat {log}")
        job = make_job(store, script, str(log))
        executor.submit(job, script)
        done = await wait_for(store, job.id, ("completed", "failed"))
        assert done.status == "completed", done.error
        assert (store.job_dir(job.id) / "stdout.txt").read_text() == "alpha\nbeta\n"

    asyncio.run(main())


def test_nonzero_exit_fails(store, executor, tmp_path):
    async def main():
        log = tmp_path / "in.log"
        log.write_text("x")
        script = make_script("boom", "bash -c 'exit 3'")
        job = make_job(store, script, str(log))
        executor.submit(job, script)
        done = await wait_for(store, job.id, ("failed", "completed"))
        assert done.status == "failed"
        assert done.exit_code == 3
        assert "Exited with code 3" in done.error

    asyncio.run(main())


def test_timeout_kills_job(store, executor, tmp_path):
    async def main():
        log = tmp_path / "in.log"
        log.write_text("x")
        script = make_script("sleepy", "sleep 30", timeout=1)
        job = make_job(store, script, str(log))
        executor.submit(job, script)
        done = await wait_for(store, job.id, ("failed", "completed"), timeout=10)
        assert done.status == "failed"
        assert "Timed out after 1 seconds" in done.error

    asyncio.run(main())


def test_cancel_running_job(store, executor, tmp_path):
    async def main():
        log = tmp_path / "in.log"
        log.write_text("x")
        script = make_script("sleepy", "sleep 30", timeout=60)
        job = make_job(store, script, str(log))
        executor.submit(job, script)
        await wait_for(store, job.id, ("running",), timeout=10)
        assert await executor.cancel(job.id) is True
        done = await wait_for(store, job.id, ("cancelled",), timeout=10)
        assert done.error == "Cancelled by user"

    asyncio.run(main())


def test_cancel_finished_job_returns_false(store, executor, tmp_path):
    async def main():
        log = tmp_path / "in.log"
        log.write_text("x")
        script = make_script("cat", "cat {log}")
        job = make_job(store, script, str(log))
        executor.submit(job, script)
        await wait_for(store, job.id, ("completed",))
        assert await executor.cancel(job.id) is False

    asyncio.run(main())


@pytest.mark.parametrize("suffix", ["gz", "bz2", "xz"])
def test_compressed_log_is_decompressed(store, executor, tmp_path, suffix):
    async def main():
        payload = "line-one\nline-two\nline-three\n"
        compressed = tmp_path / f"in.log.{suffix}"
        write_compressed(compressed, payload)
        script = make_script("cat", "cat {log}")
        job = make_job(store, script, str(compressed))
        executor.submit(job, script)
        done = await wait_for(store, job.id, ("completed", "failed"))
        assert done.status == "completed", done.error
        decompressed = store.job_dir(job.id) / "input.log"
        assert decompressed.read_text() == payload
        assert (store.job_dir(job.id) / "stdout.txt").read_text() == payload

    asyncio.run(main())


def test_script_environment_variables(store, executor, tmp_path):
    async def main():
        log = tmp_path / "in.log"
        log.write_text("x")
        script = make_script(
            "env", "bash -c 'echo $JOB_ID; echo $JOB_LOG_PATH; echo $JOB_ORIGINAL_LOG'")
        job = make_job(store, script, str(log))
        executor.submit(job, script)
        await wait_for(store, job.id, ("completed",))
        out = (store.job_dir(job.id) / "stdout.txt").read_text()
        assert job.id in out
        assert str(log) in out  # JOB_LOG_PATH (and the log is the original here)

    asyncio.run(main())


def test_declared_output_artifact_collected(store, executor, tmp_path):
    async def main():
        log = tmp_path / "in.log"
        log.write_text("x")
        script = make_script("writer", "bash -c 'echo artifact > {output}'",
                             output_filename="result.txt")
        job = make_job(store, script, str(log))
        executor.submit(job, script)
        await wait_for(store, job.id, ("completed",))
        assert job.output_files == ["result.txt"]
        path, source = executor.chain_output(job)
        assert source == "prev_output"
        assert path.name == "result.txt"
        assert path.read_text().strip() == "artifact"

    asyncio.run(main())
