"""Log upload endpoint: happy path, collisions, sanitization, size cap."""
from __future__ import annotations

import app.routers.logs as logs_router
from conftest import LOGS_TMP

CONTENT = "2026-10-29 11:00:00 ERROR something broke\n" * 50


def _upload(client, name, content=CONTENT):
    return client.post("/api/logs/upload", files={"file": (name, content)})


def _listed_names(client):
    return {l["name"] for l in client.get("/api/logs").json()["logs"]}


def test_upload_roundtrip(client):
    res = _upload(client, "uploaded.log")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["saved"] == "uploaded.log"
    assert body["size"] == len(CONTENT.encode())
    assert body["log"]["name"] == "uploaded.log"

    # visible in the listing, correct content on disk, runnable as a job input
    assert "uploaded.log" in _listed_names(client)
    assert (LOGS_TMP / "uploaded.log").read_text() == CONTENT


def test_upload_duplicate_rejected(client):
    assert _upload(client, "dup.log").status_code == 200
    original = (LOGS_TMP / "dup.log").read_text()
    res = _upload(client, "dup.log", "different content")
    assert res.status_code == 409
    assert (LOGS_TMP / "dup.log").read_text() == original  # untouched


def test_upload_filename_is_sanitized(client):
    # path components are stripped: only the basename ever lands
    res = _upload(client, "../../evil.log")
    assert res.status_code == 200
    assert (LOGS_TMP / "evil.log").is_file()
    assert (LOGS_TMP.parent / "evil.log").exists() is False

    res = _upload(client, "sub/dir/name.log")
    assert res.status_code == 200
    assert (LOGS_TMP / "name.log").is_file()

    # names that can't be made safe are refused outright
    for bad in (".hidden", "..", "", " ", "no\nnewlines", "a" * 300):
        assert _upload(client, bad).status_code == 422, bad


def test_upload_empty_file_rejected(client):
    assert _upload(client, "empty.log", "").status_code == 422
    assert "empty.log" not in _listed_names(client)


def test_upload_size_cap(client, monkeypatch):
    monkeypatch.setattr(logs_router, "MAX_LOG_UPLOAD_MB", 1)
    res = _upload(client, "too-big-upload.log", "x" * (1024 * 1024 + 1))
    assert res.status_code == 413
    # the partial file was cleaned up and never appears in listings
    assert "too-big-upload.log" not in _listed_names(client)
    assert not (LOGS_TMP / "too-big-upload.log").exists()
    assert not (LOGS_TMP / ".too-big-upload.log.part").exists()


def test_upload_can_be_disabled(client, monkeypatch):
    monkeypatch.setattr(logs_router, "MAX_LOG_UPLOAD_MB", 0)
    assert _upload(client, "off.log").status_code == 403
    assert "off.log" not in _listed_names(client)
