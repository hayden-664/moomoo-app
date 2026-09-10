"""HTTP surface over the moomoo SDK, bound to localhost.

Consumed by the Next.js dashboard and, if you point it there, by Claude.

Every route touching the real account is a read, and that is enforced rather
than asserted: ``moomoo_client`` holds the only context pointed at it and
crashes on import if an order method appears in it. The ``/paper/*`` routes
reach a separate engine under ``sidecar/paper/`` whose own context can only
see the simulated account, so an order placed through them cannot land
anywhere else. See AGENTS.md for the four layers that make that true.
"""

from __future__ import annotations

import datetime as dt
import logging
import pathlib

import asyncio
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

import currency
import indicators
import klines
import paper
import research
import scheduler
import telegram
from config import (
    ACC_ID,
    ACC_INDEX,
    CURRENCY,
    PNL_LOOKBACK_DAYS,
    SIDECAR_HOST,
    SIDECAR_PORT,
    TRD_MARKET,
)
from currency import account_breakdown, currencies_in
from moomoo_client import MoomooError, OpendUnreachable, client, opend_reachable, require_opend
from paper import engine as paper_engine
from paper import journal as paper_journal
from paper import rules as paper_rules
from paper import broker as paper_broker_mod
from paper.broker import broker as paper_broker
from pnl import net_pnl
from screener import screen

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
log = logging.getLogger("sidecar")

@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Two tasks, not one. The alert scheduler does blocking P&L work on its own
    # period; the paper engine needs a different cadence and must be killable
    # without taking the daily alerts down with it.
    tasks = [
        asyncio.create_task(scheduler.run()),
        asyncio.create_task(paper_engine.run()),
    ]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task
        paper_broker.close()
        client.close()


app = FastAPI(
    title="moomoo-app sidecar",
    description="Read-only bridge to moomoo OpenD.",
    version="0.1.0",
    lifespan=lifespan,
)

# The dashboard is the only intended caller and it runs on localhost.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.exception_handler(OpendUnreachable)
async def _opend_unreachable(_request, exc: OpendUnreachable):
    return JSONResponse(status_code=503, content={"detail": str(exc)})


@app.exception_handler(paper.SimulationViolation)
async def _simulation_violation(_request, exc: paper.SimulationViolation):
    # 500, not 502: this is a bug in us, not a broker failure.
    return JSONResponse(status_code=500, content={"detail": str(exc)})


@app.exception_handler(MoomooError)
async def _moomoo_error(_request, exc: MoomooError):
    return JSONResponse(status_code=502, content={"detail": str(exc)})


# The read-only guarantee has a scope, and saying which account it covers is
# the difference between a fact and folklore now that a paper engine exists.
_READ_ONLY_SCOPE = f"real account {ACC_ID or f'index {ACC_INDEX}'} (reads only)"


def _paper_state() -> dict:
    """Engine summary for /health. Must never raise - health is a liveness probe."""
    state: dict = {
        "enabled": paper_engine.ENABLED,
        "acc_id": paper.PAPER_ACC_ID,
        "trd_env": paper.PAPER_TRD_ENV,
        "killed": False,
        "cycle_minutes": paper_engine.CYCLE_MINUTES,
        # Empty means entries are evaluated on every cycle.
        "evaluate_at": paper_engine.EVALUATE_AT or None,
        "notify": paper_engine.NOTIFY,
    }
    try:
        open_now, why = paper_engine.entry_window()
        state["entry_window"] = {"open": open_now, "reason": why}
    except Exception as exc:  # noqa: BLE001 - health must never raise
        state["entry_window"] = {"open": None, "reason": str(exc)}
    try:
        # Set when a session ended without the entry window ever being reached.
        # Trading nothing all day is invisible otherwise: it is not a halt.
        state["missed_entry_window"] = paper_engine.missed_entry_window()
    except Exception:  # noqa: BLE001
        state["missed_entry_window"] = None
    try:
        state["killed"] = paper_journal.killed()
        state["kill_reason"] = paper_journal.kill_reason()
        last = paper_journal.last_cycle()
        state["last_cycle"] = last.get("started_at") if last else None
    except Exception as exc:  # noqa: BLE001
        state["error"] = str(exc)
    try:
        state["rules_sha"] = paper_rules.load().sha256
    except paper_rules.RuleError as exc:
        state["rules_error"] = str(exc)
    return state


def _default_window(days: int = 365) -> tuple[str, str]:
    today = dt.date.today()
    return (today - dt.timedelta(days=days)).isoformat(), today.isoformat()


@app.get("/health")
def health() -> dict:
    """Confirm OpenD is reachable without assuming a session is logged in."""
    if not opend_reachable():
        return {
            "opend": "unreachable",
            "error": "OpenD is not listening on its configured port. Start OpenD and log in.",
            "telegram": telegram.configured(),
            "read_only": True,
            "read_only_scope": _READ_ONLY_SCOPE,
        }
    try:
        state = client.quote.get_global_state()
        ok = state[0] == 0
        detail = state[1] if ok else str(state[1])
    except Exception as exc:  # noqa: BLE001 - health must never raise
        return {"opend": "unreachable", "error": str(exc), "telegram": telegram.configured()}
    return {
        "opend": "connected" if ok else "error",
        "market": TRD_MARKET,
        "state": detail,
        "telegram": telegram.configured(),
        "read_only": True,
        "read_only_scope": _READ_ONLY_SCOPE,
        "paper": _paper_state(),
        "alerts": _alerts_state(),
    }


def _alerts_state() -> dict:
    import os

    return {
        "enabled": scheduler._enabled() and telegram.configured(),
        "daily_time": os.getenv("ALERT_DAILY_TIME", "08:30"),
        "days": os.getenv("ALERT_DAYS", "mon-fri"),
        "move_threshold": float(os.getenv("ALERT_MOVE_ABS", "0") or 0),
    }


@app.get("/permissions")
def permissions() -> dict:
    """Which markets this account can pull option/stock quotes for."""
    info = client.user_info()

    def right(key: str) -> str | None:
        v = info.get(key)
        return None if v in (None, "", "N/A") else str(v)

    markets = {
        "US": {"stock": right("us_qot_right"), "option": right("us_option_qot_right")},
        "HK": {"stock": right("hk_qot_right"), "option": right("hk_option_qot_right")},
        "CN": {"stock": right("cn_qot_right"), "option": None},
    }
    return {
        "markets": markets,
        "options_enabled": [
            m for m, r in markets.items() if r["option"] and r["option"] != "NO"
        ],
    }


@app.get("/accounts")
def accounts() -> list[dict]:
    """List sub-accounts, so the right security firm / index can be chosen."""
    return client.account_list()


@app.get("/account")
def account() -> dict:
    """Balances, with the reporting-currency totals split back per currency.

    ``total_assets``, ``cash``, ``market_val`` and ``power`` come back already
    converted into MOOMOO_CURRENCY, so on a multi-currency account they read as
    pure base currency while quietly containing a foreign balance. The split
    and the FX rate implied by it are attached so the UI can show both.
    """
    acct = client.account_info()
    return {**acct, "currency_split": account_breakdown(acct, base=CURRENCY)}


@app.get("/positions")
def positions() -> list[dict]:
    return client.positions()


def _fx_rates(rows: list[dict]) -> tuple[dict[str, float], str]:
    """Rates for converting *rows* into MOOMOO_CURRENCY, and where they came from.

    Primary source is the broker's own conversion rate, read by asking
    ``accinfo_query`` for the same total in each currency and dividing (cached
    60s). OpenD serves no forex quotes to ask instead -- the FX market returns
    "Unsupported quote market" and carries no entitlement.

    The fallback solves the rate out of a single payload's per-currency fields,
    which only works when exactly one foreign currency holds a balance.

    A currency neither source can price is simply absent from the result:
    ``net_pnl`` leaves it out of the totals and names it in ``conversion``,
    rather than folding it in at face value.
    """
    live_src = "moomoo's own conversion rate, read live from the account"
    implied_src = "inferred from the broker's conversion of total assets"

    foreign = [c for c in currencies_in(rows, CURRENCY) if c != CURRENCY]
    if not foreign:
        return {}, live_src

    try:
        live = client.fx_rates(CURRENCY, foreign)
    except MoomooError as exc:
        log.warning("live FX rate unavailable: %s", exc)
        live = {}
    missing = [c for c in foreign if c not in live]
    if not missing:
        return live, live_src

    try:
        implied = account_breakdown(client.account_info(), base=CURRENCY)["implied_fx"]
    except MoomooError as exc:
        log.warning("fallback FX rate unavailable: %s", exc)
        implied = {}
    # Live wins wherever both have an answer.
    merged = {**{c: r for c, r in implied.items() if c in missing}, **live}
    if not merged:
        return {}, "unavailable"
    return merged, live_src if not implied else f"{live_src}; {implied_src} where unavailable"


@app.get("/pnl")
def pnl(days: int = Query(PNL_LOOKBACK_DAYS, ge=1, le=PNL_LOOKBACK_DAYS)) -> dict:
    """Net P&L across open positions and (derived) realized trades.

    ``days`` narrows what is *reported*. Deals are always pulled over the full
    lookback so that FIFO matching can see the buys behind a sell, and so that
    changing the range in the UI costs no additional broker calls.
    """
    start, end = _default_window(days)
    full_start, _ = _default_window(PNL_LOOKBACK_DAYS)
    positions = client.positions()
    try:
        deals = client.deal_history(full_start, end)
    except MoomooError as exc:
        # History can fail independently (permissions, window too wide). Degrade
        # to open-position P&L rather than losing the whole response.
        log.warning("deal history unavailable: %s", exc)
        deals = []
    rates, rate_source = _fx_rates(positions + deals)
    summary = net_pnl(
        positions, deals, since=start, base=CURRENCY, rates=rates, rate_source=rate_source
    )
    in_window = [d for d in deals if str(d.get("create_time") or "")[:10] >= start]
    summary["window"] = {
        "start": start,
        "end": end,
        "days": days,
        "deals": len(in_window),
        "matched_from": full_start,
    }
    # position_count on the promoted top level counts the base currency only,
    # matching the net beside it; the whole-account count is separate.
    summary["total_position_count"] = len(positions)
    return summary


@app.get("/history")
def history(days: int = Query(90, ge=1, le=1825)) -> list[dict]:
    start, end = _default_window(days)
    return client.deal_history(start, end)


@app.get("/options/screen")
def options_screen(
    code: str = Query(..., description="Underlying, e.g. US.AAPL"),
    dte_min: int = Query(7, ge=0, le=730),
    dte_max: int = Query(60, ge=1, le=730),
    delta_min: float = Query(0.15, ge=0, le=1),
    delta_max: float = Query(0.45, ge=0, le=1),
    option_type: str = Query("ALL", pattern="^(ALL|CALL|PUT)$"),
    min_open_interest: int = Query(100, ge=0),
    limit: int = Query(25, ge=1, le=100),
) -> dict:
    if dte_max < dte_min:
        raise HTTPException(400, "dte_max must be >= dte_min")
    if delta_max < delta_min:
        raise HTTPException(400, "delta_max must be >= delta_min")
    return screen(
        code=code,
        dte_min=dte_min,
        dte_max=dte_max,
        delta_min=delta_min,
        delta_max=delta_max,
        option_type=option_type,
        min_open_interest=min_open_interest,
        limit=limit,
    )


# -- research: securities the account does not hold -------------------------


@app.get("/research/search")
def research_search(
    keyword: str = Query(..., min_length=1, description="Company or ticker, e.g. 'nvidia'"),
    limit: int = Query(10, ge=1, le=50),
) -> list[dict]:
    return research.search(keyword, limit)


@app.get("/research/watchlist")
def research_watchlist(group: str = Query("All")) -> dict:
    return research.watchlist(group)


@app.get("/research/quote")
def research_quote(
    codes: str = Query(..., description="Comma-separated, e.g. US.NVDA,US.AMD"),
) -> list[dict]:
    wanted = [c.strip() for c in codes.split(",") if c.strip()]
    if not wanted:
        raise HTTPException(400, "at least one code is required")
    return research.quote(wanted)


@app.get("/research/profile")
def research_profile(code: str = Query(..., description="e.g. US.NVDA")) -> dict:
    return research.profile(code)


@app.get("/research/volatility")
def research_volatility(code: str = Query(..., description="e.g. US.NVDA")) -> dict:
    return research.volatility(code)


@app.get("/research/capital-flow")
def research_capital_flow(
    code: str = Query(..., description="e.g. US.NVDA"),
    period: str = Query("INTRADAY", pattern="^(INTRADAY|DAY|WEEK|MONTH)$"),
) -> dict:
    return research.capital_flow(code, period)


@app.get("/research/earnings-reaction")
def research_earnings_reaction(code: str = Query(..., description="e.g. US.NVDA")):
    return research.earnings_reaction(code)


@app.get("/research/calendar")
def research_calendar(
    kind: str = Query(..., pattern="^(earnings|dividends|economic)$"),
    market: str = Query("US", pattern="^(US|HK|SH|SZ|SG|JP|MY|AU|CA)$"),
    days: int = Query(7, ge=1, le=60),
) -> dict:
    return research.calendar(kind, market, days)


@app.post("/research/screen")
def research_screen(body: dict) -> dict:
    """Stock screener. POST because the filter list is structured, not flat.

    Body: ``{"market": "US", "filters": [{"field": ..., "min": ..., "max": ...}],
    "plate": null, "begin": 0, "num": 50}``.
    """
    try:
        return research.screen_stocks(
            market=body.get("market", "US"),
            filters=body.get("filters"),
            plate=body.get("plate"),
            begin=int(body.get("begin", 0)),
            num=int(body.get("num", 50)),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/research/screen/fields")
def research_screen_fields() -> dict:
    """The screener's field vocabulary, so filters can be built without guessing."""
    return research.screen_fields()


@app.get("/quota")
def quota() -> dict:
    """Remaining historical-kline and subscription budget.

    Worth checking before warming a lot of new symbols: the kline charge is per
    symbol per period, so codes already listed cost nothing to pull again.
    """
    return klines.quota()


@app.get("/candles")
def candles(
    code: str = Query(..., description="e.g. US.TSLA"),
    days: int = Query(365, ge=5, le=1825),
    ktype: str = Query("K_DAY", pattern="^K_(1M|5M|15M|30M|60M|DAY|WEEK|MON)$"),
) -> dict:
    return klines.candles(code=code, days=days, ktype=ktype)


@app.get("/markers")
def markers(
    code: str = Query(..., description="e.g. US.TSLA"),
    days: int = Query(365, ge=5, le=1825),
) -> list[dict]:
    """Your fills for one symbol, ready to plot against the bars."""
    return klines.markers(code=code, days=days)


@app.get("/technicals")
def technicals(
    code: str = Query(..., description="e.g. US.TSLA"),
    days: int = Query(indicators.DEFAULT_DAYS, ge=30, le=1825),
    ktype: str = Query("K_DAY", pattern="^K_(1M|5M|15M|30M|60M|DAY|WEEK|MON)$"),
) -> dict:
    """Indicators derived from the cached bars.

    Served off the same series ``/candles`` returns, so an indicator can never
    disagree with the candle it is drawn on. ``days`` defaults wide enough for
    SMA200 to be populated rather than null.
    """
    return indicators.technicals(code=code, days=days, ktype=ktype)


@app.post("/notify/pnl")
async def notify_pnl(days: int = Query(365, ge=1, le=1825)) -> dict:
    """Push the current net P&L summary to Telegram."""
    summary = pnl(days=days)
    try:
        acct = client.account_info()
    except MoomooError:
        acct = None
    return await telegram.notify(telegram.format_pnl(summary, acct))


@app.post("/notify")
async def notify(body: dict) -> dict:
    """Push arbitrary text to Telegram.

    Exists so a scheduled agent can deliver a written report to the same place
    the P&L alerts already land. Sent as plain text and split at the 4096-char
    cap — see ``telegram.notify_text`` for why HTML parsing is not used here.

    The bot token never leaves the sidecar's environment: callers post text,
    not credentials.
    """
    text = (body or {}).get("text") or ""
    if not text.strip():
        raise HTTPException(400, "body must include a non-empty 'text'")
    return await telegram.notify_text(text, silent=bool((body or {}).get("silent")))


@app.post("/notify/test")
async def notify_test() -> dict:
    return await telegram.notify("✅ moomoo-app is wired up to Telegram.")


# Same script the launchd job runs, so a manual trigger and the scheduled one
# can never drift apart in behaviour.
_REVIEW_SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "run-daily-review.sh"
_review_running = False


@app.post("/notify/review")
async def notify_review() -> dict:
    """Run the /daily-review skill and deliver it to Telegram, in the background.

    The skill takes ~2 minutes end to end, so this launches the same
    ``run-daily-review.sh`` the launchd job uses and returns immediately
    rather than holding the request open. ``require_opend`` is checked here
    first purely so a click gets an instant, clear error instead of a
    "started" response followed by silence.
    """
    require_opend()
    global _review_running
    if _review_running:
        raise HTTPException(409, "a daily review is already running")
    if not _REVIEW_SCRIPT.exists():
        raise HTTPException(500, f"missing {_REVIEW_SCRIPT}")

    _review_running = True

    async def _run() -> None:
        global _review_running
        try:
            with open("/tmp/moomoo-daily-review.log", "a") as out:
                proc = await asyncio.create_subprocess_exec(
                    str(_REVIEW_SCRIPT),
                    cwd=str(_REVIEW_SCRIPT.parent),
                    stdout=out,
                    stderr=asyncio.subprocess.STDOUT,
                )
                await proc.wait()
        except Exception:  # noqa: BLE001 - background task must not go unlogged
            log.exception("manual daily review run failed")
        finally:
            _review_running = False

    asyncio.create_task(_run())
    return {"started": True}


# -- paper engine --------------------------------------------------------------
# Orders placed through these routes reach the simulated account only; see
# sidecar/paper/guard.py for what makes that true rather than intended.

_paper_run_running = False


@app.exception_handler(paper_broker_mod.PaperContextBusy)
async def _paper_busy(_request, exc: Exception) -> JSONResponse:
    """Two sidecars is a setup mistake, not a broker failure - 503, not 500."""
    return JSONResponse(
        status_code=503,
        content={
            "error": "paper context held by another process",
            "detail": str(exc),
            "hint": "`npm run dev` starts a sidecar and so does the launchd agent. "
                    "Run one. `lsof -nP -iTCP:8788 -sTCP:LISTEN` names the survivor.",
        },
    )


@app.get("/paper/status")
def paper_status() -> dict:
    """Everything needed to answer "is it on, what is it looking at, and why not"."""
    out: dict = {
        "engine": _paper_state(),
        "orders_today": paper_journal.orders_today(),
        # The cap counts entries, not activity - an exit is never refused by
        # it - so the counter shown against the cap has to be the same unit.
        "entries_today": paper_journal.orders_today(side="BUY"),
        "open_orders": paper_journal.open_orders(),
    }
    try:
        ruleset = paper_rules.load()
        out["rules"] = {
            "sha256": ruleset.sha256,
            "ktype": ruleset.ktype,
            "source_path": ruleset.source_path,
            "rule_ids": [r.id for r in ruleset.rules],
            "enabled_rule_ids": [r.id for r in ruleset.enabled()],
            "portfolio": ruleset.portfolio.__dict__,
        }
        out["caps"] = {
            "max_orders_per_day": ruleset.portfolio.max_orders_per_day,
            "max_concurrent_positions": ruleset.portfolio.max_concurrent_positions,
            "max_open_notional_pct": ruleset.portfolio.max_open_notional_pct,
            "max_notional_usd": paper.PAPER_MAX_NOTIONAL_USD,
            "max_risk_pct": paper.PAPER_MAX_RISK_PCT,
        }
    except paper_rules.RuleError as exc:
        out["rules_error"] = str(exc)

    last = paper_journal.last_cycle()
    if last:
        import json as _json

        out["last_cycle"] = {
            **last,
            "universe": _json.loads(last.get("universe_json") or "null"),
        }
    return out


@app.post("/paper/digest")
async def paper_digest(session: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$")) -> dict:
    """Send the simulated session note. Its own message, never the daily review.

    The review reports a real brokerage account and this reports money that does
    not exist; they travel separately so the two can never be read as one
    statement. Deliberately not on the browser POST allowlist - it sends a
    message, and the allowlist is brakes-only.

    Journal-backed, so it still reports on a morning when OpenD is down. The
    broker figures are best-effort decoration on top.
    """
    # Defaults to the last session whose close has passed, not trade_date():
    # this fires at 08:50 local, which is the previous evening in New York, so
    # trade_date() names a session that has not happened yet.
    summary = paper_journal.session_summary(
        session or paper_engine.last_completed_session()
    )
    extra: dict = {
        "halted": paper_journal.killed(),
        "kill_reason": paper_journal.kill_reason(),
    }
    try:
        acct = paper_broker.account()
        equity = currency.num(acct.get("total_assets"))
        extra["paper_equity"] = equity
        opening = summary.get("day_open_equity")
        if equity is not None and opening is not None:
            extra["paper_change"] = equity - float(opening)
    except Exception as exc:  # noqa: BLE001 - the note matters more than the decoration
        log.info("paper digest: broker unavailable (%s)", exc)

    text = telegram.format_paper_session(summary, extra)
    sent = await telegram.notify(text)
    return {"sent": bool(sent), "session": summary["session"],
            "entries_evaluated": summary["entries_evaluated"]}


@app.get("/paper/positions")
def paper_positions() -> list[dict]:
    """Simulated holdings. Same row shape as /positions, so the table renders both.

    Closed rows are dropped: a paper account keeps returning a symbol with
    ``qty`` 0 after it has been sold, which the real account does not do, and a
    flat position is not a position.
    """
    return [p for p in paper_broker.positions() if float(p.get("qty") or 0) != 0]


@app.get("/paper/orders")
def paper_orders(
    days: int = Query(7, ge=1, le=365),
    open_only: bool = Query(False),
) -> list[dict]:
    """Journal-backed, so it answers even when OpenD is down."""
    return paper_journal.open_orders() if open_only else paper_journal.orders(days=days)


@app.get("/paper/journal")
def paper_journal_route(
    days: int = Query(7, ge=1, le=365),
    limit: int = Query(100, ge=1, le=1000),
    outcome: str | None = Query(
        None, pattern="^(MATCHED|NO_MATCH|BLOCKED|PLACED|PLACE_FAILED)$"
    ),
) -> list[dict]:
    return paper_journal.journal(days=days, limit=limit, outcome=outcome)


@app.get("/paper/rules")
def paper_rules_route() -> dict:
    """The parsed ruleset, never the raw file.

    A malformed file surfaces here as a validation error naming the bad rule,
    rather than as text a reader has to diff by eye.
    """
    try:
        ruleset = paper_rules.load()
    except paper_rules.RuleError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {
        "version": ruleset.version,
        "ktype": ruleset.ktype,
        "sha256": ruleset.sha256,
        "source_path": ruleset.source_path,
        "codes": list(ruleset.codes),
        "portfolio": ruleset.portfolio.__dict__,
        "rules": [
            {
                "id": r.id, "enabled": r.enabled, "side": r.side,
                "when": r.when, "exit": r.exit,
                "risk_pct": r.risk_pct, "atr_mult_stop": r.atr_mult_stop,
                "max_notional_pct": r.max_notional_pct,
                "cooldown_days": r.cooldown_days,
                "time_in_force": r.time_in_force, "session": r.session,
                "limit_offset_pct": r.limit_offset_pct,
            }
            for r in ruleset.rules
        ],
        "note": (
            "User-authored. Nothing in this app may generate, tune or rank a rule; "
            "the engine only evaluates what is written here."
        ),
    }


@app.post("/paper/kill")
def paper_kill(reason: str = Query("manual", max_length=200)) -> dict:
    """Halt the engine. Idempotent, and the one paper route the browser may call.

    Takes its reason as a query parameter because the Next.js proxy forwards no
    request body - the same limitation that keeps /research/screen off the
    browser allowlist.
    """
    paper_journal.kill(reason)
    return {"killed": True, "reason": reason}


# A halt caused by the isolation guarantee failing is not a resumable
# condition. Everything else - a daily loss limit, a manual stop - is.
_UNRESUMABLE = "simulation violation"


@app.post("/paper/resume")
def paper_resume(reason: str = Query(..., max_length=200)) -> dict:
    """Clear the kill switch.

    The caller must echo the current kill reason. A halt usually means
    something went wrong, and requiring the reason back means a resume is a
    decision rather than a stray click.

    One halt is not clearable this way at all: a `SimulationViolation` means the
    account the engine was pinned to is no longer the account it is talking to.
    That needs looking at, not a button, so it must be cleared out of band by
    deleting `sidecar/.cache/paper/KILLED`.
    """
    current = paper_journal.kill_reason() or ""
    if not paper_journal.killed():
        return {"killed": False, "note": "engine was not halted"}
    if _UNRESUMABLE in current.lower():
        raise HTTPException(
            403,
            "this halt was caused by the simulation guarantee failing and cannot "
            "be cleared from the API. Investigate first, then remove "
            "sidecar/.cache/paper/KILLED by hand.",
        )
    if reason.strip() != current.strip():
        raise HTTPException(
            409, f"resume must echo the current kill reason: {current!r}"
        )
    paper_journal.resume()
    return {"killed": False, "resumed_from": current}


@app.post("/paper/dry-run")
async def paper_dry_run(
    ignore_market_hours: bool = Query(True),
) -> dict:
    """Evaluate everything and place nothing.

    The workhorse for validating a rules change: it walks the same universe,
    sizing and guardrail pipeline a live cycle does and returns the orders it
    would have sent.
    """
    require_opend()
    return await asyncio.to_thread(
        paper_engine.cycle, True, ignore_market_hours
    )


@app.post("/paper/run-once")
async def paper_run_once() -> dict:
    """One real cycle now, against the simulated account.

    Single-flight for the same reason /notify/review is: two concurrent cycles
    would race on the per-day order caps.
    """
    require_opend()
    global _paper_run_running
    if _paper_run_running:
        raise HTTPException(409, "a paper cycle is already running")
    _paper_run_running = True
    try:
        out = await asyncio.to_thread(paper_engine.cycle, False, False)
        await asyncio.to_thread(paper_engine.reconcile)
        await asyncio.to_thread(paper_engine.drain_fills)
        return out
    finally:
        _paper_run_running = False


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=SIDECAR_HOST, port=SIDECAR_PORT)
