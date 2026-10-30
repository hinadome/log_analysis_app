#!/usr/bin/env bash
# Start LogSmith at https://localhost:8000
#
# A local TLS certificate is generated on first start (tools/generate_cert.sh)
# and reused afterwards. Set LOGSMITH_HTTP=1 to serve plain HTTP instead.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q -r backend/requirements.txt
fi
if [ ! -f frontend/dist/index.html ]; then
  (cd frontend && npm install --silent && npm run build)
fi

# Fail fast with a readable message if the port is taken — uvicorn's raw
# "[Errno 48] address already in use" traceback helps nobody. Extra args
# passed through to uvicorn may override the port, so honor --port there.
PORT=8000
prev=""
for arg in "$@"; do
  if [ "$prev" = "--port" ]; then PORT="$arg"; fi
  prev="$arg"
done
if command -v lsof >/dev/null 2>&1 && lsof -ti ":$PORT" >/dev/null 2>&1; then
  PID="$(lsof -ti ":$PORT" | head -1)"
  echo "Error: port $PORT is already in use by PID $PID:" >&2
  ps -p "$PID" -o command= >&2 2>/dev/null || true
  echo >&2
  echo "Stop that process first (kill $PID), or start on another port:" >&2
  echo "  ./run.sh --port 8001" >&2
  exit 1
fi

SSL_FLAGS=""
if [ "${LOGSMITH_HTTP:-0}" != "1" ]; then
  ./tools/generate_cert.sh
  SSL_FLAGS="--ssl-certfile certs/localhost.pem --ssl-keyfile certs/localhost-key.pem"
  echo "LogSmith listening on https://localhost:$PORT"
else
  echo "LogSmith listening on http://localhost:$PORT (LOGSMITH_HTTP=1)"
fi

exec .venv/bin/uvicorn --app-dir backend app.main:app --host 127.0.0.1 --port "$PORT" $SSL_FLAGS "$@"
