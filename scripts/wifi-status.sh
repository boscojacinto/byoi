#!/usr/bin/env bash
# Is the seat reachable on cafe Wi-Fi? (phone and this PC share a LAN)
set -euo pipefail
cd "$(dirname "$0")/.."
echo "=== cafe Wi-Fi seat checklist ==="
if ss -lntp 2>/dev/null | grep -q ':8080'; then
  echo "OK   house HTTP :8080"
else
  echo "MISS house HTTP :8080"
fi
if ss -lntp 2>/dev/null | grep -q ':8787'; then
  echo "OK   seat :8787"
else
  echo "MISS seat :8787"
fi
if ss -lntp 2>/dev/null | grep -q ':8788'; then
  echo "OK   seat mTLS control :8788"
else
  echo "MISS seat mTLS control :8788"
fi

LAN=$(python3 - <<'PY'
import socket
try:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect(("8.8.8.8", 80))
    print(s.getsockname()[0])
    s.close()
except OSError:
    print("127.0.0.1")
PY
)
echo "     LAN ${LAN}"
echo "     Phone: same Wi-Fi → https://${LAN}:8787/join?otp=…"
echo
CA="${BYOI_TLS_DIR:-$PWD/data/tls}/ca.pem"
CURL_TLS=()
if [[ -f "$CA" ]]; then
  CURL_TLS=(--cacert "$CA")
fi
curl -fsS -m 2 "${CURL_TLS[@]}" https://127.0.0.1:8787/local/status 2>/dev/null | python3 -m json.tool 2>/dev/null \
  || curl -fsS -m 2 http://127.0.0.1:8787/local/status 2>/dev/null | python3 -m json.tool 2>/dev/null \
  || echo "MISS seat /local/status (start the agent with ./scripts/run-seat.sh)"
if [[ "$LAN" != "127.0.0.1" ]]; then
  curl -fsS -m 2 "${CURL_TLS[@]}" "https://${LAN}:8787/local/status" >/dev/null 2>&1 && echo "OK   seat /local/status on ${LAN}" \
    || echo "MISS seat HTTPS /local/status on ${LAN} (run ./scripts/salon-tls.sh if the LAN IP changed)"
fi
