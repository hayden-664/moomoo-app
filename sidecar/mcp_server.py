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
        "Read-only access to Hayden's moomoo brokerage account: balances, "
        "positions, net P&L and option-chain screening.\n\n"
        "This server reports facts. It cannot place, modify or cancel orders, "
        "and it does not rank trades or judge whether any position or contract "
        "is a good idea. Report the numbers and let the user draw conclusions; "
        "do not offer buy/sell/hold recommendations from this data."
    ),
)


async def _get(path: str, params: dict | None = None) -> dict | list:
    async with httpx.AsyncClient(timeout=TIMEOUT) as http:
        r = await http.get(f"{SIDECAR}/{path}", params=params or {})
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


@mcp.tool(description="Account balances: total assets, cash, market value, buying power, margin.")
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
        "fees, dividends and corporate actions — always describe them as approximate."
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


if __name__ == "__main__":
    mcp.run(transport="stdio")
