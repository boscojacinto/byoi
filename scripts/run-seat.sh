#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="${PYTHONPATH:-}:src:."
export BYOI_TRANSPORT="${BYOI_TRANSPORT:-wifi}"
exec python3 -m uvicorn apps.seat.main:app --host 0.0.0.0 --port 8787
