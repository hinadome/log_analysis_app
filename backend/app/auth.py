"""Simple shared-password authentication.

Set LOGSMITH_TOKEN to require a password for the whole API. The browser gets
an HttpOnly session cookie after login; automation can send
`Authorization: Bearer <LOGSMITH_TOKEN>` instead. When LOGSMITH_TOKEN is
unset, authentication is disabled (fine for localhost-only use).

Login attempts are rate-limited per client IP so the shared password can't
be brute-forced online.
"""
from __future__ import annotations

import hashlib
import hmac
import time
from collections import deque

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from .config import AUTH_TOKEN

COOKIE_NAME = "logsmith_session"
SESSION_SALT = "logsmith-session:v1"

# Brute-force protection: at most N failed logins per IP per window.
LOGIN_FAILURE_WINDOW = 60.0  # seconds
LOGIN_MAX_FAILURES = 5
_LOGIN_FAILURES: dict[str, deque[float]] = {}

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


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _prune_failures(ip: str, now: float) -> None:
    """Drop expired timestamps for one IP; remove the entry when empty."""
    attempts = _LOGIN_FAILURES.get(ip)
    if not attempts:
        return
    while attempts and now - attempts[0] >= LOGIN_FAILURE_WINDOW:
        attempts.popleft()
    if not attempts:
        _LOGIN_FAILURES.pop(ip, None)


def _record_failure(ip: str) -> None:
    now = time.monotonic()
    _prune_failures(ip, now)
    _LOGIN_FAILURES.setdefault(ip, deque()).append(now)
    # keep the table bounded even if it saw many distinct clients
    if len(_LOGIN_FAILURES) > 10_000:
        stale = [k for k, v in _LOGIN_FAILURES.items()
                 if not v or now - v[-1] >= LOGIN_FAILURE_WINDOW]
        for key in stale:
            _LOGIN_FAILURES.pop(key, None)


class LoginBody(BaseModel):
    password: str = ""


@router.get("/status")
def auth_status():
    return {"auth_required": auth_required()}


@router.post("/login")
def login(body: LoginBody, request: Request, response: Response):
    if not AUTH_TOKEN:
        return {"ok": True, "auth_required": False}

    ip = _client_ip(request)
    now = time.monotonic()
    _prune_failures(ip, now)
    attempts = _LOGIN_FAILURES.get(ip)
    if attempts and len(attempts) >= LOGIN_MAX_FAILURES:
        retry_after = max(1, round(LOGIN_FAILURE_WINDOW - (now - attempts[0])) + 1)
        raise HTTPException(
            status_code=429,
            detail=f"Too many failed attempts — try again in {retry_after}s",
            headers={"Retry-After": str(retry_after)},
        )

    if not hmac.compare_digest(body.password, AUTH_TOKEN):
        _record_failure(ip)
        raise HTTPException(status_code=401, detail="Incorrect password")

    _LOGIN_FAILURES.pop(ip, None)  # success clears the slate
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
