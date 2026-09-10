#!/bin/bash
# Send the paper engine's simulated session note to Telegram.
#
# Deliberately its own job and its own message. The daily review reports a real
# brokerage account; this reports money that does not exist. They are never
# merged, so a fabricated P&L can never be read as a real one.
#
# It fires at 08:50, five minutes after the daily review, so the real numbers
# land first and the simulation follows clearly separated.
#
# Journal-backed: it still reports on a morning when OpenD is down.
set -euo pipefail
PORT="${SIDECAR_PORT:-8788}"
curl -fsS -X POST "http://127.0.0.1:${PORT}/paper/digest" \
  || { echo "sidecar unreachable on ${PORT}; no session note sent"; exit 1; }
echo
