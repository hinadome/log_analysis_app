"""Filesystem-backed user accounts (the multi-user registry).

data/users.json holds every account. Its presence with at least one user
switches the server into multi-user mode (see app/auth.py): login becomes
username + password, and sessions carry a real identity. With no accounts
the server keeps its legacy behavior — anonymous when SCRIPTENGINE_TOKEN is
unset, one shared password when it is — so existing single-operator
deployments are untouched.

Passwords are scrypt-hashed with a per-user salt (stdlib hashlib — no
new dependency), stored as "scrypt$N$r$p$salt_hex$hash_hex". The file is
written atomically (temp + rename) because a half-written registry would
lock everybody out.

The store reloads on mtime change, so hand-editing the file (or wiping it)
takes effect without a restart — the same file-is-truth style as the job
store. Mutators take the User they mean to change (fetched via get/get_by_id
immediately before), mutate and save — the microsecond race between a
concurrent hand-edit and a save is not worth locking for this tool.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ..config import DATA_DIR
from ..models import User, UserRole

# Same shape the editor enforces for script ids — safe as a directory name
# (per-user scripts live in scripts/users/<name>/) and unambiguous in UIs.
SAFE_USERNAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,31}$")

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1  # ~50ms per hash; login is rare


def _hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt,
        n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(user: User, password: str) -> bool:
    """Constant-time check against the stored hash."""
    try:
        _scheme, n, r, p, salt_hex, _hash = user.password_hash.split("$")
        if _scheme != "scrypt":
            return False
        candidate = _hash_password(password, bytes.fromhex(salt_hex))
        return hmac.compare_digest(candidate, user.password_hash)
    except (ValueError, TypeError):
        return False


class UserStore:
    def __init__(self, path: Path = DATA_DIR / "users.json") -> None:
        self._path = path
        self._users: list[User] = []
        self._stamp: tuple[int, int] | None = None  # (mtime_ns, size) of last load
        self._load()

    # -- persistence -----------------------------------------------------
    def _load(self) -> None:
        try:
            stat = self._path.stat()
        except OSError:
            self._users, self._stamp = [], None
            return
        if self._stamp == (stat.st_mtime_ns, stat.st_size):
            return  # unchanged since the last look
        try:
            raw = self._path.read_text(encoding="utf-8")
            self._users = [User.model_validate(u) for u in json.loads(raw).get("users", [])]
            self._stamp = (stat.st_mtime_ns, stat.st_size)
        except (OSError, ValueError):
            # unreadable/corrupt registry: treat as empty rather than crash
            # the server; the admin can still fix the file by hand.
            self._users, self._stamp = [], None

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"users": [u.model_dump() for u in self._users]}, indent=2)
        # atomic: a partial users.json would lock everyone out
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, prefix=".users-")
        try:
            with open(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            Path(tmp).replace(self._path)  # atomic: temp file becomes users.json
        finally:
            Path(tmp).unlink(missing_ok=True)  # no-op after a successful rename
        stat = self._path.stat()
        self._stamp = (stat.st_mtime_ns, stat.st_size)

    # -- queries ----------------------------------------------------------
    def has_users(self) -> bool:
        self._load()
        return bool(self._users)

    def list(self) -> list[User]:
        self._load()
        return list(self._users)

    def get(self, username: str) -> User | None:
        self._load()
        uname = (username or "").strip()
        return next((u for u in self._users if u.username == uname), None)

    def get_by_id(self, user_id: str) -> User | None:
        self._load()
        return next((u for u in self._users if u.id == user_id), None)

    # -- mutations (admin surface) ----------------------------------------
    def create(self, username: str, password: str, role: UserRole = "user") -> User:
        uname = (username or "").strip()
        if not SAFE_USERNAME.match(uname):
            raise ValueError(
                "User names start with a letter/digit and use letters, digits, "
                "'-' or '_' (max 32 chars)")
        if self.get(uname):
            raise ValueError(f"User '{uname}' already exists")
        if len(password or "") < 4:
            raise ValueError("Password must be at least 4 characters")
        if role not in ("admin", "user"):
            raise ValueError("Role must be 'admin' or 'user'")
        user = User(
            id=uuid.uuid4().hex,
            username=uname,
            password_hash=_hash_password(password),
            role=role,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        self._users.append(user)
        self._save()
        return user

    def set_password(self, user: User, password: str) -> None:
        if len(password or "") < 4:
            raise ValueError("Password must be at least 4 characters")
        user.password_hash = _hash_password(password)
        self._save()

    def set_disabled(self, user: User, disabled: bool) -> None:
        user.disabled = disabled
        self._save()

    def set_role(self, user: User, role: UserRole) -> None:
        if role not in ("admin", "user"):
            raise ValueError("Role must be 'admin' or 'user'")
        user.role = role
        self._save()


store = UserStore()
