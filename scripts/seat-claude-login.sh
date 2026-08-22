#!/usr/bin/env bash
# One-time Claude login on the seat PC so guests inherit a long-lived token.
# Guests never run `claude login`; they open the guest PWA after OTP unlock.
#
# Extra accounts buy quota. When the current login approaches a 5h/7d cap,
# the seat compacts the transcript and continues on the next credentialed dir.
#
#   ./scripts/seat-claude-login.sh
#   ./scripts/seat-claude-login.sh --account claude-seat-2
set -euo pipefail
cd "$(dirname "$0")/.."
ACCOUNT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --account)
      ACCOUNT="${2:-}"
      shift 2
      ;;
    --account=*)
      ACCOUNT="${1#--account=}"
      shift
      ;;
    *)
      echo "usage: $0 [--account LABEL]" >&2
      exit 2
      ;;
  esac
done

ROOT="$(pwd)"
POOL="${BYOI_CLAUDE_ACCOUNTS_DIR:-$ROOT/data/claude-accounts}"
if [[ -n "$ACCOUNT" ]]; then
  export CLAUDE_CONFIG_DIR="$POOL/$ACCOUNT"
  mkdir -p "$CLAUDE_CONFIG_DIR"
  echo "On this seat PC, as the user who owns the seat agent:"
  echo
  echo "  CLAUDE_CONFIG_DIR=$CLAUDE_CONFIG_DIR claude setup-token"
  echo
  echo "Credential isolation is Linux-only (CLAUDE_CONFIG_DIR + .credentials.json)."
  echo "Add at least two --account logins so a usage limit can fail over in place."
else
  echo "On this seat PC, as the user who owns the seat agent:"
  echo
  echo "  claude setup-token"
  echo
  echo "Or a named spare under $POOL:"
  echo
  echo "  $0 --account claude-seat-1"
  echo "  $0 --account claude-seat-2"
  echo
  echo "On the desk PC, the account that writes and grades acceptance suites:"
  echo
  echo "  $0 --account claude-host"
  echo
fi
echo "The seat agent talks to Claude Code over stream-json (chat, not a TTY);"
echo "guests only connect after the host pushes an OTP to this seat."
echo
if command -v claude >/dev/null 2>&1; then
  echo "claude is on PATH: $(command -v claude)"
else
  echo "WARN: claude is not on PATH"
fi
if [[ -n "${CLAUDE_CONFIG_DIR:-}" ]]; then
  echo "CLAUDE_CONFIG_DIR: $CLAUDE_CONFIG_DIR"
fi
workspace="${BYOI_WORKSPACE:-$ROOT}"
echo "guest workspace: $workspace"
