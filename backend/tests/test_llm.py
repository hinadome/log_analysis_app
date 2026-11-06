"""The LLM provider switch: store, adapter and the admin API.

No network — the adapter tests fake httpx.post with canned wire
responses (both formats), so the suite exercises the switch without
ever calling out.
"""
from __future__ import annotations

import json
import os
import time

import httpx
import pytest

import app.services.llm as llm_module
from app import config
from app.services.llm import LlmError, mask_key
from app.services.llm_store import PROVIDERS, LlmStore, seed_from_env
from app.services import llm_store as llm_store_module

from test_users_admin import as_user  # the per-request-Cookie voice


@pytest.fixture()
def llm_file(tmp_path, monkeypatch):
    """The settings-store singleton, backed by an empty per-test file
    (the users_file pattern — no leakage between tests)."""
    path = tmp_path / "llm.json"
    monkeypatch.setattr(llm_store_module.store, "_path", path)
    llm_store_module.store._data = {"provider": "none",
                                    "providers": LlmStore._defaults()}
    llm_store_module.store._stamp = None
    yield llm_store_module.store


# --- the store ----------------------------------------------------------------

def test_store_defaults_to_off(tmp_path):
    fresh = LlmStore(tmp_path / "missing.json")
    settings = fresh.settings()
    assert settings["provider"] == "none"
    assert settings["providers"] == {
        key: {"api_key": "", "model": "", "base_url": ""} for key in PROVIDERS}


def test_store_update_persists_and_roundtrips(tmp_path):
    path = tmp_path / "llm.json"
    LlmStore(path).update("openai", {"openai": {"api_key": "sk-test",
                                                "model": "gpt-4o-mini"}})
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["provider"] == "openai"
    # a fresh instance (a restart, or another worker) sees the same thing
    assert LlmStore(path).settings()["providers"]["openai"] == {
        "api_key": "sk-test", "model": "gpt-4o-mini", "base_url": ""}


def test_store_blank_api_key_keeps_the_stored_one(tmp_path):
    store = LlmStore(tmp_path / "llm.json")
    store.update("openai", {"openai": {"api_key": "sk-keep", "model": "m1"}})
    store.update("openai", {"openai": {"api_key": "", "model": "m2"}})
    fields = store.settings()["providers"]["openai"]
    assert fields == {"api_key": "sk-keep", "model": "m2", "base_url": ""}
    store.update("openai", {"openai": {"api_key": "sk-new"}})
    assert store.settings()["providers"]["openai"]["api_key"] == "sk-new"


def test_store_rejects_unknown_provider(tmp_path):
    with pytest.raises(ValueError, match="Unknown LLM provider"):
        LlmStore(tmp_path / "llm.json").update("grok", {})
    with pytest.raises(ValueError, match="Unknown LLM provider"):
        LlmStore(tmp_path / "llm.json").update("none", {"grok": {"model": "x"}})


def test_store_hand_edit_wins(tmp_path):
    """The file is truth: an edit on disk is picked up without a restart
    (the mtime-stamp cache notices the change)."""
    path = tmp_path / "llm.json"
    store = LlmStore(path)
    store.update("none", {})
    data = json.loads(path.read_text(encoding="utf-8"))
    data["provider"] = "ollama"
    path.write_text(json.dumps(data), encoding="utf-8")
    os.utime(path, ns=(time.time_ns() + 10**9, time.time_ns() + 10**9))
    assert store.settings()["provider"] == "ollama"


def test_store_corrupt_file_means_off(tmp_path):
    path = tmp_path / "llm.json"
    path.write_text("{not json", encoding="utf-8")
    assert LlmStore(path).settings()["provider"] == "none"


# --- env seeding (first boot only) --------------------------------------------

def test_seed_from_env_writes_first_boot(llm_file, monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "openai")
    monkeypatch.setattr(config, "LLM_API_KEY", "sk-seed")
    monkeypatch.setattr(config, "LLM_MODEL", "gpt-4o-mini")
    monkeypatch.setattr(config, "LLM_BASE_URL", "")
    assert seed_from_env() is True
    settings = llm_file.settings()
    assert settings["provider"] == "openai"
    assert settings["providers"]["openai"]["api_key"] == "sk-seed"
    # second boot: the file exists, so even changed env vars are ignored
    monkeypatch.setattr(config, "LLM_PROVIDER", "anthropic")
    assert seed_from_env() is False
    assert llm_file.settings()["provider"] == "openai"


def test_seed_from_env_without_provider_writes_nothing(llm_file, monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "")
    monkeypatch.setattr(config, "LLM_API_KEY", "sk-seed")
    assert seed_from_env() is False
    assert llm_file.settings()["provider"] == "none"


def test_seed_from_env_ignores_unknown_provider(llm_file, monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "grok")
    assert seed_from_env() is False
    assert llm_file.settings()["provider"] == "none"


# --- the adapter (fake httpx.post — no network) -------------------------------

class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text else (json.dumps(payload) if payload is not None else "")

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


@pytest.fixture()
def sent(monkeypatch):
    """Capture what the adapter would send; answer with a canned payload
    per wire format."""
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append({"url": url, "headers": headers or {},
                      "json": json, "timeout": timeout})
        if "/chat/completions" in url:
            return FakeResponse(payload={"choices": [{"message": {"content": " hello"}}]})
        return FakeResponse(payload={"content": [{"type": "text", "text": " hi"}]})

    monkeypatch.setattr(llm_module.httpx, "post", fake_post)
    return calls


def test_complete_openai_wire(llm_file, sent):
    llm_file.update("openai", {"openai": {"api_key": "sk-x", "model": "gpt-4o-mini"}})
    assert llm_module.complete("say hi") == " hello"
    (call,) = sent
    assert call["url"] == "https://api.openai.com/v1/chat/completions"
    assert call["headers"]["Authorization"] == "Bearer sk-x"
    assert call["json"]["model"] == "gpt-4o-mini"
    assert call["json"]["messages"] == [{"role": "user", "content": "say hi"}]
    assert call["timeout"]  # mandatory: a hung endpoint must not hang the app


def test_complete_openai_wire_with_system(llm_file, sent):
    llm_file.update("openai", {"openai": {"api_key": "sk-x", "model": "m"}})
    llm_module.complete("say hi", system="be brief")
    assert sent[0]["json"]["messages"][0] == {"role": "system", "content": "be brief"}


def test_complete_omits_bearer_when_no_key(llm_file, sent):
    llm_file.update("ollama", {"ollama": {"model": "llama3.1"}})
    llm_module.complete("hi")
    (call,) = sent
    assert "Authorization" not in call["headers"]  # local endpoints want none
    assert call["url"] == "http://localhost:11434/v1/chat/completions"


def test_complete_custom_base_url_tolerates_trailing_slash(llm_file, sent):
    llm_file.update("openai-compatible",
                    {"openai-compatible": {"base_url": "http://vllm:8000/v1/", "model": "m"}})
    llm_module.complete("hi")
    assert sent[0]["url"] == "http://vllm:8000/v1/chat/completions"


def test_complete_anthropic_wire(llm_file, sent):
    llm_file.update("anthropic", {"anthropic": {"api_key": "sk-ant",
                                                "model": "claude-sonnet-4-5"}})
    assert llm_module.complete("say hi", system="be brief") == " hi"
    (call,) = sent
    assert call["url"] == "https://api.anthropic.com/v1/messages"
    assert call["headers"]["x-api-key"] == "sk-ant"
    assert call["headers"]["anthropic-version"]  # the API requires the version header
    assert call["json"]["system"] == "be brief"
    assert call["json"]["max_tokens"]  # the API requires it — never send none


def test_complete_off_raises(llm_file):
    with pytest.raises(LlmError, match="No LLM provider"):
        llm_module.complete("hi")


def test_complete_missing_model_raises(llm_file):
    llm_file.update("openai", {"openai": {"api_key": "sk-x"}})
    with pytest.raises(LlmError, match="no model"):
        llm_module.complete("hi")


def test_complete_anthropic_without_key_raises(llm_file):
    llm_file.update("anthropic", {"anthropic": {"model": "claude-sonnet-4-5"}})
    with pytest.raises(LlmError, match="API key"):
        llm_module.complete("hi")


def test_http_error_becomes_llm_error(llm_file, monkeypatch):
    llm_file.update("openai", {"openai": {"api_key": "sk-x", "model": "m"}})
    monkeypatch.setattr(llm_module.httpx, "post",
                        lambda *a, **k: FakeResponse(status_code=401, text="bad key"))
    with pytest.raises(LlmError, match="HTTP 401"):
        llm_module.complete("hi")


def test_transport_error_becomes_llm_error(llm_file, monkeypatch):
    def refused(*args, **kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(llm_module.httpx, "post", refused)
    llm_file.update("openai", {"openai": {"api_key": "sk-x", "model": "m"}})
    with pytest.raises(LlmError, match="call failed"):
        llm_module.complete("hi")


def test_mask_key():
    assert mask_key("") == ""
    assert mask_key("short") == "••••"
    assert mask_key("sk-1234567890abcd") == "sk-…abcd"


# --- the admin API -------------------------------------------------------------

def _provider(body, key):
    return next(p for p in body["providers"] if p["key"] == key)


def test_settings_single_user_operator_is_admin(client, llm_file):
    res = client.get("/api/llm/settings")
    assert res.status_code == 200
    body = res.json()
    assert body["provider"] == "none"
    assert [p["key"] for p in body["providers"]] == list(PROVIDERS)


def test_settings_requires_admin_in_multi_user(client, users_file, sessions_file,
                                              fresh_logins, llm_file):
    users_file.create("root", "root-pw", role="admin")
    users_file.create("alice", "alice-pw")
    alice = as_user(client, "alice", "alice-pw")
    assert alice.get("/api/llm/settings").status_code == 403
    assert alice.put("/api/llm/settings",
                     json={"provider": "openai", "providers": {}}).status_code == 403
    assert alice.post("/api/llm/test").status_code == 403
    assert as_user(client, "root", "root-pw").get("/api/llm/settings").status_code == 200


def test_settings_key_never_leaks(client, llm_file):
    client.put("/api/llm/settings", json={
        "provider": "openai",
        "providers": {"openai": {"api_key": "sk-secret-123456", "model": "gpt-4o-mini"}},
    })
    res = client.get("/api/llm/settings")
    assert "sk-secret-123456" not in res.text  # never the raw key
    openai = _provider(res.json(), "openai")
    assert openai["has_key"] is True
    assert openai["api_key_masked"] == "sk-…3456"


def test_settings_blank_key_keeps_stored(client, llm_file):
    body = {"provider": "openai",
            "providers": {"openai": {"api_key": "sk-keep-me-9999", "model": "m1"}}}
    client.put("/api/llm/settings", json=body)
    body["providers"]["openai"] = {"api_key": "", "model": "m2"}  # blank = keep
    res = client.put("/api/llm/settings", json=body)
    openai = _provider(res.json(), "openai")
    assert openai["has_key"] is True
    assert openai["model"] == "m2"
    assert llm_file.settings()["providers"]["openai"]["api_key"] == "sk-keep-me-9999"


def test_settings_validation(client, llm_file):
    assert client.put("/api/llm/settings",
                      json={"provider": "grok", "providers": {}}).status_code == 422
    res = client.put("/api/llm/settings", json={
        "provider": "openai",
        "providers": {"openai": {"base_url": "ftp://nope"}}})
    assert res.status_code == 422
    assert "http://" in res.json()["detail"]


def test_llm_test_endpoint_ok(client, llm_file, monkeypatch):
    monkeypatch.setattr(llm_module, "complete", lambda *a, **k: "  ok ")
    llm_file.update("openai", {"openai": {"api_key": "k", "model": "m"}})
    res = client.post("/api/llm/test")
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["reply"] == "ok"  # trimmed
    assert isinstance(body["elapsed_ms"], int)


def test_llm_test_endpoint_failure_is_a_settings_answer(client, llm_file, monkeypatch):
    """A dead provider is not a server error: ok=false with the reason,
    so the settings page can just show it."""
    def boom(*args, **kwargs):
        raise LlmError("OpenAI answered HTTP 401: bad key")

    monkeypatch.setattr(llm_module, "complete", boom)
    res = client.post("/api/llm/test")
    assert res.status_code == 200
    assert res.json() == {"ok": False, "error": "OpenAI answered HTTP 401: bad key"}
