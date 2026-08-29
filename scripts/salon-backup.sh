#!/usr/bin/env bash
# Back up the parts of data/ that cannot be recreated.
#
#   ./scripts/salon-backup.sh                 # -> data/backups/salon-<stamp>.tar.gz
#   ./scripts/salon-backup.sh /mnt/elsewhere  # somewhere off this disk
#
# A volume snapshot covers a dead node; this covers the ordinary case a snapshot
# is bad at — restoring one credential file without rolling the whole disk back.
#
# What is deliberately not here: data/projects/ is git and has an origin, and
# guest workspaces under data/seat-runtime/ are destroyed at checkout. Backing
# either up would copy gigabytes to protect nothing.
set -euo pipefail
cd "$(dirname "$0")/.."
DATA="${BYOI_DATA:-$PWD/data}"
DEST="${1:-$DATA/backups}"

# Secrets and a private CA. The archive is as sensitive as the box it came from.
mkdir -p "$DEST"
chmod 700 "$DEST"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="$DEST/salon-$STAMP.tar.gz"

PATHS=()
for rel in tls secrets claude-accounts salon.db; do
  if [[ -e "$DATA/$rel" ]]; then
    PATHS+=("$rel")
  else
    echo "skipping $rel (not present)" >&2
  fi
done

if [[ ${#PATHS[@]} -eq 0 ]]; then
  echo "nothing to back up under $DATA — is BYOI_DATA right?" >&2
  exit 1
fi

# SQLite is being written to by the desk, and copying the file mid-transaction
# can capture a torn page. Take a consistent copy through the backup API rather
# than `cp`. Via python3, not the sqlite3 CLI: this repo is Python, so python3
# is always here, while the CLI is in neither the desk image nor a slim VM.
STAGE=""
if [[ " ${PATHS[*]} " == *" salon.db "* ]]; then
  STAGE="$(mktemp -d)"
  trap 'rm -rf "$STAGE"' EXIT
  python3 - "$DATA/salon.db" "$STAGE/salon.db" <<'PY'
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
with sqlite3.connect(f"file:{src}?mode=ro", uri=True) as a, sqlite3.connect(dst) as b:
    a.backup(b)
PY
fi

umask 077
if [[ -n "$STAGE" ]]; then
  OTHERS=()
  for rel in "${PATHS[@]}"; do
    [[ "$rel" == "salon.db" ]] || OTHERS+=("$rel")
  done
  tar -czf "$OUT" -C "$DATA" "${OTHERS[@]}" -C "$STAGE" salon.db
else
  tar -czf "$OUT" -C "$DATA" "${PATHS[@]}"
fi
chmod 600 "$OUT"

echo "wrote $OUT ($(du -h "$OUT" | cut -f1))"
echo
echo "This archive holds the salon CA key, the operator password hash, and live"
echo "Claude credentials. Copy it off this machine and keep it somewhere you"
echo "would keep an SSH key:"
echo "  scp root@<vm>:$OUT ."
