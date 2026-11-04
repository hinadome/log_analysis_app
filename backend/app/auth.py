"""Authentication: shared password (legacy) or per-user accounts.

Two modes, auto-detected — nothing about an existing deployment changes:

* **Multi-user** — data/users.json exists with at least one account.
  Login is username + password; the cookie carries an opaque session id
  resolved by data/sessions.json, so logout, password resets and disabling
  a user actually revoke access. Scripts and jobs get owners (see the
  routers); an admin account can oversee everything.

* **Legacy / anonymous** — no accounts. LOGSMITH_TOKEN set means the old
  single shared password (cookie = hash of it, or `Authorization: Bearer`
  for automation); unset means no authentication at all (localhost use).

Login attempts are rate-limited per client IP in both modes so passwords
can't be brute-forced online.
"""
from __future__ import annotations

import hashlib
import hmac
import time
from collections import deque

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from .config import AUTH_TOKEN
from .models import User
from .services.session_store import store as session_store
from .services.user_store import store as user_store, verify_password

COOKIE_NAME = "logsmith_session"
SESSION_SALT = "logsmith-session:v1"  # legacy mode only (stateless cookie)
SESSION_MAX_AGE = 7 * 86400  # 7 days, then the browser drops the cookie

# Brute-force protection: at most N failed logins per IP per window.
LOGIN_FAILURE_WINDOW = 60.0  # seconds
LOGIN_MAX_FAILURES = 5
_LOGIN_FAILURES: dict[str, deque[float]] = {}

router = APIRouter(prefix="/auth", tags=["auth"])


# --- mode ---------------------------------------------------------------------

def multi_user_enabled() -> bool:
    """True once at least one account exists (data/users.json)."""
    return user_store.has_users()


def auth_required() -> bool:
    if multi_user_enabled():
        return True
    return bool(AUTH_TOKEN)


# --- authentication checks ------------------------------------------------------

def _legacy_session_value() -> str:
    return hashlib.sha256(f"{SESSION_SALT}:{AUTH_TOKEN}".encode()).hexdigest()


def is_authenticated(request: Request) -> bool:
    if multi_user_enabled():
        # the session must exist AND its user must still be allowed in —
        # disabling an account kills its live sessions too
        return session_store.valid(request.cookies.get(COOKIE_NAME, ""))
    if not AUTH_TOKEN:
        return True
    header = request.headers.get("Authorization", "")
    if header == f"Bearer {AUTH_TOKEN}":
        return True
    return hmac.compare_digest(request.cookies.get(COOKIE_NAME, ""),
                               _legacy_session_value())


def current_user(request: Request) -> User | None:
    """The acting account — None outside multi-user mode (the operator).

    None therefore means "single-user mode: full local rights", which is
    exactly the pre-existing behavior of the whole app.
    """
    if not multi_user_enabled():
        return None
    username = session_store.user_for(request.cookies.get(COOKIE_NAME, ""))
    return user_store.get(username) if username else None


def require_admin(request: Request) -> User | None:
    """403 unless the caller may use admin endpoints.

    In multi-user mode that means an admin account. In single-user mode
    (no accounts) the local operator IS the admin — returning None lets
    them create the first account from the UI (the bootstrap case).
    """
    user = current_user(request)
    if user is None:
        if not multi_user_enabled():
            return None
        raise HTTPException(status_code=403, detail="Admin access required")
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


# --- login (rate-limited in both modes) -----------------------------------------

def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _gate(request: Request) -> None:
    """429 while this IP is hot from too many failures."""
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


def _record_failure(request: Request) -> None:
    ip = _client_ip(request)
    now = time.monotonic()
    _prune_failures(ip, now)
    _LOGIN_FAILURES.setdefault(ip, deque()).append(now)
    # keep the table bounded even if it saw many distinct clients
    if len(_LOGIN_FAILURES) > 10_000:
        stale = [k for k, v in _LOGIN_FAILURES.items()
                 if not v or now - v[-1] >= LOGIN_FAILURE_WINDOW]
        for key in stale:
            _LOGIN_FAILURES.pop(key, None)


def check_login(request: Request, username: str, password: str) -> str:
    """Verify a login in whichever mode the server is in.

    Returns the cookie value to set ("" when auth is disabled); raises
    HTTPException 401/429 on failure. A success clears the failure
    counter. Shared by the JSON login and the HTML form.
    """
    if multi_user_enabled():
        _gate(request)
        user = user_store.get(username or "")
        # one message for unknown user, wrong password and disabled account —
        # never confirm which usernames exist
        if user is None or user.disabled or not verify_password(user, password or ""):
            _record_failure(request)
            raise HTTPException(status_code=401,
                                detail="Incorrect username or password")
        _LOGIN_FAILURES.pop(_client_ip(request), None)
        return session_store.create(user.username)

    # legacy: the shared password (no username involved)
    _gate(request)
    if not AUTH_TOKEN:
        return ""
    if not hmac.compare_digest(password or "", AUTH_TOKEN):
        _record_failure(request)
        raise HTTPException(status_code=401, detail="Incorrect password")
    _LOGIN_FAILURES.pop(_client_ip(request), None)
    return _legacy_session_value()


def check_password(request: Request, password: str) -> str:
    """Legacy entry kept for the shared-password flow (see check_login)."""
    return check_login(request, "", password)


def apply_session_cookie(request: Request, response: Response,
                         value: str = "") -> None:
    """Set the session cookie on a response (7-day expiry, Secure on HTTPS).

    `value` is the sid (multi-user) or the legacy hash; "" derives the
    legacy value, matching the old single-argument signature.
    """
    if not value and not multi_user_enabled():
        value = _legacy_session_value()
    response.set_cookie(
        COOKIE_NAME,
        value,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/",
        max_age=SESSION_MAX_AGE,  # sessions expire; without this they live forever
    )


def _prune_failures(ip: str, now: float) -> None:
    """Drop expired timestamps for one IP; remove the entry when empty."""
    attempts = _LOGIN_FAILURES.get(ip)
    if not attempts:
        return
    while attempts and now - attempts[0] >= LOGIN_FAILURE_WINDOW:
        attempts.popleft()
    if not attempts:
        _LOGIN_FAILURES.pop(ip, None)


class LoginBody(BaseModel):
    username: str = ""
    password: str = ""


@router.get("/status")
def auth_status(request: Request):
    # the legacy/anonymous shape stays exactly {"auth_required": bool} —
    # multi-user adds identity on top, never changes the old contract
    if not multi_user_enabled():
        return {"auth_required": auth_required()}
    user = current_user(request)
    return {
        "auth_required": True,
        "multi_user": True,
        "user": {"username": user.username, "role": user.role} if user else None,
    }


@router.post("/login")
def login(body: LoginBody, request: Request, response: Response):
    value = check_login(request, body.username, body.password)
    if value:
        apply_session_cookie(request, response, value)
    return {"ok": True, "auth_required": auth_required()}


@router.post("/logout")
def logout(request: Request, response: Response):
    if multi_user_enabled():
        session_store.drop(request.cookies.get(COOKIE_NAME, ""))
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}
