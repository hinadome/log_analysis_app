"""Chain execution: artifact/stdout hand-off, failure skipping, gap tolerance.

These tests lock in the chain invariants documented in ARCHITECTURE.md §10:
  1. a step always receives the previous step's output (artifact > stdout)
  2. a compressed hand-off is decompressed before the next step runs
  3. a failed/cancelled step skips everything after it
  4. a completed step with no output fails the next step
  5. a deleted middle step does not stall the chain
  6. a bookkeeping error never leaves waiting steps behind
"""
from __future__ import annotations

import asyncio
import time

from conftest import make_job, make_script, patch_script_lookup
from test_executor import wait_for


def _payload(tmp_path):
    log = tmp_path / "in.log"
    log.write_text("ERROR one\nERROR two\nINFO three\n")
    return log


def test_chain_artifact_flow_and_decompression(store, executor, monkeypatch, tmp_path):
    """Step 1 writes a gzipped artifact; step 2 reads it decompressed."""
    async def main():
        log = _payload(tmp_path)
        step1 = make_script("gz", "bash -c 'gzip -c {log} > {output}'",
                            output_filename="filtered.log.gz")
        step2 = make_script("cat", "cat {log}")
        patch_script_lookup(monkeypatch, [step1, step2])
        j1 = make_job(store, step1, str(log))
        j2 = make_job(store, step2, str(log))
        executor.submit_chain([(j1, step1), (j2, step2)], "chain-a")

        d1 = await wait_for(store, j1.id, ("completed", "failed"))
        assert d1.status == "completed", d1.error
        d2 = await wait_for(store, j2.id, ("completed", "failed"))
        assert d2.status == "completed", d2.error

        assert d1.output_files == ["filtered.log.gz"]
        assert d2.input_source == "prev_output"
        # the compressed artifact was materialized as a decompressed copy
        assert (store.job_dir(j2.id) / "input.log").read_text() == log.read_text()
        assert (store.job_dir(j2.id) / "stdout.txt").read_text() == log.read_text()

    asyncio.run(main())


def test_chain_stdout_fallback(store, executor, monkeypatch, tmp_path):
    """No artifact, non-empty stdout: the next step reads the stdout file."""
    async def main():
        log = _payload(tmp_path)
        step1 = make_script("filter", "grep ERROR {log}")
        step2 = make_script("count", "bash -c 'wc -l < {log}'")
        patch_script_lookup(monkeypatch, [step1, step2])
        j1 = make_job(store, step1, str(log))
        j2 = make_job(store, step2, str(log))
        executor.submit_chain([(j1, step1), (j2, step2)], "chain-b")

        d2 = await wait_for(store, j2.id, ("completed", "failed"))
        assert d2.status == "completed", d2.error
        assert d2.input_source == "prev_stdout"
        assert d2.input_path == str(store.stdout_path(j1.id))
        assert (store.job_dir(j2.id) / "stdout.txt").read_text().strip() == "2"

    asyncio.run(main())


def test_chain_fail_skips_remaining(store, executor, tmp_path):
    async def main():
        log = _payload(tmp_path)
        boom = make_script("boom", "bash -c 'exit 7'")
        cat = make_script("cat", "cat {log}")
        j1 = make_job(store, boom, str(log))
        j2 = make_job(store, cat, str(log))
        j3 = make_job(store, cat, str(log))
        executor.submit_chain([(j1, boom), (j2, cat), (j3, cat)], "chain-c")

        d1 = await wait_for(store, j1.id, ("failed",))
        assert d1.exit_code == 7
        d2 = await wait_for(store, j2.id, ("skipped",))
        d3 = await wait_for(store, j3.id, ("skipped",))
        assert d2.error == "Skipped: step 1 did not complete"
        assert d3.error == "Skipped: step 1 did not complete"

    asyncio.run(main())


def test_chain_no_output_fails_next_step(store, executor, tmp_path):
    """A completed step with no output cannot feed the next one."""
    async def main():
        log = _payload(tmp_path)
        silent = make_script("silent", "bash -c 'exit 0'")
        cat = make_script("cat", "cat {log}")
        j1 = make_job(store, silent, str(log))
        j2 = make_job(store, cat, str(log))
        j3 = make_job(store, cat, str(log))
        executor.submit_chain([(j1, silent), (j2, cat), (j3, cat)], "chain-d")

        d1 = await wait_for(store, j1.id, ("completed", "failed"))
        assert d1.status == "completed"
        d2 = await wait_for(store, j2.id, ("failed",))
        assert d2.error == "Previous step produced no output to chain"
        d3 = await wait_for(store, j3.id, ("skipped",))
        assert d3.error == "Skipped: step 2 did not complete"

    asyncio.run(main())


def test_chain_tolerates_deleted_middle_step(store, executor, monkeypatch, tmp_path):
    """Deleting a waiting middle step must not stall the chain."""
    async def main():
        log = _payload(tmp_path)
        slow = make_script("slow", "bash -c 'sleep 0.5; cat {log}'")
        cat = make_script("cat", "cat {log}")
        patch_script_lookup(monkeypatch, [slow, cat])
        j1 = make_job(store, slow, str(log))
        j2 = make_job(store, cat, str(log))
        j3 = make_job(store, cat, str(log))
        executor.submit_chain([(j1, slow), (j2, cat), (j3, cat)], "chain-e")

        # While step 1 runs, the waiting middle step disappears.
        store.delete(j2.id)
        assert store.get(j2.id) is None

        d1 = await wait_for(store, j1.id, ("completed",))
        assert d1.status == "completed"
        # Step 3 picks up step 1's output across the gap.
        d3 = await wait_for(store, j3.id, ("completed", "failed"), timeout=15)
        assert d3.status == "completed", d3.error
        assert d3.input_source == "prev_stdout"
        assert d3.input_path == str(store.stdout_path(j1.id))

    asyncio.run(main())


def test_chain_advance_error_skips_remaining(store, executor, tmp_path):
    """A bookkeeping crash during advancement never leaves steps waiting."""
    async def main():
        log = _payload(tmp_path)
        cat = make_script("cat", "cat {log}")

        def broken_chain_output(job):
            raise RuntimeError("boom")

        executor.chain_output = broken_chain_output  # instance-level patch
        try:
            j1 = make_job(store, cat, str(log))
            j2 = make_job(store, cat, str(log))
            executor.submit_chain([(j1, cat), (j2, cat)], "chain-f")

            d1 = await wait_for(store, j1.id, ("completed",))
            assert d1.status == "completed"
            d2 = await wait_for(store, j2.id, ("skipped",))
            assert d2.error == "Skipped: step 1 did not complete"
        finally:
            del executor.chain_output  # restore the class method

    asyncio.run(main())


def test_cancel_flag_blocks_advancement(store, executor, tmp_path):
    """A cancellation racing with step completion must not hand off."""
    async def main():
        log = _payload(tmp_path)
        slow = make_script("slow", "bash -c 'sleep 0.5; cat {log}'")
        cat = make_script("cat", "cat {log}")
        j1 = make_job(store, slow, str(log))
        j2 = make_job(store, cat, str(log))
        executor.submit_chain([(j1, slow), (j2, cat)], "chain-g")

        # Simulate a cancel landing while step 1 is still finishing.
        executor._cancelling_chains.add("chain-g")

        d1 = await wait_for(store, j1.id, ("completed",))
        assert d1.status == "completed"  # the step itself had already finished
        d2 = await wait_for(store, j2.id, ("skipped",))
        assert d2.error == "Cancelled by user"

    asyncio.run(main())
