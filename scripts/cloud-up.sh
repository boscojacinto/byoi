#!/usr/bin/env bash
# Bring the salon up on a cloud VM: edge, desk, and the seat image the desk
# raises a container from at each check-in.
#
#   ./scripts/cloud-up.sh
#
# Prerequisites are checked rather than guessed at, because each one otherwise
# fails much later and much less clearly.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"

if [[ ! -f deploy/.env ]]; then
  echo "deploy/.env is missing — copy deploy/.env.example and fill it in" >&2
  exit 1
fi

# The salon CA still exists in the cloud. It no longer holds guest TLS (Caddy
# does), but it is how a seat knows the desk is the desk.
if [[ ! -f data/tls/ca.pem ]]; then
  echo "==> minting the salon CA and host token"
  ./scripts/salon-tls.sh >/dev/null
fi

if ! BYOI_SECRETS_DIR="${BYOI_SECRETS_DIR:-$ROOT/data/secrets}" \
     python3 -c 'import sys; sys.path.insert(0, "."); from apps.api.operator import password_is_set; sys.exit(0 if password_is_set() else 1)'; then
  echo "no operator password set — the desk would be unopenable." >&2
  echo "  ./scripts/salon-secrets.sh operator" >&2
  exit 1
fi

SEAT_IMAGE="$(grep -E '^BYOI_SEAT_IMAGE=' deploy/.env | cut -d= -f2- || true)"
SEAT_IMAGE="${SEAT_IMAGE:-byoi-seat:latest}"

echo "==> building the seat image ($SEAT_IMAGE)"
docker build -f deploy/Dockerfile.seat -t "$SEAT_IMAGE" .

echo "==> bringing up the edge and the desk"
# The desk bind-mounts per-seat certificates and Claude account dirs into the
# containers it creates. Those paths are resolved by the daemon on this VM, not
# inside the desk container, so it has to be told where data/ really is.
export BYOI_HOST_DATA_DIR="$PWD/data"
docker compose --env-file deploy/.env -f deploy/docker-compose.yml up -d --build

DOMAIN="$(grep -E '^BYOI_DOMAIN=' deploy/.env | cut -d= -f2-)"
echo
echo "Desk:  https://${DOMAIN}/"
echo "Seats: https://s-<session>.${DOMAIN}/guest/   (created at check-in)"
echo
echo "Point *.${DOMAIN} and ${DOMAIN} at this VM. The first request waits on"
echo "the DNS-01 challenge, so give the wildcard a minute."
