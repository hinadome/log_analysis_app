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

SSL_FLAGS=""
if [ "${LOGSMITH_HTTP:-0}" != "1" ]; then
  ./tools/generate_cert.sh
  SSL_FLAGS="--ssl-certfile certs/localhost.pem --ssl-keyfile certs/localhost-key.pem"
  echo "LogSmith listening on https://localhost:8000"
else
  echo "LogSmith listening on http://localhost:8000 (LOGSMITH_HTTP=1)"
fi

exec .venv/bin/uvicorn --app-dir backend app.main:app --host 127.0.0.1 --port 8000 $SSL_FLAGS "$@"
