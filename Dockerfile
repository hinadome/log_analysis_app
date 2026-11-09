# ScriptEngine — multi-stage image: build the React app, then ship it with the API.
#
# Build:   docker build -t scriptengine .
# Run:     docker run -p 8000:8000 \
#            -v scriptengine-logs:/app/logs -v scriptengine-data:/app/data \
#            -e SCRIPTENGINE_TOKEN=... scriptengine
#
# The container serves plain HTTP (TLS belongs to whatever proxy fronts it)
# and ships the Python/Bash sample scripts; Node/Ruby/Perl scripts need their
# interpreters added to the image.

# ---------- stage 1: frontend build ----------
FROM node:22-slim AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-fund --no-audit
COPY frontend/ ./
RUN npm run build

# ---------- stage 2: runtime ----------
FROM python:3.12-slim
WORKDIR /app

# app/ resolves paths relative to backend/app/../.. — keep the repo layout
ENV SCRIPTENGINE_HTTP=1 \
    LOGS_DIR=/app/logs \
    DATA_DIR=/app/data \
    SCRIPTS_DIR=/app/scripts

COPY backend/requirements.lock ./backend/requirements.lock
RUN pip install --no-cache-dir -r backend/requirements.lock

COPY backend/app ./backend/app
COPY scripts ./scripts
COPY --from=frontend /build/dist ./frontend/dist

# writable state lives in volumes (logs to analyze, job history)
RUN mkdir -p /app/logs /app/data
VOLUME /app/logs /app/data

EXPOSE 8000
CMD ["uvicorn", "--app-dir", "backend", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
