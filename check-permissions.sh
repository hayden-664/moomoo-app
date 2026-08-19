#!/usr/bin/env bash
# Re-check option quote entitlements. Run AFTER buying a quote card and
# restarting OpenD — rights do not take effect until OpenD is restarted.
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env ] && set -a && . ./.env && set +a
PORT="${SIDECAR_PORT:-8788}"

if ! curl -sf --max-time 5 "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
  echo "Sidecar not running on :${PORT}. Start it with: npm run dev"
  exit 1
fi

curl -s --max-time 30 "http://127.0.0.1:${PORT}/permissions" | python3 -c '
import json, sys
d = json.load(sys.stdin)
print()
print(f"{"MARKET":<8} {"STOCKS":<10} {"OPTIONS":<10}")
print("-" * 30)
for m, r in d["markets"].items():
    print(f"{m:<8} {str(r["stock"] or "-"):<10} {str(r["option"] or "-"):<10}")
print()
ok = d["options_enabled"]
print(f"Screener works for: {", ".join(ok) if ok else "nothing yet"}")
if "US" not in ok:
    print("US options still unavailable to the API.")
    print("If you just bought a card, restart OpenD and run this again.")
else:
    print("US options are live — the screener will work on your US holdings.")
print()
'
