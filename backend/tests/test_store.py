"""JobStore: persistence, chain listing, reconcile, prune."""
from __future__ import annotations

from conftest import make_script


def _create(store, script_id="s"):
    return store.create(
        script_id=script_id,
        script_name=script_id,
        log_path="/tmp/x.log",
        log_name="x.log",
        params={},
    )


def test_create_get_roundtrip(store):
    job = _create(store)
    loaded = store.get(job.id)
    assert loaded is not None
    assert loaded.id == job.id
    assert loaded.status == "queued"
    assert store.get("not-an-id") is None


def test_list_newest_first(store, monkeypatch):
    # ids embed a per-second timestamp; force distinct ones so the
    # ordering is deterministic even when created within one second
    seq = iter(["20260101-000000-aaaaaaaa", "20260102-000000-bbbbbbbb"])
    monkeypatch.setattr(store, "new_id", lambda: next(seq))
    first = _create(store)
    second = _create(store)
    ids = [j.id for j in store.list()]
    assert ids.index(second.id) < ids.index(first.id)


def test_delete_removes_record(store):
    job = _create(store)
    assert store.delete(job.id) is True
    assert store.get(job.id) is None
    assert store.delete(job.id) is False


def test_list_chain_orders_by_index(store):
    a, b, c = _create(store), _create(store), _create(store)
    # write them out of order on purpose
    for job, index in ((c, 2), (a, 0), (b, 1)):
        job.chain_id = "chain-x"
        job.chain_index = index
        store.save(job)
    order = [j.chain_index for j in store.list_chain("chain-x")]
    assert order == [0, 1, 2]
    assert store.list_chain("other") == []


def test_reconcile_marks_unfinished_failed(store):
    original = {}
    for status in ("queued", "waiting", "running", "completed", "failed"):
        job = _create(store)
        job.status = status
        store.save(job)
        original[job.id] = status

    store.reconcile()

    for job in store.list():
        was = original[job.id]
        if was in ("queued", "waiting", "running"):
            assert job.status == "failed"
            assert job.error == "Interrupted by server restart"
            assert job.finished_at
        else:
            # completed / already-failed jobs are untouched
            assert job.status == was


def test_prune_removes_only_old_jobs(store, tmp_path):
    fresh = _create(store)
    old = _create(store)
    # rename the old job's directory so its id timestamp predates the cutoff
    old_dir = store.job_dir(old.id)
    old_dir.rename(tmp_path / "20200101-000000-deadbeef")
    (tmp_path / "20200101-000000-deadbeef" / "job.json").write_text("{}")

    removed = store.prune(1)

    assert removed == 1
    assert store.get(old.id) is None
    assert store.get(fresh.id) is not None
    assert store.prune(0) == 0  # disabled


def test_label_persisted(store):
    job = store.create(
        script_id="s", script_name="s", log_path="/tmp/x.log",
        log_name="x.log", params={}, label="nightly sweep",
    )
    assert store.get(job.id).label == "nightly sweep"


def test_save_survives_deleted_dir(store):
    job = _create(store)
    store.delete(job.id)
    store.save(job)  # must not resurrect the directory
    assert store.get(job.id) is None


def test_make_script_helper():
    script = make_script(command="cat {log}")
    assert script.command == "cat {log}"
