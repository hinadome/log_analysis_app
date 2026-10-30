#!/usr/bin/env bash
# Generate a local TLS certificate for serving LogSmith over HTTPS.
#
# Idempotent: if the certificate already exists it is kept as-is.
# Prefers mkcert (creates certs your browser actually trusts) and falls
# back to a self-signed openssl certificate with localhost SANs.
set -euo pipefail
cd "$(dirname "$0")/.."

CERT=certs/localhost.pem
KEY=certs/localhost-key.pem

if [ -f "$CERT" ] && [ -f "$KEY" ]; then
  echo "TLS certificate already exists at $CERT — not regenerating."
  exit 0
fi

mkdir -p certs

if command -v mkcert >/dev/null 2>&1; then
  echo "Generating certificate with mkcert (locally trusted by your browser)…"
  mkcert -install >/dev/null 2>&1 || true
  mkcert -key-file "$KEY" -cert-file "$CERT" localhost 127.0.0.1 ::1
else
  echo "mkcert not found — generating a self-signed certificate with openssl…"
  openssl req -x509 -newkey rsa:2048 -nodes \
    -keyout "$KEY" -out "$CERT" \
    -days 825 \
    -subj "/CN=localhost" \
    -addext "subjectAltName=DNS:localhost,IP:127.0.0.1,IP:::1" \
    -addext "keyUsage=digitalSignature,keyEncipherment" \
    -addext "extendedKeyUsage=serverAuth"
  echo "Note: browsers will show a warning for self-signed certificates."
  echo "      Install mkcert (brew install mkcert) and delete certs/ to get a trusted one."
fi

chmod 600 "$KEY"
echo "Certificate ready: $CERT"
