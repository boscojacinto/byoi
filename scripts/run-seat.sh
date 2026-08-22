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

HTTP_PID=""
cleanup() {
  if [[ -n "$HTTP_PID" ]]; then
    kill "$HTTP_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

# HTTP copy of the same app so the seat PC browser does not need the salon CA.
if [[ ${#SSL_ARGS[@]} -gt 0 ]]; then
  HTTP_PORT="${BYOI_SEAT_HTTP_PORT:-8786}"
  BYOI_TLS=0 python3 -m uvicorn apps.seat.main:app --host 0.0.0.0 --port "$HTTP_PORT" &
  HTTP_PID=$!
fi

python3 -m uvicorn apps.seat.main:app --host 0.0.0.0 --port 8787 "${SSL_ARGS[@]}"
