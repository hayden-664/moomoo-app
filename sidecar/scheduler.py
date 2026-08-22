"""Background alert scheduler.

Runs inside the sidecar process, so it is alive exactly when the sidecar is
alive — and therefore only while this Mac is awake with OpenD logged in. There
is no cloud component; nothing fires when the machine is off or asleep.

Two independent alerts:

* **Daily summary** - one Telegram message at ``ALERT_DAILY_TIME`` on the days
  listed in ``ALERT_DAYS``. The last-sent date is persisted to ``.state.json``,
  so it fires at most once per calendar day even across sidecar restarts. If
  the Mac is off at the scheduled time, the summary is sent on the first cycle
  after it comes back up that same day (a missed day is skipped, not queued).
* **Move alert** - checks net P&L every ``ALERT_CHECK_MINUTES`` and messages
  only when it has moved more than ``ALERT_MOVE_ABS`` since the last alert,
  which keeps a drifting number from generating a stream of notifications.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import pathlib

import telegram
from config import CURRENCY, PNL_LOOKBACK_DAYS
from currency import currencies_in
from moomoo_client import MoomooError, OpendUnreachable, client
from pnl import net_pnl
from screener import screen

log = logging.getLogger("scheduler")

_DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}

# Survives restarts so a restart cannot re-send an alert already sent today.
_STATE_PATH = pathlib.Path(__file__).with_name(".state.json")


def _load_state() -> dict:
    try:
        return json.loads(_STATE_PATH.read_text())
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    try:
        _STATE_PATH.write_text(json.dumps(state))
    except OSError:
        log.warning("could not persist scheduler state to %s", _STATE_PATH)


def _get_date(state: dict, key: str) -> dt.date | None:
    raw = state.get(key)
    try:
        return dt.date.fromisoformat(raw) if raw else None
    except (TypeError, ValueError):
        return None


def _enabled() -> bool:
    return os.getenv("ALERTS_ENABLED", "true").lower() not in ("0", "false", "no")


def _alert_days() -> set[int]:
    raw = os.getenv("ALERT_DAYS", "mon-fri").strip().lower()
    if raw in ("daily", "all"):
        return set(range(7))
    out: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if "-" in part:
            a, _, b = part.partition("-")
            if a in _DAYS and b in _DAYS:
                start, end = _DAYS[a], _DAYS[b]
                out.update(
                    range(start, end + 1)
                    if start <= end
                    # A wrapping range like fri-mon.
                    else [*range(start, 7), *range(0, end + 1)]
                )
        elif part in _DAYS:
            out.add(_DAYS[part])
    return out or set(range(5))


def _parse_time(raw: str, fallback: dt.time) -> dt.time:
    try:
        hh, _, mm = raw.strip().partition(":")
        return dt.time(int(hh), int(mm or 0))
    except ValueError:
        log.warning("bad time %r, defaulting to %s", raw, fallback)
        return fallback


def _daily_time() -> dt.time:
    return _parse_time(os.getenv("ALERT_DAILY_TIME", "08:30"), dt.time(8, 30))


def _snapshot() -> dict:
    """Current net P&L, or raise if the broker side is unavailable."""
    positions = client.positions()
    today = dt.date.today()
    # Same lookback as the /pnl route so both share one deal-history cache entry.
    start = (today - dt.timedelta(days=PNL_LOOKBACK_DAYS)).isoformat()
    try:
        deals = client.deal_history(start, today.isoformat())
    except MoomooError:
        deals = []
    try:
        rates = client.fx_rates(CURRENCY, currencies_in(positions + deals, CURRENCY))
    except MoomooError:
        rates = {}  # foreign figures stay out of the totals rather than being guessed
    summary = net_pnl(positions, deals, since=start, base=CURRENCY, rates=rates)
    summary["window"] = {"start": start, "end": today.isoformat(), "days": PNL_LOOKBACK_DAYS}
    return summary


async def _send_daily() -> None:
    summary = await asyncio.to_thread(_snapshot)
    try:
        acct = await asyncio.to_thread(client.account_info)
    except MoomooError:
        acct = None
    header = "☀️ <b>Daily summary</b>\n\n"
    await telegram.notify(header + telegram.format_pnl(summary, acct))
    log.info("daily summary sent (net=%s)", summary["net"])


async def _maybe_send_move(last_net: float | None) -> float | None:
    threshold = float(os.getenv("ALERT_MOVE_ABS", "0") or 0)
    if threshold <= 0:
        return last_net
    summary = await asyncio.to_thread(_snapshot)
    net = summary["net"]
    if last_net is None:
        return net
    delta = net - last_net
    if abs(delta) < threshold:
        return last_net
    arrow = "📈" if delta > 0 else "📉"
    await telegram.notify(
        f"{arrow} <b>Net P&amp;L moved ${delta:+,.2f}</b>\n\n"
        + telegram.format_pnl(summary)
    )
    log.info("move alert sent (delta=%.2f)", delta)
    return net


def _entitled_markets() -> set[str]:
    """Markets whose option data this account can actually read."""
    try:
        info = client.user_info()
    except MoomooError:
        return set()
    out = set()
    for market, key in (("US", "us_option_qot_right"), ("HK", "hk_option_qot_right")):
        v = info.get(key)
        if v and str(v) not in ("NO", "N/A"):
            out.add(market)
    return out


def _screen_codes() -> list[str]:
    """Underlyings to screen: explicit list, else whatever is currently held.

    Codes in markets with no option entitlement are dropped rather than
    screened, so the digest reports one clear line instead of a wall of
    identical permission errors.
    """
    raw = os.getenv("ALERT_SCREEN_CODES", "").strip()
    if raw:
        codes = [c.strip().upper() for c in raw.split(",") if c.strip()]
    else:
        try:
            held = {p.get("code") for p in client.positions() if p.get("code")}
        except MoomooError:
            return []
        # Cap the fan-out: each code costs a chain call plus a snapshot, and
        # the quote APIs are rate-limited.
        codes = sorted(c for c in held if c)[
            : int(os.getenv("ALERT_SCREEN_MAX", "5") or 5)
        ]

    entitled = _entitled_markets()
    return [c for c in codes if c.split(".")[0] in entitled]


def _screen_one(code: str) -> dict:
    try:
        return screen(
            code=code,
            dte_min=int(os.getenv("ALERT_SCREEN_DTE_MIN", "14") or 14),
            dte_max=int(os.getenv("ALERT_SCREEN_DTE_MAX", "45") or 45),
            delta_min=float(os.getenv("ALERT_SCREEN_DELTA_MIN", "0.2") or 0.2),
            delta_max=float(os.getenv("ALERT_SCREEN_DELTA_MAX", "0.45") or 0.45),
            option_type=os.getenv("ALERT_SCREEN_TYPE", "CALL"),
            min_open_interest=int(os.getenv("ALERT_SCREEN_MIN_OI", "250") or 250),
            limit=3,
        )
    except MoomooError as exc:
        # A permission error on one market should not sink the whole digest.
        return {"underlying": code, "error": str(exc), "candidates": []}


async def _send_screen() -> None:
    codes = await asyncio.to_thread(_screen_codes)
    if not codes:
        entitled = await asyncio.to_thread(_entitled_markets)
        log.info("screen digest skipped: no screenable codes (entitled=%s)", entitled or "none")
        # Say so once a day rather than silently going quiet, but do not repeat
        # a wall of per-symbol permission errors.
        await telegram.notify(
            "🔎 <b>Options screen</b>\n\n<i>No holdings in a market with option "
            "data on this account"
            + (f" (option data available: {', '.join(sorted(entitled))})" if entitled else "")
            + ".</i>"
        )
        return
    results = []
    for code in codes:
        results.append(await asyncio.to_thread(_screen_one, code))
        await asyncio.sleep(1)  # stay clear of the quote rate limits
    await telegram.notify(telegram.format_screen(results))
    log.info("screen digest sent for %s", ", ".join(codes))


async def run() -> None:
    """Main scheduler loop. Never raises; logs and retries."""
    if not _enabled():
        log.info("alerts disabled (ALERTS_ENABLED=false)")
        return
    if not telegram.configured():
        log.info("alerts idle: TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set")
        return

    check_minutes = max(1, int(os.getenv("ALERT_CHECK_MINUTES", "15") or 15))
    log.info(
        "alerts active: daily %s on %s, move threshold $%s, checking every %dm",
        _daily_time().strftime("%H:%M"),
        os.getenv("ALERT_DAYS", "mon-fri"),
        os.getenv("ALERT_MOVE_ABS", "0"),
        check_minutes,
    )

    state = _load_state()
    last_daily = _get_date(state, "last_daily")
    last_screen = _get_date(state, "last_screen")
    last_net: float | None = None
    if last_daily or last_screen:
        log.info(
            "resuming: daily last sent %s, screen last sent %s",
            last_daily or "never",
            last_screen or "never",
        )
    screen_time = _parse_time(os.getenv("ALERT_SCREEN_TIME", "09:00"), dt.time(9, 0))
    screen_on = os.getenv("ALERT_SCREEN_ENABLED", "true").lower() not in (
        "0", "false", "no",
    )

    while True:
        try:
            now = dt.datetime.now()
            today = now.date()

            due = (
                today != last_daily
                and today.weekday() in _alert_days()
                and now.time() >= _daily_time()
            )
            if due:
                # Stamp before sending so a failure cannot cause a retry storm;
                # a missed summary is better than a repeating one.
                last_daily = today
                state["last_daily"] = today.isoformat()
                _save_state(state)
                await _send_daily()

            screen_due = (
                screen_on
                and today != last_screen
                and today.weekday() in _alert_days()
                and now.time() >= screen_time
            )
            if screen_due:
                last_screen = today
                state["last_screen"] = today.isoformat()
                _save_state(state)
                await _send_screen()

            last_net = await _maybe_send_move(last_net)

        except OpendUnreachable:
            log.debug("scheduler: OpenD down, skipping this cycle")
        except Exception:  # noqa: BLE001 - the loop must survive anything
            log.warning("scheduler cycle failed", exc_info=True)

        await asyncio.sleep(check_minutes * 60)
