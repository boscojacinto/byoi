#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="${PYTHONPATH:-}:src:."
export BYOI_TRANSPORT="${BYOI_TRANSPORT:-wifi}"
TLS_DIR="${BYOI_TLS_DIR:-$PWD/data/tls}"
if [[ -f "$TLS_DIR/host.token" ]]; then
  export BYOI_TLS_DIR="${BYOI_TLS_DIR:-$TLS_DIR}"
  export BYOI_HOST_TOKEN_FILE="${BYOI_HOST_TOKEN_FILE:-$TLS_DIR/host.token}"
fi
SSL_ARGS=()
if [[ "${BYOI_GUEST_TLS:-1}" != "0" && -f "$TLS_DIR/seat.pem" && -f "$TLS_DIR/seat-key.pem" ]]; then
  SSL_ARGS=(--ssl-certfile "$TLS_DIR/seat.pem" --ssl-keyfile "$TLS_DIR/seat-key.pem")
fi
exec python3 -m uvicorn apps.seat.main:app --host 0.0.0.0 --port 8787 "${SSL_ARGS[@]}"
