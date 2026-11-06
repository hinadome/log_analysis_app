"""LLM provider settings — the admin surface for the switch.

GET returns everything both UIs render, with api_key masked: keys never
leave the server (the password rule from users.py). PUT accepts the full
form; an empty api_key means "keep the stored one", so a masked key
round-trips through a browser without being re-typed. POST /test fires a
one-shot completion with the saved settings — the honest check that a
switch actually works before anyone relies on it.

Admin-gated like the terminal: in multi-user mode only an admin may aim
the server at an external endpoint (and hold its key); the single-user
operator is the admin (auth.require_admin's deliberate soft spot).
"""
from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..auth import require_admin
from ..services import llm
from ..services.llm import LlmError, mask_key
from ..services.llm_store import PROVIDERS, store

router = APIRouter(prefix="/llm", tags=["llm"])


class ProviderBody(BaseModel):
    api_key: str = ""
    model: str = ""
    base_url: str = ""


class SettingsBody(BaseModel):
    provider: str
    providers: dict[str, ProviderBody] = {}


def _payload() -> dict:
    """The settings as the UIs render them — registry metadata joined
    with stored fields, keys masked."""
    settings = store.settings()
    providers = []
    for key, meta in PROVIDERS.items():
        fields = settings["providers"].get(key) or {}
        providers.append({
            "key": key,
            "label": meta["label"],
            "hint": meta["hint"],
            "wire": meta["wire"],
            "default_base_url": meta["default_base_url"],
            "base_url": fields.get("base_url", ""),
            "model": fields.get("model", ""),
            "has_key": bool(fields.get("api_key")),
            "api_key_masked": mask_key(fields.get("api_key", "")),
        })
    return {"provider": settings["provider"], "providers": providers}


def _validate(body: SettingsBody) -> None:
    if body.provider != "none" and body.provider not in PROVIDERS:
        raise HTTPException(status_code=422,
                            detail=f"Unknown provider: {body.provider}")
    for key, fields in body.providers.items():
        if key not in PROVIDERS:
            raise HTTPException(status_code=422, detail=f"Unknown provider: {key}")
        base = (fields.base_url or "").strip()
        if base and not base.startswith(("http://", "https://")):
            raise HTTPException(
                status_code=422,
                detail=f"{PROVIDERS[key]['label']} base URL must start with "
                       f"http:// or https://")


@router.get("/settings")
def get_settings(request: Request):
    require_admin(request)
    return _payload()


@router.put("/settings")
def update_settings(body: SettingsBody, request: Request):
    require_admin(request)
    _validate(body)
    try:
        store.update(body.provider,
                     {k: v.model_dump() for k, v in body.providers.items()})
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _payload()  # the new state, masked — save in one round-trip


@router.post("/test")
def test_provider(request: Request):
    """One tiny completion with the saved settings, timed — the check
    that a provider switch actually works. Returns ok: false (HTTP 200)
    on failure: a misconfigured provider is a settings answer, not a
    server error."""
    require_admin(request)
    try:
        started = time.perf_counter()
        reply = llm.complete("Reply with exactly: ok", max_tokens=8, timeout=20.0)
        elapsed_ms = round((time.perf_counter() - started) * 1000)
    except LlmError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "reply": reply.strip()[:200], "elapsed_ms": elapsed_ms}
