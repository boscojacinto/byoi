#!/usr/bin/env bash
# One-time Claude login on the seat PC so guests inherit a long-lived token.
# Guests never run `claude login`; they attach tmux claude-guest after OTP unlock.
set -euo pipefail
echo "On this seat PC, as the user who owns tmux claude-guest:"
echo
echo "  claude setup-token"
echo
echo "That stores a long-term credential for this machine."
echo "The seat agent starts tmux session 'claude-guest' with the claude CLI;"
echo "guests only attach after the host pushes an OTP to this seat."
echo
if command -v claude >/dev/null 2>&1; then
  echo "claude is on PATH: $(command -v claude)"
else
  echo "WARN: claude is not on PATH"
fi
if command -v tmux >/dev/null 2>&1 && tmux has-session -t claude-guest 2>/dev/null; then
  echo "tmux session claude-guest is already running"
else
  echo "tmux session claude-guest is not running (the seat agent will create it)"
fi
