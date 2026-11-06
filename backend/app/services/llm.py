"""The LLM adapter: one call function over two wire formats.

Everything configurable lives in llm_store (data/llm.json — see there);
this module only interprets it. Two request shapes cover the market:

* "openai" wire — POST {base_url}/chat/completions with
  Authorization: Bearer. OpenAI itself, Ollama, vLLM, LM Studio,
  OpenRouter, DeepSeek, Groq, … all speak it (the Bearer header is
  simply omitted when no key is set — local endpoints want none).
* "anthropic" wire — POST {base_url}/v1/messages with x-api-key and
  anthropic-version. Claude's native API.

complete() is synchronous (httpx, mandatory timeout — an LLM endpoint
can hang and this app does not hang with it). Call it from sync FastAPI
routes (those run in a threadpool) or wrap it in a thread from async
code; never call it bare inside an async handler.
"""
from __future__ import annotations

import json

import httpx

from .llm_store import PROVIDERS, store


class LlmError(Exception):
    """A failed or ill-configured LLM call. The message is user-facing
    (it renders in both UIs), so it explains, not just reports."""


DEFAULT_TIMEOUT = 60.0  # seconds, one completion


def active() -> dict | None:
    """The resolved active provider config, or None when the LLM is off
    (provider "none" — the default). base_url falls back to the
    registry's default when the stored one is empty."""
    settings = store.settings()
    key = settings["provider"]
    if key == "none":
        return None
    meta = PROVIDERS[key]
    fields = settings["providers"].get(key) or {}
    return {
        "key": key,
        "label": meta["label"],
        "wire": meta["wire"],
        "base_url": fields.get("base_url") or meta["default_base_url"],
        "api_key": fields.get("api_key", ""),
        "model": fields.get("model", ""),
    }


def mask_key(key: str) -> str:
    """Enough of a key to recognize it, never enough to use it. Same
    stance as the users API: secrets do not leave the server."""
    if not key:
        return ""
    if len(key) <= 8:
        return "••••"
    return f"{key[:3]}…{key[-4:]}"


def complete(prompt: str, system: str | None = None,
             max_tokens: int = 1024, timeout: float = DEFAULT_TIMEOUT) -> str:
    """One completion from the active provider. Raises LlmError — never
    a raw httpx exception — so callers have one failure type to render."""
    cfg = active()
    if cfg is None:
        raise LlmError("No LLM provider is configured — pick one in Settings")
    if not cfg["model"]:
        raise LlmError(f"The {cfg['label']} provider has no model set — "
                        f"add one in Settings")
    if cfg["wire"] == "anthropic":
        return _anthropic(cfg, prompt, system, max_tokens, timeout)
    return _openai(cfg, prompt, system, max_tokens, timeout)


def _openai(cfg: dict, prompt: str, system: str | None,
            max_tokens: int, timeout: float) -> str:
    url = f"{cfg['base_url'].rstrip('/')}/chat/completions"
    headers = {"Authorization": f"Bearer {cfg['api_key']}"} if cfg["api_key"] else {}
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    data = _post(url, headers, cfg["label"], timeout, model=cfg["model"],
                 messages=messages, max_tokens=max_tokens)
    try:
        return data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise LlmError(f"Unexpected {cfg['label']} response shape: "
                       f"{json.dumps(data)[:200]}") from exc


def _anthropic(cfg: dict, prompt: str, system: str | None,
               max_tokens: int, timeout: float) -> str:
    if not cfg["api_key"]:
        raise LlmError("Anthropic needs an API key — set one in Settings")
    url = f"{cfg['base_url'].rstrip('/')}/v1/messages"
    headers = {"x-api-key": cfg["api_key"], "anthropic-version": "2023-06-01"}
    body: dict = {"model": cfg["model"], "max_tokens": max_tokens,
                  "messages": [{"role": "user", "content": prompt}]}
    if system:
        body["system"] = system
    data = _post(url, headers, cfg["label"], timeout, **body)
    text = "".join(block.get("text", "") for block in data.get("content", []))
    if not text:
        raise LlmError(f"Unexpected {cfg['label']} response shape: "
                       f"{json.dumps(data)[:200]}")
    return text


def _post(url: str, headers: dict, label: str, timeout: float, **body) -> dict:
    try:
        res = httpx.post(url, headers=headers, json=body, timeout=timeout)
    except httpx.HTTPError as exc:
        raise LlmError(f"{label} call failed: {exc}") from exc
    if res.status_code >= 400:
        raise LlmError(f"{label} answered HTTP {res.status_code}: "
                       f"{res.text[:300]}")
    try:
        return res.json()
    except ValueError as exc:
        raise LlmError(f"{label} answered non-JSON "
                       f"(HTTP {res.status_code})") from exc
