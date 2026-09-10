"""Estimated transaction costs for the paper engine.

Why estimated rather than reported
----------------------------------
A paper account answers ``order_fee_query`` with "Paper trading is not
supported", so a simulated fill costs nothing and the broker will never tell us
what it would have cost. The real account *will*, so this model is fitted to
that account's own fee history and carried with an ``estimated`` label
everywhere it surfaces - the same discipline `pnl.closed_realized` follows.
Never present these as broker-reported figures.

Why it matters here
-------------------
Fees on this account are almost entirely flat, so they are brutally regressive
on small positions: a $0.99 platform fee is 0.30% of a $330 trade and 0.02% of a
$5,000 one. A round trip on a small position can cost more than the move the
rule was trying to capture, and because the paper account charges nothing, the
simulation would report that as a win. `engine._order_guardrails` uses
`round_trip_pct` to refuse trades too small to be worth their own costs.

The shape, read off 40 real fills (2026-06 to 2026-09)
-----------------------------------------------------
    Commission        0.00        always - this account pays no commission
    Platform Fees     0.99        flat, every order, both sides
    Stamp Duty        ~0.115%     stepped in $0.25, with a $0.25 floor
    Settlement Fee    0.00-0.06   negligible, scales loosely with notional
    SEC Fee           ~0.0023%    sells only
    Trading Activity  0.01        sells only, effectively flat

The stamp duty steps land a cent or two under a clean $0.25 boundary, which is
consistent with it being charged in MYR and converted. The model rounds to the
boundary and so runs slightly conservative - which is the right direction for a
guard that decides whether a trade clears its costs.
"""

from __future__ import annotations

import math

# Fitted to the real account. `tests/test_fees.py` checks these against a frozen
# sample of its actual fills, so a change in moomoo's schedule fails a test
# rather than silently skewing every sizing decision.
PLATFORM_FEE = 0.99
STAMP_RATE = 0.00115
STAMP_STEP = 0.25
STAMP_MIN = 0.25
SETTLEMENT_RATE = 0.00006
SEC_RATE = 0.000023
TAF_FLAT = 0.01

SOURCE = "estimated from this account's own fee history; paper fills are free"


def estimate(notional: float, side: str) -> float:
    """Estimated cost of one order, in the account's settlement currency."""
    if notional <= 0:
        return 0.0
    stamp = max(
        STAMP_MIN,
        math.ceil(notional * STAMP_RATE / STAMP_STEP) * STAMP_STEP,
    )
    total = PLATFORM_FEE + stamp + notional * SETTLEMENT_RATE
    if side == "SELL":
        total += notional * SEC_RATE + TAF_FLAT
    return round(total, 2)


def round_trip(notional: float) -> float:
    """Buying and later selling the same notional. The number that decides a trade."""
    return round(estimate(notional, "BUY") + estimate(notional, "SELL"), 2)


def round_trip_pct(notional: float) -> float:
    """Round-trip cost as a percentage of the position.

    This is the move a trade has to make before it has earned anything, and it
    is the figure the entry guardrail is written against.
    """
    if notional <= 0:
        return float("inf")
    return round_trip(notional) / notional * 100


def breakeven_price(entry_price: float, qty: float) -> float | None:
    """Price the position must reach for the round trip to net zero."""
    if not entry_price or qty <= 0:
        return None
    notional = entry_price * qty
    return round((notional + round_trip(notional)) / qty, 4)


def describe(notional: float) -> dict:
    """Factual cost breakdown for the journal and the Telegram receipt."""
    return {
        "buy": estimate(notional, "BUY"),
        "sell": estimate(notional, "SELL"),
        "round_trip": round_trip(notional),
        "round_trip_pct": round(round_trip_pct(notional), 3),
        "estimated": True,
        "source": SOURCE,
    }
