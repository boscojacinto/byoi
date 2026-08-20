#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="${PYTHONPATH:-}:src:."
export BYOI_DATA="${BYOI_DATA:-$PWD/data}"
exec python3 -m uvicorn apps.api.main:app --host 0.0.0.0 --port 8080
