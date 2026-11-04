"""Terminal endpoints: create a shell session, stream its output, feed it.

Everything hides behind LOGSMITH_TERMINAL (config.TERMINAL_ENABLED): while
the flag is off, every endpoint answers 404 as if the feature did not
exist. With accounts on (multi-user mode) the shell is admin-only — a
terminal is a shell as the server user, the one surface no regular
account should reach; the single-user operator keeps it. The transport is
deliberately SSE + POST rather than a WebSocket — it needs no extra
dependency and rides the same HTTP auth middleware as the rest of the
API (and EventSource cannot send headers, but the session cookie needs
none).

Protocol (SSE frames, JSON like the job events stream):
    data: {"type": "output", "data": "<base64 pty bytes>"}
    data: {"type": "exit",   "code": <exit code or null>}
"""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .. import auth
from ..config import TERMINAL_ENABLED, TERMINAL_MAX_SESSIONS
from ..services.terminal import SessionDead, SessionLimit, terminal

router = APIRouter(prefix="/terminal", tags=["terminal"])

# keystrokes per request — enough for a paste, small enough to reason about
MAX_INPUT_BYTES = 256 * 1024


def _gated(request: Request, sid: str | None = None):
    """404 while the feature is off (hide it); 403 for non-admins once
    accounts exist (require_admin is a no-op for the single-user operator);
    also resolve the session."""
    if not TERMINAL_ENABLED:
        raise HTTPException(status_code=404, detail="Not found")
    auth.require_admin(request)
    if sid is not None:
        session = terminal.get(sid)
        if session is None:
            raise HTTPException(status_code=404, detail="Unknown terminal session")
        return session
    return None


class SizeBody(BaseModel):
    cols: int = Field(ge=2, le=500)
    rows: int = Field(ge=2, le=300)


@router.post("/sessions")
async def create_session(request: Request):
    _gated(request)
    try:
        session = terminal.create()
    except SessionLimit:
        raise HTTPException(
            status_code=429,
            detail=(f"All {TERMINAL_MAX_SESSIONS} terminal sessions are in use — "
                    "close one (or wait for the idle timeout) and try again"))
    return {"id": session.id, "shell": session.shell}


@router.get("/sessions/{sid}/output")
async def session_output(sid: str, request: Request):
    session = _gated(request, sid)
    return StreamingResponse(
        terminal.stream_output(session),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/sessions/{sid}/input")
async def session_input(sid: str, request: Request):
    """Raw body = keystrokes, exactly as typed (no JSON wrapper)."""
    session = _gated(request, sid)
    data = await request.body()
    if len(data) > MAX_INPUT_BYTES:
        raise HTTPException(status_code=413,
                            detail="input too large for a terminal paste")
    try:
        await terminal.write(session, data)
    except SessionDead:
        raise HTTPException(status_code=410, detail="The session has ended")
    except (TimeoutError, asyncio.TimeoutError):  # same class on 3.11+
        raise HTTPException(status_code=504, detail="the shell stopped reading input")
    return {"ok": True}


@router.post("/sessions/{sid}/size")
async def session_size(sid: str, body: SizeBody, request: Request):
    session = _gated(request, sid)
    try:
        terminal.resize(session, body.cols, body.rows)
    except SessionDead:
        raise HTTPException(status_code=410, detail="The session has ended")
    except OSError:
        raise HTTPException(status_code=410, detail="The session has ended")
    return {"ok": True, "cols": body.cols, "rows": body.rows}


@router.delete("/sessions/{sid}")
async def kill_session(sid: str, request: Request):
    _gated(request)
    if not terminal.kill(sid):
        raise HTTPException(status_code=404, detail="Unknown terminal session")
    return {"ok": True}
