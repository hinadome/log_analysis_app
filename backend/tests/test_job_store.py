"""JobStore parse cache: parse once per unchanged file, correct under updates.

The directory listing is the source of truth: deletions and whole-directory
wipes must never leak cached jobs, and every save() (mtime+size change) must
force a re-parse.
"""
from __future__ import annotations

import shutil


def _create(store, script_id="grep_search"):
    return store.create(
        script_id=script_id,
        script_name="Grep Search",
        log_path="/tmp/app.log",
        log_name="app.log",
        params={"pattern": "ERROR"},
    )


def test_list_caches_and_stays_fresh(store):
    j1 = _create(store)
    j2 = _create(store)
    listed = store.list()
    assert {j.id for j in listed} == {j1.id, j2.id}
    # newest-first: id order, descending (timestamp prefix)
    assert listed == sorted(listed, key=lambda j: j.id, reverse=True)
    assert set(store._cache) == {j1.id, j2.id}

    # a save() rewrites job.json — the next list() must see the new state
    j1.status = "completed"
    store.save(j1)
    by_id = {j.id: j for j in store.list()}
    assert by_id[j1.id].status == "completed"

    # returned jobs are copies: mutating one must not poison the cache
    by_id[j1.id].status = "failed"
    assert {j.id: j for j in store.list()}[j1.id].status == "completed"


def test_delete_never_leaks_from_cache(store):
    j1 = _create(store)
    j2 = _create(store)
    store.delete(j1.id)
    assert {j.id for j in store.list()} == {j2.id}
    assert j1.id not in store._cache

    # a deleted id coming back (e.g. restored from backup) is picked up
    store.job_dir(j1.id).mkdir(parents=True, exist_ok=True)
    resurrected = j2.model_copy(update={"id": j1.id})
    store.save(resurrected)
    assert {j.id for j in store.list()} == {j1.id, j2.id}


def test_wiped_directory_empties_cache(store):
    _create(store)
    _create(store)
    assert len(store.list()) == 2

    shutil.rmtree(store._root)
    assert store.list() == []
    assert store._cache == {}


def test_unreadable_job_json_is_skipped(store):
    j1 = _create(store)
    (store.job_dir(j1.id) / "job.json").write_text("{ not json")
    assert store.list() == []
