#!/usr/bin/env bash
# Private CA + mTLS certs + non-default host token.
# Identity for host↔seat is the certificate (IP may change).
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="${PYTHONPATH:-}:."
python3 - <<'PY'
from apps.tls import generate, HOST_CLIENT_NAME, SEAT_SERVER_NAME

p = generate()
token = p.token.read_text().strip()
print("TLS written to", p.root)
print()
print("Seat server name:", SEAT_SERVER_NAME)
print("Host client name:", HOST_CLIENT_NAME)
print("Host token file:", p.token)
print()
print("Same machine: export these in both house and seat shells:")
print(f"  export BYOI_TLS_DIR={p.root}")
print(f"  export BYOI_HOST_TOKEN_FILE={p.token}")
print(f"  export BYOI_SEAT_CONTROL_URL=https://127.0.0.1:8788")
print()
print("Two machines: copy")
print("  desk: ca.pem host.pem host-key.pem host.token")
print("  seat: ca.pem seat.pem seat-key.pem host.token")
print("On the desk, point at the seat's current LAN IP (IP can change):")
print("  export BYOI_SEAT_CONTROL_URL=https://<seat-lan-ip>:8788")
print("In the host browser console:")
print(f"  localStorage.setItem('byoiHostToken', '{token}')")
print()
print("Guest HTTPS SAN IPs are baked into seat.pem. If the seat LAN IP changes:")
print("  ./scripts/salon-tls.sh")
print("  (keeps the CA; reissues the seat cert; copies CA into the guest app)")
print()
print("Keep ca-key.pem offline. Anyone with it can mint a fake host or seat.")
print("Guest APK: cd apps/guest && npx expo run:android   # Expo Go cannot trust this CA")
PY
