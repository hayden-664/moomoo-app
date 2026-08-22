"""Net P&L aggregation, split by settlement currency.

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

This account trades both US (USD) and Bursa (MYR). Figures are bucketed by
settlement currency first, then converted into the reporting currency and
summed, so the headline numbers are a single comparable total. What is *not*
allowed is adding the buckets without converting them: a MYR round-trip used to
land in the dollar total untouched, with no warning, because the mixed-currency
flag was computed from *open* positions and the MYR position was already closed.

The rate comes from ``currency.account_breakdown`` -- recovered from the
broker's own conversion of ``total_assets``, so the dashboard agrees with what
moomoo itself reports. Two caveats travel with it:

- Realized P&L is converted at *today's* rate, not the rate on the day of the
  trade. The broker does not report historical rates, so a MYR gain banked
  months ago is stated in today's dollars.
- A currency the rate cannot be solved for is left out of the totals and named
  in ``conversion.unconverted`` rather than being folded in at 1:1.

``by_currency`` always carries the unconverted, per-currency truth alongside.
"""

from __future__ import annotations

import re
from collections import defaultdict, deque
from typing import Iterable

from currency import currency_of

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


def _empty_open(ccy: str) -> dict:
    return {
        "currency": ccy,
        "open_unrealized": 0.0,
        "open_realized": 0.0,
        "today": 0.0,
        "market_value": 0.0,
        "cost_basis": 0.0,
        "position_count": 0,
    }


def summarise_positions(positions: Iterable[dict], base: str = "USD") -> dict[str, dict]:
    """Open-position totals, keyed by the currency each position settles in."""
    out: dict[str, dict] = {}
    for p in positions:
        ccy = currency_of(p, base)
        block = out.setdefault(ccy, _empty_open(ccy))
        block["open_unrealized"] += _f(p, "unrealized_pl", "pl_val")
        block["open_realized"] += _f(p, "realized_pl")
        block["today"] += _f(p, "today_pl_val")
        block["market_value"] += _f(p, "market_val")
        qty = _f(p, "qty", "can_sell_qty")
        block["cost_basis"] += _f(p, "cost_price", "average_cost", "diluted_cost") * qty
        block["position_count"] += 1
    for block in out.values():
        for key in ("open_unrealized", "open_realized", "today", "market_value", "cost_basis"):
            block[key] = round(block[key], 2)
    return out


_EXCLUDES = ["commissions", "dividends", "corporate actions"]


def _empty_block(ccy: str, key: str) -> dict:
    return {
        "currency": ccy,
        key: 0.0,
        "by_symbol": {},
        "approximate": True,
        "excludes": _EXCLUDES,
    }


def realized_from_deals(
    deals: Iterable[dict], since: str | None = None, base: str = "USD"
) -> dict[str, dict]:
    """FIFO-match buys against sells to recover realized P&L from deal history.

    Returns ``{currency: {"closed": block, "partial": block}}`` -- ``closed``
    for symbols now flat, ``partial`` for money banked out of positions still
    held. Each symbol belongs to exactly one currency (its market's), so the
    matching itself is unaffected by the split; only the summing is.

    Long and short inventory are tracked separately per symbol so that a
    short-then-cover sequence is matched correctly rather than being read as
    an unmatched sell.

    ``since`` filters which realized events are *reported*, not which deals are
    matched. Matching always runs over every deal given, because truncating the
    input first would leave sells whose opening buy predates the window looking
    like unmatched shorts, and invent P&L that never happened. Each close is
    dated by its closing deal and only those on or after ``since`` are summed.
    """
    longs: dict[str, deque[list[float]]] = defaultdict(deque)
    shorts: dict[str, deque[list[float]]] = defaultdict(deque)
    # (code, currency, date of the closing deal, realized amount)
    events: list[tuple[str, str, str, float]] = []

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
        ccy = currency_of(deal, base)
        side = side_of(deal)
        day = str(deal.get("create_time") or "")[:10]

        if side in ("BUY", "BUY_BACK"):
            # A buy first covers any open short, then opens/extends a long.
            remaining = qty
            while remaining > 0 and shorts[code]:
                lot = shorts[code][0]
                take = min(lot[0], remaining)
                events.append((code, ccy, day, (lot[1] - price) * take * mult))
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
                events.append((code, ccy, day, (price - lot[1]) * take * mult))
                lot[0] -= take
                remaining -= take
                if lot[0] <= 0:
                    longs[code].popleft()
            if remaining > 0:
                shorts[code].append([remaining, price])

    per_symbol: dict[tuple[str, str], float] = defaultdict(float)
    for code, ccy, day, amount in events:
        if since and day < since:
            continue
        per_symbol[(ccy, code)] += amount

    # A symbol with no inventory left is a finished round-trip. One that still
    # holds a lot has banked something from a partial sell, which is reported
    # separately: it is real money, but it belongs to a position that is still
    # open, and the two have different provenance downstream.
    closed: dict[str, dict[str, float]] = defaultdict(dict)
    partial: dict[str, dict[str, float]] = defaultdict(dict)
    for (ccy, code), v in per_symbol.items():
        if abs(v) < 0.005:
            continue
        bucket = partial if (longs[code] or shorts[code]) else closed
        bucket[ccy][code] = round(v, 2)

    def block(values: dict[str, float], ccy: str, key: str) -> dict:
        return {
            "currency": ccy,
            key: round(sum(values.values()), 2),
            "by_symbol": dict(sorted(values.items(), key=lambda kv: kv[1], reverse=True)),
            "approximate": True,
            "excludes": _EXCLUDES,
        }

    out: dict[str, dict] = {}
    for ccy in set(closed) | set(partial):
        out[ccy] = {
            "closed": block(closed.get(ccy, {}), ccy, "closed_realized"),
            "partial": block(partial.get(ccy, {}), ccy, "partial_realized"),
        }
    return out


def _combine(ccy: str, open_side: dict, closed_side: dict, partial_side: dict) -> dict:
    """Assemble one currency's net from its own open/closed/partial parts."""
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

    return {
        "currency": ccy,
        "net": round(open_side["open_unrealized"] + total_realized, 2),
        "total_realized": round(total_realized, 2),
        "open": open_side,
        "closed": closed_side,
        "partial": partial_side,
        "position_count": open_side["position_count"],
    }


def _convert(
    by_currency: dict[str, dict],
    base: str,
    rates: dict[str, float] | None,
    rate_source: str,
) -> dict:
    """Fold every currency into *base* at *rates* and return one set of totals.

    ``rates`` maps a currency to its value in one unit of ``base`` (MYR -> 0.2476).
    The base currency is always 1.0. A currency with no usable rate is skipped
    and named in ``unconverted`` -- silently treating it as 1:1 is how the
    original bug happened.
    """
    usable = {base: 1.0}
    usable.update({c: r for c, r in (rates or {}).items() if r})

    total = _combine(
        base,
        _empty_open(base),
        _empty_block(base, "closed_realized"),
        _empty_block(base, "partial_realized"),
    )
    open_t, closed_t, partial_t = total["open"], total["closed"], total["partial"]
    closed_sym: dict[str, float] = {}
    partial_sym: dict[str, float] = {}
    unconverted: list[str] = []
    converted_from: list[str] = []
    net = realized = 0.0
    counted_in_net = False

    for ccy, b in by_currency.items():
        rate = usable.get(ccy)
        if rate is None:
            unconverted.append(ccy)
            continue
        if ccy != base:
            converted_from.append(ccy)
        o = b["open"]
        for key in ("open_unrealized", "open_realized", "today", "market_value", "cost_basis"):
            open_t[key] += o[key] * rate
        open_t["position_count"] += o["position_count"]
        closed_t["closed_realized"] += b["closed"]["closed_realized"] * rate
        partial_t["partial_realized"] += b["partial"]["partial_realized"] * rate
        # counted_in_net is decided per currency; the merged flag is true when
        # any currency fell back to the reconstruction, which is what the
        # provenance note in the UI is describing.
        counted_in_net = counted_in_net or b["partial"]["counted_in_net"]
        realized += b["total_realized"] * rate
        net += b["net"] * rate
        for code, v in b["closed"]["by_symbol"].items():
            closed_sym[code] = round(v * rate, 2)
        for code, v in b["partial"]["by_symbol"].items():
            partial_sym[code] = round(v * rate, 2)

    for key in ("open_unrealized", "open_realized", "today", "market_value", "cost_basis"):
        open_t[key] = round(open_t[key], 2)
    closed_t["closed_realized"] = round(closed_t["closed_realized"], 2)
    partial_t["partial_realized"] = round(partial_t["partial_realized"], 2)
    partial_t["counted_in_net"] = counted_in_net

    def sort_desc(d: dict[str, float]) -> dict[str, float]:
        return dict(sorted(d.items(), key=lambda kv: kv[1], reverse=True))

    closed_t["by_symbol"] = sort_desc(closed_sym)
    partial_t["by_symbol"] = sort_desc(partial_sym)
    total["net"] = round(net, 2)
    total["total_realized"] = round(realized, 2)
    total["position_count"] = open_t["position_count"]

    total["conversion"] = {
        "base": base,
        "rates": {c: usable[c] for c in by_currency if c in usable},
        "converted_from": sorted(converted_from),
        # Named, never silently dropped: a figure missing from the totals has
        # to be visible somewhere.
        "unconverted": sorted(unconverted),
        "source": rate_source,
        "note": "realized figures use today's rate, not the rate on the trade date",
    }
    return total


def net_pnl(
    positions: Iterable[dict],
    deals: Iterable[dict],
    since: str | None = None,
    base: str = "USD",
    rates: dict[str, float] | None = None,
    rate_source: str = "the broker's own conversion rate",
) -> dict:
    """Net P&L totalled in ``base``, with the per-currency parts kept alongside.

    The top-level ``net``/``total_realized``/``open``/``closed``/``partial``
    keys are every currency converted into ``base`` and summed -- one
    comparable number. ``by_currency`` carries the same figures unconverted,
    and ``conversion`` says which rate was used and what it could not reach.
    """
    opens = summarise_positions(positions, base=base)
    derived = realized_from_deals(deals, since=since, base=base)

    by_currency: dict[str, dict] = {}
    for ccy in sorted(set(opens) | set(derived) | {base}):
        parts = derived.get(ccy, {})
        by_currency[ccy] = _combine(
            ccy,
            opens.get(ccy) or _empty_open(ccy),
            parts.get("closed") or _empty_block(ccy, "closed_realized"),
            parts.get("partial") or _empty_block(ccy, "partial_realized"),
        )

    # A currency with nothing in it at all is noise; the base currency stays
    # regardless so the promoted top-level keys always exist.
    def is_empty(b: dict) -> bool:
        return b["net"] == 0 and b["total_realized"] == 0 and b["position_count"] == 0

    by_currency = {
        c: b for c, b in by_currency.items() if c == base or not is_empty(b)
    }

    # Base first, then the rest alphabetically: every consumer leads with the
    # reporting currency, so the order is fixed here rather than in each of them.
    currencies = [base] + sorted(c for c in by_currency if c != base)
    by_currency = {c: by_currency[c] for c in currencies}

    totals = _convert(by_currency, base, rates, rate_source)
    return {
        "base": base,
        "currencies": currencies,
        # True when real money sits in more than one currency. Unlike the old
        # flag this counts realized history too, so a closed-out foreign
        # round-trip can no longer hide inside a base-currency total.
        "mixed_currency": len(currencies) > 1,
        "by_currency": by_currency,
        **{k: v for k, v in totals.items() if k != "currency"},
    }
