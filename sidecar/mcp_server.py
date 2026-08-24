"""MCP server exposing the moomoo sidecar to Claude.

Talks to the sidecar over localhost HTTP rather than to OpenD directly, so it
inherits the deal-history cache, the rate-limit handling, the net-P&L
assembly and — importantly — the read-only guarantees. There is no tool here
that can place, modify or cancel an order, because no such endpoint exists.

Run:  sidecar/.venv/bin/python sidecar/mcp_server.py
"""

from __future__ import annotations

import os

import httpx
from mcp.server.mcpserver import MCPServer

SIDECAR = os.getenv("SIDECAR_URL", "http://127.0.0.1:8788")
TIMEOUT = float(os.getenv("MCP_TIMEOUT", "120"))

mcp = MCPServer(
    name="moomoo",
    version="0.1.0",
    instructions=(
        "Read-only access to Hayden's moomoo brokerage account and to moomoo's "
        "market data.\n\n"
        "Account tools cover balances, positions, net P&L and deal history. "
        "Research tools cover any listed security, held or not: search, "
        "watchlist, quotes, candles, company profile, volatility, capital flow, "
        "earnings reactions, calendars and a stock screener.\n\n"
        "This server reports facts. It cannot place, modify or cancel orders, "
        "and it does not rank trades or judge whether any position, contract or "
        "screened stock is a good idea. Report the numbers and let the user draw "
        "conclusions; do not offer buy/sell/hold recommendations from this data.\n\n"
        "One exception carries someone else's opinion rather than a number: the "
        "`third_party_views` branch of `profile` returns analyst consensus and "
        "Morningstar's rating and fair value. Attribute those to the firm that "
        "published them. Passing them on as your own view, or as this server's, "
        "would turn a quoted rating into advice."
    ),
)


async def _get(path: str, params: dict | None = None) -> dict | list:
    async with httpx.AsyncClient(timeout=TIMEOUT) as http:
        r = await http.get(f"{SIDECAR}/{path}", params=params or {})
    return _handle(r)


async def _post(path: str, body: dict) -> dict | list:
    async with httpx.AsyncClient(timeout=TIMEOUT) as http:
        r = await http.post(f"{SIDECAR}/{path}", json=body)
    return _handle(r)


def _handle(r: httpx.Response) -> dict | list:
    if r.status_code == 503:
        return {
            "error": "OpenD or the sidecar is not running",
            "detail": r.json().get("detail") if r.headers.get("content-type","").startswith("application/json") else r.text,
            "fix": "Run `npm run dev` in the project, and log in to OpenD.",
        }
    if r.status_code >= 400:
        return {"error": f"request failed ({r.status_code})", "detail": r.text[:400]}
    return r.json()


@mcp.tool(description="Connection health: whether OpenD is reachable, plus Telegram and alert state.")
async def health() -> dict:
    return await _get("health")


@mcp.tool(description="Quote entitlements per market. Explains why option screening may be unavailable.")
async def permissions() -> dict:
    return await _get("permissions")


@mcp.tool(
    description=(
        "Account balances: total assets, cash, market value, buying power, margin. "
        "total_assets/cash/market_val/power are converted into the reporting "
        "currency by the broker; `currency_split` carries the untouched "
        "per-currency balances and the FX rate that conversion implies."
    )
)
async def account() -> dict:
    return await _get("account")


@mcp.tool(description="All open positions with quantity, cost, last price, market value and per-position P&L.")
async def positions() -> list | dict:
    return await _get("positions")


@mcp.tool(
    description=(
        "Net P&L broken into its sources. `total_realized` is everything cashed "
        "out — use it when asked what has actually been realized. It splits into "
        "`closed.closed_realized` (positions fully exited) and "
        "`partial.partial_realized` (banked from selling part of a holding still "
        "owned). `open.open_unrealized` is broker-reported mark-to-market; the "
        "realized figures are DERIVED by FIFO-matching deal history and exclude "
        "fees, dividends and corporate actions — always describe them as approximate.\n\n"
        "Top-level figures are every currency converted into `base` and summed. "
        "`by_currency` holds the same figures unconverted, per settlement "
        "currency. `conversion` names the rate used and where it came from; "
        "realized figures use today's rate rather than the trade-date rate, so "
        "describe converted totals accordingly. Anything in "
        "`conversion.unconverted` is EXCLUDED from the totals; say so if it is "
        "non-empty."
    )
)
async def pnl(days: int = 365) -> dict:
    return await _get("pnl", {"days": days})


@mcp.tool(description="Executed deals over the given window, most useful for reviewing closed trades.")
async def deal_history(days: int = 90) -> list | dict:
    return await _get("history", {"days": days})


@mcp.tool(
    description=(
        "Screen an option chain. Returns contracts matching the filters, each "
        "with a factual one-line characterisation: cost, breakeven, required "
        "move, IV and liquidity. Report these as mechanics, not as suggestions. "
        "Requires option quote entitlement for the underlying's market — check "
        "`permissions` first if it fails."
    )
)
async def screen_options(
    code: str,
    dte_min: int = 14,
    dte_max: int = 45,
    delta_min: float = 0.2,
    delta_max: float = 0.45,
    option_type: str = "ALL",
    min_open_interest: int = 250,
    limit: int = 15,
) -> dict:
    return await _get(
        "options/screen",
        {
            "code": code,
            "dte_min": dte_min,
            "dte_max": dte_max,
            "delta_min": delta_min,
            "delta_max": delta_max,
            "option_type": option_type,
            "min_open_interest": min_open_interest,
            "limit": limit,
        },
    )


@mcp.tool(
    description=(
        "Resolve a company name or partial ticker to moomoo codes. Every other "
        "research tool needs a market-prefixed code (US.NVDA, HK.00700) — start "
        "here when the user names a company rather than a code. `is_watched` "
        "shows whether it is already on their moomoo watchlist."
    )
)
async def search(keyword: str, limit: int = 10) -> list | dict:
    return await _get("research/search", {"keyword": keyword, "limit": limit})


@mcp.tool(
    description=(
        "The user's own moomoo watchlist — names they are tracking but may not "
        "own. Use it when they say 'my watchlist', or to pick the universe for "
        "a scan without asking them to list tickers. `groups` names the other "
        "watchlist groups that can be requested."
    )
)
async def watchlist(group: str = "All") -> dict:
    return await _get("research/watchlist", {"group": group})


@mcp.tool(
    description=(
        "Live snapshot for any codes, held or not: price, day range, volume, "
        "turnover, market cap, P/E, 52-week range. Batch several codes in one "
        "call rather than looping."
    )
)
async def quote(codes: str) -> list | dict:
    return await _get("research/quote", {"codes": codes})


@mcp.tool(
    description=(
        "Daily/intraday OHLCV bars for any code. Use for trend, drawdown or "
        "range questions. ktype is one of K_1M, K_5M, K_15M, K_30M, K_60M, "
        "K_DAY, K_WEEK, K_MON."
    )
)
async def candles(code: str, days: int = 365, ktype: str = "K_DAY") -> dict:
    return await _get("candles", {"code": code, "days": days, "ktype": ktype})


@mcp.tool(
    description=(
        "Full picture of one company: live quote, company profile, the sector "
        "and index plates it belongs to, and — under `third_party_views` — the "
        "analyst consensus and Morningstar star rating and fair value. Those "
        "two are ratings published by OUTSIDE firms. Report them with their "
        "attribution ('Morningstar puts fair value at X'); never restate them "
        "as your own assessment or as this tool's conclusion."
    )
)
async def profile(code: str) -> dict:
    return await _get("research/profile", {"code": code})


@mcp.tool(
    description=(
        "Implied and historical volatility for an underlying: IV, IV rank, IV "
        "percentile, 30/60-day HV, and call/put volume and open interest. "
        "Answers 'is this name's option pricing high or low right now' WITHOUT "
        "needing option-chain entitlement — so it works on US names where "
        "screen_options fails."
    )
)
async def volatility(code: str) -> dict:
    return await _get("research/volatility", {"code": code})


@mcp.tool(
    description=(
        "Money flowing in and out of a stock, split by order size (super/big/"
        "mid/small). `distribution_today` is the cumulative split so far; "
        "`flow` is the series it came from. The split only means something "
        "against its own trend — do not read a single reading as a signal."
    )
)
async def capital_flow(code: str, period: str = "INTRADAY") -> dict:
    return await _get("research/capital-flow", {"code": code, "period": period})


@mcp.tool(
    description=(
        "How the stock actually traded around each past earnings release — "
        "prices for the days before and after, plus the option IV going in. "
        "Use for 'how much does this name usually move on earnings'."
    )
)
async def earnings_reaction(code: str) -> list | dict:
    return await _get("research/earnings-reaction", {"code": code})


@mcp.tool(
    description=(
        "Upcoming events. kind='earnings' (with consensus EPS/revenue and "
        "implied vol), 'dividends' (ex/record/payable dates, today only), or "
        "'economic' (macro releases with previous/consensus/actual)."
    )
)
async def calendar(kind: str, market: str = "US", days: int = 7) -> dict:
    return await _get("research/calendar", {"kind": kind, "market": market, "days": days})


@mcp.tool(
    description=(
        "The field vocabulary for stock_screener, grouped by filter kind, with "
        "the extra argument each kind takes. Call this BEFORE stock_screener "
        "unless you already know the exact field names — there are ~90 and they "
        "are not guessable."
    )
)
async def screener_fields() -> dict:
    return await _get("research/screen/fields")


@mcp.tool(
    description=(
        "Screen a whole market for stocks the user does not own, on "
        "fundamentals (P/E, ROE, margins, growth), price action (change, "
        "turnover, distance from 52-week high) or chart patterns (MACD/RSI/KDJ "
        "crosses, MA alignment, Bollinger breaks). `filters` is a list of "
        "{field, min, max} — get valid field names from screener_fields first. "
        "Percent fields take whole numbers (20 means 20%). A filter with no min "
        "or max returns that column without constraining it.\n\n"
        "Like screen_options, this DESCRIBES: it returns the values each name "
        "matched on, in the order requested. It does not score, rank by quality "
        "or suggest what to buy — report the numbers and let the user judge."
    )
)
async def stock_screener(
    market: str = "US",
    filters: list | None = None,
    plate: str | None = None,
    begin: int = 0,
    num: int = 50,
) -> dict:
    return await _post(
        "research/screen",
        {"market": market, "filters": filters, "plate": plate, "begin": begin, "num": num},
    )


@mcp.tool(
    description=(
        "Technical indicators for one symbol, computed locally from moomoo's "
        "own adjusted bars: moving averages and distance from each, RSI, MACD "
        "(with recent cross), stochastic, ADX/DI, ATR, Bollinger bands with "
        "%B, realized volatility, OBV and volume vs average, 52-week range, "
        "and trailing returns.\n\n"
        "Costs no broker call when the bar cache is warm, so it is cheap to "
        "run across many names. Pair with `volatility` to compare realized "
        "against implied.\n\n"
        "These are measurements, not signals. Report the values and the "
        "mechanical state (`rsi_zone` is `above_70`, not 'overbought'); do not "
        "translate them into a view on the stock."
    )
)
async def technicals(code: str, days: int = 400, ktype: str = "K_DAY") -> dict:
    return await _get("technicals", {"code": code, "days": days, "ktype": ktype})


if __name__ == "__main__":
    mcp.run(transport="stdio")
