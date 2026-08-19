"""Option chain screening.

Two-step by necessity: ``get_option_chain`` returns only *static* contract
terms (code, strike, expiry, call/put). Greeks, IV and open interest can be
filtered on server-side via ``OptionDataFilter`` but are NOT returned by it, so
the surviving codes must be re-requested through ``get_market_snapshot`` to get
live pricing and greeks. Those two payloads are merged here.

On the "thesis" line
--------------------
Each candidate carries a one-line ``characterisation``: a factual, mechanical
description of what the contract *is* and what it *requires to break even*,
derived purely from live numbers. It is deliberately not a recommendation and
contains no directional opinion - this tool screens and describes, it does not
advise. Judging whether a trade is worth taking is the user's call.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import moomoo as mm

from moomoo_client import client


def _f(row: dict, *names: str) -> float | None:
    for n in names:
        v = row.get(n)
        if v in (None, ""):
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return None


def _dte(strike_time: str | None) -> int | None:
    if not strike_time:
        return None
    try:
        expiry = dt.date.fromisoformat(str(strike_time)[:10])
    except ValueError:
        return None
    return (expiry - dt.date.today()).days


def _mid(snap: dict) -> float | None:
    bid, ask = _f(snap, "bid_price"), _f(snap, "ask_price")
    if bid and ask and bid > 0 and ask > 0:
        return (bid + ask) / 2
    return _f(snap, "last_price")


def characterise(c: dict) -> str:
    """One-line factual description of the contract and its breakeven maths."""
    bits: list[str] = []
    kind = "call" if c["option_type"] == "CALL" else "put"
    strike = c["strike_price"]
    bits.append(f"${strike:g} {kind}")

    if c.get("dte") is not None:
        bits.append(f"{c['dte']}d to expiry")
    if c.get("delta") is not None:
        bits.append(f"{abs(c['delta']):.2f}Δ")

    head = ", ".join(bits)

    tail: list[str] = []
    if c.get("cost") is not None:
        tail.append(f"costs ${c['cost']:,.0f}/contract")
    if c.get("breakeven") is not None and c.get("required_move_pct") is not None:
        tail.append(
            f"breaks even at ${c['breakeven']:.2f} "
            f"({c['required_move_pct']:+.1f}% from spot)"
        )
    if c.get("iv") is not None:
        tail.append(f"IV {c['iv']:.0f}%")
    if c.get("open_interest") is not None:
        tail.append(f"OI {c['open_interest']:,.0f}")
    if c.get("spread_pct") is not None:
        tail.append(f"{c['spread_pct']:.0f}% bid/ask spread")

    return f"{head} — " + "; ".join(tail) if tail else head


def screen(
    code: str,
    dte_min: int = 7,
    dte_max: int = 60,
    delta_min: float = 0.15,
    delta_max: float = 0.45,
    option_type: str = "ALL",
    min_open_interest: int = 100,
    limit: int = 25,
) -> dict[str, Any]:
    """Screen one underlying's chain and return described candidates."""
    today = dt.date.today()
    start = (today + dt.timedelta(days=dte_min)).isoformat()
    end = (today + dt.timedelta(days=dte_max)).isoformat()

    # Spot price of the underlying, for breakeven / required-move maths.
    underlying = client.snapshot([code])
    spot = _f(underlying[0], "last_price") if underlying else None

    # Server-side filter on greeks/liquidity, so we pull back far fewer codes.
    # delta_min/max take raw delta (0.15), NOT percent. Passing 15 silently
    # matches nothing rather than erroring, so keep these unscaled.
    data_filter = mm.OptionDataFilter()
    data_filter.delta_min = delta_min
    data_filter.delta_max = delta_max
    data_filter.open_interest_min = min_open_interest

    # The chain interface rejects any expiry span wider than 30 days, so the
    # requested window is walked in <=30-day slices and the results combined.
    chain: list[dict] = []
    slice_start = dt.date.fromisoformat(start)
    window_end = dt.date.fromisoformat(end)
    while slice_start <= window_end:
        slice_end = min(slice_start + dt.timedelta(days=29), window_end)
        chain.extend(
            client.option_chain(
                code=code,
                start=slice_start.isoformat(),
                end=slice_end.isoformat(),
                option_type=getattr(mm.OptionType, option_type, mm.OptionType.ALL),
                data_filter=data_filter,
            )
        )
        slice_start = slice_end + dt.timedelta(days=1)
    if not chain:
        return {"underlying": code, "spot": spot, "candidates": [], "count": 0}

    # Step two: the chain has no live data, so re-request the survivors.
    by_code = {r["code"]: r for r in chain if r.get("code")}
    snaps = {s["code"]: s for s in client.snapshot(list(by_code)) if s.get("code")}

    candidates: list[dict] = []
    for opt_code, static in by_code.items():
        snap = snaps.get(opt_code, {})
        strike = _f(static, "strike_price") or _f(snap, "option_strike_price")
        if strike is None:
            continue

        opt_type = str(static.get("option_type") or snap.get("option_type") or "").upper()
        if "CALL" in opt_type:
            opt_type = "CALL"
        elif "PUT" in opt_type:
            opt_type = "PUT"
        else:
            continue

        mult = _f(snap, "option_contract_size", "option_contract_multiplier") or 100
        price = _mid(snap)
        bid, ask = _f(snap, "bid_price"), _f(snap, "ask_price")

        breakeven = None
        required_move_pct = None
        if price is not None:
            breakeven = strike + price if opt_type == "CALL" else strike - price
            if spot:
                required_move_pct = (breakeven - spot) / spot * 100

        spread_pct = None
        if bid and ask and ask > 0:
            spread_pct = (ask - bid) / ((ask + bid) / 2) * 100

        cand = {
            "code": opt_code,
            "option_type": opt_type,
            "strike_price": strike,
            "expiry": static.get("strike_time") or snap.get("strike_time"),
            "dte": _dte(static.get("strike_time") or snap.get("strike_time")),
            "delta": _f(snap, "option_delta"),
            "gamma": _f(snap, "option_gamma"),
            "theta": _f(snap, "option_theta"),
            "vega": _f(snap, "option_vega"),
            "iv": _f(snap, "option_implied_volatility"),
            "open_interest": _f(snap, "option_open_interest"),
            "volume": _f(snap, "volume"),
            "bid": bid,
            "ask": ask,
            "mid": price,
            "cost": round(price * mult, 2) if price is not None else None,
            "breakeven": round(breakeven, 2) if breakeven is not None else None,
            "required_move_pct": (
                round(required_move_pct, 2) if required_move_pct is not None else None
            ),
            "spread_pct": round(spread_pct, 2) if spread_pct is not None else None,
        }
        cand["characterisation"] = characterise(cand)
        candidates.append(cand)

    # Cheapest required move first: a pure ordering on the numbers, not a
    # ranking of trade quality.
    candidates.sort(
        key=lambda c: (
            c["required_move_pct"] is None,
            abs(c["required_move_pct"]) if c["required_move_pct"] is not None else 0,
        )
    )
    return {
        "underlying": code,
        "spot": spot,
        "count": len(candidates),
        "candidates": candidates[:limit],
        "filters": {
            "dte": [dte_min, dte_max],
            "delta": [delta_min, delta_max],
            "min_open_interest": min_open_interest,
            "option_type": option_type,
        },
    }
