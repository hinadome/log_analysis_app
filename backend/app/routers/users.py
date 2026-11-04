"""User management — the admin surface (multi-user mode).

List, create, reset passwords, disable/enable, change roles. Guarded by
auth.require_admin, which has one deliberate soft spot: in single-user
mode (no accounts) the local operator IS the admin, so they can create
the first account from the UI — no env-var bootstrap required. Creating
that first account flips the server into multi-user mode, and the
response mints a session for it so the operator stays signed in as the
new admin instead of being bounced to a login page mid-click.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from ..auth import current_user, multi_user_enabled, require_admin, apply_session_cookie
from ..services.session_store import store as session_store
from ..services.user_store import store as user_store

router = APIRouter(prefix="/users", tags=["users"])


def _public(user) -> dict:
    """Everything except the password hash."""
    return {"id": user.id, "username": user.username, "role": user.role,
            "created_at": user.created_at, "disabled": user.disabled}


def _find_or_404(username: str):
    user = user_store.get(username)
    if user is None:
        raise HTTPException(404, detail=f"User not found: {username}")
    return user


def _bad_request(exc: ValueError) -> HTTPException:
    return HTTPException(422, detail=str(exc))


def _guard_last_admin(user, action: str) -> None:
    """The server must keep at least one working admin — stepping down
    (self-demotion, self-disable) is fine whenever another admin remains,
    and refused when this is the last one."""
    if user.role == "admin" and not user.disabled and \
            sum(1 for u in user_store.list() if u.role == "admin" and not u.disabled) == 1:
        raise HTTPException(422, detail=f"That's the last admin — the server "
                                        f"must keep one to {action} them")


class CreateBody(BaseModel):
    username: str
    password: str
    role: str = "user"


class PasswordBody(BaseModel):
    password: str


class RoleBody(BaseModel):
    role: str


@router.get("")
def list_users(request: Request):
    require_admin(request)
    return {"users": [_public(u) for u in user_store.list()]}


@router.post("")
def create_user(body: CreateBody, request: Request, response: Response):
    me = require_admin(request)

    # No accounts yet = this is the bootstrap: the first account must be an
    # admin, or the operator locks themselves out of every admin action.
    if not multi_user_enabled() and body.role != "admin":
        raise HTTPException(422, detail="The first account must be an admin — "
                                        "it becomes the server's administrator")

    try:
        user = user_store.create(body.username, body.password, role=body.role)
    except ValueError as exc:
        raise _bad_request(exc) from exc

    # Creating the first account flipped multi-user mode on; mint a session
    # so whoever bootstraped stays signed in as the new admin.
    if me is None:
        apply_session_cookie(request, response, session_store.create(user.username))
    return {"user": _public(user)}


@router.post("/{username}/password")
def reset_password(username: str, body: PasswordBody, request: Request):
    require_admin(request)
    user = _find_or_404(username)
    try:
        user_store.set_password(user, body.password)
    except ValueError as exc:
        raise _bad_request(exc) from exc
    # every live session of that user dies — a reset means "old logins
    # don't count", including the admin's own if they reset themselves
    session_store.drop_user(user.username)
    return {"ok": True, "revoked_sessions": True}


@router.post("/{username}/disable")
def disable_user(username: str, request: Request):
    require_admin(request)
    user = _find_or_404(username)
    _guard_last_admin(user, "disable")
    user_store.set_disabled(user, True)
    session_store.drop_user(user.username)  # live sessions die with it
    return {"user": _public(user)}


@router.post("/{username}/enable")
def enable_user(username: str, request: Request):
    require_admin(request)
    user = _find_or_404(username)
    user_store.set_disabled(user, False)
    return {"user": _public(user)}


@router.post("/{username}/role")
def change_role(username: str, body: RoleBody, request: Request):
    require_admin(request)
    user = _find_or_404(username)
    if body.role != "admin":
        _guard_last_admin(user, "demote")
    try:
        user_store.set_role(user, body.role)
    except ValueError as exc:
        raise _bad_request(exc) from exc
    return {"user": _public(user)}
