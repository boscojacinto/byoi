#!/usr/bin/env bash
# Turn a fresh Utho Ubuntu box into something scripts/cloud-up.sh can run on.
# Copied to the VM and run there as root, once:
#
#   scp vm-bootstrap.sh root@<ip>:/tmp/ && ssh root@<ip> bash /tmp/vm-bootstrap.sh
#
# Idempotent: re-running it after a rebuild or a partial failure is fine.
#
# Docker comes from Docker's own apt repository rather than `curl … | sh`,
# because the repository is signed and the pipe is not, and because `apt
# upgrade` then keeps the daemon patched without anyone remembering to.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "run this as root on the VM" >&2
  exit 1
fi

. /etc/os-release
if [[ "${ID:-}" != "ubuntu" && "${ID_LIKE:-}" != *debian* ]]; then
  echo "expected Ubuntu/Debian; this box says ID=${ID:-?}" >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive

echo "==> base packages"
apt-get update -qq
# openssl: apps/tls.py shells out to it to mint the salon CA.
# python3:  cloud-up.sh checks the operator password before touching Docker.
# rsync:    how the checkout arrives from the operator's laptop.
apt-get install -y -qq ca-certificates curl gnupg openssl python3 rsync git ufw

echo "==> docker"
if ! command -v docker >/dev/null; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL "https://download.docker.com/linux/${ID}/gpg" \
    | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/${ID} ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io \
    docker-buildx-plugin docker-compose-plugin
  systemctl enable --now docker
fi
docker --version
docker compose version

echo "==> firewall"
# The desk publishes seats through Caddy on 443, so nothing else needs to be
# open. Seat containers talk to the desk over a Docker network, not the host.
ufw allow 22/tcp    >/dev/null
ufw allow 80/tcp    >/dev/null
ufw allow 443/tcp   >/dev/null
ufw --force enable  >/dev/null
ufw status verbose

echo "==> unattended security updates"
apt-get install -y -qq unattended-upgrades
dpkg-reconfigure -f noninteractive unattended-upgrades

echo "==> /opt/byoi"
mkdir -p /opt/byoi
chmod 700 /opt/byoi

# Seats are memory-capped containers (BYOI_SEAT_MEMORY, 4g each) and the kernel
# OOM killer is a worse failure than a slow seat. Say so plainly rather than
# letting someone find out on a busy Saturday.
mem_gb=$(( $(awk '/MemTotal/ {print $2}' /proc/meminfo) / 1024 / 1024 ))
echo
echo "RAM: ${mem_gb}g"
echo "Budget roughly 4g per concurrent seat plus 2g for the desk and Caddy."
echo "Set BYOI_MAX_SEATS in deploy/.env to match: $(( (mem_gb - 2) / 4 )) fits here."
echo
echo "Bootstrapped. Next: copy the checkout to /opt/byoi and run scripts/cloud-up.sh."
