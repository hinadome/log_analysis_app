"""Saved-pipeline API: list, save (upsert by name), delete, validation.

Pipelines are the server-side home of the React UI's old localStorage
"saved chains" — reusable ordered chains shared by both UIs and every
browser. Saving validates like a submission (unknown script 404, bad
params 422), so a stored pipeline always loads cleanly.
"""
from __future__ import annotations

PIPELINE = {
    "name": "errors then count",
    "steps": [
        {"script_id": "grep_search", "params": {"pattern": "ERROR", "context": 2}},
        {"script_id": "error_counter", "params": {"level": "ERROR"}},
    ],
}


def test_save_list_and_delete_roundtrip(client):
    res = client.post("/api/pipelines", json=PIPELINE)
    assert res.status_code == 200
    saved = res.json()["pipeline"]
    assert saved["name"] == "errors then count"
    assert [s["script_id"] for s in saved["steps"]] == ["grep_search", "error_counter"]
    assert saved["steps"][0]["params"]["pattern"] == "ERROR"
    assert saved["steps"][0]["params"]["context"] == 2  # typed, not stringified
    assert saved["created_at"] and saved["updated_at"]

    listed = client.get("/api/pipelines").json()["pipelines"]
    assert [p["name"] for p in listed] == ["errors then count"]
    # saving snapshots the validated configuration: non-default values
    # kept, empty strings drop back to "use the default" (not stored)
    assert listed[0]["steps"][1]["params"] == {"level": "ERROR", "json": False}

    assert client.delete("/api/pipelines/errors then count").status_code == 200
    assert client.get("/api/pipelines").json()["pipelines"] == []


def test_save_upserts_by_name(client):
    client.post("/api/pipelines", json=PIPELINE)
    first = client.get("/api/pipelines").json()["pipelines"][0]

    replaced = {
        "name": "errors then count",
        "steps": [{"script_id": "slow_scan", "params": {"seconds": 2}}],
    }
    res = client.post("/api/pipelines", json=replaced)
    assert res.status_code == 200

    listed = client.get("/api/pipelines").json()["pipelines"]
    assert len(listed) == 1  # same name — replaced, not duplicated
    assert listed[0]["steps"][0]["script_id"] == "slow_scan"
    assert listed[0]["created_at"] == first["created_at"]  # identity kept
    assert listed[0]["updated_at"] >= first["updated_at"]


def test_save_rejects_unknown_script(client):
    res = client.post("/api/pipelines", json={
        "name": "bad", "steps": [{"script_id": "nope", "params": {}}]})
    assert res.status_code == 404
    assert "nope" in res.json()["detail"]
    assert client.get("/api/pipelines").json()["pipelines"] == []  # nothing stored


def test_save_validates_params_like_a_submission(client):
    # missing required parameter
    res = client.post("/api/pipelines", json={
        "name": "bad", "steps": [{"script_id": "grep_search", "params": {}}]})
    assert res.status_code == 422
    # value of the wrong type
    res = client.post("/api/pipelines", json={
        "name": "bad", "steps": [{"script_id": "grep_search",
                                  "params": {"pattern": "x", "context": "abc"}}]})
    assert res.status_code == 422
    # enum value outside the choices
    res = client.post("/api/pipelines", json={
        "name": "bad", "steps": [{"script_id": "error_counter",
                                  "params": {"level": "BOGUS"}}]})
    assert res.status_code == 422
    assert client.get("/api/pipelines").json()["pipelines"] == []


def test_save_rejects_empty_name_and_empty_steps(client):
    res = client.post("/api/pipelines", json={"name": "  ", "steps": PIPELINE["steps"]})
    assert res.status_code == 422
    res = client.post("/api/pipelines", json={"name": "x", "steps": []})
    assert res.status_code == 422


def test_delete_missing_pipeline_is_404(client):
    assert client.delete("/api/pipelines/never-saved").status_code == 404
