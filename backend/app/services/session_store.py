"""Filesystem-backed login sessions (multi-user mode only).

data/sessions.json maps a random session id to the user it belongs to.
The browser keeps only the opaque sid in its cookie; the mapping lives
here, so logout and "disable that user" actually revoke access — impossible
with the legacy stateless cookie (a hash of the shared password).

Same file-is-truth style as the other stores: mtime-cached reads, atomic
writes, pruned lazily (sessions older than SESSION_MAX_AGE never count).
"""
from __future__ import annotations

import json
import secrets
import tempfile
import time
from pathlib import Path

from ..config import DATA_DIR
from .user_store import store as user_store

SESSION_MAX_AGE = 7 * 86400  # seconds; mirrors the cookie's Max-Age


class SessionStore:
    def __init__(self, path: Path = DATA_DIR / "sessions.json") -> None:
        self._path = path
        self._sessions: dict[str, dict] = {}
        self._stamp: tuple[int, int] | None = None
        self._load()

    def _load(self) -> None:
        try:
            stat = self._path.stat()
        except OSError:
            self._sessions, self._stamp = {}, None
            return
        if self._stamp == (stat.st_mtime_ns, stat.st_size):
            return
        try:
            raw = self._path.read_text(encoding="utf-8")
            parsed = json.loads(raw)
            self._sessions = parsed.get("sessions", {}) if isinstance(parsed, dict) else {}
            self._stamp = (stat.st_mtime_ns, stat.st_size)
        except (OSError, ValueError):
            self._sessions, self._stamp = {}, None

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"sessions": self._sessions}, indent=2)
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, prefix=".sessions-")
        try:
            with open(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            Path(tmp).replace(self._path)  # atomic: temp file becomes sessions.json
        finally:
            Path(tmp).unlink(missing_ok=True)
        stat = self._path.stat()
        self._stamp = (stat.st_mtime_ns, stat.st_size)

    def _prune(self) -> None:
        cutoff = time.time() - SESSION_MAX_AGE
        stale = [sid for sid, s in self._sessions.items()
                 if float(s.get("created_at", 0)) < cutoff]
        if stale:
            for sid in stale:
                self._sessions.pop(sid, None)
            self._save()

    # -- lifecycle ---------------------------------------------------------
    def create(self, username: str) -> str:
        self._load()
        self._prune()
        sid = secrets.token_urlsafe(32)
        self._sessions[sid] = {
            "user": username,
            "created_at": time.time(),
            "last_seen": time.time(),
        }
        self._save()
        return sid

    def user_for(self, sid: str | None) -> str | None:
        """The username a session belongs to (None when unknown/expired)."""
        if not sid:
            return None
        self._load()
        self._prune()
        entry = self._sessions.get(sid)
        if not entry:
            return None
        return entry.get("user")

    def drop(self, sid: str | None) -> None:
        if sid and self._sessions.pop(sid, None) is not None:
            self._save()

    def drop_user(self, username: str) -> int:
        """Revoke every session of one user (password reset / disable)."""
        self._load()
        doomed = [sid for sid, s in self._sessions.items()
                  if s.get("user") == username]
        for sid in doomed:
            self._sessions.pop(sid, None)
        if doomed:
            self._save()
        return len(doomed)

    def valid(self, sid: str | None) -> bool:
        """True when the session exists and its user can still log in."""
        username = self.user_for(sid)
        if username is None:
            return False
        user = user_store.get(username)
        return user is not None and not user.disabled


store = SessionStore()
