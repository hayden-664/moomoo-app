"""Net P&L aggregation.

The broker does not expose a single "net P&L" number for securities accounts
(``accinfo_query`` only carries realized/unrealized for *futures* accounts), so
it is assembled here from three sources of differing authority:

1. ``open_unrealized``  - broker-reported, mark-to-market on open positions.
2. ``open_realized``    - broker-reported, banked from partial closes of
                          positions that are still open.
3. ``closed_realized``  - DERIVED. Positions closed outright vanish from
                          ``position_list_query``, so their P&L is
                          reconstructed by FIFO-matching executed deals.

(3) is an approximation: it does not account for commissions, dividends,
stock splits or other corporate actions, and is only as complete as the date
window of deal history it was given. The API surfaces it under a separate key
with ``approximate: true`` so the UI never presents it as broker truth.
"""

from __future__ import annotations

import re
from collections import defaultdict, deque
from typing import Iterable

# US-style option codes, e.g. "US.AAPL250117C150000". Options are quoted per
# share but traded per contract, so realized P&L needs the 100x multiplier.
_OPTION_CODE = re.compile(r"^[A-Z]{2}\.[A-Z.]+\d{6}[CP]\d+$")


def contract_multiplier(code: str) -> int:
    return 100 if _OPTION_CODE.match(code or "") else 1


def _f(row: dict, *names: str) -> float:
    """First present, numeric, non-null field among *names*."""
    for n in names:
        v = row.get(n)
        if v is None:
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return 0.0


def summarise_positions(positions: Iterable[dict]) -> dict:
    unrealized = realized = today = market_value = cost_basis = 0.0
    for p in positions:
        unrealized += _f(p, "unrealized_pl", "pl_val")
        realized += _f(p, "realized_pl")
        today += _f(p, "today_pl_val")
        market_value += _f(p, "market_val")
        qty = _f(p, "qty", "can_sell_qty")
        cost_basis += _f(p, "cost_price", "average_cost", "diluted_cost") * qty
    return {
        "open_unrealized": round(unrealized, 2),
        "open_realized": round(realized, 2),
        "today": round(today, 2),
        "market_value": round(market_value, 2),
        "cost_basis": round(cost_basis, 2),
    }


def realized_from_deals(deals: Iterable[dict]) -> dict:
    """FIFO-match buys against sells to recover P&L on fully-closed trades.

    Long and short inventory are tracked separately per symbol so that a
    short-then-cover sequence is matched correctly rather than being read as
    an unmatched sell.
    """
    longs: dict[str, deque[list[float]]] = defaultdict(deque)
    shorts: dict[str, deque[list[float]]] = defaultdict(deque)
    per_symbol: dict[str, float] = defaultdict(float)

    def side_of(deal: dict) -> str:
        return str(deal.get("trd_side", "")).upper()

    ordered = sorted(deals, key=lambda d: str(d.get("create_time") or ""))

    for deal in ordered:
        code = deal.get("code") or ""
        qty = _f(deal, "qty")
        price = _f(deal, "price")
        if qty <= 0:
            continue
        mult = contract_multiplier(code)
        side = side_of(deal)

        if side in ("BUY", "BUY_BACK"):
            # A buy first covers any open short, then opens/extends a long.
            remaining = qty
            while remaining > 0 and shorts[code]:
                lot = shorts[code][0]
                take = min(lot[0], remaining)
                per_symbol[code] += (lot[1] - price) * take * mult
                lot[0] -= take
                remaining -= take
                if lot[0] <= 0:
                    shorts[code].popleft()
            if remaining > 0:
                longs[code].append([remaining, price])

        elif side in ("SELL", "SELL_SHORT"):
            remaining = qty
            while remaining > 0 and longs[code]:
                lot = longs[code][0]
                take = min(lot[0], remaining)
                per_symbol[code] += (price - lot[1]) * take * mult
                lot[0] -= take
                remaining -= take
                if lot[0] <= 0:
                    longs[code].popleft()
            if remaining > 0:
                shorts[code].append([remaining, price])

    # Only symbols with no inventory left are genuinely closed round-trips;
    # anything still holding a lot is already counted in open position P&L.
    closed = {
        code: round(v, 2)
        for code, v in per_symbol.items()
        if not longs[code] and not shorts[code]
    }
    return {
        "closed_realized": round(sum(closed.values()), 2),
        "by_symbol": dict(sorted(closed.items(), key=lambda kv: kv[1], reverse=True)),
        "approximate": True,
        "excludes": ["commissions", "dividends", "corporate actions"],
    }


def net_pnl(positions: Iterable[dict], deals: Iterable[dict]) -> dict:
    open_side = summarise_positions(positions)
    closed_side = realized_from_deals(deals)
    net = (
        open_side["open_unrealized"]
        + open_side["open_realized"]
        + closed_side["closed_realized"]
    )
    return {
        "net": round(net, 2),
        "open": open_side,
        "closed": closed_side,
    }
