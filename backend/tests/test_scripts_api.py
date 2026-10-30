"""Script management endpoints (the UI editor's backend).

SCRIPTS_DIR is redirected to a temp directory — these tests must never
touch the repository's real sample scripts.
"""
from __future__ import annotations

import pytest

import app.routers.scripts as scripts_router
import app.services.scanner as scanner_module
from conftest import SAMPLE_LOG
from test_api import submit, wait_status

EXPECTED_UPPER = SAMPLE_LOG.read_text().upper()


@pytest.fixture()
def scripts_dir(tmp_path, monkeypatch):
    target = tmp_path / "scripts"
    target.mkdir()
    monkeypatch.setattr(scanner_module, "SCRIPTS_DIR", target)
    monkeypatch.setattr(scripts_router, "SCRIPTS_DIR", target)
    return target


def _body(script_id="shout", extension="sh", code=None, **manifest):
    return {
        "id": script_id,
        "extension": extension,
        "code": code or 'tr a-z A-Z < "$1"',
        "manifest": {
            "name": "Shout",
            "description": "Uppercases the log",
            "command": "bash {script} {log} {args}",
            "timeout": 30,
            "parameters": [
                {"name": "opt", "flag": "--opt", "type": "string", "default": "x"},
            ],
            **manifest,
        },
    }


def test_create_list_and_run(client, scripts_dir):
    res = client.post("/api/scripts", json=_body())
    assert res.status_code == 200, res.text
    created = res.json()["script"]
    assert created["id"] == "shout"
    assert created["name"] == "Shout"
    assert created["command"] == "bash {script} {log} {args}"

    # discovered immediately — no restart needed
    listed = client.get("/api/scripts").json()["scripts"]
    assert any(s["id"] == "shout" for s in listed)

    # both files landed on disk
    assert (scripts_dir / "shout.sh").is_file()
    assert (scripts_dir / "shout.json").is_file()

    # and it runs like any other script
    jobs = submit(client, [{"script_id": "shout", "params": {}}])
    wait_status(client, [jobs[0]["id"]], ("completed",))
    stdout = client.get(f"/api/jobs/{jobs[0]['id']}/stdout").text
    assert stdout == EXPECTED_UPPER


def test_create_rejects_duplicates_and_bad_input(client, scripts_dir):
    assert client.post("/api/scripts", json=_body()).status_code == 200

    res = client.post("/api/scripts", json=_body())  # same id
    assert res.status_code == 409

    res = client.post("/api/scripts", json=_body(script_id="bad id!"))
    assert res.status_code == 422

    res = client.post("/api/scripts", json=_body(script_id="empty", code="   "))
    assert res.status_code == 422

    # parameter spec missing its flag -> pydantic 422
    body = _body(script_id="badparams")
    body["manifest"]["parameters"] = [{"name": "x"}]
    res = client.post("/api/scripts", json=body)
    assert res.status_code == 422

    # nothing but the first (valid) script exists
    ids = {s["id"] for s in client.get("/api/scripts").json()["scripts"]}
    assert ids == {"shout"}


def test_blank_command_gets_default_template(client, scripts_dir):
    body = _body(script_id="no_cmd")
    body["manifest"]["command"] = "   "
    res = client.post("/api/scripts", json=body)
    assert res.status_code == 200
    assert res.json()["script"]["command"] == "bash {script} {log} {args}"

    # and it actually runs
    jobs = submit(client, [{"script_id": "no_cmd", "params": {}}])
    wait_status(client, [jobs[0]["id"]], ("completed",))


def test_get_source_roundtrip(client, scripts_dir):
    client.post("/api/scripts", json=_body())
    res = client.get("/api/scripts/shout")
    assert res.status_code == 200
    src = res.json()
    assert src["id"] == "shout"
    assert src["extension"] == "sh"
    assert src["code"] == 'tr a-z A-Z < "$1"'
    assert src["manifest"]["name"] == "Shout"
    assert src["manifest"]["parameters"][0]["name"] == "opt"


def test_update_code_and_extension(client, scripts_dir):
    client.post("/api/scripts", json=_body())

    body = _body(extension="py", code=(
        "import sys\n"
        "print(open(sys.argv[1]).read().upper(), end='')\n"))
    body["manifest"]["command"] = "python3 {script} {log} {args}"
    res = client.put("/api/scripts/shout", json=body)
    assert res.status_code == 200, res.text

    # old .sh gone, new .py in place, single sidecar
    assert not (scripts_dir / "shout.sh").exists()
    assert (scripts_dir / "shout.py").is_file()
    assert (scripts_dir / "shout.json").is_file()

    jobs = submit(client, [{"script_id": "shout", "params": {}}])
    wait_status(client, [jobs[0]["id"]], ("completed",))
    stdout = client.get(f"/api/jobs/{jobs[0]['id']}/stdout").text
    assert stdout == EXPECTED_UPPER

    # body id must match the path
    res = client.put("/api/scripts/shout", json=_body(script_id="other"))
    assert res.status_code == 422


def test_delete_script(client, scripts_dir):
    client.post("/api/scripts", json=_body())
    jobs = submit(client, [{"script_id": "shout", "params": {}}])
    wait_status(client, [jobs[0]["id"]], ("completed",))

    res = client.delete("/api/scripts/shout")
    assert res.status_code == 200 and res.json()["ok"] is True
    assert not (scripts_dir / "shout.sh").exists()
    assert not (scripts_dir / "shout.json").exists()
    assert client.get("/api/scripts/shout").status_code == 404

    # job history survives deletion (stdout was copied into the job dir)…
    assert client.get(f"/api/jobs/{jobs[0]['id']}").json()["status"] == "completed"
    # …but a re-run is impossible
    res = client.post(f"/api/jobs/{jobs[0]['id']}/rerun")
    assert res.status_code == 404
    assert "no longer available" in res.json()["detail"]

    res = client.delete("/api/scripts/shout")
    assert res.status_code == 404


def test_unknown_script_404(client, scripts_dir):
    assert client.get("/api/scripts/nope").status_code == 404
    assert client.delete("/api/scripts/nope").status_code == 404
