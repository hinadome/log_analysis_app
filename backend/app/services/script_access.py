"""Script visibility and permissions (multi-user mode).

Ownership is locational — the filesystem is the index, no registry to
keep in sync: a script at the top level of scripts/ is the shared
library (owner None), one under scripts/users/<name>/ belongs to that
account. This module is the single place that turns "who is asking"
into "which scripts exist for them" and "what may they do with each".

`user` in every helper is the acting account, or None for the trusted
contexts that carry no identity: the single-user operator (the local
admin before any account exists) and the scheduler firing a shared
schedule. Both see and may do everything — exactly the pre-multi-user
behavior, which is what keeps single-user deployments identical to
what shipped before.
"""
from __future__ import annotations

from pathlib import Path

from ..models import ScriptInfo, User
from . import scanner

# Container under scripts/ holding the per-account private spaces.
USERS_SUBDIR = "users"


def private_dir(username: str) -> Path:
    """The directory a user's private scripts live in."""
    return scanner.SCRIPTS_DIR / USERS_SUBDIR / username


def destination_dir(user: User | None) -> Path:
    """Where a new script lands: the caller's private space in multi-user
    mode (admins included — sharing is then an explicit step), the shared
    library root otherwise."""
    if user is None:
        return scanner.SCRIPTS_DIR
    d = private_dir(user.username)
    d.mkdir(parents=True, exist_ok=True)
    return d


def visible_scripts(user: User | None) -> list[ScriptInfo]:
    """Scripts one account may see and run.

    A regular account gets the shared library plus its own private
    scripts; other users' private scripts don't exist for them (not
    listed, not runnable, not editable — 404, never a confirmation).
    Admins and the trusted no-identity contexts see everything.
    """
    scripts = scanner.discover_scripts()
    if user is None or user.role == "admin":
        return scripts
    return [s for s in scripts if s.owner is None or s.owner == user.username]


def visible_by_id(user: User | None) -> dict[str, ScriptInfo]:
    return {s.id: s for s in visible_scripts(user)}


# --- per-script permissions --------------------------------------------------
#
# Sharing policy (deliberately permissive — small trusted teams):
#   edit    private: owner or admin; shared: any account (the
#           LOGSMITH_ALLOW_SCRIPT_EDITING flag stays the real gate)
#   delete  private: owner or admin; shared: admin only
#   share   owner or admin — moves the script into the shared library
#   unshare admin only — moves it out, into that admin's private space
#           (a shared script records no origin, so the admin who
#           unshares takes it over)

def may_edit(script: ScriptInfo, user: User | None) -> bool:
    if script.owner is None or user is None or user.role == "admin":
        return True
    return script.owner == user.username


def may_delete(script: ScriptInfo, user: User | None) -> bool:
    if script.owner is None:
        return user is None or user.role == "admin"
    return may_edit(script, user)


def may_share(script: ScriptInfo, user: User | None) -> bool:
    return script.owner is not None and (
        user is None or user.role == "admin" or script.owner == user.username)


def may_unshare(script: ScriptInfo, user: User | None) -> bool:
    # requires a real admin account: the unshared script has to land in
    # someone's private space, namely theirs
    return script.owner is None and user is not None and user.role == "admin"
