#!/usr/bin/env bash
# Store a desk-only deploy credential without putting it in shell history or ps.
#
#   ./scripts/salon-secrets.sh vercel
#   ./scripts/salon-secrets.sh neon
#   ./scripts/salon-secrets.sh upstash
#   ./scripts/salon-secrets.sh --list
#
# The value is read from the terminal, never from an argument.
set -euo pipefail
cd "$(dirname "$0")/.."
DIR="${BYOI_SECRETS_DIR:-$(pwd)/data/secrets}"

write_secret() {
  local file="$1" prompt="$2" value
  mkdir -p "$DIR"; chmod 700 "$DIR"
  printf '%s: ' "$prompt" >&2
  read -r -s value
  printf '\n' >&2
  if [[ -z "$value" ]]; then
    echo "nothing entered; $file unchanged" >&2
    return
  fi
  printf '%s' "$value" > "$DIR/$file"
  chmod 600 "$DIR/$file"
  echo "wrote $DIR/$file (0600)" >&2
}

case "${1:-}" in
  vercel)
    write_secret vercel.token "Vercel token (vercel.com/account/tokens)"
    printf 'Vercel team slug (blank for personal): ' >&2
    read -r scope
    if [[ -n "$scope" ]]; then
      printf '%s' "$scope" > "$DIR/vercel.scope"; chmod 600 "$DIR/vercel.scope"
      echo "wrote $DIR/vercel.scope" >&2
    fi
    ;;
  neon)
    write_secret neon.token "Neon API key (console.neon.tech/app/settings/api-keys)"
    ;;
  upstash)
    printf 'Upstash account email: ' >&2
    read -r email
    mkdir -p "$DIR"; chmod 700 "$DIR"
    printf '%s' "$email" > "$DIR/upstash.email"; chmod 600 "$DIR/upstash.email"
    write_secret upstash.token "Upstash API key (console.upstash.com/account/api)"
    ;;
  --list|"")
    python3 - <<'PY'
import json, sys
sys.path.insert(0, ".")
from apps.secrets import status
for row in status():
    mark = "set " if row["configured"] else "  - "
    where = row["source"] or "not configured"
    warn = "  !! world-readable" if row["world_readable"] else ""
    print(f"{mark}{row['name']:<24} {where}{warn}")
PY
    ;;
  *)
    echo "usage: $0 [vercel|neon|upstash|--list]" >&2
    exit 2
    ;;
esac
