"""Application configuration.

All paths can be overridden with environment variables (LOGS_DIR, SCRIPTS_DIR,
DATA_DIR, MAX_CONCURRENT_JOBS, ...).
"""
import os
from pathlib import Path

# Repository root: backend/app/config.py -> up three levels
BASE_DIR = Path(__file__).resolve().parent.parent.parent

LOGS_DIR = Path(os.environ.get("LOGS_DIR", BASE_DIR / "logs")).resolve()
SCRIPTS_DIR = Path(os.environ.get("SCRIPTS_DIR", BASE_DIR / "scripts")).resolve()
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "data")).resolve()
JOBS_DIR = DATA_DIR / "jobs"

# How many scripts may run at the same time; extra jobs wait in "queued".
MAX_CONCURRENT_JOBS = int(os.environ.get("MAX_CONCURRENT_JOBS", "4"))

# Default preview size when peeking at a log file in the UI.
LOG_PREVIEW_LINES = int(os.environ.get("LOG_PREVIEW_LINES", "200"))

# How much of stdout/stderr to inline in job detail responses (live tailing).
STDOUT_TAIL_BYTES = int(os.environ.get("STDOUT_TAIL_BYTES", "32768"))

# Fallback script timeout when a manifest does not specify one.
DEFAULT_TIMEOUT = int(os.environ.get("DEFAULT_TIMEOUT", "600"))

# Shared password for the web UI. Empty (unset) disables authentication —
# only safe while the server is bound to localhost.
AUTH_TOKEN = os.environ.get("SCRIPTENGINE_TOKEN", "").strip()

# First-run bootstrap for multi-user mode: when data/users.json doesn't exist
# yet and both are set, an admin account is created from them at startup
# (SCRIPTENGINE_ADMIN_USER + SCRIPTENGINE_ADMIN_PASSWORD). After that, user
# management lives in the admin UI — these variables are ignored.
ADMIN_BOOTSTRAP_USER = os.environ.get("SCRIPTENGINE_ADMIN_USER", "").strip()
ADMIN_BOOTSTRAP_PASSWORD = os.environ.get("SCRIPTENGINE_ADMIN_PASSWORD", "")

# Auto-delete jobs older than this many days (0 = keep forever).
JOB_RETENTION_DAYS = int(os.environ.get("JOB_RETENTION_DAYS", "0"))

# Script authoring lock. When False, creating, updating or deleting
# scripts through the UI/API is rejected with 403 — the server turns into
# "run the vetted scripts only" for everyone who has the password. Use it
# when other people reach this server: scripts are arbitrary code running
# as the server user, so the editor is effectively remote code execution.
ALLOW_SCRIPT_EDITING = os.environ.get("SCRIPTENGINE_ALLOW_SCRIPT_EDITING", "1").strip().lower() not in ("0", "false", "no", "off")

# Docker sandbox: run every script inside a throwaway container instead of
# directly on the host — no network, a memory cap, and only the script,
# its input and the output directory mounted (input read-only). Off by
# default; see sandbox.Dockerfile for the image run.sh builds for it.
DOCKER_SANDBOX = os.environ.get("SCRIPTENGINE_DOCKER_SANDBOX", "0").strip().lower() in ("1", "true", "yes", "on")
SANDBOX_IMAGE = os.environ.get("SCRIPTENGINE_SANDBOX_IMAGE", "scriptengine-sandbox:latest").strip()
SANDBOX_MEMORY = os.environ.get("SCRIPTENGINE_SANDBOX_MEMORY", "512m").strip()

# Which frontend to serve: "react" (the built SPA in frontend/dist) or
# "python" (server-rendered pages — see app/pages.py). run.sh exposes this
# as `--ui python` / `--ui react`; the two frontends are mutually exclusive
# because the SPA owns a catch-all route.
UI = os.environ.get("SCRIPTENGINE_UI", "react").strip().lower()

# The in-process cron ticker (services/scheduler.py): every 30s, fire
# schedules whose cron expression matches the current minute. Disable with
# SCRIPTENGINE_SCHEDULER=0 — schedules stay listed and editable, they just
# never fire on their own (e.g. to hand firing to a system crontab
# hitting POST /api/schedules/<name>/run instead).
SCHEDULER_ENABLED = os.environ.get(
    "SCRIPTENGINE_SCHEDULER", "1").strip().lower() not in ("0", "false", "no", "off")

# Remote terminal: an interactive shell on the server, in the browser
# (React's Terminal tab; a command box on the python UI's /terminal page).
# OFF by default — a shell is arbitrary code as the server user, strictly
# more powerful than script editing, so it also stays off whenever
# SCRIPTENGINE_ALLOW_SCRIPT_EDITING=0 (the "run the vetted scripts only"
# lockdown). While off, every terminal endpoint answers 404 and both UIs
# hide the feature.
TERMINAL_ENABLED = (
    os.environ.get("SCRIPTENGINE_TERMINAL", "0").strip().lower() in ("1", "true", "yes", "on")
    and ALLOW_SCRIPT_EDITING
)
# Concurrent shell sessions (each holds a pty; strays are reaped by the
# idle timeout below).
TERMINAL_MAX_SESSIONS = int(os.environ.get("SCRIPTENGINE_TERMINAL_MAX_SESSIONS", "4"))
# A session with no input/output for this many seconds is killed (a dead
# browser leaves one behind; 0 disables the reaper).
TERMINAL_IDLE_TIMEOUT = int(os.environ.get("SCRIPTENGINE_TERMINAL_IDLE_TIMEOUT", "1800"))

# LLM provider, first-boot seeding for data/llm.json (the admin Settings
# page owns the file afterwards — these are ignored once it exists, the
# ADMIN_BOOTSTRAP pattern): SCRIPTENGINE_LLM_PROVIDER names the initial
# provider (openai / anthropic / ollama / openai-compatible); the other
# three fill in that provider's api key, model and base URL.
LLM_PROVIDER = os.environ.get("SCRIPTENGINE_LLM_PROVIDER", "").strip()
LLM_API_KEY = os.environ.get("SCRIPTENGINE_LLM_API_KEY", "").strip()
LLM_MODEL = os.environ.get("SCRIPTENGINE_LLM_MODEL", "").strip()
LLM_BASE_URL = os.environ.get("SCRIPTENGINE_LLM_BASE_URL", "").strip()

# Upper bound for a single log file uploaded through the browser (MiB).
MAX_LOG_UPLOAD_MB = int(os.environ.get("MAX_LOG_UPLOAD_MB", "512"))

# Built frontend (served by the backend when present).
FRONTEND_DIST = BASE_DIR / "frontend" / "dist"
