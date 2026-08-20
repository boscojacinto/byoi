#!/usr/bin/env bash
# Salon guest link is cafe Wi-Fi now. Bluetooth PAN is not the floor path.
echo "Salon seats use cafe Wi-Fi, not Bluetooth PAN."
echo "Run: $(dirname "$0")/wifi-status.sh"
exec "$(dirname "$0")/wifi-status.sh"
