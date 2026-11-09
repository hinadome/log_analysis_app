#!/usr/bin/env bash
# Generate a local TLS certificate for serving ScriptEngine over HTTPS.
#
# Idempotent: if the certificate already exists it is kept as-is.
# Prefers mkcert (creates certs your browser actually trusts) and falls
# back to a self-signed openssl certificate with localhost SANs.
#
# Extra SANs for LAN access: SCRIPTENGINE_CERT_HOSTS='192.168.1.20,mac.local'
# (delete certs/ first — an existing certificate is never regenerated).
set -euo pipefail
cd "$(dirname "$0")/.."

CERT=certs/localhost.pem
KEY=certs/localhost-key.pem

if [ -f "$CERT" ] && [ -f "$KEY" ]; then
  echo "TLS certificate already exists at $CERT — not regenerating."
  exit 0
fi

mkdir -p certs

# localhost + loopback IPs, plus any extra names for reaching the machine
# from other devices.
HOSTS=(localhost 127.0.0.1 ::1)
if [ -n "${SCRIPTENGINE_CERT_HOSTS:-}" ]; then
  IFS=',' read -r -a extra <<< "$SCRIPTENGINE_CERT_HOSTS"
  for h in "${extra[@]}"; do
    h="${h#"${h%%[![:space:]]*}"}"   # trim whitespace
    h="${h%"${h##*[![:space:]]}"}"
    [ -n "$h" ] && HOSTS+=("$h")
  done
fi

if command -v mkcert >/dev/null 2>&1; then
  echo "Generating certificate with mkcert (locally trusted by your browser)…"
  mkcert -install >/dev/null 2>&1 || true
  mkcert -key-file "$KEY" -cert-file "$CERT" "${HOSTS[@]}"
else
  echo "mkcert not found — generating a self-signed certificate with openssl…"
  SAN="DNS:localhost,IP:127.0.0.1,IP:::1"
  for h in "${HOSTS[@]:3}"; do
    case "$h" in
      [0-9]*.[0-9]*.[0-9]*.[0-9]*) SAN="$SAN,IP:$h" ;; # dotted quad → IPv4
      *:*)                          SAN="$SAN,IP:$h" ;; # colons → IPv6
      *)                            SAN="$SAN,DNS:$h" ;; # anything else → hostname
    esac
  done
  openssl req -x509 -newkey rsa:2048 -nodes \
    -keyout "$KEY" -out "$CERT" \
    -days 825 \
    -subj "/CN=localhost" \
    -addext "subjectAltName=$SAN" \
    -addext "keyUsage=digitalSignature,keyEncipherment" \
    -addext "extendedKeyUsage=serverAuth"
  echo "Note: browsers will show a warning for self-signed certificates."
  echo "      Install mkcert (brew install mkcert) and delete certs/ to get a trusted one."
fi

chmod 600 "$KEY"
echo "Certificate ready: $CERT (SANs: ${HOSTS[*]})"
