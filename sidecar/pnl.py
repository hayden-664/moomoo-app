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
4. ``partial_realized`` - DERIVED. Money banked by selling *part* of a holding
                          you still own. This is the same quantity as (2), from
                          a different source, and only one of the two may be
                          added to the net or it double-counts.

(3) and (4) are approximations: they do not account for commissions, dividends,
stock splits or other corporate actions, and are only as complete as the date
window of deal history they were given. The API surfaces them under separate
keys with ``approximate: true`` so the UI never presents them as broker truth.

On this securities account the broker reports ``realized_pl`` as 0 on every
position row, so (2) is always zero and (4) is the only source for partial-sell
gains. An earlier version dropped (4) entirely, on the assumption that open
position P&L already covered it; it does not, and those gains went missing from
the net.
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


_EXCLUDES = ["commissions", "dividends", "corporate actions"]


def realized_from_deals(deals: Iterable[dict]) -> dict:
    """FIFO-match buys against sells to recover realized P&L from deal history.

    Returns two blocks: ``closed`` for symbols now flat, and ``partial`` for
    money banked out of positions still held.

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

    # A symbol with no inventory left is a finished round-trip. One that still
    # holds a lot has banked something from a partial sell, which is reported
    # separately: it is real money, but it belongs to a position that is still
    # open, and the two have different provenance downstream.
    closed: dict[str, float] = {}
    partial: dict[str, float] = {}
    for code, v in per_symbol.items():
        if abs(v) < 0.005:
            continue
        bucket = partial if (longs[code] or shorts[code]) else closed
        bucket[code] = round(v, 2)

    def block(values: dict[str, float], key: str) -> dict:
        return {
            key: round(sum(values.values()), 2),
            "by_symbol": dict(sorted(values.items(), key=lambda kv: kv[1], reverse=True)),
            "approximate": True,
            "excludes": _EXCLUDES,
        }

    return {
        "closed": block(closed, "closed_realized"),
        "partial": block(partial, "partial_realized"),
    }


def net_pnl(positions: Iterable[dict], deals: Iterable[dict]) -> dict:
    open_side = summarise_positions(positions)
    derived = realized_from_deals(deals)
    closed_side = derived["closed"]
    partial_side = derived["partial"]

    # open_realized and partial_realized measure the same thing from different
    # sources, so adding both would double-count. Prefer the broker when it
    # reports anything; fall back to the reconstruction when it reports zero,
    # which is what this account does.
    use_derived = open_side["open_realized"] == 0
    partial_side["counted_in_net"] = use_derived

    banked = (
        partial_side["partial_realized"] if use_derived else open_side["open_realized"]
    )
    # Everything actually cashed out: finished round-trips plus what was taken
    # off the table from positions still held. Computed here rather than in each
    # consumer so the dashboard, Telegram and MCP cannot disagree about it.
    total_realized = closed_side["closed_realized"] + banked

    net = open_side["open_unrealized"] + total_realized
    return {
        "net": round(net, 2),
        "total_realized": round(total_realized, 2),
        "open": open_side,
        "closed": closed_side,
        "partial": partial_side,
    }
