"""Simple shared-password authentication.

Set LOGSMITH_TOKEN to require a password for the whole API. The browser gets
an HttpOnly session cookie after login; automation can send
`Authorization: Bearer <LOGSMITH_TOKEN>` instead. When LOGSMITH_TOKEN is
unset, authentication is disabled (fine for localhost-only use).
"""
from __future__ import annotations

import hashlib
import hmac

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from .config import AUTH_TOKEN

COOKIE_NAME = "logsmith_session"
SESSION_SALT = "logsmith-session:v1"

router = APIRouter(prefix="/auth", tags=["auth"])


def auth_required() -> bool:
    return bool(AUTH_TOKEN)


def _session_value() -> str:
    return hashlib.sha256(f"{SESSION_SALT}:{AUTH_TOKEN}".encode()).hexdigest()


def is_authenticated(request: Request) -> bool:
    if not AUTH_TOKEN:
        return True
    header = request.headers.get("Authorization", "")
    if header == f"Bearer {AUTH_TOKEN}":
        return True
    return hmac.compare_digest(request.cookies.get(COOKIE_NAME, ""), _session_value())


class LoginBody(BaseModel):
    password: str = ""


@router.get("/status")
def auth_status():
    return {"auth_required": auth_required()}


@router.post("/login")
def login(body: LoginBody, request: Request, response: Response):
    if not AUTH_TOKEN:
        return {"ok": True, "auth_required": False}
    if not hmac.compare_digest(body.password, AUTH_TOKEN):
        raise HTTPException(status_code=401, detail="Incorrect password")
    response.set_cookie(
        COOKIE_NAME,
        _session_value(),
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/",
    )
    return {"ok": True, "auth_required": True}


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}
