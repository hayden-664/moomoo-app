#!/usr/bin/env bash
# Runs the /daily-review skill headlessly and delivers the result to Telegram.
#
# Invoked by local.moomoo-app.daily-review.plist. Safe to run by hand to test:
#   ./run-daily-review.sh
#
# Why it posts to the sidecar instead of calling Telegram directly: the bot
# token stays in exactly one place — this script only reads it from .env to
# check it's present, never to send anything itself.
set -uo pipefail
cd "$(dirname "$0")"

log() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

# Same .env this repo's other scripts source (run-sidecar.sh). Read directly
# here rather than asking the sidecar, since the whole point is to bail out
# before waking OpenD or spending a claude -p run on a report with nowhere
# to go.
[ -f .env ] && set -a && . ./.env && set +a

if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] || [ -z "${TELEGRAM_CHAT_ID:-}" ]; then
    log "TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set; nothing to deliver to, skipping"
    exit 0
fi

SIDECAR="${SIDECAR_URL:-http://127.0.0.1:8788}"
# launchd starts with a minimal PATH, so the CLI is addressed absolutely.
CLAUDE="${CLAUDE_BIN:-$HOME/.local/bin/claude}"

# --if-due: send only if today's review has not gone out yet, and only on the
# days the alerts are configured for. This is what makes the review fire when
# you start the dev server rather than at a fixed clock time — the same
# once-per-calendar-day guard scheduler.py uses for the Telegram summary, so a
# missed morning is picked up whenever the machine next comes up, and running
# `npm run dev` five times a day still only sends one review.
IF_DUE=0
PROMPT=""
for arg in "$@"; do
    case "$arg" in
        --if-due) IF_DUE=1 ;;
        *) PROMPT="$arg" ;;
    esac
done
PROMPT="${PROMPT:-/daily-review}"

# Lives beside the kline cache, which is already gitignored.
STATE_DIR="sidecar/.cache"
STATE_FILE="$STATE_DIR/daily-review-last"
TODAY=$(date +%F)

deliver() {
    # --arg does the JSON escaping; a report full of quotes and newlines would
    # otherwise produce malformed JSON.
    if command -v jq >/dev/null 2>&1; then
        payload=$(jq -n --arg t "$1" '{text:$t}')
    else
        # Minimal fallback: escape backslashes, quotes, newlines and tabs.
        esc=$(printf '%s' "$1" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')
        payload="{\"text\": $esc}"
    fi
    curl -sS -m 30 -X POST "$SIDECAR/notify" \
        -H 'Content-Type: application/json' -d "$payload"
}

if [ "$IF_DUE" -eq 1 ]; then
    if [ -f "$STATE_FILE" ] && [ "$(cat "$STATE_FILE" 2>/dev/null)" = "$TODAY" ]; then
        log "review already sent today ($TODAY); nothing to do"
        exit 0
    fi
    # Weekdays only, matching ALERT_DAYS for the sidecar's own summary.
    if [ "$(date +%u)" -gt 5 ]; then
        log "weekend; skipping"
        exit 0
    fi
    # The review's own premise is that the US session has closed - it says so
    # under "Timing context", and every number in it is read as an overnight
    # close. Local time is UTC+8, so the close lands at 04:00-05:00 and a run
    # before then reports a session still in progress as though it were final.
    # Starting the dev server at 02:55 used to do exactly that.
    EARLIEST="${REVIEW_EARLIEST_HOUR:-6}"
    if [ "$(date +%H)" -lt "$EARLIEST" ]; then
        log "US session has not closed yet locally (before ${EARLIEST}:00); skipping"
        exit 0
    fi
fi

# When invoked from start-dev.sh the sidecar is booting in parallel, so a short
# wait covers the gap. It is not there to wait for a human — the --if-due guard
# means a missed morning is picked up on the next start instead.
WAIT_SECONDS="${REVIEW_WAIT_SECONDS:-300}"
INTERVAL=15

# No-credential fallback for the one case Telegram cannot cover: the sidecar
# never came up, so there is nothing to POST to. You are at the machine — a
# local notification is the honest channel, and it keeps the bot token out of
# this script.
notify_local() {
    osascript -e "display notification \"$1\" with title \"moomoo daily review\"" \
        >/dev/null 2>&1 || true
}

deadline=$(( $(date +%s) + WAIT_SECONDS ))
sidecar_up=0
opend_up=0
logged_wait=0

while :; do
    health=$(curl -sS -m 10 "$SIDECAR/health" 2>/dev/null || true)
    if [ -n "$health" ]; then
        sidecar_up=1
        if printf '%s' "$health" | grep -q '"opend"[[:space:]]*:[[:space:]]*"connected"'; then
            opend_up=1
            break
        fi
    fi
    [ "$(date +%s)" -ge "$deadline" ] && break
    if [ "$logged_wait" -eq 0 ]; then
        log "waiting up to ${WAIT_SECONDS}s for sidecar + OpenD (start: npm run dev)"
        logged_wait=1
    fi
    sleep "$INTERVAL"
done

if [ "$sidecar_up" -eq 0 ]; then
    log "sidecar never reachable at $SIDECAR within ${WAIT_SECONDS}s; nothing sent"
    notify_local "Sidecar never started. Run npm run dev, then ./run-daily-review.sh"
    exit 1
fi
if [ "$opend_up" -eq 0 ]; then
    log "OpenD not connected within ${WAIT_SECONDS}s; sending a notice instead"
    deliver "⚠️ Daily review skipped: OpenD is not connected. Start OpenD and log in."
    exit 0
fi

log "running: $PROMPT"
# Headless runs auto-deny anything not pre-allowed, so the moomoo tools are
# named explicitly. Without this the agent silently produces a report with no
# data in it.
report=$("$CLAUDE" -p "$PROMPT" \
    --allowedTools "mcp__moomoo__*" \
    2>/tmp/moomoo-daily-review.err)
status=$?

if [ $status -ne 0 ] || [ -z "${report// }" ]; then
    log "claude exited $status with empty or failed output"
    deliver "⚠️ Daily review failed (exit $status). See /tmp/moomoo-daily-review.err"
    exit 1
fi

log "delivering ${#report} chars"
deliver "$report"

# Stamped only after delivery, so a failed send is retried on the next start
# rather than being marked done.
mkdir -p "$STATE_DIR" && printf '%s' "$TODAY" > "$STATE_FILE"
log "done"
