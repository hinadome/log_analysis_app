"""HTTP API: submission, chains, search, rerun (full + from_step), cancel.

Runs against the real sample scripts in scripts/ with the sample log that
conftest writes into the isolated LOGS_DIR. Each test gets a fresh TestClient
(wipe + lifespan), so jobs never leak between tests.
"""
from __future__ import annotations

import time
from pathlib import Path

from conftest import SAMPLE_LOG

LOG = "app.log"

# (script_id, params) steps reused by several tests
ERROR_STEPS = [
    {"script_id": "grep_search", "params": {"pattern": "ERROR"}},
    {"script_id": "error_counter", "params": {"level": "ALL", "json": True}},
    {"script_id": "top_ips", "params": {"top": 5}},
]


def wait_until(predicate, timeout=30.0, interval=0.25, message="condition not met"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    assert predicate(), f"{message} within {timeout}s"


def submit(client, steps, mode="parallel", label=None, log=LOG):
    body = {"log_path": log, "mode": mode, "jobs": steps}
    if label is not None:
        body["label"] = label
    res = client.post("/api/jobs", json=body)
    assert res.status_code == 200, res.text
    return res.json()["jobs"]


def wait_status(client, job_ids, statuses, timeout=30.0):
    """Wait until every job reached one of the given statuses."""
    for job_id in job_ids:
        wait_until(
            lambda jid=job_id: client.get(f"/api/jobs/{jid}").json()["status"] in statuses,
            timeout=timeout,
            message=f"job {job_id} never reached {statuses}",
        )


def test_health(client):
    res = client.get("/api/health")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert body["auth_required"] is False  # LOGSMITH_TOKEN="" in conftest


def test_logs_and_scripts_endpoints(client):
    logs = client.get("/api/logs").json()["logs"]
    assert any(l["name"] == LOG for l in logs)

    scripts = client.get("/api/scripts").json()["scripts"]
    ids = {s["id"] for s in scripts}
    assert {"grep_search", "error_counter", "top_ips", "slow_scan",
            "response_stats", "hourly_rate", "status_breakdown"} <= ids
    grep = next(s for s in scripts if s["id"] == "grep_search")
    assert any(p["name"] == "pattern" and p["required"] for p in grep["parameters"])


def test_language_samples_run(client):
    """One sample per scripting language, with parameters, on suitable logs."""
    cases = [
        ("response_stats", "access.log", {"top": 2, "errors_only": True}, "/health"),
        ("hourly_rate", LOG, {"interval": "minute", "level": "ERROR"}, "10:00"),
        ("status_breakdown", "access.log", {"top": 2, "by_bytes": False}, "200"),
    ]
    job_ids = []
    for script_id, log_name, params, _needle in cases:
        jobs = submit(client, [{"script_id": script_id, "params": params}], log=log_name)
        job_ids.append((jobs[0]["id"], _needle))

    for job_id, needle in job_ids:
        wait_status(client, [job_id], ("completed",))
        stdout = client.get(f"/api/jobs/{job_id}/stdout").text
        assert needle in stdout, f"unexpected output:\n{stdout}"


def test_parallel_submission(client):
    jobs = submit(client, [
        {"script_id": "grep_search", "params": {"pattern": "SQLException"}},
        {"script_id": "error_counter", "params": {"level": "ALL"}},
    ], mode="parallel")
    assert len(jobs) == 2
    assert all(j["chain_id"] is None for j in jobs)

    wait_status(client, [j["id"] for j in jobs], ("completed",))
    grep = next(j for j in jobs if j["script_id"] == "grep_search")
    stdout = client.get(f"/api/jobs/{grep['id']}/stdout")
    assert stdout.status_code == 200
    assert "SQLException" in stdout.text
    assert "timeout" in stdout.text and "deadlock" in stdout.text


def test_parallel_jobs_overlap_and_api_answers_while_they_run(client):
    """Concurrency is a hard contract of the workbench, not a nice-to-have.

    Parallel submissions must execute at the same time (overlapping
    started_at/finished_at windows — a sequential runner would leave gaps),
    and the API must keep answering while jobs run: the executor runs
    scripts as asyncio subprocesses, so a running job can never make the
    server unresponsive. Both properties regressed would read to a user as
    "script processes are not async".
    """
    steps = [{"script_id": "slow_scan", "params": {"seconds": 1, "interval": 0.2}}] * 3
    jobs = submit(client, steps)
    ids = [j["id"] for j in jobs]

    # While the jobs run, the API answers: a mid-flight poll must observe
    # a non-terminal status through a normal request/response.
    seen_mid_flight = False
    for _ in range(100):
        statuses = [client.get(f"/api/jobs/{jid}").json()["status"] for jid in ids]
        if "running" in statuses:
            seen_mid_flight = True
            break
        if all(s in ("completed", "failed") for s in statuses):
            break
        time.sleep(0.05)
    assert seen_mid_flight, "jobs were never observed running — poll answered too late?"

    wait_status(client, ids, ("completed",))

    # Overlap: every job started before the first one finished. ISO-8601
    # timestamps from the same clock compare correctly as strings.
    final = [client.get(f"/api/jobs/{jid}").json() for jid in ids]
    starts = [j["started_at"] for j in final]
    ends = [j["finished_at"] for j in final]
    assert max(starts) < min(ends), (
        f"parallel jobs did not overlap:\n"
        + "\n".join(f"  {s} -> {e}" for s, e in zip(starts, ends))
    )


def test_chain_flow(client):
    jobs = submit(client, ERROR_STEPS, mode="chain")
    assert len(jobs) == 3
    chain_id = jobs[0]["chain_id"]
    assert all(j["chain_id"] == chain_id for j in jobs)

    wait_status(client, [j["id"] for j in jobs], ("completed",))

    # Step 1 read the log; steps 2+ read the previous step's output.
    detail = client.get(f"/api/jobs/{jobs[0]['id']}").json()
    assert detail["input_source"] == "log"
    step2 = client.get(f"/api/jobs/{jobs[1]['id']}").json()
    assert step2["input_source"] == "prev_stdout"
    assert step2["input_path"].endswith("stdout.txt")
    # detail of any step lists the whole chain, in order
    statuses = [s["status"] for s in step2["chain_steps"]]
    assert statuses == ["completed", "completed", "completed"]
    assert [s["index"] for s in step2["chain_steps"]] == [0, 1, 2]


def test_atomic_validation_rejects_without_side_effects(client):
    # unknown script -> 404, and no job may be created
    res = client.post("/api/jobs", json={
        "log_path": LOG, "mode": "chain", "jobs": [
            {"script_id": "grep_search", "params": {"pattern": "ERROR"}},
            {"script_id": "does_not_exist", "params": {}},
        ]})
    assert res.status_code == 404
    assert "does_not_exist" in res.json()["detail"]

    # unknown parameter -> 422
    res = client.post("/api/jobs", json={
        "log_path": LOG, "jobs": [
            {"script_id": "grep_search", "params": {"bogus": 1}}]})
    assert res.status_code == 422

    # missing required parameter -> 422
    res = client.post("/api/jobs", json={
        "log_path": LOG, "jobs": [{"script_id": "grep_search", "params": {}}]})
    assert res.status_code == 422

    # bad enum value -> 422
    res = client.post("/api/jobs", json={
        "log_path": LOG, "jobs": [
            {"script_id": "error_counter", "params": {"level": "BOGUS"}}]})
    assert res.status_code == 422

    # missing log -> 404
    res = client.post("/api/jobs", json={
        "log_path": "nope.log", "jobs": [
            {"script_id": "grep_search", "params": {"pattern": "x"}}]})
    assert res.status_code == 404

    # nothing was created by any failed request
    assert client.get("/api/jobs").json()["jobs"] == []


def test_submit_with_absolute_path_outside_logs_dir(client, tmp_path):
    """An absolute path to any existing file is a valid input: scripts run
    as this user with full filesystem access anyway, so typing a path is
    just another way to pick the input file."""
    outside = (tmp_path / "elsewhere" / "system.log")
    outside.parent.mkdir()
    outside.write_text("2026-10-30 ERROR typed absolute path\n", encoding="utf-8")

    steps = [{"script_id": "grep_search", "params": {"pattern": "typed absolute path"}}]
    jobs = submit(client, steps, log=str(outside))
    wait_status(client, [jobs[0]["id"]], ("completed",))
    job = client.get(f"/api/jobs/{jobs[0]['id']}").json()
    assert Path(job["log_path"]) == outside.resolve()
    # the full path is the label — its base name alone would identify nothing
    assert job["log_name"] == str(outside.resolve())
    assert "typed absolute path" in job["stdout_tail"]

    # rerun resolves the stored absolute path the same way
    rerun = client.post(f"/api/jobs/{jobs[0]['id']}/rerun").json()["job"]
    wait_status(client, [rerun["id"]], ("completed",))
    assert rerun["log_name"] == str(outside.resolve())

    # a logs/ file submitted by its absolute path keeps its bare name
    jobs = submit(client, steps, log=str(SAMPLE_LOG))
    wait_status(client, [jobs[0]["id"]], ("completed",))
    assert client.get(f"/api/jobs/{jobs[0]['id']}").json()["log_name"] == "app.log"

    # a typed path that does not exist -> 404 naming it
    res = client.post("/api/jobs", json={
        "log_path": "/definitely/not/here.log", "jobs": steps})
    assert res.status_code == 404
    assert "/definitely/not/here.log" in res.json()["detail"]

    # relative names still may not escape the logs directory
    res = client.post("/api/jobs", json={
        "log_path": "../outside.log", "jobs": steps})
    assert res.status_code == 400
    assert "escapes" in res.json()["detail"]


def test_label_and_search(client):
    jobs = submit(client, ERROR_STEPS, mode="chain", label="db-error investigation")
    assert all(j["label"] == "db-error investigation" for j in jobs)

    found = client.get("/api/jobs", params={"q": "db-error"}).json()["jobs"]
    assert {j["id"] for j in found} == {j["id"] for j in jobs}

    # search also matches script name, and misses cleanly
    assert len(client.get("/api/jobs", params={"q": "regex"}).json()["jobs"]) == 1
    assert client.get("/api/jobs", params={"q": "nosuchthing"}).json()["jobs"] == []

    # the server-side search the UI now uses also matches chain ids
    chain_q = client.get("/api/jobs", params={"q": jobs[0]["chain_id"]}).json()["jobs"]
    assert {j["id"] for j in chain_q} == {j["id"] for j in jobs}


def test_status_filter(client):
    jobs = submit(client, ERROR_STEPS, mode="chain")
    wait_status(client, [j["id"] for j in jobs], ("completed",))

    # single status
    done = client.get("/api/jobs", params={"status": "completed"}).json()
    assert {j["id"] for j in done["jobs"]} == {j["id"] for j in jobs}
    assert done["total"] == len(jobs)

    # comma-separated statuses (what the UI's "Active"/"Cancelled" chips send)
    both = client.get("/api/jobs", params={"status": "completed,failed"}).json()
    assert {j["id"] for j in both["jobs"]} == {j["id"] for j in jobs}

    # a status nothing currently has
    none_running = client.get("/api/jobs", params={"status": "running"}).json()
    assert none_running["jobs"] == [] and none_running["total"] == 0


def test_job_input_endpoint(client):
    # a plain job's input is the selected log itself
    job = submit(client, [{"script_id": "grep_search", "params": {"pattern": "ERROR"}}])[0]
    wait_status(client, [job["id"]], ("completed",))
    res = client.get(f"/api/jobs/{job['id']}/input")
    assert res.status_code == 200
    assert "ERROR db connection lost" in res.text

    # a chained step's input is exactly the previous step's stdout
    steps = submit(client, ERROR_STEPS, mode="chain")
    wait_status(client, [j["id"] for j in steps], ("completed",))
    step1_stdout = client.get(f"/api/jobs/{steps[0]['id']}/stdout").text
    step2_input = client.get(f"/api/jobs/{steps[1]['id']}/input")
    assert step2_input.status_code == 200
    assert step2_input.text == step1_stdout


def test_job_input_serves_decompressed_copy(client):
    import gzip

    from conftest import LOGS_TMP

    gz_path = LOGS_TMP / "gz_input.log.gz"
    gz_path.write_bytes(gzip.compress(b"2026-10-29 10:00:05 ERROR gz trouble\n"))
    job = submit(client, [{"script_id": "grep_search", "params": {"pattern": "ERROR"}}],
                 log=gz_path.name)[0]
    wait_status(client, [job["id"]], ("completed",))

    # the endpoint serves the decompressed input.log, never the raw .gz bytes
    res = client.get(f"/api/jobs/{job['id']}/input")
    assert res.status_code == 200
    assert "gz trouble" in res.text


def test_job_input_404_when_source_gone(client):
    from conftest import LOGS_TMP

    log = LOGS_TMP / "input_gone_test.log"
    log.write_text("2026-10-29 10:00:05 ERROR disappearing act\n")
    job = submit(client, [{"script_id": "grep_search", "params": {"pattern": "ERROR"}}],
                 log=log.name)[0]
    wait_status(client, [job["id"]], ("completed",))
    log.unlink()

    res = client.get(f"/api/jobs/{job['id']}/input")
    assert res.status_code == 404


def test_job_events_stream(client):
    """The SSE stream must carry stdout deltas AND status changes as real
    `data:` frames — anything else is silently dropped by EventSource."""
    import json as jsonlib

    job = submit(client, [{"script_id": "slow_scan",
                           "params": {"seconds": 3, "interval": 0.4}}])[0]
    events = []
    with client.stream("GET", f"/api/jobs/{job['id']}/events") as res:
        assert res.status_code == 200
        assert res.headers["content-type"].startswith("text/event-stream")
        for line in res.iter_lines():
            if line.startswith("data: "):
                events.append(jsonlib.loads(line[len("data: "):]))

    types = {e.get("type") for e in events}
    assert "stdout" in types
    streamed = "".join(e["text"] for e in events if e.get("type") == "stdout")
    assert "scanned" in streamed  # slow_scan's progress lines arrived live
    finals = [e for e in events if e.get("type") == "status" and e.get("status") == "completed"]
    assert finals, "the stream should end with a terminal status frame"
    assert finals[-1]["exit_code"] == 0


def test_jobs_pagination(client):
    for i in range(5):
        submit(client, [{"script_id": "grep_search", "params": {"pattern": "ERROR"}}],
               label=f"page-{i}")

    res = client.get("/api/jobs").json()
    assert res["total"] == 5
    ids = [j["id"] for j in res["jobs"]]

    page1 = client.get("/api/jobs", params={"limit": 2}).json()
    assert [j["id"] for j in page1["jobs"]] == ids[:2]
    assert page1["total"] == 5

    page2 = client.get("/api/jobs", params={"limit": 2, "offset": 2}).json()
    assert [j["id"] for j in page2["jobs"]] == ids[2:4]

    page3 = client.get("/api/jobs", params={"limit": 2, "offset": 4}).json()
    assert [j["id"] for j in page3["jobs"]] == ids[4:]

    # offset past the end is empty, not an error
    assert client.get("/api/jobs", params={"offset": 99}).json()["jobs"] == []

    # total is post-filter, so counts stay honest while paging a search
    found = client.get("/api/jobs", params={"q": "page-3"}).json()
    assert found["total"] == 1 and len(found["jobs"]) == 1

    assert client.get("/api/jobs", params={"limit": 0}).status_code == 422
    # the documented cap is 5000 — 1001 pins the boundary the old le=1000 broke
    assert client.get("/api/jobs", params={"limit": 1001}).status_code == 200
    assert client.get("/api/jobs", params={"limit": 5001}).status_code == 422


def test_rerun_single_job(client):
    jobs = submit(client, [{"script_id": "grep_search", "params": {"pattern": "WARN"}}])
    wait_status(client, [jobs[0]["id"]], ("completed",))

    res = client.post(f"/api/jobs/{jobs[0]['id']}/rerun")
    assert res.status_code == 200
    new = res.json()["job"]
    assert new["id"] != jobs[0]["id"]
    assert new["label"] is None
    wait_status(client, [new["id"]], ("completed",))
    rerun_stdout = client.get(f"/api/jobs/{new['id']}/stdout").text
    assert "retrying" in rerun_stdout


def test_rerun_chain_from_step(client):
    jobs = submit(client, ERROR_STEPS, mode="chain", label="db-error investigation")
    wait_status(client, [j["id"] for j in jobs], ("completed",))

    res = client.post(f"/api/jobs/{jobs[1]['id']}/rerun", params={"from_step": 2})
    assert res.status_code == 200, res.text
    first = res.json()["job"]

    # only steps 2..n re-ran, numbering preserved, seed reused, label kept
    assert first["chain_index"] == 1
    assert first["chain_size"] == 3
    assert first["input_source"] == "prev_stdout"
    assert first["label"] == "db-error investigation"
    assert first["chain_id"] != jobs[0]["chain_id"]

    detail = client.get(f"/api/jobs/{first['id']}").json()
    assert [s["index"] for s in detail["chain_steps"]] == [1, 2]
    wait_until(lambda: all(s["status"] == "completed"
                           for s in client.get(f"/api/jobs/{first['id']}").json()["chain_steps"]),
               message="partial rerun never completed")
    # the original chain is untouched
    assert client.get(f"/api/jobs/{jobs[0]['id']}").json()["status"] == "completed"

    # beyond the chain size
    res = client.post(f"/api/jobs/{jobs[1]['id']}/rerun", params={"from_step": 9})
    assert res.status_code == 400


def test_rerun_from_step_requires_completed_predecessor(client):
    jobs = submit(client, [
        {"script_id": "slow_scan", "params": {"seconds": 1, "interval": 0.2, "fail": True}},
        {"script_id": "grep_search", "params": {"pattern": "ERROR"}},
        {"script_id": "top_ips", "params": {"top": 3}},
    ], mode="chain")
    wait_status(client, [j["id"] for j in jobs], ("failed", "skipped", "cancelled", "completed"))

    # step 1 failed -> its output cannot seed a partial rerun of step 2
    res = client.post(f"/api/jobs/{jobs[1]['id']}/rerun", params={"from_step": 2})
    assert res.status_code == 409
    # ...but a full rerun is always allowed
    res = client.post(f"/api/jobs/{jobs[1]['id']}/rerun")
    assert res.status_code == 200


def test_delete_live_chain_cancels_it(client):
    jobs = submit(client, [
        {"script_id": "slow_scan", "params": {"seconds": 5, "interval": 0.5}},
        {"script_id": "grep_search", "params": {"pattern": "ERROR"}},
        {"script_id": "top_ips", "params": {"top": 3}},
    ], mode="chain")
    step2_id = jobs[1]["id"]

    wait_until(
        lambda: client.get(f"/api/jobs/{jobs[0]['id']}").json()["status"] == "running",
        message="step 1 never started",
    )

    # deleting a step of a live chain cancels the whole chain
    res = client.delete(f"/api/jobs/{step2_id}")
    assert res.status_code == 200
    assert res.json()["ok"] is True
    assert client.get(f"/api/jobs/{step2_id}").status_code == 404

    wait_status(client, [jobs[0]["id"], jobs[2]["id"]], ("cancelled", "skipped", "failed"))
    final1 = client.get(f"/api/jobs/{jobs[0]['id']}").json()
    final3 = client.get(f"/api/jobs/{jobs[2]['id']}").json()
    assert final1["status"] == "cancelled"
    assert final3["status"] == "skipped"


def test_cancel_endpoint(client):
    jobs = submit(client, [
        {"script_id": "slow_scan", "params": {"seconds": 5, "interval": 0.5}}])
    job_id = jobs[0]["id"]
    wait_until(lambda: client.get(f"/api/jobs/{job_id}").json()["status"] == "running",
               message="job never started")

    res = client.post(f"/api/jobs/{job_id}/cancel")
    assert res.status_code == 200 and res.json()["ok"] is True
    wait_status(client, [job_id], ("cancelled",))
    assert client.get(f"/api/jobs/{job_id}").json()["error"] == "Cancelled by user"

    # cancelling a finished job is a conflict
    res = client.post(f"/api/jobs/{job_id}/cancel")
    assert res.status_code == 409


def test_prune_endpoint_requires_days(client):
    jobs = submit(client, [{"script_id": "grep_search", "params": {"pattern": "ERROR"}}])
    wait_status(client, [jobs[0]["id"]], ("completed",))
    # retention disabled in conftest (JOB_RETENTION_DAYS=0) -> explicit 400
    res = client.post("/api/jobs/prune")
    assert res.status_code == 400
    # with days=1 nothing is old enough to delete
    res = client.post("/api/jobs/prune", params={"days": 1})
    assert res.status_code == 200
    assert res.json()["removed"] == 0
