"""Read-only HTTP surface over the moomoo SDK, bound to localhost.

Consumed by the Next.js dashboard and, if you point it there, by Claude. Every
route is a read. There is no order-placing endpoint anywhere in this service.
"""

from __future__ import annotations

import datetime as dt
import logging

import asyncio
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

import klines
import scheduler
import telegram
from config import CURRENCY, PNL_LOOKBACK_DAYS, SIDECAR_HOST, SIDECAR_PORT, TRD_MARKET
from currency import account_breakdown, currencies_in
from moomoo_client import MoomooError, OpendUnreachable, client, opend_reachable
from pnl import net_pnl
from screener import screen

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
log = logging.getLogger("sidecar")

@asynccontextmanager
async def lifespan(_app: FastAPI):
    task = asyncio.create_task(scheduler.run())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
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


@app.exception_handler(MoomooError)
async def _moomoo_error(_request, exc: MoomooError):
    return JSONResponse(status_code=502, content={"detail": str(exc)})


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


@app.post("/notify/pnl")
async def notify_pnl(days: int = Query(365, ge=1, le=1825)) -> dict:
    """Push the current net P&L summary to Telegram."""
    summary = pnl(days=days)
    try:
        acct = client.account_info()
    except MoomooError:
        acct = None
    return await telegram.notify(telegram.format_pnl(summary, acct))


@app.post("/notify/test")
async def notify_test() -> dict:
    return await telegram.notify("✅ moomoo-app is wired up to Telegram.")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=SIDECAR_HOST, port=SIDECAR_PORT)
