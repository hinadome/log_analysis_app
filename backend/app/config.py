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
AUTH_TOKEN = os.environ.get("LOGSMITH_TOKEN", "").strip()

# Auto-delete jobs older than this many days (0 = keep forever).
JOB_RETENTION_DAYS = int(os.environ.get("JOB_RETENTION_DAYS", "0"))

# Which frontend to serve: "react" (the built SPA in frontend/dist) or
# "python" (server-rendered pages — see app/pages.py). run.sh exposes this
# as `--ui python` / `--ui react`; the two frontends are mutually exclusive
# because the SPA owns a catch-all route.
UI = os.environ.get("LOGSMITH_UI", "react").strip().lower()

# Upper bound for a single log file uploaded through the browser (MiB).
MAX_LOG_UPLOAD_MB = int(os.environ.get("MAX_LOG_UPLOAD_MB", "512"))

# Built frontend (served by the backend when present).
FRONTEND_DIST = BASE_DIR / "frontend" / "dist"
