"""Script ownership (multi-user stage 3): private spaces, sharing, visibility.

Ownership is locational — scripts/ top level is the shared library,
scripts/users/<name>/ is that account's private space — so most of these
tests assert on file locations as much as on API responses. The scripts
directory is a temp dir (never the repo's samples), and accounts live in
per-test stores, exactly like the stage-1/2 user tests.
"""
from __future__ import annotations

import io
import json
import tarfile

import pytest

import app.routers.scripts as scripts_router
import app.services.scanner as scanner_module
from test_api import submit, wait_status
from test_scripts_api import _body, _pack, _post_import
from test_users_admin import as_user

CODE = 'tr a-z A-Z < "$1"'


@pytest.fixture()
def scripts_dir(tmp_path, monkeypatch):
    target = tmp_path / "scripts"
    target.mkdir()
    monkeypatch.setattr(scanner_module, "SCRIPTS_DIR", target)
    monkeypatch.setattr(scripts_router, "SCRIPTS_DIR", target)
    return target


@pytest.fixture()
def team(client, scripts_dir, users_file, sessions_file, fresh_logins):
    """admin + alice + bob, all signed in on separate clients."""
    users_file.create("admin", "admin-pw", role="admin")
    users_file.create("alice", "alice-pw")
    users_file.create("bob", "bob-pw")
    admin = as_user(client, "admin", "admin-pw")
    alice = as_user(client, "alice", "alice-pw")
    bob = as_user(client, "bob", "bob-pw")
    return admin, alice, bob


def put_shared(scripts_dir, script_id="shared-tool", owner_files=True):
    """A flat shared script written straight to disk."""
    (scripts_dir / f"{script_id}.sh").write_text(CODE, encoding="utf-8")
    (scripts_dir / f"{script_id}.json").write_text(json.dumps({
        "name": "Shared tool", "command": "bash {script} {log} {args}",
        "timeout": 30}), encoding="utf-8")


def ids(client):
    return {s["id"] for s in client.get("/api/scripts").json()["scripts"]}


# --- the scanner: locational ownership -----------------------------------------

def test_scanner_attributes_owner_by_location(scripts_dir):
    put_shared(scripts_dir)
    private = scripts_dir / "users" / "alice"
    private.mkdir(parents=True)
    (private / "mine.sh").write_text(CODE, encoding="utf-8")

    scripts = {s.id: s for s in scanner_module.discover_scripts()}
    assert scripts["shared-tool"].owner is None
    assert scripts["mine"].owner == "alice"

    # the users/ container itself is never a script, whatever is in it
    (scripts_dir / "users" / "manifest.json").write_text("{}", encoding="utf-8")
    assert "users" not in {s.id for s in scanner_module.discover_scripts()}


# --- visibility ------------------------------------------------------------------

def test_regular_user_sees_shared_and_own_private(team, scripts_dir):
    admin, alice, bob = team
    put_shared(scripts_dir)
    alice.post("/api/scripts", json=_body("mine"))
    bob.post("/api/scripts", json=_body("bobs"))

    assert ids(alice) == {"shared-tool", "mine"}   # bob's private is not hers to see
    assert ids(bob) == {"shared-tool", "bobs"}
    assert ids(admin) == {"shared-tool", "mine", "bobs"}  # admin sees everything


def test_another_users_private_script_is_invisible_everywhere(team, scripts_dir):
    _, alice, bob = team
    alice.post("/api/scripts", json=_body("mine"))

    # 404 everywhere — never a confirmation that it exists
    assert bob.get("/api/scripts/mine").status_code == 404
    assert bob.get("/api/scripts/mine/export").status_code == 404
    assert bob.put("/api/scripts/mine", json=_body("mine")).status_code == 404
    assert bob.delete("/api/scripts/mine").status_code == 404
    res = bob.post("/api/jobs", json={"mode": "parallel",
                                      "jobs": [{"script_id": "mine", "params": {}}]})
    assert res.status_code == 404
    assert res.json()["detail"] == "Unknown script: mine"
    # a pipeline may not reference it either
    res = bob.post("/api/pipelines", json={"name": "p", "steps": [
        {"script_id": "mine", "params": {}}]})
    assert res.status_code == 404


# --- create: destination and global uniqueness -----------------------------------

def test_create_lands_private_in_multi_user(team, scripts_dir):
    _, alice, bob = team
    res = alice.post("/api/scripts", json=_body("mine"))
    assert res.status_code == 200
    assert res.json()["script"]["owner"] == "alice"
    assert (scripts_dir / "users" / "alice" / "mine.sh").is_file()

    # globally unique: bob's identical id is a 409 that doesn't say whose
    res = bob.post("/api/scripts", json=_body("mine"))
    assert res.status_code == 409
    assert "alice" not in res.json()["detail"]
    assert "already exists" in res.json()["detail"]


def test_create_in_single_user_lands_shared(client, scripts_dir, users_file,
                                            sessions_file):
    res = client.post("/api/scripts", json=_body("solo"))
    assert res.status_code == 200
    assert res.json()["script"]["owner"] is None
    assert (scripts_dir / "solo.sh").is_file()
    listed = client.get("/api/scripts").json()["scripts"]
    assert all(s["owner"] is None for s in listed)


# --- edit / delete permissions -----------------------------------------------------

def test_edit_permissions(team, scripts_dir):
    admin, alice, bob = team
    put_shared(scripts_dir)
    alice.post("/api/scripts", json=_body("mine"))

    # private: owner and admin may edit, others don't even see it
    assert alice.put("/api/scripts/mine", json=_body("mine", code="new code")).status_code == 200
    assert admin.put("/api/scripts/mine", json=_body("mine", code="admin pass")).status_code == 200
    assert (scripts_dir / "users" / "alice" / "mine.sh").read_text() == "admin pass"

    # an update keeps the script where it lives (ownership unchanged)
    mine = [s for s in alice.get("/api/scripts").json()["scripts"]
            if s["id"] == "mine"][0]
    assert mine["owner"] == "alice"

    # shared: any editor account may edit (the editing flag is the gate)
    assert bob.put("/api/scripts/shared-tool",
                   json=_body("shared-tool", code="bob edits shared")).status_code == 200


def test_delete_permissions(team, scripts_dir):
    admin, alice, bob = team
    put_shared(scripts_dir)
    alice.post("/api/scripts", json=_body("mine"))

    # own private: fine
    alice.post("/api/scripts", json=_body("temp"))
    assert alice.delete("/api/scripts/temp").status_code == 200

    # another's private: invisible (404)
    assert bob.delete("/api/scripts/mine").status_code == 404

    # shared: admin only
    res = bob.delete("/api/scripts/shared-tool")
    assert res.status_code == 403
    assert "admin" in res.json()["detail"]
    assert admin.delete("/api/scripts/shared-tool").status_code == 200


# --- share / unshare ---------------------------------------------------------------

def test_share_flow_makes_script_visible_and_runnable(team, scripts_dir):
    _, alice, bob = team
    alice.post("/api/scripts", json=_body("mine"))
    assert "mine" not in ids(bob)

    res = alice.post("/api/scripts/mine/share")
    assert res.status_code == 200
    assert res.json()["script"]["owner"] is None
    assert (scripts_dir / "mine.sh").is_file()          # moved to the shared root
    assert not (scripts_dir / "users" / "alice" / "mine.sh").exists()

    # bob sees it and can run it
    assert "mine" in ids(bob)
    jobs = submit(bob, [{"script_id": "mine", "params": {}}])
    wait_status(bob, [j["id"] for j in jobs], ["completed"])


def test_share_permissions_and_edges(team, scripts_dir):
    admin, alice, bob = team
    alice.post("/api/scripts", json=_body("mine"))
    put_shared(scripts_dir)

    # invisible to bob -> 404, not 403 (no existence leak)
    assert bob.post("/api/scripts/mine/share").status_code == 404

    # admin may share on the owner's behalf
    assert admin.post("/api/scripts/mine/share").status_code == 200

    # already shared -> 422
    res = alice.post("/api/scripts/mine/share")
    assert res.status_code == 422
    assert "already shared" in res.json()["detail"]


def test_unshare_moves_to_the_calling_admins_private(team, scripts_dir):
    admin, alice, bob = team
    put_shared(scripts_dir)

    # a regular user may not unshare
    res = bob.post("/api/scripts/shared-tool/unshare")
    assert res.status_code == 403

    res = admin.post("/api/scripts/shared-tool/unshare")
    assert res.status_code == 200
    assert res.json()["script"]["owner"] == "admin"
    assert (scripts_dir / "users" / "admin" / "shared-tool.sh").is_file()

    # gone from the library: nobody else sees it anymore
    assert "shared-tool" not in ids(alice)
    assert "shared-tool" not in ids(bob)
    assert bob.get("/api/scripts/shared-tool").status_code == 404

    # and it is not a shared script anymore
    res = admin.post("/api/scripts/shared-tool/unshare")
    assert res.status_code == 422


def test_unshare_needs_a_real_admin_account(client, scripts_dir, users_file,
                                            sessions_file):
    """Single-user mode: the operator is admin, but unshare has nowhere to
    move the script to (no account to own it) — it stays a 403."""
    put_shared(scripts_dir)
    assert client.post("/api/scripts/shared-tool/unshare").status_code == 403


def test_share_unshare_directory_script(team, scripts_dir):
    _, alice, bob = team
    # a directory-style script in alice's private space, hand-placed
    d = scripts_dir / "users" / "alice" / "dirscript"
    d.mkdir(parents=True)
    (d / "manifest.json").write_text(json.dumps(
        {"name": "Dir", "command": "bash {script} {log} {args}", "timeout": 30,
         "entry": "run.sh"}), encoding="utf-8")
    (d / "run.sh").write_text(CODE, encoding="utf-8")
    assert "dirscript" in ids(alice) and "dirscript" not in ids(bob)

    assert alice.post("/api/scripts/dirscript/share").status_code == 200
    assert (scripts_dir / "dirscript" / "run.sh").is_file()
    assert "dirscript" in ids(bob)

    admin = team[0]
    assert admin.post("/api/scripts/dirscript/unshare").status_code == 200
    assert (scripts_dir / "users" / "admin" / "dirscript" / "run.sh").is_file()


# --- import: destination, conflicts, in-place overwrite -----------------------------

def test_import_lands_in_the_callers_private_space(team, scripts_dir):
    _, alice, bob = team
    archive = _pack({"mine.sh": CODE, "mine.json": json.dumps(
        {"name": "Mine", "command": "bash {script} {log} {args}", "timeout": 30})})
    res = _post_import(alice, archive)
    assert res.status_code == 200
    assert (scripts_dir / "users" / "alice" / "mine.sh").is_file()
    assert "mine" not in ids(bob)

    # same ids again, as bob: a 409 that doesn't reveal whose
    res = _post_import(bob, archive)
    assert res.status_code == 409
    assert "alice" not in res.json()["detail"]

    # and overwrite is refused — bob may not replace alice's script
    res = _post_import(bob, archive, overwrite="1")
    assert res.status_code == 409
    assert "may not replace" in res.json()["detail"]

    # alice overwriting her own: fine, in place
    res = _post_import(alice, archive, overwrite="1")
    assert res.status_code == 200
    assert (scripts_dir / "users" / "alice" / "mine.sh").is_file()


def test_import_overwrite_keeps_location_and_owner(team, scripts_dir):
    admin, alice, bob = team
    put_shared(scripts_dir)
    archive = _pack({"shared-tool.sh": "echo replaced",
                     "shared-tool.json": json.dumps(
                         {"name": "Shared tool", "command": "bash {script} {log} {args}",
                          "timeout": 30})})

    # bob cannot replace a shared script (only admins delete those)
    res = _post_import(bob, archive, overwrite="1")
    assert res.status_code == 409

    # admin can — and it stays shared, where it was
    res = _post_import(admin, archive, overwrite="1")
    assert res.status_code == 200
    assert (scripts_dir / "shared-tool.sh").read_text() == "echo replaced"
    shared = [s for s in admin.get("/api/scripts").json()["scripts"]
              if s["id"] == "shared-tool"][0]
    assert shared["owner"] is None


# --- export-all: exactly what the caller may see ------------------------------------

def test_export_all_respects_visibility(team, scripts_dir):
    _, alice, bob = team
    put_shared(scripts_dir)
    alice.post("/api/scripts", json=_body("mine"))
    bob.post("/api/scripts", json=_body("bobs"))

    def members(client):
        data = client.get("/api/scripts/export").content
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            return {m.name for m in tar.getmembers()}

    assert members(alice) == {"shared-tool.sh", "shared-tool.json",
                              "mine.sh", "mine.json", "RESTORE.txt"}
    assert members(bob) == {"shared-tool.sh", "shared-tool.json",
                            "bobs.sh", "bobs.json", "RESTORE.txt"}


# --- rerun keeps respecting visibility ------------------------------------------------

def test_rerun_of_a_job_whose_script_became_invisible(team, scripts_dir):
    admin, alice, bob = team
    put_shared(scripts_dir)
    jobs = submit(bob, [{"script_id": "shared-tool", "params": {}}])
    wait_status(bob, [j["id"] for j in jobs], ["completed"])

    # the admin pulls the script out of the library mid-history
    admin.post("/api/scripts/shared-tool/unshare")
    res = bob.post(f"/api/jobs/{jobs[0]['id']}/rerun")
    assert res.status_code == 404
    assert "no longer available" in res.json()["detail"]


# --- the python UI page ---------------------------------------------------------------

def test_scripts_page_shares_and_badges(team, scripts_dir):
    admin, alice, bob = team
    put_shared(scripts_dir)
    alice.post("/api/scripts", json=_body("mine"))

    page = alice.get("/scripts")
    assert page.status_code == 200
    assert "you (private)" in page.text            # owner column
    assert "shared" in page.text
    assert "/scripts/mine/share" in page.text      # alice may share hers
    assert "/scripts/shared-tool/unshare" not in page.text  # not an admin

    page = bob.get("/scripts")
    assert "mine" not in page.text                 # alice's script is absent
    assert "/scripts/shared-tool/delete" not in page.text   # shared: admin-only delete

    # share via the form
    res = alice.post("/scripts/mine/share", follow_redirects=False)
    assert res.status_code == 303
    assert "mine" in ids(bob)
