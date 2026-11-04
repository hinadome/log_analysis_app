"""Script management endpoints (the UI editor's backend).

SCRIPTS_DIR is redirected to a temp directory — these tests must never
touch the repository's real sample scripts.
"""
from __future__ import annotations

import io
import json
import tarfile

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
    assert client.get("/api/scripts/nope/export").status_code == 404


def test_export_flat_script(client, scripts_dir):
    client.post("/api/scripts", json=_body())

    res = client.get("/api/scripts/shout/export")
    assert res.status_code == 200, res.text
    assert res.headers["content-type"] == "application/gzip"
    assert res.headers["content-disposition"] == 'attachment; filename="shout.tar.gz"'

    with tarfile.open(fileobj=io.BytesIO(res.content)) as tar:
        names = set(tar.getnames())
        assert names == {"shout.sh", "shout.json", "RESTORE.txt"}
        code = tar.extractfile("shout.sh").read().decode()
        assert code == 'tr a-z A-Z < "$1"'
        manifest = json.loads(tar.extractfile("shout.json").read().decode())
        assert manifest["name"] == "Shout"


def test_export_directory_script(client, scripts_dir):
    (scripts_dir / "bundle").mkdir()
    (scripts_dir / "bundle" / "manifest.json").write_text(
        json.dumps({"name": "Bundle", "command": "python3 {script} {log} {args}"}),
        encoding="utf-8",
    )
    (scripts_dir / "bundle" / "main.py").write_text(
        "import sys\nprint(open(sys.argv[1]).read(), end='')\n", encoding="utf-8")
    assert any(s["id"] == "bundle" for s in client.get("/api/scripts").json()["scripts"])

    res = client.get("/api/scripts/bundle/export")
    assert res.status_code == 200
    with tarfile.open(fileobj=io.BytesIO(res.content)) as tar:
        assert {"manifest.json", "main.py", "RESTORE.txt"} == set(tar.getnames())


def test_export_restores_to_a_working_script(client, scripts_dir):
    """The export is a real backup: extract it into a fresh scripts dir and
    the script works there."""
    client.post("/api/scripts", json=_body())
    archive = client.get("/api/scripts/shout/export").content

    restored = scripts_dir.parent / "restored-scripts"
    restored.mkdir(exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        for member in tar:
            if member.name == "RESTORE.txt":
                continue
            tar.extract(member, restored, filter="data")

    monkey = pytest.MonkeyPatch()
    monkey.setattr(scanner_module, "SCRIPTS_DIR", restored)
    monkey.setattr(scripts_router, "SCRIPTS_DIR", restored)
    try:
        listed = client.get("/api/scripts").json()["scripts"]
        assert any(s["id"] == "shout" for s in listed)
    finally:
        monkey.undo()


# --- import (the export's counterpart) ---------------------------------------

def _pack(files: dict, *, mode=0o644) -> bytes:
    """A hand-built tar.gz from {name: bytes-or-str} — import tests' archives."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content in files.items():
            if isinstance(content, str):
                content = content.encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = mode
            tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def _post_import(client, archive: bytes, filename="import.tar.gz", **params):
    return client.post("/api/scripts/import",
                       files={"file": (filename, archive, "application/gzip")},
                       params=params)


def test_import_roundtrip_flat(client, scripts_dir):
    """export → delete → import: the script is back, byte for byte, and runs."""
    client.post("/api/scripts", json=_body())
    archive = client.get("/api/scripts/shout/export").content
    client.delete("/api/scripts/shout")
    assert all(s["id"] != "shout" for s in client.get("/api/scripts").json()["scripts"])

    res = _post_import(client, archive, filename="shout.tar.gz")
    assert res.status_code == 200, res.text
    assert res.json()["imported"] == ["shout"]

    scripts = {s["id"]: s for s in client.get("/api/scripts").json()["scripts"]}
    assert scripts["shout"]["name"] == "Shout"
    assert scripts["shout"]["parameters"][0]["name"] == "opt"
    assert (scripts_dir / "shout.sh").read_text() == 'tr a-z A-Z < "$1"'
    assert json.loads((scripts_dir / "shout.json").read_text())["command"] == "bash {script} {log} {args}"
    assert (scripts_dir / "shout.sh").stat().st_mode & 0o111, "imported code is executable"

    # and it actually runs: submit a job against it
    jobs = submit(client, [{"script_id": "shout", "params": {}}])
    wait_status(client, [jobs[0]["id"]], ("completed",))
    assert client.get(f"/api/jobs/{jobs[0]['id']}/stdout").text == EXPECTED_UPPER


def test_import_roundtrip_directory(client, scripts_dir):
    (scripts_dir / "bundle").mkdir()
    (scripts_dir / "bundle" / "manifest.json").write_text(
        json.dumps({"name": "Bundle", "command": "python3 {script} {log} {args}"}), encoding="utf-8")
    (scripts_dir / "bundle" / "main.py").write_text(
        "import sys\nprint(open(sys.argv[1]).read(), end='')\n", encoding="utf-8")
    (scripts_dir / "bundle" / "data").mkdir()
    (scripts_dir / "bundle" / "data" / "table.csv").write_text("a,b\n", encoding="utf-8")
    archive = client.get("/api/scripts/bundle/export").content
    assert any(s["id"] == "bundle" for s in client.get("/api/scripts").json()["scripts"])

    import shutil
    shutil.rmtree(scripts_dir / "bundle")
    assert not any(s["id"] == "bundle" for s in client.get("/api/scripts").json()["scripts"])

    # the id comes from the file name — the export flattens the directory away
    res = _post_import(client, archive, filename="bundle.tar.gz")
    assert res.status_code == 200, res.text
    assert res.json()["imported"] == ["bundle"]

    scripts = {s["id"]: s for s in client.get("/api/scripts").json()["scripts"]}
    assert scripts["bundle"]["style"] == "directory"
    assert scripts["bundle"]["name"] == "Bundle"
    assert (scripts_dir / "bundle" / "main.py").is_file()
    assert (scripts_dir / "bundle" / "data" / "table.csv").read_text() == "a,b\n"
    assert (scripts_dir / "bundle" / "main.py").stat().st_mode & 0o111

    # an explicit ?name= wins over the file name
    other = _post_import(client, archive, filename="whatever.tar.gz", name="bundle2")
    assert other.status_code == 200
    assert other.json()["imported"] == ["bundle2"]
    assert (scripts_dir / "bundle2" / "main.py").is_file()


def test_import_flat_pack(client, scripts_dir):
    """Several flat scripts in one archive — all land, all are discovered."""
    pack = _pack({
        "foo.py": "print('foo')\n",
        "foo.json": json.dumps({"name": "Foo", "command": "python3 {script} {log} {args}"}),
        "bar.sh": "echo bar\n",  # bare: known extension, no sidecar needed
    })
    res = _post_import(client, pack)
    assert res.status_code == 200, res.text
    assert sorted(res.json()["imported"]) == ["bar", "foo"]

    scripts = {s["id"]: s for s in client.get("/api/scripts").json()["scripts"]}
    assert scripts["foo"]["name"] == "Foo"
    assert scripts["bar"]["command"].startswith("bash ")


def test_import_conflict_409_then_overwrite(client, scripts_dir):
    client.post("/api/scripts", json=_body())
    replacement = _pack({
        "shout.sh": "echo replaced\n",
        "shout.json": json.dumps({"name": "Shout v2", "command": "bash {script} {log} {args}"}),
    })

    # same id, no overwrite: refused, original untouched
    res = _post_import(client, replacement)
    assert res.status_code == 409
    assert "shout" in res.json()["detail"]
    assert (scripts_dir / "shout.sh").read_text() == 'tr a-z A-Z < "$1"'

    res = _post_import(client, replacement, overwrite="1")
    assert res.status_code == 200, res.text
    scripts = {s["id"]: s for s in client.get("/api/scripts").json()["scripts"]}
    assert scripts["shout"]["name"] == "Shout v2"
    assert (scripts_dir / "shout.sh").read_text() == "echo replaced\n"


def test_import_overwrite_replaces_across_styles(client, scripts_dir):
    """A directory-style import can replace a flat script of the same id
    (and vice versa) — the old files don't linger."""
    client.post("/api/scripts", json=_body())  # flat shout
    res = _post_import(client, _pack({
        "manifest.json": json.dumps({"name": "Now a dir", "command": "bash {script} {log} {args}"}),
        "shout.sh": "echo dir\n",
    }), name="shout", overwrite="1")
    assert res.status_code == 200, res.text

    scripts = {s["id"]: s for s in client.get("/api/scripts").json()["scripts"]}
    assert scripts["shout"]["style"] == "directory"
    assert (scripts_dir / "shout" / "shout.sh").is_file()
    assert not (scripts_dir / "shout.sh").exists(), "the old flat files must be gone"


def test_import_refuses_undiscovered_directory(client, scripts_dir):
    """A leftover directory that the scanner doesn't know owns its name:
    the import refuses instead of burying itself inside it."""
    stray = scripts_dir / "stray"
    stray.mkdir()
    (stray / "notes.txt").write_text("not a script\n", encoding="utf-8")
    # no manifest.json -> not discovered
    assert all(s["id"] != "stray" for s in client.get("/api/scripts").json()["scripts"])

    res = _post_import(client, _pack({
        "manifest.json": json.dumps({"name": "Stray"}),
        "main.sh": "echo x\n",
    }), name="stray")
    assert res.status_code == 409
    assert "by hand" in res.json()["detail"]
    # the stray content is untouched, nothing nested inside it
    assert (stray / "notes.txt").read_text() == "not a script\n"
    assert list(stray.iterdir()) == [stray / "notes.txt"]


# --- export all (the library backup) -----------------------------------------

def test_export_all_contents(client, scripts_dir):
    """Flat scripts as top-level pairs, directory scripts under their names
    (NOT flattened — a library archive would collide), plus the README."""
    client.post("/api/scripts", json=_body())  # flat: shout.sh + shout.json
    (scripts_dir / "bundle").mkdir()
    (scripts_dir / "bundle" / "manifest.json").write_text(
        json.dumps({"name": "Bundle", "command": "python3 {script} {log} {args}"}), encoding="utf-8")
    (scripts_dir / "bundle" / "main.py").write_text("print(1)\n", encoding="utf-8")
    (scripts_dir / "bundle" / "data").mkdir()
    (scripts_dir / "bundle" / "data" / "t.csv").write_text("a\n", encoding="utf-8")

    res = client.get("/api/scripts/export")
    assert res.status_code == 200, res.text
    assert res.headers["content-type"] == "application/gzip"
    assert res.headers["content-disposition"].startswith('attachment; filename="logsmith-scripts-')
    with tarfile.open(fileobj=io.BytesIO(res.content)) as tar:
        names = set(tar.getnames())
        assert {"shout.sh", "shout.json",
                "bundle/manifest.json", "bundle/main.py", "bundle/data/t.csv",
                "RESTORE.txt"} == names
        readme = tar.extractfile("RESTORE.txt").read().decode()
        assert "2 script(s)" in readme
        assert "Import" in readme


def test_export_all_round_trip(client, scripts_dir):
    """The library backup restores: export all, wipe, import one archive,
    everything is back — names, styles, params, and it runs."""
    client.post("/api/scripts", json=_body())  # flat shout
    (scripts_dir / "bundle").mkdir()
    (scripts_dir / "bundle" / "manifest.json").write_text(
        json.dumps({"name": "Bundle", "command": "python3 {script} {log} {args}"}), encoding="utf-8")
    (scripts_dir / "bundle" / "main.py").write_text(
        "import sys\nprint(open(sys.argv[1]).read(), end='')\n", encoding="utf-8")
    before = {s["id"]: s for s in client.get("/api/scripts").json()["scripts"]}

    archive = client.get("/api/scripts/export").content

    import shutil
    for entry in scripts_dir.iterdir():
        shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
    assert client.get("/api/scripts").json()["scripts"] == []

    res = _post_import(client, archive, filename="logsmith-scripts.tar.gz")
    assert res.status_code == 200, res.text
    assert sorted(res.json()["imported"]) == ["bundle", "shout"]

    after = {s["id"]: s for s in client.get("/api/scripts").json()["scripts"]}
    assert set(after) == set(before)
    assert after["shout"]["parameters"] == before["shout"]["parameters"]
    assert after["shout"]["style"] == "flat"
    assert after["bundle"]["style"] == "directory"
    assert after["bundle"]["name"] == "Bundle"

    # one of each style actually runs after the round trip
    jobs = submit(client, [{"script_id": "shout", "params": {}}])
    wait_status(client, [jobs[0]["id"]], ("completed",))
    assert client.get(f"/api/jobs/{jobs[0]['id']}/stdout").text == EXPECTED_UPPER


def test_export_all_with_no_scripts(client, scripts_dir):
    """A backup of nothing is still a valid archive (the README only)."""
    res = client.get("/api/scripts/export")
    assert res.status_code == 200
    with tarfile.open(fileobj=io.BytesIO(res.content)) as tar:
        assert tar.getnames() == ["RESTORE.txt"]


def test_export_all_route_wins_over_script_id(client, scripts_dir):
    """GET /api/scripts/export is the library download — the fixed path
    beats the /{script_id} parameter, so the id itself is reserved: creating
    a script named 'export' (or importing one) is a 422, not a script whose
    source can never be fetched."""
    res = client.get("/api/scripts/export")
    assert res.status_code == 200
    assert res.headers["content-type"] == "application/gzip"

    res = client.post("/api/scripts", json=_body(script_id="export"))
    assert res.status_code == 422
    assert "reserved" in res.json()["detail"]

    # import paths refuse it too — flat pack and directory-style alike
    assert _post_import(client, _pack({
        "export.sh": "echo hi\n",
        "export.json": json.dumps({"name": "Export"}),
    })).status_code == 422
    assert _post_import(client, _pack({
        "manifest.json": json.dumps({"name": "Export"}),
        "main.sh": "echo hi\n",
    }), name="export").status_code == 422
    assert _post_import(client, _pack({
        "export/manifest.json": json.dumps({"name": "Export"}),
        "export/main.sh": "echo hi\n",
    })).status_code == 422
    assert list(scripts_dir.iterdir()) == []


def test_import_pack_with_directory_scripts(client, scripts_dir):
    """The export-all shape: flat pairs and manifest-bearing subdirectories
    in one archive — all land; a subdirectory without a manifest is an
    error; a flat stem colliding with a directory name is an error."""
    pack = _pack({
        "hello.sh": "echo hi\n",
        "hello.json": json.dumps({"name": "Hello"}),
        "bundler/manifest.json": json.dumps({"name": "The Bundler"}),
        "bundler/main.sh": "echo bundled\n",
        "bundler/data/x.csv": "1\n",
    })
    res = _post_import(client, pack)
    assert res.status_code == 200, res.text
    assert sorted(res.json()["imported"]) == ["bundler", "hello"]
    assert (scripts_dir / "bundler" / "data" / "x.csv").read_text() == "1\n"

    scripts = {s["id"]: s for s in client.get("/api/scripts").json()["scripts"]}
    assert scripts["hello"]["style"] == "flat"
    assert scripts["bundler"]["style"] == "directory"
    assert (scripts_dir / "bundler" / "main.sh").stat().st_mode & 0o111

    # a directory without a manifest is not a script — refused
    assert _post_import(client, _pack({
        "ok.sh": "echo ok\n", "justdata/thing.txt": "x",
    })).status_code == 422

    # two scripts claiming one id inside a single archive
    assert _post_import(client, _pack({
        "clash.sh": "echo a\n",
        "clash/manifest.json": json.dumps({"name": "Clash"}),
    })).status_code == 422


def test_import_rejects_bad_archives(client, scripts_dir):
    # not a tar.gz at all
    assert _post_import(client, b"just some bytes").status_code == 422
    # empty upload
    assert _post_import(client, b"").status_code == 422
    # only the export's README — nothing importable
    assert _post_import(client, _pack({"RESTORE.txt": b"note"})).status_code == 422

    # path traversal: refused, nothing written
    res = _post_import(client, _pack({"../evil.sh": "echo pwned\n"}))
    assert res.status_code == 422
    assert not (scripts_dir.parent / "evil.sh").exists()
    res = _post_import(client, _pack({"/abs/evil.sh": "echo pwned\n"}))
    assert res.status_code == 422

    # a symlink member is not a regular file — refused
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        link = tarfile.TarInfo("evil")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        tar.addfile(link)
    assert _post_import(client, buf.getvalue()).status_code == 422

    # unknown extension without a sidecar would be invisible to the scanner
    assert _post_import(client, _pack({"data.dat": "x"})).status_code == 422
    # a sidecar whose code file is missing
    assert _post_import(client, _pack({"lonely.json": "{}"})).status_code == 422
    # not a safe id
    assert _post_import(client, _pack({"my script.sh": "echo hi\n"})).status_code == 422
    # directories need a top-level manifest.json to be directory-style
    assert _post_import(client, _pack({"sub/thing.py": "print(1)\n"})).status_code == 422
    # a directory-style import with no derivable id
    res = _post_import(client, _pack({
        "manifest.json": json.dumps({"name": "X"}),
        "x.sh": "echo x\n",
    }), filename="weird name.tar.gz")
    assert res.status_code == 422

    # nothing above wrote anything
    assert list(scripts_dir.iterdir()) == []


def test_import_locked_when_editing_disabled(client, scripts_dir, monkeypatch):
    monkeypatch.setattr(scripts_router, "ALLOW_SCRIPT_EDITING", False)
    res = _post_import(client, _pack({"ok.sh": "echo ok\n"}))
    assert res.status_code == 403
    assert list(scripts_dir.iterdir()) == []


def test_import_page_form(client, scripts_dir):
    """The python UI's Import form rides the same endpoint, in-process."""
    archive = _pack({
        "form.sh": "echo from-form\n",
        "form.json": json.dumps({"name": "From Form", "command": "bash {script} {log} {args}"}),
    })
    res = client.post("/scripts/import",
                      files={"file": ("form.tar.gz", archive, "application/gzip")},
                      data={"name": "", "overwrite": ""}, follow_redirects=False)
    assert res.status_code == 303
    from urllib.parse import unquote_plus
    assert unquote_plus(res.headers["location"]) == "/scripts?msg=Imported 1 script(s): form"
    assert any(s["id"] == "form" for s in client.get("/api/scripts").json()["scripts"])

    # the form's error path is a flash, not a stack trace
    res = client.post("/scripts/import",
                      files={"file": ("form.tar.gz", archive, "application/gzip")},
                      data={"name": "", "overwrite": ""}, follow_redirects=False)
    assert res.status_code == 303
    assert "err=" in res.headers["location"]
    # and the replace checkbox maps to overwrite
    res = client.post("/scripts/import",
                      files={"file": ("form.tar.gz", archive, "application/gzip")},
                      data={"name": "", "overwrite": "1"}, follow_redirects=False)
    assert "msg=" in res.headers["location"]


# --- executable scripts + actionable spawn errors -----------------------------
#
# A manifest command may run the script file directly ("{script} {log}" with
# no interpreter); that needs the execute bit (the editor writes 755) and a
# shebang. The spawn-failure errors must say which of the two is missing.

def test_editor_creates_executable_scripts(client, scripts_dir):
    code_path = scripts_dir / "shout.sh"
    client.post("/api/scripts", json=_body())
    assert code_path.stat().st_mode & 0o111, "created scripts must be executable"

    # updates keep it executable, across an extension change too
    res = client.put("/api/scripts/shout", json=_body(extension="py"))
    assert res.status_code == 200, res.text
    assert (scripts_dir / "shout.py").stat().st_mode & 0o111


def test_direct_execution_of_editor_script(client, scripts_dir):
    """{script} without an interpreter: works for editor-created scripts
    as long as the code starts with a shebang."""
    body = _body("direct", code='#!/bin/bash\necho "lines: $(wc -l < "$1")"\n',
                 command="{script} {log} {args}")
    assert client.post("/api/scripts", json=body).status_code == 200
    jobs = submit(client, [{"script_id": "direct", "params": {}}])
    wait_status(client, [jobs[0]["id"]], ("completed",))
    stdout = client.get(f"/api/jobs/{jobs[0]['id']}/stdout").text
    assert "lines:" in stdout


def test_spawn_error_hints(client, scripts_dir):
    """Hand-made scripts (no +x) and missing interpreters fail with a
    message that says what to fix, not just a bare errno."""
    # EACCES: script without the execute bit (as if written by hand)
    client.post("/api/scripts", json=_body(
        "notexec", code="#!/bin/bash\ntrue\n", command="{script} {log} {args}"))
    (scripts_dir / "notexec.sh").chmod(0o644)
    jobs = submit(client, [{"script_id": "notexec", "params": {}}])
    wait_status(client, [jobs[0]["id"]], ("failed",))
    err = client.get(f"/api/jobs/{jobs[0]['id']}").json()["error"]
    assert "not executable" in err
    assert "interpreter" in err

    # ENOEXEC: executable but no shebang line
    client.post("/api/scripts", json=_body(
        "noshebang", code="true\n", command="{script} {log} {args}"))
    jobs = submit(client, [{"script_id": "noshebang", "params": {}}])
    wait_status(client, [jobs[0]["id"]], ("failed",))
    err = client.get(f"/api/jobs/{jobs[0]['id']}").json()["error"]
    assert "shebang" in err

    # ENOENT: interpreter that does not exist on the server
    client.post("/api/scripts", json=_body(
        "nointerp", command="definitely-not-an-interpreter {script} {log} {args}"))
    jobs = submit(client, [{"script_id": "nointerp", "params": {}}])
    wait_status(client, [jobs[0]["id"]], ("failed",))
    err = client.get(f"/api/jobs/{jobs[0]['id']}").json()["error"]
    assert "was not found" in err

    # ENOENT via shebang: the script itself exists and is executable, but
    # its first line names an interpreter that is not installed — the
    # kernel reports ENOENT for the interpreter, which looks like the
    # script went missing. The error must name the shebang instead.
    client.post("/api/scripts", json=_body(
        "badshebang", code="#!/definitely/not/an/interpreter\ntrue\n",
        command="{script} {log} {args}"))
    jobs = submit(client, [{"script_id": "badshebang", "params": {}}])
    wait_status(client, [jobs[0]["id"]], ("failed",))
    err = client.get(f"/api/jobs/{jobs[0]['id']}").json()["error"]
    assert "#!/definitely/not/an/interpreter" in err
    assert "not installed" in err


def test_submit_fails_fast_on_missing_interpreter(client, scripts_dir):
    """A command naming an interpreter that is not installed fails at
    submission — instantly, with the interpreter named — not at spawn."""
    client.post("/api/scripts", json=_body(
        "nointerpreter", command="definitely-not-an-interpreter {script} {log} {args}"))
    jobs = submit(client, [{"script_id": "nointerpreter", "params": {}}])
    job = client.get(f"/api/jobs/{jobs[0]['id']}").json()
    assert job["status"] == "failed"  # already failed — never left the queue
    assert "'definitely-not-an-interpreter' was not found" in job["error"]


def test_submit_fails_fast_on_missing_env_shebang_interpreter(client, scripts_dir):
    """#!/usr/bin/env X with a missing X used to die inside env as a
    bare 'Exited with code 127'; submission now names the interpreter."""
    client.post("/api/scripts", json=_body(
        "envmissing", code="#!/usr/bin/env no-such-interpreter\ntrue\n",
        command="{script} {log} {args}"))
    jobs = submit(client, [{"script_id": "envmissing", "params": {}}])
    job = client.get(f"/api/jobs/{jobs[0]['id']}").json()
    assert job["status"] == "failed"
    assert "#!/usr/bin/env no-such-interpreter" in job["error"]


def _write_crlf_script(scripts_dir, script_id="crlfdirect", command="{script} {log} {args}"):
    """A hand-added CRLF script — written directly, because the editor
    normalizes CRLF on save (its own regression test covers that)."""
    (scripts_dir / f"{script_id}.sh").write_bytes(
        b"#!/usr/bin/env bash\r\necho hi\r\n")
    (scripts_dir / f"{script_id}.json").write_text(json.dumps({
        "name": "CRLF", "command": command, "timeout": 30, "parameters": [],
    }))


def test_submit_fails_fast_on_crlf_script(client, scripts_dir):
    """A CRLF-ended script used to pass every interpreter check (strip()
    eats the \\r) and die at spawn as a cryptic exit 127 —
    "env: bash\\r: No such file or directory". Submission now names the
    Windows line endings and the fix."""
    _write_crlf_script(scripts_dir)
    jobs = submit(client, [{"script_id": "crlfdirect", "params": {}}])
    job = client.get(f"/api/jobs/{jobs[0]['id']}").json()
    assert job["status"] == "failed"  # at submission — never left the queue
    assert job["exit_code"] is None
    assert "Windows line endings (CRLF)" in job["error"]
    assert "dos2unix" in job["error"]


def test_crlf_script_caught_even_when_interpreted(client, scripts_dir):
    """The CRLF check is on the code file, not the shebang path: a
    'bash {script} …' command runs bash fine, but every line of a CRLF
    script ends with \\r — caught at submission all the same."""
    _write_crlf_script(scripts_dir, "crlfbash", command="bash {script} {log} {args}")
    jobs = submit(client, [{"script_id": "crlfbash", "params": {}}])
    job = client.get(f"/api/jobs/{jobs[0]['id']}").json()
    assert job["status"] == "failed"
    assert "Windows line endings (CRLF)" in job["error"]


def test_editor_save_normalizes_crlf(client, scripts_dir):
    """Code saved through the editor is normalized to LF — a paste from
    Windows tools must not create a file that fails at spawn."""
    client.post("/api/scripts", json=_body(
        "was_crlf", code="#!/usr/bin/env bash\r\necho ok\r\n",
        command="{script} {log} {args}"))
    on_disk = (scripts_dir / "was_crlf.sh").read_bytes()
    assert b"\r" not in on_disk
    jobs = submit(client, [{"script_id": "was_crlf", "params": {}}])
    wait_status(client, [jobs[0]["id"]], ("completed",))


def test_category_round_trip_and_default_absent(client, scripts_dir):
    """The optional manifest category groups scripts under a picker filter
    chip; scripts without one report "" and keep the key out of the file."""
    client.post("/api/scripts", json=_body("catlog", category="web"))
    client.post("/api/scripts", json=_body("nocat"))
    listed = {s["id"]: s for s in client.get("/api/scripts").json()["scripts"]}
    assert listed["catlog"]["category"] == "web"
    assert listed["nocat"]["category"] == ""

    # the editor source round-trips it (so editing never drops the label)
    src = client.get("/api/scripts/catlog").json()
    assert src["manifest"]["category"] == "web"
    # and an empty one is not written into the manifest file
    assert "category" not in json.loads((scripts_dir / "nocat.json").read_text())


def test_chain_first_step_preflight_skips_the_rest(client, scripts_dir):
    """When a chain's first step cannot start, the later steps are
    skipped — not left waiting forever."""
    client.post("/api/scripts", json=_body(
        "stepone", command="definitely-not-an-interpreter {script} {log} {args}"))
    client.post("/api/scripts", json=_body())  # step two: the working one
    jobs = submit(client, [{"script_id": "stepone", "params": {}},
                           {"script_id": "shout", "params": {}}],
                  mode="chain")
    first = client.get(f"/api/jobs/{jobs[0]['id']}").json()
    second = client.get(f"/api/jobs/{jobs[1]['id']}").json()
    assert first["status"] == "failed"
    assert "definitely-not-an-interpreter" in first["error"]
    assert second["status"] == "skipped"
