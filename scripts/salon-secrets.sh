#!/usr/bin/env bash
# Store a desk-only deploy credential without putting it in shell history or ps.
#
#   ./scripts/salon-secrets.sh operator   # desk sign-in password
#   ./scripts/salon-secrets.sh print-relay  # token for the venue's printer agent
#   ./scripts/salon-secrets.sh vercel
#   ./scripts/salon-secrets.sh neon
#   ./scripts/salon-secrets.sh upstash
#   ./scripts/salon-secrets.sh github-app   # only for carrying one over manually —
#                                            # normally created from the desk's
#                                            # Projects tab ("Set up GitHub App")
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
  operator)
    # The desk is reachable from the internet now, so this is the front door.
    printf 'Desk operator password: ' >&2
    read -r -s pw; printf '\n' >&2
    printf 'Again: ' >&2
    read -r -s pw2; printf '\n' >&2
    if [[ "$pw" != "$pw2" ]]; then
      echo "passwords did not match; nothing written" >&2
      exit 1
    fi
    BYOI_OPERATOR_PW="$pw" python3 - <<'OPPY'
import os, sys
sys.path.insert(0, ".")
from apps.api.operator import OperatorError, set_password
try:
    path = set_password(os.environ["BYOI_OPERATOR_PW"])
except OperatorError as exc:
    print(exc, file=sys.stderr)
    raise SystemExit(1)
print(f"wrote {path} (0600)", file=sys.stderr)
OPPY
    ;;
  print-relay)
    # Generated rather than typed: it is copied to the counter machine once
    # and never remembered by a person.
    mkdir -p "$DIR"; chmod 700 "$DIR"
    if [[ -s "$DIR/print-relay.token" ]]; then
      echo "already set — printing it again for the counter machine" >&2
    else
      openssl rand -hex 32 > "$DIR/print-relay.token"
      chmod 600 "$DIR/print-relay.token"
      echo "wrote $DIR/print-relay.token (0600)" >&2
    fi
    echo "On the machine with the printer:" >&2
    echo "  export BYOI_PRINT_RELAY_TOKEN=$(cat "$DIR/print-relay.token")" >&2
    ;;
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
  github-app)
    # A GitHub App is normally created from the desk UI itself (Projects tab
    # → "Set up GitHub App"), which stores these automatically. This exists
    # only to carry an existing App's credentials to a second salon by hand.
    mkdir -p "$DIR"; chmod 700 "$DIR"
    printf 'GitHub App ID: ' >&2
    read -r app_id
    if [[ -z "$app_id" ]]; then
      echo "nothing entered; github-app unchanged" >&2
      exit 1
    fi
    printf 'GitHub App slug (from its settings URL): ' >&2
    read -r app_slug
    printf '%s' "$app_id" > "$DIR/github-app.id"; chmod 600 "$DIR/github-app.id"
    [[ -n "$app_slug" ]] && { printf '%s' "$app_slug" > "$DIR/github-app.slug"; chmod 600 "$DIR/github-app.slug"; }
    echo "Paste the App's private key PEM, then press Ctrl-D:" >&2
    cat > "$DIR/github-app.pem"
    chmod 600 "$DIR/github-app.pem"
    echo "wrote $DIR/github-app.{id,slug,pem}" >&2
    ;;
  --list|"")
    python3 - <<'PY'
import json, sys
sys.path.insert(0, ".")
from apps.api.operator import hash_file, password_is_set
from apps.secrets import status
mark = "set " if password_is_set() else "  - "
where = str(hash_file()) if password_is_set() else "not configured — the desk cannot be signed into"
print(f"{mark}{'desk operator password':<24} {where}")
for row in status():
    mark = "set " if row["configured"] else "  - "
    where = row["source"] or "not configured"
    warn = "  !! world-readable" if row["world_readable"] else ""
    print(f"{mark}{row['name']:<24} {where}{warn}")
PY
    ;;
  *)
    echo "usage: $0 [operator|print-relay|vercel|neon|upstash|github-app|--list]" >&2
    exit 2
    ;;
esac
