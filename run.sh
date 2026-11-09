#!/usr/bin/env bash
# ScriptEngine — start and stop the app. Plain userland, no root/systemctl needed.
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
# (no build step). The SCRIPTENGINE_UI environment variable does the same.
#
#   --cert-hostname host(s)  regenerate the TLS certificate covering the
#                            given hostname(s) — comma-separated for
#                            several, e.g. --cert-hostname myserver.local,192.168.1.5
#                            (the localhost SANs are always included too).
#                            Normally a certificate is generated once and
#                            kept; passing this option replaces it.
#
# Security: binding a non-local address without SCRIPTENGINE_TOKEN generates a
# strong password (printed once, saved in .run/authtoken, reused across
# restarts) — scripts run as this user, so an open server is remote code
# execution. Set SCRIPTENGINE_TOKEN to choose the password, or SCRIPTENGINE_INSECURE=1
# to run open anyway (not recommended). SCRIPTENGINE_ALLOW_SCRIPT_EDITING=0 also
# locks script authoring while keeping the existing scripts runnable.
#
# SCRIPTENGINE_DOCKER_SANDBOX=1 runs every script inside a throwaway Docker
# container instead of directly on the host: no network, a memory cap, and
# only the script + its input + the output directory mounted. The sandbox
# image (sandbox.Dockerfile) is built automatically on first use.
#
# First start sets itself up: .venv, python packages, node modules and the
# SPA build are created on demand when missing (or when their requirements
# files changed). --ui python needs no Node at all.
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

# --- .env: SCRIPTENGINE_* lines only ------------------------------------------
# The repo's .env is shared with other tooling, so only variables named
# SCRIPTENGINE_* are honored — anything else in the file stays untouched.
# A variable already in the environment wins over the file (CLI flags
# still win over both).
if [ -f .env ]; then
  while IFS= read -r envline || [ -n "$envline" ]; do
    case "$envline" in
      SCRIPTENGINE_*\=*) ;;
      *) continue ;;
    esac
    envline="${envline#export }"
    envname="${envline%%=*}"
    [ -n "${!envname:-}" ] || export "$envline"
  done < .env
fi

RUN_DIR=.run
BACKEND_PID="$RUN_DIR/backend.pid"
FRONTEND_PID="$RUN_DIR/frontend.pid"
BACKEND_LOG="$RUN_DIR/backend.log"
FRONTEND_LOG="$RUN_DIR/frontend.log"
FRONTEND_PORT=5173
BACKEND_PORT=8000  # overridden by --port in start/foreground modes
HOST="${SCRIPTENGINE_HOST:-0.0.0.0}"  # bind address: all interfaces (use 127.0.0.1 to go back to local-only)
UI="${SCRIPTENGINE_UI:-react}"        # frontend: react SPA or python pages (--ui)
CERT_HOSTNAMES=""                 # --cert-hostname: regenerate the TLS cert for these names

usage() {
  sed -n '2,45p' "$0" | sed 's/^# \{0,1\}//'
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
# non-local bind with no SCRIPTENGINE_TOKEN would be remote code execution for
# anyone on the network. This generates a strong password instead (reused
# across restarts). Set SCRIPTENGINE_TOKEN to choose your own, or
# SCRIPTENGINE_INSECURE=1 to run open (not recommended).
AUTH_TOKEN_FILE="$RUN_DIR/authtoken"
AUTH_MODE="none"   # none (local bind) | custom | generated | insecure
AUTH_SHOW_TOKEN=0  # 1 = print the token value at startup (first generation)

ensure_auth() {
  if [ -n "${SCRIPTENGINE_TOKEN:-}" ]; then AUTH_MODE="custom"; return 0; fi
  if [ "$HOST" = "localhost" ] || [ "$HOST" = "127.0.0.1" ]; then return 0; fi
  if [ "${SCRIPTENGINE_INSECURE:-0}" = "1" ]; then AUTH_MODE="insecure"; return 0; fi
  mkdir -p "$RUN_DIR"
  if [ -s "$AUTH_TOKEN_FILE" ]; then
    SCRIPTENGINE_TOKEN="$(cat "$AUTH_TOKEN_FILE")"
  else
    if command -v openssl >/dev/null 2>&1; then
      SCRIPTENGINE_TOKEN="$(openssl rand -hex 16)"
    else
      SCRIPTENGINE_TOKEN="$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    fi
    printf '%s\n' "$SCRIPTENGINE_TOKEN" > "$AUTH_TOKEN_FILE"
    chmod 600 "$AUTH_TOKEN_FILE" 2>/dev/null || true
    AUTH_SHOW_TOKEN=1
  fi
  AUTH_MODE="generated"
  export SCRIPTENGINE_TOKEN
}

auth_report() {
  case "$AUTH_MODE" in
    custom)
      echo "Auth     ●  password required (SCRIPTENGINE_TOKEN)" ;;
    generated)
      if [ "$AUTH_SHOW_TOKEN" = "1" ]; then
        echo "Auth     ●  password required: $SCRIPTENGINE_TOKEN"
        echo "           generated, saved in $AUTH_TOKEN_FILE — set SCRIPTENGINE_TOKEN to pin your own"
      else
        echo "Auth     ●  password required (token: cat $AUTH_TOKEN_FILE)"
      fi
      echo "           tip: SCRIPTENGINE_ALLOW_SCRIPT_EDITING=0 also locks script authoring" ;;
    insecure)
      echo "Auth     ✗  DISABLED (SCRIPTENGINE_INSECURE=1) — anyone who can reach this" >&2
      echo "           machine can run scripts as you" >&2 ;;
    none)
      echo "Auth     ●  none (local bind only)" ;;
  esac
}

# Docker sandbox (SCRIPTENGINE_DOCKER_SANDBOX=1): scripts run in throwaway
# containers instead of directly on the host. Fails loudly — never silently
# falls back to direct execution, which is exactly what the flag exists to
# avoid.
SANDBOX_ENABLED=0
SANDBOX_IMAGE="${SCRIPTENGINE_SANDBOX_IMAGE:-scriptengine-sandbox:latest}"

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
  [ "${SCRIPTENGINE_DOCKER_SANDBOX:-0}" = "1" ] || return 0
  SANDBOX_ENABLED=1
  export SCRIPTENGINE_DOCKER_SANDBOX=1
  if ! command -v docker >/dev/null 2>&1; then
    echo "Error: SCRIPTENGINE_DOCKER_SANDBOX=1 but docker is not installed." >&2
    exit 1
  fi
  if ! docker_daemon_ok; then
    echo "Error: SCRIPTENGINE_DOCKER_SANDBOX=1 but the Docker daemon is not" >&2
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
  echo "Sandbox  ●  docker ($SANDBOX_IMAGE — no network, ${SCRIPTENGINE_SANDBOX_MEMORY:-512m} RAM per job)"
}

# The terminal is on by default: admins get a shell on the server in the
# browser. SCRIPTENGINE_TERMINAL=0 hides it, and it is also forced off by
# SCRIPTENGINE_ALLOW_SCRIPT_EDITING=0 (the lockdown that means "run the
# vetted scripts only" — a shell beats the script editor).
truthy() { case "$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')" in
             1|true|yes|on) return 0 ;; *) return 1 ;; esac; }

falsy() { case "$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')" in
             0|false|no|off) return 0 ;; *) return 1 ;; esac; }

terminal_report() {
  local locked=0
  truthy "${SCRIPTENGINE_ALLOW_SCRIPT_EDITING:-1}" || locked=1
  if [ "$locked" = "1" ] && ! falsy "${SCRIPTENGINE_TERMINAL:-1}"; then
    echo "Terminal ✗  locked off by SCRIPTENGINE_ALLOW_SCRIPT_EDITING=0 (the lockdown)"
  elif falsy "${SCRIPTENGINE_TERMINAL:-1}"; then
    echo "Terminal ○  off (SCRIPTENGINE_TERMINAL=0 hides it)"
  else
    echo "Terminal ●  enabled (admins only) — shells run as the server user"
  fi
}

# The TLS cert only covers localhost; other devices will see a browser warning.
cert_hint() {
  if [ "${SCRIPTENGINE_HTTP:-0}" = "1" ]; then return 0; fi
  [ -n "$CERT_HOSTNAMES" ] && return 0  # just regenerated with explicit names
  if [ "$HOST" = "localhost" ] || [ "$HOST" = "127.0.0.1" ]; then return 0; fi
  local example="my-machine.local"
  local ip
  ip=$(lan_ip)
  [ -n "$ip" ] && example="$ip,my-machine.local"
  echo "  Note: the TLS certificate only covers localhost — other devices" >&2
  echo "  will show a browser warning. To include them:" >&2
  echo "    ./run.sh restart --cert-hostname '$example'" >&2
  echo "  (or set SCRIPTENGINE_CERT_HOSTS='$example' and delete certs/)" >&2
}

# Backend readiness URL for readiness checks (honors --port and SCRIPTENGINE_HTTP).
# /api/auth/status is exempt from authentication, so it answers even when a
# password is required (which /api/health would not).
health_url() {
  local scheme="https"
  [ "${SCRIPTENGINE_HTTP:-0}" = "1" ] && scheme="http"
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

# One-time setup shared by every mode. Idempotent and self-healing:
# - .venv is (re)created when missing or broken
# - python packages reinstall when backend/requirements.txt changed
# - everything is reported, so a slow first start is never a mystery
ensure_backend_env() {
  local stamp=.venv/.scriptengine-installed
  if [ ! -x .venv/bin/python ]; then
    if ! command -v python3 >/dev/null 2>&1; then
      echo "Error: python3 (3.10+) is not installed or not on PATH." >&2
      exit 1
    fi
    rm -rf .venv   # a venv without a python binary is a broken half-state
    echo "Setup    ●  creating virtualenv .venv…"
    python3 -m venv .venv
  fi
  if [ ! -f "$stamp" ] || [ backend/requirements.txt -nt "$stamp" ]; then
    echo "Setup    ●  installing python packages (backend/requirements.txt)…"
    .venv/bin/pip install -q -r backend/requirements.txt
    touch "$stamp"
  fi
  if [ ! -x .venv/bin/uvicorn ]; then
    echo "Error: .venv looks broken (uvicorn missing after install)." >&2
    echo "       Fix: rm -rf .venv && ./run.sh start" >&2
    exit 1
  fi
}

# Frontend packages — only the react UI needs Node at all (--ui python
# skips this entirely). Reinstalled when node_modules is missing or
# frontend/package-lock.json changed since the last install.
ensure_node_modules() {
  [ "$UI" = "react" ] || return 0
  if ! command -v npm >/dev/null 2>&1; then
    echo "Error: npm is not installed or not on PATH — the react UI needs it." >&2
    echo "       Install Node.js, or run the server-rendered UI instead:" >&2
    echo "         ./run.sh start --ui python" >&2
    exit 1
  fi
  local stamp=frontend/node_modules/.scriptengine-installed
  if [ ! -d frontend/node_modules ] || [ ! -f "$stamp" ] \
     || [ frontend/package-lock.json -nt "$stamp" ]; then
    echo "Setup    ●  installing frontend packages (frontend/package.json)…"
    (cd frontend && npm install --silent --no-fund --no-audit)
    touch "$stamp"
  fi
}

# TLS flags; generates the certificate on first use (never regenerates).
ssl_flags() {
  if [ "${SCRIPTENGINE_HTTP:-0}" = "1" ]; then
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

# Pick up '--cert-hostname host(s)' from the args (comma-separated names).
scan_cert_hostname() {
  local args=("$@") i
  for ((i = 0; i < $#; i++)); do
    if [ "${args[$i]}" = "--cert-hostname" ]; then
      if [ $((i + 1)) -ge $# ] || [ -z "${args[$((i + 1))]}" ]; then
        echo "Error: --cert-hostname needs a hostname, e.g. --cert-hostname myserver.local." >&2
        exit 1
      fi
      CERT_HOSTNAMES="${args[$((i + 1))]}"
    fi
  done
}

# --cert-hostname: the explicit "regenerate my certificate" path. The cert
# is normally created once and never replaced (ssl_flags/generate_cert.sh);
# this deletes it first so the next ssl_flags call makes a fresh one, with
# the given names merged into SCRIPTENGINE_CERT_HOSTS (which adds the SANs).
ensure_cert() {
  [ -n "$CERT_HOSTNAMES" ] || return 0
  if [ "${SCRIPTENGINE_HTTP:-0}" = "1" ]; then
    echo "Error: --cert-hostname has no effect with SCRIPTENGINE_HTTP=1 —" >&2
    echo "       HTTP mode serves no certificate." >&2
    exit 1
  fi
  if [ -n "${SCRIPTENGINE_CERT_HOSTS:-}" ]; then
    CERT_HOSTNAMES="${SCRIPTENGINE_CERT_HOSTS},${CERT_HOSTNAMES}"
  fi
  export SCRIPTENGINE_CERT_HOSTS="$CERT_HOSTNAMES"
  rm -f certs/localhost.pem certs/localhost-key.pem
  echo "TLS      ●  regenerating certificate for: $CERT_HOSTNAMES (localhost SANs stay included)"
}

# Pick up '--ui python|react' from the args (SCRIPTENGINE_UI works too).
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
  export SCRIPTENGINE_UI="$UI"
}

# The args left for uvicorn after this script's own flags ('--ui X',
# '--cert-hostname H') are removed (uvicorn would reject the unknown
# options). Bash 3.2-safe: fills the PASSTHROUGH array.
strip_ours() {
  PASSTHROUGH=()
  local skip_next=0
  for arg in "$@"; do
    if [ "$skip_next" = "1" ]; then skip_next=0; continue; fi
    case "$arg" in
      --ui|--cert-hostname) skip_next=1; continue ;;
    esac
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
    echo "Error: Vite is not installed in frontend/node_modules." >&2
    echo "       This should not happen (run.sh installs it) — try: rm -rf frontend/node_modules && ./run.sh start --dev" >&2
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
  scan_cert_hostname "$@"
  mkdir -p "$RUN_DIR"
  already_running_or_busy "Backend" "$BACKEND_PID" "$BACKEND_PORT"
  ensure_backend_env
  ensure_node_modules
  ensure_auth
  ensure_sandbox
  ensure_cert
  cert_hint

  local scheme="https"
  [ "${SCRIPTENGINE_HTTP:-0}" = "1" ] && scheme="http"

  if [ "$mode" = "prod" ] && [ "$UI" = "react" ] && [ ! -f frontend/dist/index.html ]; then
    echo "Building the frontend (first start)…"
    (cd frontend && npm run build >/dev/null)
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
  terminal_report
  local ip
  ip=$(lan_ip)
  if [ -n "$ip" ] && [ "$HOST" != "localhost" ] && [ "$HOST" != "127.0.0.1" ]; then
    echo "           reachable as $scheme://$ip:$BACKEND_PORT from other devices (bound to $HOST)"
  fi

  if [ "$mode" = "dev" ]; then
    if [ "$UI" != "react" ]; then
      echo "           dev mode with --ui python: backend auto-reload only, no Vite."
    else
      if [ "${SCRIPTENGINE_HTTP:-0}" = "1" ]; then
        echo "  NOTE: SCRIPTENGINE_HTTP=1 — the Vite proxy expects HTTPS (vite.config.js)." >&2
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
  [ "${SCRIPTENGINE_HTTP:-0}" = "1" ] && scheme="http"
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
    scan_cert_hostname "$@"
    strip_ours "$@"
    if command -v lsof >/dev/null 2>&1 && port_busy "$BACKEND_PORT"; then
      PID="$(lsof -ti ":$BACKEND_PORT" | head -1)"
      echo "Error: port $BACKEND_PORT is already in use by PID $PID:" >&2
      ps -p "$PID" -o command= >&2 2>/dev/null || true
      echo >&2
      echo "Stop that process first (kill $PID), or start on another port:" >&2
      echo "  ./run.sh --port 8001" >&2
      exit 1
    fi
    ensure_backend_env
    ensure_node_modules
    if [ "$UI" = "react" ] && [ ! -f frontend/dist/index.html ]; then
      echo "Building the frontend (first start)…"
      (cd frontend && npm run build >/dev/null)
    fi
    ensure_auth
    ensure_sandbox
    ensure_cert
    cert_hint
    if [ "$UI" = "python" ]; then
      echo "UI       ●  python (server-rendered pages — no build step)"
    else
      echo "UI       ●  react (SPA from frontend/dist)"
    fi
    auth_report
    sandbox_report
    terminal_report
    # shellcheck disable=SC2086
    exec .venv/bin/uvicorn --app-dir backend app.main:app --host "$HOST" --port "$BACKEND_PORT" $(ssl_flags) ${PASSTHROUGH[@]+"${PASSTHROUGH[@]}"}
    ;;
esac
