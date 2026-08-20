#!/usr/bin/env bash
# Salon guest link is cafe Wi-Fi now. Do not stand up a Bluetooth NAP.
echo "Salon seats use cafe Wi-Fi, not Bluetooth PAN."
echo "Put the phone and this PC on the same network, then:"
echo "  $(dirname "$0")/run-seat.sh"
echo "  $(dirname "$0")/wifi-status.sh"
exit 0
