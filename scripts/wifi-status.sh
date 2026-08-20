#!/usr/bin/env bash
# Is the seat reachable on cafe Wi-Fi? (phone and this PC share a LAN)
set -euo pipefail
echo "=== cafe Wi-Fi seat checklist ==="
if ss -lntp 2>/dev/null | grep -q ':8080'; then
  echo "OK   house HTTP :8080"
else
  echo "MISS house HTTP :8080"
fi
if ss -lntp 2>/dev/null | grep -q ':8787'; then
  echo "OK   seat HTTP :8787"
else
  echo "MISS seat HTTP :8787"
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
echo "     Phone: same Wi-Fi → http://${LAN}:8787/join?otp=…"
echo
echo "Seat agent must listen on all interfaces:"
echo "  PYTHONPATH=src:. uvicorn apps.seat.main:app --host 0.0.0.0 --port 8787"
echo
curl -fsS -m 2 http://127.0.0.1:8787/local/status 2>/dev/null | python3 -m json.tool 2>/dev/null || echo "MISS seat /local/status (start the agent)"
if [[ "$LAN" != "127.0.0.1" ]]; then
  curl -fsS -m 2 "http://${LAN}:8787/local/status" >/dev/null 2>&1 && echo "OK   seat /local/status on ${LAN}" || echo "MISS seat /local/status on ${LAN} (bind 0.0.0.0)"
fi
