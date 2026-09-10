#!/usr/bin/env bash
# Starts the read-only moomoo sidecar. Loads .env if present.
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env ] && set -a && . ./.env && set +a
# One process may hold a paper trade context. OpenD delivers an order push to
# every connected context that can see the account, so a second sidecar doubles
# every event and races the first on the journal - and uvicorn runs the app
# lifespan before it binds, so a second instance that cannot even take the port
# still opens a context and runs a full engine loop. `npm run dev` and the
# launchd agent both land here; on 2026-09-10 both were live for fourteen hours.
# The broker holds an advisory lock as the real guarantee; this is the early,
# legible half of it.
if curl -sS -m 2 "http://127.0.0.1:${SIDECAR_PORT:-8788}/health" >/dev/null 2>&1; then
    echo "sidecar already running on ${SIDECAR_PORT:-8788}; not starting a second one."
    echo "  holder: $(lsof -nP -iTCP:${SIDECAR_PORT:-8788} -sTCP:LISTEN -t 2>/dev/null | tr '\n' ' ')"
    exit 0
fi

exec sidecar/.venv/bin/python sidecar/app.py
