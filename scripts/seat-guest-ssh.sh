#!/usr/bin/env bash
# Restrict guest SSH so a phone on cafe Wi-Fi can only attach the seat tmux TTY.
#   ssh guest@<seat-lan-ip>
#   (forced) tmux attach -t claude-guest
set -euo pipefail
USER_NAME="${BYOI_GUEST_USER:-guest}"
SESSION="${BYOI_TMUX:-claude-guest}"

if [[ $EUID -ne 0 ]]; then
  echo "run as root: sudo $0" >&2
  exit 1
fi

id -u "$USER_NAME" >/dev/null 2>&1 || useradd -m -s /bin/bash "$USER_NAME"
install -d -m 700 -o "$USER_NAME" -g "$USER_NAME" "/home/$USER_NAME/.ssh"

DROPIN=/etc/ssh/sshd_config.d/byoi-guest.conf
cat >"$DROPIN" <<EOF
Match User $USER_NAME
    ForceCommand tmux attach -t $SESSION
    AllowTcpForwarding no
    X11Forwarding no
    PermitTunnel no
EOF

systemctl reload ssh || systemctl reload sshd || true
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
echo "guest SSH forced to: tmux attach -t $SESSION"
echo "on the phone (same Wi-Fi): ssh $USER_NAME@$LAN"
