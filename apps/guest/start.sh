#!/usr/bin/env bash
# Expo SDK 54 needs Node >= 20 (Array.prototype.toReversed).
set -euo pipefail
cd "$(dirname "$0")"
export PATH="${HOME}/.local/bin:${PATH}"
exec npx expo start "$@"
