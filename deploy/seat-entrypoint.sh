#!/usr/bin/env bash
# Guest HTTP on :8787 (Caddy holds the real certificate in front of it) and the
# mTLS control app on :8788 in the same process. Deliberately not run-seat.sh:
# that script's job is the salon PC, where the seat terminates guest TLS itself.
set -euo pipefail

if [[ ! -d "${BYOI_GUEST_RUNTIME_DIR:-/run/byoi}" ]]; then
  echo "seat: ${BYOI_GUEST_RUNTIME_DIR:-/run/byoi} is missing — a guest's own" \
       "Claude token would fall back to disk" >&2
fi

exec python3 -m uvicorn apps.seat.main:app \
  --host 0.0.0.0 --port 8787 \
  --proxy-headers --forwarded-allow-ips '*'
