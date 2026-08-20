#!/usr/bin/env bash
set -euo pipefail
BRIDGE="${BYOI_PAN_IFACE:-pan0}"
PIDFILE=/run/byoi-dnsmasq.pid
if [[ ${EUID} -ne 0 ]]; then
  echo "run as root: sudo $0" >&2
  exit 1
fi
gdbus call --system --dest org.bluez --object-path /org/bluez/hci0 \
  --method org.bluez.NetworkServer1.Unregister nap >/dev/null 2>&1 || true
if [[ -f $PIDFILE ]]; then
  kill "$(cat "$PIDFILE")" 2>/dev/null || true
  rm -f "$PIDFILE"
fi
ip link set "$BRIDGE" down 2>/dev/null || true
ip link delete "$BRIDGE" type bridge 2>/dev/null || true
echo "NAP stopped."
