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
# Anything else (e.g. './run.sh --port 8001') runs the foreground server with
# the args passed through to uvicorn, exactly like before.
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

usage() {
  sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'
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

# Warn loudly when the app is exposed beyond this machine with no password —
# LogSmith runs scripts with the permissions of this user.
check_exposure() {
  if [ "$HOST" != "localhost" ] && [ "$HOST" != "127.0.0.1" ] && [ -z "${LOGSMITH_TOKEN:-}" ]; then
    echo "  ⚠  WARNING: binding to $HOST with no LOGSMITH_TOKEN —" >&2
    echo "     anyone who can reach this machine can run scripts as you." >&2
    echo "     Set a password:  LOGSMITH_TOKEN='choose-one' ./run.sh start" >&2
  fi
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

# Backend health URL for readiness checks (honors --port and LOGSMITH_HTTP).
health_url() {
  local scheme="https"
  [ "${LOGSMITH_HTTP:-0}" = "1" ] && scheme="http"
  echo "$scheme://localhost:$BACKEND_PORT/api/health"
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

cmd_start() { # args may contain --dev
  local mode="prod"
  local args=("$@")
  for arg in "$@"; do
    [ "$arg" = "--dev" ] && mode="dev"
  done
  scan_port "$@"
  scan_host "$@"
  mkdir -p "$RUN_DIR"
  ensure_backend_env
  already_running_or_busy "Backend" "$BACKEND_PID" "$BACKEND_PORT"
  check_exposure
  cert_hint

  local scheme="https"
  [ "${LOGSMITH_HTTP:-0}" = "1" ] && scheme="http"

  if [ "$mode" = "prod" ] && [ ! -f frontend/dist/index.html ]; then
    echo "Building the frontend (first start)…"
    (cd frontend && npm install --silent && npm run build >/dev/null)
  fi

  start_backend "$mode"
  wait_ready "$(health_url)" "backend" "$BACKEND_LOG"
  echo "Backend  ●  $scheme://localhost:$BACKEND_PORT  (PID $(pid_of "$BACKEND_PID"), log: $BACKEND_LOG)"
  local ip
  ip=$(lan_ip)
  if [ -n "$ip" ] && [ "$HOST" != "localhost" ] && [ "$HOST" != "127.0.0.1" ]; then
    echo "           reachable as $scheme://$ip:$BACKEND_PORT from other devices (bound to $HOST)"
  fi

  if [ "$mode" = "dev" ]; then
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
  echo "$mode" > "$RUN_DIR/mode"
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
    scan_port "$@"
    scan_host "$@"
    ensure_backend_env
    if [ ! -f frontend/dist/index.html ]; then
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
    check_exposure
    cert_hint
    # shellcheck disable=SC2086
    exec .venv/bin/uvicorn --app-dir backend app.main:app --host "$HOST" --port "$BACKEND_PORT" $(ssl_flags) "$@"
    ;;
esac
