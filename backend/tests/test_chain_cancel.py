"""Chain cancellation semantics."""
from __future__ import annotations

import asyncio

from conftest import make_job, make_script, patch_script_lookup
from test_executor import wait_for


def test_cancel_chain_kills_running_and_skips_waiting(store, executor, tmp_path):
    async def main():
        log = tmp_path / "in.log"
        log.write_text("x\n")
        sleepy = make_script("sleepy", "sleep 30", timeout=60)
        cat = make_script("cat", "cat {log}")
        j1 = make_job(store, sleepy, str(log))
        j2 = make_job(store, cat, str(log))
        j3 = make_job(store, cat, str(log))
        executor.submit_chain([(j1, sleepy), (j2, cat), (j3, cat)], "chain-x")

        await wait_for(store, j1.id, ("running",), timeout=10)
        assert await executor.cancel_chain("chain-x") is True

        d1 = await wait_for(store, j1.id, ("cancelled",), timeout=10)
        assert d1.error == "Cancelled by user"
        d2 = await wait_for(store, j2.id, ("skipped",))
        d3 = await wait_for(store, j3.id, ("skipped",))
        assert d2.error == "Cancelled by user"
        assert d3.error == "Cancelled by user"

    asyncio.run(main())


def test_cancel_chain_after_finish_returns_false(store, executor, monkeypatch, tmp_path):
    async def main():
        log = tmp_path / "in.log"
        log.write_text("x\n")
        cat = make_script("cat", "cat {log}")
        patch_script_lookup(monkeypatch, [cat])
        j1 = make_job(store, cat, str(log))
        j2 = make_job(store, cat, str(log))
        executor.submit_chain([(j1, cat), (j2, cat)], "chain-y")
        await wait_for(store, j1.id, ("completed",))
        await wait_for(store, j2.id, ("completed",))
        assert await executor.cancel_chain("chain-y") is False

    asyncio.run(main())


def test_skip_remaining_default_reason_mentions_step(store, executor, tmp_path):
    async def main():
        log = tmp_path / "in.log"
        log.write_text("x\n")
        boom = make_script("boom", "bash -c 'exit 1'")
        cat = make_script("cat", "cat {log}")
        j1 = make_job(store, boom, str(log))
        j2 = make_job(store, cat, str(log))
        executor.submit_chain([(j1, boom), (j2, cat)], "chain-z")

        await wait_for(store, j1.id, ("failed",))
        d2 = await wait_for(store, j2.id, ("skipped",))
        # default reason names the step that did not complete
        assert d2.error == "Skipped: step 1 did not complete"

    asyncio.run(main())
