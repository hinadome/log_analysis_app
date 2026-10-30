#!/usr/bin/env bash
# LogSmith — start and stop the app. Plain userland, no root/systemctl needed.
#
#   ./run.sh                  production server in the FOREGROUND (Ctrl+C stops it)
#   ./run.sh start            production server in the background
#   ./run.sh start --dev      dev mode in the background: backend with auto-reload
#                             + Vite dev server on http://localhost:5173
#   ./run.sh stop             stop everything this script started
#   ./run.sh status           show what is running
#   ./run.sh restart [--dev]  stop, then start again
#
# Every mode also accepts --ui python|react (default react): which frontend
# the backend serves — the built React SPA, or the server-rendered pages
# (no build step). The LOGSMITH_UI environment variable does the same.
#
# Security: binding a non-local address without LOGSMITH_TOKEN generates a
# strong password (printed once, saved in .run/authtoken, reused across
# restarts) — scripts run as this user, so an open server is remote code
# execution. Set LOGSMITH_TOKEN to choose the password, or LOGSMITH_INSECURE=1
# to run open anyway (not recommended). LOGSMITH_ALLOW_SCRIPT_EDITING=0 also
# locks script authoring while keeping the existing scripts runnable.
#
# LOGSMITH_DOCKER_SANDBOX=1 runs every script inside a throwaway Docker
# container instead of directly on the host: no network, a memory cap, and
# only the script + its input + the output directory mounted. The sandbox
# image (sandbox.Dockerfile) is built automatically on first use.
#
# Anything else (e.g. './run.sh --port 8001') runs the foreground server with
# the args passed through to uvicorn, exactly like before (except --ui,
# which this script consumes).
#
# Background processes append logs to .run/*.log and their PIDs live in
# .run/*.pid (both gitignored). `stop` only kills PIDs this script wrote —
# it never touches a process it did not start.
set -euo pipefail
cd "$(dirname "$0")"

RUN_DIR=.run
BACKEND_PID="$RUN_DIR/backend.pid"
FRONTEND_PID="$RUN_DIR/frontend.pid"
BACKEND_LOG="$RUN_DIR/backend.log"
FRONTEND_LOG="$RUN_DIR/frontend.log"
FRONTEND_PORT=5173
BACKEND_PORT=8000  # overridden by --port in start/foreground modes
HOST="${LOGSMITH_HOST:-0.0.0.0}"  # bind address: all interfaces (use 127.0.0.1 to go back to local-only)
UI="${LOGSMITH_UI:-react}"        # frontend: react SPA or python pages (--ui)

usage() {
  sed -n '2,34p' "$0" | sed 's/^# \{0,1\}//'
}

# --- small helpers ------------------------------------------------------

pid_of() { [ -f "$1" ] && cat "$1" || true; }

is_running() { # $1 = pid file
  local pid
  pid=$(pid_of "$1")
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

port_busy() { lsof -ti ":$1" >/dev/null 2>&1; }

# Best-effort LAN address, to show a URL other devices can reach.
lan_ip() {
  local ip=""
  if command -v ipconfig >/dev/null 2>&1; then
    ip=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)
  fi
  if [ -z "$ip" ] && command -v hostname >/dev/null 2>&1; then
    ip=$(hostname -I 2>/dev/null | awk '{print $1}' || true)
  fi
  echo "$ip"
}

# Secure by default: scripts run with the permissions of this user, so a
# non-local bind with no LOGSMITH_TOKEN would be remote code execution for
# anyone on the network. This generates a strong password instead (reused
# across restarts). Set LOGSMITH_TOKEN to choose your own, or
# LOGSMITH_INSECURE=1 to run open (not recommended).
AUTH_TOKEN_FILE="$RUN_DIR/authtoken"
AUTH_MODE="none"   # none (local bind) | custom | generated | insecure
AUTH_SHOW_TOKEN=0  # 1 = print the token value at startup (first generation)

ensure_auth() {
  if [ -n "${LOGSMITH_TOKEN:-}" ]; then AUTH_MODE="custom"; return 0; fi
  if [ "$HOST" = "localhost" ] || [ "$HOST" = "127.0.0.1" ]; then return 0; fi
  if [ "${LOGSMITH_INSECURE:-0}" = "1" ]; then AUTH_MODE="insecure"; return 0; fi
  mkdir -p "$RUN_DIR"
  if [ -s "$AUTH_TOKEN_FILE" ]; then
    LOGSMITH_TOKEN="$(cat "$AUTH_TOKEN_FILE")"
  else
    if command -v openssl >/dev/null 2>&1; then
      LOGSMITH_TOKEN="$(openssl rand -hex 16)"
    else
      LOGSMITH_TOKEN="$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    fi
    printf '%s\n' "$LOGSMITH_TOKEN" > "$AUTH_TOKEN_FILE"
    chmod 600 "$AUTH_TOKEN_FILE" 2>/dev/null || true
    AUTH_SHOW_TOKEN=1
  fi
  AUTH_MODE="generated"
  export LOGSMITH_TOKEN
}

auth_report() {
  case "$AUTH_MODE" in
    custom)
      echo "Auth     ●  password required (LOGSMITH_TOKEN)" ;;
    generated)
      if [ "$AUTH_SHOW_TOKEN" = "1" ]; then
        echo "Auth     ●  password required: $LOGSMITH_TOKEN"
        echo "           generated, saved in $AUTH_TOKEN_FILE — set LOGSMITH_TOKEN to pin your own"
      else
        echo "Auth     ●  password required (token: cat $AUTH_TOKEN_FILE)"
      fi
      echo "           tip: LOGSMITH_ALLOW_SCRIPT_EDITING=0 also locks script authoring" ;;
    insecure)
      echo "Auth     ✗  DISABLED (LOGSMITH_INSECURE=1) — anyone who can reach this" >&2
      echo "           machine can run scripts as you" >&2 ;;
    none)
      echo "Auth     ●  none (local bind only)" ;;
  esac
}

# Docker sandbox (LOGSMITH_DOCKER_SANDBOX=1): scripts run in throwaway
# containers instead of directly on the host. Fails loudly — never silently
# falls back to direct execution, which is exactly what the flag exists to
# avoid.
SANDBOX_ENABLED=0
SANDBOX_IMAGE="${LOGSMITH_SANDBOX_IMAGE:-logsmith-sandbox:latest}"

# `docker info` can hang for a long time when Docker Desktop is starting up
# or half-quit; guard it with a watchdog so the launcher stays responsive.
docker_daemon_ok() {
  docker info >/dev/null 2>&1 &
  local probe=$!
  ( sleep 10 && kill "$probe" 2>/dev/null ) &
  local watchdog=$!
  local rc=0
  wait "$probe" 2>/dev/null || rc=$?
  kill "$watchdog" 2>/dev/null || true
  wait "$watchdog" 2>/dev/null || true
  [ "$rc" = "0" ]
}

ensure_sandbox() {
  [ "${LOGSMITH_DOCKER_SANDBOX:-0}" = "1" ] || return 0
  SANDBOX_ENABLED=1
  export LOGSMITH_DOCKER_SANDBOX=1
  if ! command -v docker >/dev/null 2>&1; then
    echo "Error: LOGSMITH_DOCKER_SANDBOX=1 but docker is not installed." >&2
    exit 1
  fi
  if ! docker_daemon_ok; then
    echo "Error: LOGSMITH_DOCKER_SANDBOX=1 but the Docker daemon is not" >&2
    echo "       reachable — start Docker Desktop first (or unset the flag" >&2
    echo "       to run scripts directly)." >&2
    exit 1
  fi
  if ! docker image inspect "$SANDBOX_IMAGE" >/dev/null 2>&1; then
    echo "Sandbox  ●  building image $SANDBOX_IMAGE (first time only)…"
    docker build -f sandbox.Dockerfile -t "$SANDBOX_IMAGE" .
  fi
}

sandbox_report() {
  [ "$SANDBOX_ENABLED" = "1" ] || return 0
  echo "Sandbox  ●  docker ($SANDBOX_IMAGE — no network, ${LOGSMITH_SANDBOX_MEMORY:-512m} RAM per job)"
}

# The TLS cert only covers localhost; other devices will see a browser warning.
cert_hint() {
  if [ "${LOGSMITH_HTTP:-0}" = "1" ]; then return 0; fi
  if [ "$HOST" = "localhost" ] || [ "$HOST" = "127.0.0.1" ]; then return 0; fi
  local example="my-machine.local"
  local ip
  ip=$(lan_ip)
  [ -n "$ip" ] && example="$ip,my-machine.local"
  echo "  Note: the TLS certificate only covers localhost — other devices" >&2
  echo "  will show a browser warning. To include them: delete certs/ and" >&2
  echo "  rerun with LOGSMITH_CERT_HOSTS='$example'" >&2
}

# Backend readiness URL for readiness checks (honors --port and LOGSMITH_HTTP).
# /api/auth/status is exempt from authentication, so it answers even when a
# password is required (which /api/health would not).
health_url() {
  local scheme="https"
  [ "${LOGSMITH_HTTP:-0}" = "1" ] && scheme="http"
  echo "$scheme://localhost:$BACKEND_PORT/api/auth/status"
}

# Poll a URL until it answers; warns with the log tail on timeout.
wait_ready() { # $1 = url, $2 = label, $3 = log file
  local i
  for i in $(seq 1 150); do
    if curl -sk -o /dev/null "$1" 2>/dev/null; then return 0; fi
    sleep 0.1
  done
  echo "  WARNING: $2 is not answering at $1 after 15s — last lines of $3:" >&2
  tail -5 "$3" >&2 2>/dev/null || true
  return 1
}

# One-time setup shared by every mode.
ensure_backend_env() {
  if [ ! -d .venv ]; then
    python3 -m venv .venv
    .venv/bin/pip install -q -r backend/requirements.txt
  fi
}

# TLS flags; generates the certificate on first use (never regenerates).
ssl_flags() {
  if [ "${LOGSMITH_HTTP:-0}" = "1" ]; then
    return 0
  fi
  ./tools/generate_cert.sh >&2
  echo "--ssl-certfile certs/localhost.pem --ssl-keyfile certs/localhost-key.pem"
}

# Pick up '--port N' and '--host H' from the args so checks use the real values.
scan_port() {
  BACKEND_PORT=8000
  local prev=""
  for arg in "$@"; do
    if [ "$prev" = "--port" ]; then BACKEND_PORT="$arg"; fi
    prev="$arg"
  done
}

scan_host() {
  local prev=""
  for arg in "$@"; do
    if [ "$prev" = "--host" ]; then HOST="$arg"; fi
    prev="$arg"
  done
}

# Pick up '--ui python|react' from the args (LOGSMITH_UI works too).
scan_ui() {
  local prev=""
  for arg in "$@"; do
    if [ "$prev" = "--ui" ]; then
      case "$arg" in
        python|react) UI="$arg" ;;
        *) echo "Error: --ui must be 'python' or 'react' (got '$arg')." >&2; exit 1 ;;
      esac
    fi
    prev="$arg"
  done
  export LOGSMITH_UI="$UI"
}

# The args left for uvicorn after '--ui X' pairs are removed (uvicorn would
# reject the unknown flag). Bash 3.2-safe: fills the PASSTHROUGH array.
strip_ui() {
  PASSTHROUGH=()
  local skip_next=0
  for arg in "$@"; do
    if [ "$skip_next" = "1" ]; then skip_next=0; continue; fi
    if [ "$arg" = "--ui" ]; then skip_next=1; continue; fi
    PASSTHROUGH+=("$arg")
  done
}

# --- start --------------------------------------------------------------

already_running_or_busy() { # $1 = "Backend"|"Frontend dev server", $2 = pid file, $3 = port
  if is_running "$2"; then
    echo "Error: $1 already running (PID $(pid_of "$2"))." >&2
    echo "  ./run.sh stop      to stop it" >&2
    echo "  ./run.sh restart    to stop and start again" >&2
    exit 1
  fi
  if port_busy "$3"; then
    local pid
    pid=$(lsof -ti ":$3" | head -1)
    echo "Error: port $3 is already in use by PID $pid:" >&2
    ps -p "$pid" -o command= >&2 2>/dev/null || true
    echo >&2
    echo "Stop that process first (kill $pid), or start on another port:" >&2
    echo "  ./run.sh start --port $(( $3 + 1 ))" >&2
    exit 1
  fi
}

start_backend() { # $1 = "dev" | "prod"
  local reload=""
  [ "$1" = "dev" ] && reload="--reload --reload-dir backend"
  # shellcheck disable=SC2086  # flags must word-split
  nohup .venv/bin/uvicorn --app-dir backend app.main:app \
    --host "$HOST" --port "$BACKEND_PORT" $(ssl_flags) $reload \
    >> "$BACKEND_LOG" 2>&1 &
  echo $! > "$BACKEND_PID"
}

start_frontend() {
  if [ ! -x frontend/node_modules/.bin/vite ]; then
    echo "Error: Vite is not installed. Run: cd frontend && npm install" >&2
    exit 1
  fi
  nohup sh -c 'cd frontend && exec ./node_modules/.bin/vite --strictPort' \
    >> "$FRONTEND_LOG" 2>&1 &
  echo $! > "$FRONTEND_PID"
}

cmd_start() { # args may contain --dev and --ui python|react
  local mode="prod"
  local args=("$@")
  for arg in "$@"; do
    [ "$arg" = "--dev" ] && mode="dev"
  done
  scan_port "$@"
  scan_host "$@"
  scan_ui "$@"
  mkdir -p "$RUN_DIR"
  ensure_backend_env
  already_running_or_busy "Backend" "$BACKEND_PID" "$BACKEND_PORT"
  ensure_auth
  ensure_sandbox
  cert_hint

  local scheme="https"
  [ "${LOGSMITH_HTTP:-0}" = "1" ] && scheme="http"

  if [ "$mode" = "prod" ] && [ "$UI" = "react" ] && [ ! -f frontend/dist/index.html ]; then
    echo "Building the frontend (first start)…"
    (cd frontend && npm install --silent && npm run build >/dev/null)
  fi

  start_backend "$mode"
  wait_ready "$(health_url)" "backend" "$BACKEND_LOG"
  echo "Backend  ●  $scheme://localhost:$BACKEND_PORT  (PID $(pid_of "$BACKEND_PID"), log: $BACKEND_LOG)"
  if [ "$UI" = "python" ]; then
    echo "UI       ●  python (server-rendered pages — no build step)"
  else
    echo "UI       ●  react (SPA from frontend/dist)"
  fi
  auth_report
  sandbox_report
  local ip
  ip=$(lan_ip)
  if [ -n "$ip" ] && [ "$HOST" != "localhost" ] && [ "$HOST" != "127.0.0.1" ]; then
    echo "           reachable as $scheme://$ip:$BACKEND_PORT from other devices (bound to $HOST)"
  fi

  if [ "$mode" = "dev" ]; then
    if [ "$UI" != "react" ]; then
      echo "           dev mode with --ui python: backend auto-reload only, no Vite."
    else
      if [ "${LOGSMITH_HTTP:-0}" = "1" ]; then
        echo "  NOTE: LOGSMITH_HTTP=1 — the Vite proxy expects HTTPS (vite.config.js)." >&2
      fi
      if [ "$BACKEND_PORT" != "8000" ]; then
        echo "  NOTE: Vite's proxy targets localhost:8000 (vite.config.js); --port $BACKEND_PORT breaks it." >&2
      fi
      already_running_or_busy "Frontend dev server" "$FRONTEND_PID" "$FRONTEND_PORT"
      start_frontend
      wait_ready "http://localhost:$FRONTEND_PORT/" "frontend dev server" "$FRONTEND_LOG"
      echo "Frontend ●  http://localhost:$FRONTEND_PORT  (PID $(pid_of "$FRONTEND_PID"), log: $FRONTEND_LOG)"
      echo "           open $scheme://localhost:$BACKEND_PORT/api/health — Vite proxies /api to the backend"
    fi
  fi
  echo "$mode, ui: $UI, auth: $AUTH_MODE, sandbox: $SANDBOX_ENABLED" > "$RUN_DIR/mode"
}

# --- stop ---------------------------------------------------------------

stop_one() { # $1 = label, $2 = pid file
  local pid
  pid=$(pid_of "$2")
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    kill "$pid"
    local i
    for i in $(seq 1 50); do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.1
    done
    if kill -0 "$pid" 2>/dev/null; then
      kill -9 "$pid" 2>/dev/null || true
      echo "  $1 (PID $pid) did not stop cleanly — force killed"
    else
      echo "  $1 stopped (PID $pid)"
    fi
  else
    echo "  $1 not running"
  fi
  rm -f "$2"
}

cmd_stop() {
  stop_one "Backend" "$BACKEND_PID"
  if [ -f "$FRONTEND_PID" ]; then
    stop_one "Frontend dev server" "$FRONTEND_PID"
  fi
  rm -f "$RUN_DIR/mode"
}

# --- status -------------------------------------------------------------

cmd_status() {
  local rc=1 scheme="https"
  [ "${LOGSMITH_HTTP:-0}" = "1" ] && scheme="http"
  if is_running "$BACKEND_PID"; then
    echo "Backend  ●  running, PID $(pid_of "$BACKEND_PID") — $scheme://localhost:$BACKEND_PORT"
    rc=0
  else
    echo "Backend  ○  stopped"
  fi
  if [ -f "$FRONTEND_PID" ]; then
    if is_running "$FRONTEND_PID"; then
      echo "Frontend ●  running, PID $(pid_of "$FRONTEND_PID") — http://localhost:$FRONTEND_PORT"
      rc=0
    else
      echo "Frontend ○  stopped (stale pid file)"
    fi
  fi
  if [ -f "$RUN_DIR/mode" ]; then
    echo "Last start mode: $(cat "$RUN_DIR/mode")"
  fi
  exit "$rc"
}

# --- dispatch ------------------------------------------------------------

case "${1:-}" in
  start)
    shift
    cmd_start "$@"
    ;;
  stop)
    cmd_stop
    ;;
  status)
    cmd_status
    ;;
  restart)
    shift
    cmd_stop
    echo
    cmd_start "$@"
    ;;
  help|-h|--help)
    usage
    ;;
  *)
    # Foreground production server, args passed through to uvicorn (old behavior).
    # --ui is consumed by this script; everything else reaches uvicorn.
    scan_port "$@"
    scan_host "$@"
    scan_ui "$@"
    strip_ui "$@"
    ensure_backend_env
    if [ "$UI" = "react" ] && [ ! -f frontend/dist/index.html ]; then
      (cd frontend && npm install --silent && npm run build >/dev/null)
    fi
    if command -v lsof >/dev/null 2>&1 && port_busy "$BACKEND_PORT"; then
      PID="$(lsof -ti ":$BACKEND_PORT" | head -1)"
      echo "Error: port $BACKEND_PORT is already in use by PID $PID:" >&2
      ps -p "$PID" -o command= >&2 2>/dev/null || true
      echo >&2
      echo "Stop that process first (kill $PID), or start on another port:" >&2
      echo "  ./run.sh --port 8001" >&2
      exit 1
    fi
    ensure_auth
    ensure_sandbox
    cert_hint
    if [ "$UI" = "python" ]; then
      echo "UI       ●  python (server-rendered pages — no build step)"
    else
      echo "UI       ●  react (SPA from frontend/dist)"
    fi
    auth_report
    sandbox_report
    # shellcheck disable=SC2086
    exec .venv/bin/uvicorn --app-dir backend app.main:app --host "$HOST" --port "$BACKEND_PORT" $(ssl_flags) ${PASSTHROUGH[@]+"${PASSTHROUGH[@]}"}
    ;;
esac
