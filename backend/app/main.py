"""FastAPI application entry point.

Run (from the backend directory):
    uvicorn app.main:app --reload

Or from the repo root after building the frontend:
    uvicorn --app-dir backend app.main:app
"""
from contextlib import asynccontextmanager
from shutil import which

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import auth
from .config import (FRONTEND_DIST, LOGS_DIR, SCRIPTS_DIR, MAX_CONCURRENT_JOBS,
                     DATA_DIR, JOB_RETENTION_DAYS, UI, ALLOW_SCRIPT_EDITING,
                     DOCKER_SANDBOX, SANDBOX_IMAGE, SCHEDULER_ENABLED,
                     TERMINAL_ENABLED, ADMIN_BOOTSTRAP_USER, ADMIN_BOOTSTRAP_PASSWORD)
from .routers import jobs, llm, logs, pipelines, schedules, scripts, terminal, users
from .services import decompress_cache
from .services.job_store import JobStore
from .services.scheduler import scheduler
from .services.terminal import terminal as terminal_service
from .services.user_store import store as users_store
from .services import llm_store

jobs_store: JobStore = jobs.store


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Directories may not exist on the very first run.
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # Jobs left over from a previous run can never finish.
    jobs_store.reconcile()
    jobs.executor.start()
    # Delete jobs beyond the retention window (0 = disabled).
    removed = jobs_store.prune(JOB_RETENTION_DAYS)
    if removed:
        print(f"job retention: removed {removed} job(s) older than {JOB_RETENTION_DAYS} day(s)")
    # Drop decompressed-cache entries whose source log is gone or changed;
    # entries for still-valid sources stay so reruns on them stay free.
    cache_removed = decompress_cache.prune()
    if cache_removed:
        print(f"decompressed cache: removed {cache_removed} stale file(s)")
    if not auth.auth_required():
        print("WARNING: SCRIPTENGINE_TOKEN is not set — running without authentication.")
    # First run with bootstrap env vars: seed the initial admin, flipping the
    # server into multi-user mode (per-user scripts/jobs, user management).
    if ADMIN_BOOTSTRAP_USER and ADMIN_BOOTSTRAP_PASSWORD and not users_store.has_users():
        users_store.create(ADMIN_BOOTSTRAP_USER, ADMIN_BOOTSTRAP_PASSWORD, role="admin")
        print(f"multi-user: bootstrapped admin '{ADMIN_BOOTSTRAP_USER}' "
              "from SCRIPTENGINE_ADMIN_USER/SCRIPTENGINE_ADMIN_PASSWORD "
              "(ignored on future starts — manage users in the admin UI)")
    if auth.multi_user_enabled():
        print(f"multi-user: {len(users_store.list())} account(s) — "
              "login is username + password; scripts and jobs are per-user")
    # First run with SCRIPTENGINE_LLM_* set: write the initial data/llm.json
    # so a container/deployment lands pre-configured (afterwards the
    # Settings UI owns the file — the env vars are ignored).
    llm_store.seed_from_env()
    if DOCKER_SANDBOX:
        print(f"docker sandbox: enabled (image {SANDBOX_IMAGE}, no network)")
        if which("docker") is None:
            print("WARNING: SCRIPTENGINE_DOCKER_SANDBOX=1 but the docker CLI is "
                  "not on PATH — every job will fail until Docker is installed.")
    # The cron ticker. Schedules whose minute was missed while the server
    # was down stay missed — no catch-up by design.
    if SCHEDULER_ENABLED:
        scheduler.start()
        print(f"scheduler: enabled (checking schedules every "
              f"{scheduler.interval:.0f}s, local time)")
    if TERMINAL_ENABLED:
        print("terminal: enabled — admin shells run as the server user "
              "(SCRIPTENGINE_TERMINAL=0 hides it)")
    yield
    await scheduler.shutdown()
    await jobs.executor.shutdown()
    await terminal_service.shutdown()


app = FastAPI(title="ScriptEngine", description="Server-side log analysis workbench",
              version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "https://localhost:5173",
        "https://127.0.0.1:5173",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def authentication_middleware(request, call_next):
    path = request.url.path
    if (path.startswith("/api") and not path.startswith("/api/auth")
            and not auth.is_authenticated(request)):
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)
    return await call_next(request)


app.include_router(auth.router, prefix="/api")
app.include_router(logs.router, prefix="/api")
app.include_router(scripts.router, prefix="/api")
app.include_router(jobs.router, prefix="/api")
app.include_router(pipelines.router, prefix="/api")
app.include_router(schedules.router, prefix="/api")
app.include_router(terminal.router, prefix="/api")
app.include_router(users.router, prefix="/api")
app.include_router(llm.router, prefix="/api")


@app.get("/api/health", tags=["meta"])
def health():
    return {
        "status": "ok",
        "logs_dir": str(LOGS_DIR),
        "scripts_dir": str(SCRIPTS_DIR),
        "data_dir": str(DATA_DIR),
        "max_concurrent_jobs": MAX_CONCURRENT_JOBS,
        "auth_required": auth.auth_required(),
        "multi_user": auth.multi_user_enabled(),
        "script_editing": ALLOW_SCRIPT_EDITING,
        "docker_sandbox": DOCKER_SANDBOX,
        "sandbox_image": SANDBOX_IMAGE if DOCKER_SANDBOX else None,
        "job_retention_days": JOB_RETENTION_DAYS,
        "scheduler": SCHEDULER_ENABLED,
        "terminal": TERMINAL_ENABLED,
        "data_dir_size": _dir_size(DATA_DIR),
    }


def _dir_size(path) -> int:
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            pass
    return total


# --- frontend ------------------------------------------------------------
# Two interchangeable frontends, selected at startup via SCRIPTENGINE_UI (run.sh
# --ui): the server-rendered python pages or the built React SPA. They are
# mutually exclusive — the SPA owns a catch-all route.
if UI == "python":
    from . import pages

    # mount before the pages router: its catch-all must not swallow /static
    app.mount("/static", StaticFiles(directory=pages.STATIC_DIR), name="static")
    app.include_router(pages.router)
elif (FRONTEND_DIST / "index.html").is_file():
    # Serve the built React app when frontend/dist exists (dev mode uses Vite).
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str):
        if full_path == "api" or full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not found")
        candidate = FRONTEND_DIST / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(FRONTEND_DIST / "index.html")
