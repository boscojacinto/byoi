#!/usr/bin/env bash
# An isolated salon on this machine, talking to scripts/fake-claude.py instead
# of Claude Code. For exercising check-in, chat, and the floor screen without
# spending a real account or finding a phone.
#
#   .claude/skills/salon-sim/sim-up.sh          # foreground; Ctrl-C stops both
#   BYOI_SIM_DIR=/tmp/salon-sim sim-up.sh
#
# Isolated means isolated: its own BYOI_DATA, its own secrets, its own account
# pool, its own workspace. It never writes to data/salon.db, data/secrets/, or
# data/claude-accounts/, and it binds to 127.0.0.1 so nothing on the LAN can
# reach a salon whose operator password is printed on this terminal.
#
# The one thing it shares is data/tls — the salon CA, which is expensive to mint
# and harmless to reuse. It is read, never rewritten.
#
# Ports match scripts/sim-failover-browser.py (18080/18787/18788) so a real
# salon on 8080/8787 can keep running alongside it.
set -euo pipefail
cd "$(dirname "$0")/../../.."
ROOT="$PWD"

SIM="${BYOI_SIM_DIR:-$ROOT/data/sim}"
DESK_PORT="${BYOI_SIM_DESK_PORT:-18080}"
SEAT_PORT="${BYOI_SIM_SEAT_PORT:-18787}"
CONTROL_PORT="${BYOI_SIM_CONTROL_PORT:-18788}"
# The desk enforces an 8-character minimum, so the obvious "sim" is rejected.
OPERATOR_PW="${BYOI_SIM_OPERATOR_PW:-sim-salon}"
if [[ ${#OPERATOR_PW} -lt 8 ]]; then
  echo "BYOI_SIM_OPERATOR_PW must be at least 8 characters — the desk refuses shorter" >&2
  exit 1
fi

FAKE="$ROOT/scripts/fake-claude.py"
# Honours BYOI_TLS_DIR like the rest of the repo, so a throwaway CA can be
# pointed at without going near data/tls.
TLS="${BYOI_TLS_DIR:-$ROOT/data/tls}"

if [[ ! -f "$TLS/host.token" || ! -f "$TLS/seat.pem" ]]; then
  echo "no salon CA yet — run ./scripts/salon-tls.sh first" >&2
  exit 1
fi

PY="$ROOT/.venv/bin/python"
[[ -x "$PY" ]] || PY="$(command -v python3)"
if ! "$PY" -c 'import uvicorn' 2>/dev/null; then
  echo "uvicorn is missing — pip install -e '.[salon,dev]' in the venv" >&2
  exit 1
fi

# A stale sim is worse than no sim: a half-migrated salon.db from an older
# schema fails in ways that look like real bugs.
if [[ "${BYOI_SIM_KEEP:-0}" != "1" ]]; then
  rm -rf "$SIM"
fi
mkdir -p "$SIM"/{salon,secrets,claude-accounts,workspace,handoffs,logs}
chmod 700 "$SIM/secrets"

# Two accounts, so the pool can fail over. The fake reads the directory name as
# its label and needs nothing real in here.
for label in claude-seat-1 claude-seat-2 claude-host; do
  mkdir -p "$SIM/claude-accounts/$label"
  printf '{}\n' > "$SIM/claude-accounts/$label/.credentials.json"
done

chmod +x "$FAKE"
printf 'sim workspace\n' > "$SIM/workspace/README.md"

export PYTHONPATH="$ROOT/src:$ROOT${PYTHONPATH:+:$PYTHONPATH}"
export BYOI_DATA="$SIM/salon"
export BYOI_SECRETS_DIR="$SIM/secrets"
export BYOI_TLS_DIR="$TLS"
export BYOI_HOST_TOKEN_FILE="$TLS/host.token"
export BYOI_CLAUDE_ACCOUNTS_DIR="$SIM/claude-accounts"
export BYOI_HANDOFFS_DIR="$SIM/handoffs"
export BYOI_WORKSPACE="$SIM/workspace"
export BYOI_CLAUDE="$FAKE"
export BYOI_QUOTA_FAILOVER_PCT="${BYOI_QUOTA_FAILOVER_PCT:-80}"
export BYOI_HOUSE_URL="http://127.0.0.1:$DESK_PORT"
export BYOI_SEAT_URL="http://127.0.0.1:$SEAT_PORT"
export BYOI_SEAT_CONTROL_URL="https://127.0.0.1:$CONTROL_PORT"
export BYOI_CONTROL_PORT="$CONTROL_PORT"
export BYOI_SEAT_ID="${BYOI_SEAT_ID:-seat-1}"
# The guest here is a browser on this machine, so there is no phone to install a
# CA onto. The control port keeps its mTLS — that is what check-in rides on.
export BYOI_GUEST_TLS=0
export BYOI_TLS=1
# Never the real printer: a sim that prints paper is a sim nobody runs twice.
# `local` is the mode; what makes it harmless is the missing PERIPAGE_MAC, which
# sends the slip to a DumpTransport instead of the Bluetooth one. (print_mode()
# only understands `local` and `relay` — anything else silently becomes `local`.)
export BYOI_PRINT_MODE=local
unset PERIPAGE_MAC

BYOI_SIM_PW="$OPERATOR_PW" "$PY" - <<'PY'
import os, sys
sys.path.insert(0, ".")
from apps.api.operator import set_password
set_password(os.environ["BYOI_SIM_PW"])
PY

PIDS=()
cleanup() {
  for pid in "${PIDS[@]:-}"; do
    [[ -n "$pid" ]] && kill "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

wait_http() { # wait_http URL NAME
  for _ in $(seq 1 100); do
    if curl -fsS -m 2 -o /dev/null "$1" 2>/dev/null; then
      return 0
    fi
    sleep 0.2
  done
  echo "timeout waiting for $2 at $1 — see $SIM/logs/" >&2
  return 1
}

echo "==> desk on :$DESK_PORT"
"$PY" -m uvicorn apps.api.main:app --host 127.0.0.1 --port "$DESK_PORT" \
  > "$SIM/logs/desk.log" 2>&1 &
PIDS+=($!)
wait_http "http://127.0.0.1:$DESK_PORT/api/health" desk

# The desk records where the seat answers when it first seeds the row, and it
# has no idea we moved the seat to a sim port. Correct it before check-in.
"$PY" - <<PY
import sqlite3, time
from pathlib import Path
db = Path("$SIM/salon/salon.db")
deadline = time.time() + 10
while not db.is_file() and time.time() < deadline:
    time.sleep(0.1)
conn = sqlite3.connect(db)
conn.execute("UPDATE seats SET agent_url=? WHERE id=?",
             ("http://127.0.0.1:$SEAT_PORT", "$BYOI_SEAT_ID"))
conn.commit()
conn.close()
PY

echo "==> seat on :$SEAT_PORT (control :$CONTROL_PORT)"
"$PY" -m uvicorn apps.seat.main:app --host 127.0.0.1 --port "$SEAT_PORT" \
  > "$SIM/logs/seat.log" 2>&1 &
PIDS+=($!)
wait_http "http://127.0.0.1:$SEAT_PORT/local/status" seat

cat <<EOF

Sim salon up. Claude is $FAKE — nothing here talks to Anthropic.

  Desk      http://127.0.0.1:$DESK_PORT/     operator password: $OPERATOR_PW
  Seat      http://127.0.0.1:$SEAT_PORT/
  Guest     http://127.0.0.1:$SEAT_PORT/guest/
  Logs      $SIM/logs/{desk,seat}.log
  State     $SIM

Check a coder in at the desk; the slip's QR points at the guest URL above.
Ctrl-C stops both.
EOF

wait
