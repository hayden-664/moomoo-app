#!/usr/bin/env bash
# Starts the read-only moomoo sidecar. Loads .env if present.
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env ] && set -a && . ./.env && set +a
exec sidecar/.venv/bin/python sidecar/app.py
