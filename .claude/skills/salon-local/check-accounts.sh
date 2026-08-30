#!/usr/bin/env bash
# Does the Claude account pool actually have credentials in it?
#
#   .claude/skills/salon-local/check-accounts.sh
#
# The pool skips a credential-less directory in silence, so an account you
# believe is logged in and one that is empty look identical from the floor —
# right up to the moment a guest is sitting there. `claude setup-token` produces
# exactly that state: it prints a token for you to export and writes nothing
# into CLAUDE_CONFIG_DIR.
#
# Exits non-zero if the pool could not seat a guest, so it can gate a start-up.
set -euo pipefail
cd "$(dirname "$0")/../../.."

POOL="${BYOI_CLAUDE_ACCOUNTS_DIR:-$PWD/data/claude-accounts}"

if [[ ! -d "$POOL" ]]; then
  echo "no account pool at $POOL" >&2
  echo "  ./scripts/seat-claude-login.sh --account claude-seat-1" >&2
  exit 1
fi

if ! command -v claude >/dev/null 2>&1; then
  echo "claude is not on PATH — the seat cannot start a session at all" >&2
  exit 1
fi

seat_ok=0
host_ok=0
checked=0

for dir in "$POOL"/*/; do
  [[ -d "$dir" ]] || continue
  label="$(basename "$dir")"
  checked=$((checked + 1))

  # `claude auth status` is the authority. A .credentials.json can exist and be
  # an empty stub — scripts/sim-failover-browser.py writes exactly that — so
  # testing for the file is not the same question.
  if CLAUDE_CONFIG_DIR="$dir" claude auth status 2>/dev/null | grep -q '"loggedIn": *true'; then
    state="ok"
  elif [[ -s "$dir/.credentials.json" ]] && ! grep -q '^{}\s*$' "$dir/.credentials.json"; then
    state="credentials present, but auth status did not say loggedIn"
  else
    state="EMPTY — the pool will skip this one"
  fi

  printf '%-24s %s\n' "$label" "$state"

  if [[ "$state" == "ok" ]]; then
    if [[ "$label" == "claude-host" ]]; then
      host_ok=1
    else
      seat_ok=$((seat_ok + 1))
    fi
  fi
done

echo
if [[ $checked -eq 0 ]]; then
  echo "pool is empty — ./scripts/seat-claude-login.sh --account claude-seat-1" >&2
  exit 1
fi

status=0

case "$seat_ok" in
  0)
    echo "FAIL no credentialed seat account — a guest cannot be seated at all" >&2
    status=1
    ;;
  1)
    # Not fatal: a visit works. It just cannot survive a usage limit, and that
    # surfaces as an error on the guest's phone rather than a spare chair.
    echo "WARN one seat account. A hard usage limit will end the visit rather" >&2
    echo "     than move it — add a second with --account claude-seat-2." >&2
    ;;
  *)
    echo "OK   $seat_ok seat accounts — a usage limit can fail over in place"
    ;;
esac

if [[ $host_ok -eq 0 ]]; then
  echo "WARN no claude-host account. Check-in and chat work; writing and grading" >&2
  echo "     an acceptance suite does not." >&2
fi

exit $status
