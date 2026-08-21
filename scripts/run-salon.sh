#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="${PYTHONPATH:-}:src:."
export BYOI_DATA="${BYOI_DATA:-$PWD/data}"
TLS_DIR="${BYOI_TLS_DIR:-$PWD/data/tls}"
if [[ -f "$TLS_DIR/host.token" ]]; then
  export BYOI_TLS_DIR="${BYOI_TLS_DIR:-$TLS_DIR}"
  export BYOI_HOST_TOKEN_FILE="${BYOI_HOST_TOKEN_FILE:-$TLS_DIR/host.token}"
  export BYOI_SEAT_CONTROL_URL="${BYOI_SEAT_CONTROL_URL:-https://127.0.0.1:8788}"
fi
exec python3 -m uvicorn apps.api.main:app --host 0.0.0.0 --port 8080
