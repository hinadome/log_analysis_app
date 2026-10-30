"""FastAPI application entry point.

Run (from the backend directory):
    uvicorn app.main:app --reload

Or from the repo root after building the frontend:
    uvicorn --app-dir backend app.main:app
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import auth
from .config import (FRONTEND_DIST, LOGS_DIR, SCRIPTS_DIR, MAX_CONCURRENT_JOBS,
                     DATA_DIR, JOB_RETENTION_DAYS)
from .routers import jobs, logs, scripts
from .services.job_store import JobStore

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
    if not auth.auth_required():
        print("WARNING: LOGSMITH_TOKEN is not set — running without authentication.")
    yield
    await jobs.executor.shutdown()


app = FastAPI(title="LogSmith", description="Server-side log analysis workbench",
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


@app.get("/api/health", tags=["meta"])
def health():
    return {
        "status": "ok",
        "logs_dir": str(LOGS_DIR),
        "scripts_dir": str(SCRIPTS_DIR),
        "data_dir": str(DATA_DIR),
        "max_concurrent_jobs": MAX_CONCURRENT_JOBS,
        "auth_required": auth.auth_required(),
        "job_retention_days": JOB_RETENTION_DAYS,
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


# Serve the built React app when frontend/dist exists (dev mode uses Vite).
if (FRONTEND_DIST / "index.html").is_file():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str):
        if full_path == "api" or full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not found")
        candidate = FRONTEND_DIST / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(FRONTEND_DIST / "index.html")
