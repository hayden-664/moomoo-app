#!/usr/bin/env bash
# Everyday startup: make sure OpenD is up, then run the web app + sidecar.
set -uo pipefail
cd "$(dirname "$0")"
[ -f .env ] && set -a && . ./.env && set +a

OPEND_APP="/Applications/moomoo_OpenD.app"
PORT="${OPEND_PORT:-11111}"

listening() { nc -z 127.0.0.1 "$PORT" >/dev/null 2>&1; }

if listening; then
  echo "✓ OpenD already up on :$PORT"
else
  if [ -d "$OPEND_APP" ]; then
    echo "· OpenD not listening — launching it…"
    open -a "$OPEND_APP" 2>/dev/null || true
  else
    echo "! OpenD not found at $OPEND_APP"
  fi

  # OpenD needs a human to log in on first launch, so wait a while but do not
  # block forever — the dashboard degrades gracefully when it is absent.
  for _ in $(seq 1 30); do
    listening && break
    sleep 2
  done

  if listening; then
    echo "✓ OpenD up on :$PORT"
  else
    echo
    echo "⚠ OpenD is running but not accepting connections on :$PORT."
    echo "  It almost certainly needs you to log in — switch to the OpenD"
    echo "  window and sign in. The dashboard will connect on its own once"
    echo "  you do; no need to restart anything here."
    echo
  fi
fi

exec npx concurrently -n web,sidecar -c cyan,magenta "next dev" "./run-sidecar.sh"
