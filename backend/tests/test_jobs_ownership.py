"""Job ownership (multi-user stage 4): whose history a run belongs to.

A submitted job carries its submitter's name; the scheduler's — and
anything submitted before accounts existed — is ownerless "system"
history. Owned jobs are visible to their owner and admins only; system
jobs are readable by everyone but only an admin may stop or delete
them. A rerun belongs to whoever asked for it, whoever ran the original.
"""
from __future__ import annotations

import pytest

import app.routers.jobs as jobs_router
from test_api import submit, wait_status
from test_users_admin import as_user

GREP = {"script_id": "grep_search", "params": {"pattern": "ERROR"}}


@pytest.fixture()
def team(client, users_file, sessions_file, fresh_logins):
    """admin + alice + bob, all speaking through the shared client."""
    users_file.create("admin", "admin-pw", role="admin")
    users_file.create("alice", "alice-pw")
    users_file.create("bob", "bob-pw")
    admin = as_user(client, "admin", "admin-pw")
    alice = as_user(client, "alice", "alice-pw")
    bob = as_user(client, "bob", "bob-pw")
    return admin, alice, bob


def system_job(status="completed", label="scheduled run"):
    """A job the way the scheduler leaves them: ownerless.

    The scheduler fires with no request in hand, so create_jobs runs with
    no user in scope and the job records no owner — legacy pre-accounts
    jobs look exactly the same.
    """
    job = jobs_router.store.create(
        script_id="grep_search", script_name="Grep search",
        log_path="app.log", log_name="app.log",
        params={"pattern": "ERROR"}, label=label)
    job.status = status
    jobs_router.store.save(job)
    return job


def owned_job(client, label=None):
    """Submit one real job through the API and wait for it to finish."""
    jobs = submit(client, [GREP], label=label)
    wait_status(client, [j["id"] for j in jobs], ["completed"])
    return jobs[0]


# --- submission tags the owner -------------------------------------------------

def test_submission_tags_the_owner(team):
    admin, alice, bob = team
    job = owned_job(alice)
    assert alice.get(f"/api/jobs/{job['id']}").json()["owner"] == "alice"


# --- visibility ------------------------------------------------------------------

def test_another_users_job_is_invisible_everywhere(team):
    admin, alice, bob = team
    job = owned_job(alice)
    jid = job["id"]

    # 404 everywhere — never a confirmation that it exists
    assert bob.get(f"/api/jobs/{jid}").status_code == 404
    assert bob.get(f"/api/jobs/{jid}/stdout").status_code == 404
    assert bob.get(f"/api/jobs/{jid}/stderr").status_code == 404
    assert bob.get(f"/api/jobs/{jid}/input").status_code == 404
    assert bob.get(f"/api/jobs/{jid}/files/out.txt").status_code == 404
    assert bob.post(f"/api/jobs/{jid}/cancel").status_code == 404
    assert bob.delete(f"/api/jobs/{jid}").status_code == 404
    assert bob.post(f"/api/jobs/{jid}/rerun").status_code == 404
    # the SSE stream refuses before a single event is sent
    res = bob.get(f"/api/jobs/{jid}/events")
    assert res.status_code == 404

    # the owner and the admin see it
    assert alice.get(f"/api/jobs/{jid}").status_code == 200
    assert admin.get(f"/api/jobs/{jid}").status_code == 200


def test_system_jobs_are_readable_by_everyone_but_admin_managed(team):
    admin, alice, bob = team
    job = system_job()

    # readable by all: the shared system history
    for c in (admin, alice, bob):
        assert c.get(f"/api/jobs/{job.id}").status_code == 200
        assert c.get(f"/api/jobs/{job.id}/stdout").status_code == 200

    # readable is not modifiable — a regular user may neither stop nor delete
    assert alice.post(f"/api/jobs/{job.id}/cancel").status_code == 403
    assert alice.delete(f"/api/jobs/{job.id}").status_code == 403
    # the admin passes the gate (a finished job only fails the running check)
    assert admin.post(f"/api/jobs/{job.id}/cancel").status_code == 409
    assert admin.delete(f"/api/jobs/{job.id}").status_code == 200


# --- listing ----------------------------------------------------------------------

def test_listing_scopes_regular_users_and_narrows_admins(team):
    admin, alice, bob = team
    system_job()
    owned_job(alice, label="alice's")
    owned_job(bob, label="bob's")

    # a regular user: their own runs plus the system history
    listed = alice.get("/api/jobs").json()["jobs"]
    assert {j["owner"] for j in listed} == {"alice", None}

    # the admin oversees everyone's
    listed = admin.get("/api/jobs").json()["jobs"]
    assert {j["owner"] for j in listed} == {"alice", "bob", None}

    # ?user= narrows the admin's view to one account's runs
    listed = admin.get("/api/jobs", params={"user": "alice"}).json()["jobs"]
    assert {j["owner"] for j in listed} == {"alice"}

    # for anyone else ?user= can only ever match their own jobs
    assert bob.get("/api/jobs", params={"user": "alice"}).json()["jobs"] == []
    listed = bob.get("/api/jobs", params={"user": "bob"}).json()["jobs"]
    assert {j["owner"] for j in listed} == {"bob"}


# --- rerun -------------------------------------------------------------------------

def test_rerun_belongs_to_the_rerunner(team):
    admin, alice, bob = team
    mine = owned_job(alice)
    sys = system_job()

    # alice reruns her own: hers again
    res = alice.post(f"/api/jobs/{mine['id']}/rerun")
    assert res.status_code == 200
    assert res.json()["job"]["owner"] == "alice"

    # the admin reruns alice's: the new run is the admin's
    res = admin.post(f"/api/jobs/{mine['id']}/rerun")
    assert res.status_code == 200
    assert res.json()["job"]["owner"] == "admin"

    # bob reruns a system job: it becomes bob's
    res = bob.post(f"/api/jobs/{sys.id}/rerun")
    assert res.status_code == 200
    assert res.json()["job"]["owner"] == "bob"


# --- prune and clear-finished ---------------------------------------------------------

def test_prune_is_admin_only_in_multi_user(team):
    admin, alice, bob = team
    assert bob.post("/api/jobs/prune", params={"days": 30}).status_code == 403
    assert admin.post("/api/jobs/prune", params={"days": 30}).status_code == 200


def test_prune_still_works_for_the_single_operator(client):
    """No accounts: the operator is the admin — pruning keeps working."""
    assert client.post("/api/jobs/prune", params={"days": 30}).status_code == 200


def test_clear_finished_scopes_to_what_the_caller_may_modify(team):
    admin, alice, bob = team
    alices = owned_job(alice, label="alice's")
    bobs = owned_job(bob, label="bob's")
    sys = system_job()

    # bob sweeps: only his own finished runs go
    assert bob.post("/jobs/clear-finished", follow_redirects=False).status_code == 303
    assert bob.get(f"/api/jobs/{bobs['id']}").status_code == 404
    assert alice.get(f"/api/jobs/{alices['id']}").status_code == 200
    assert bob.get(f"/api/jobs/{sys.id}").status_code == 200  # system history stays

    # the admin sweeps the rest
    assert admin.post("/jobs/clear-finished", follow_redirects=False).status_code == 303
    assert alice.get(f"/api/jobs/{alices['id']}").status_code == 404
    assert admin.get(f"/api/jobs/{sys.id}").status_code == 404


# --- the python UI page ---------------------------------------------------------------

def test_jobs_page_owners_and_detail(team):
    admin, alice, bob = team
    alices = owned_job(alice, label="alice's")
    sys = system_job()

    # the admin gets the owner column and the per-account filter
    page = admin.get("/jobs")
    assert page.status_code == 200
    assert "<th>Owner</th>" in page.text
    assert 'name="user"' in page.text
    assert "alice" in page.text
    assert "system" in page.text

    # a regular user gets neither column nor filter, and no foreign rows
    page = bob.get("/jobs")
    assert page.status_code == 200
    assert "<th>Owner</th>" not in page.text
    assert 'name="user"' not in page.text
    assert alices["id"] not in page.text      # the row link would carry the id
    assert sys.id in page.text                # system history is public reading

    # the job page marks whose run it is; foreign jobs are a 404 page
    assert "(you)" in alice.get(f"/jobs/{alices['id']}").text
    assert "alice" in admin.get(f"/jobs/{alices['id']}").text
    assert bob.get(f"/jobs/{alices['id']}").status_code == 404
    assert "system" in alice.get(f"/jobs/{sys.id}").text
