"""Dividend income estimated from moomoo's dividend history and your deals.

Net P&L excludes dividends (see ``pnl.py``). This module fills that gap without
blurring provenance, so its result is reported *beside* net P&L and never added
into it.

The broker does not list dividends credited to a securities account, so the
figure is DERIVED:

- ``get_corporate_actions_dividends`` gives each symbol's per-share history as
  free text ("Cash Dividend: 1.65 USD Per Share") with ex and payable dates.
- Shares held going into each ex-date are reconstructed from deal history: a
  buy before the ex-date qualifies, a buy on it does not.

What that leaves out, carried on the result as ``excludes``:

- **Withholding tax.** Amounts are gross. The US withholds tax on dividends
  paid to non-resident accounts (30% where no treaty rate applies), so what
  actually lands in the account is typically less.
- Stock dividends and anything whose statement is not a plain cash dividend --
  those are listed under ``unparsed`` rather than guessed at.
- Shares that are not explained by the deal history (bought before the deal
  window, or transferred in). Symbols where the reconstructed holding falls
  short of the broker's position are named in ``unreconciled``.

Part of it is already in net P&L. On a position you still hold, moomoo folds
each dividend into that row's ``realized_pl`` on the payable date (checked
against this account: NEE and AVGO, never sold, carry exactly their dividends
there), and ``pnl.py`` puts that broker figure into net. That figure resets
when a position goes flat, so dividends from a holding since closed, or from
before it was last re-opened, are in net nowhere -- the FIFO reconstruction
ignores them. Each event therefore carries ``in_net``, and the totals split
into ``paid_in_net`` and ``paid_not_in_net``. Only the latter is missing from
net P&L; adding ``paid`` to net would double-count.

Currencies follow the rule in ``pnl.py``: each dividend is bucketed by the
currency it is declared in, each bucket converted into the reporting currency,
then summed. A currency with no rate is named in ``conversion.unconverted`` and
left out of the totals. Like realized P&L, conversion uses today's rate rather
than the rate on the payable date.

Rate limit: ``get_corporate_actions_dividends`` allows 30 calls per 30 seconds,
and this account has held more symbols than that. Histories are cached for 12
hours, at most ``_BUDGET`` uncached symbols are fetched per request, and the
rest are reported under ``unavailable`` until a later request fills them.
Failures (including Bursa codes, which need the MY quote entitlement) are not
retried for ``_RETRY`` seconds.
"""

from __future__ import annotations

import datetime as dt
import re
import threading
import time
from collections import defaultdict
from typing import Iterable

from currency import currency_of
from moomoo_client import MoomooError, client

_TTL = 12 * 3600.0
_RETRY = 600.0
# Under the 30-per-30s limit, leaving headroom for anything else in flight.
_BUDGET = 25

# Two shapes seen on this account: "Cash Dividend: 1.65 USD Per Share" (most
# stocks) and "Cash Dividend: 1.8226USD" (VOO). "Per Share" is optional.
_CASH = re.compile(r"cash dividend:\s*([\d.]+)\s*([A-Z]{3})\b", re.I)
# Dates come month-first ("10/06/2026") or spelled out ("Sep 28, 2026").
_DATE_FORMATS = ("%m/%d/%Y", "%b %d, %Y")

_EXCLUDES = [
    "withholding tax (amounts are gross)",
    "stock dividends and non-cash distributions",
    "shares not explained by deal history",
]

_lock = threading.Lock()
# code -> (fetched_at, dividend_list or None, error or None)
_histories: dict[str, tuple[float, list[dict] | None, str | None]] = {}


def parse_statement(text: str) -> tuple[float, str] | None:
    """(amount per share, currency) for a plain cash dividend, else None."""
    m = _CASH.search(text or "")
    if not m:
        return None
    try:
        return float(m.group(1)), m.group(2).upper()
    except ValueError:
        return None


def _iso(raw: str | None) -> str | None:
    """'10/06/2026' or 'Oct 6, 2026' -> '2026-10-06'; None if neither."""
    for fmt in _DATE_FORMATS:
        try:
            return dt.datetime.strptime((raw or "").strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _fetch_histories(codes: Iterable[str]) -> tuple[dict[str, list[dict]], dict[str, str]]:
    """Dividend history per code, within the cache and rate-limit budget."""
    found: dict[str, list[dict]] = {}
    missing: dict[str, str] = {}
    budget = _BUDGET
    with _lock:
        for code in sorted(set(codes)):
            now = time.monotonic()
            hit = _histories.get(code)
            if hit and hit[1] is not None and now - hit[0] < _TTL:
                found[code] = hit[1]
                continue
            if hit and hit[1] is None and now - hit[0] < _RETRY:
                missing[code] = hit[2] or "unavailable"
                continue
            if budget <= 0:
                # A stale history still beats none while waiting for budget.
                if hit and hit[1] is not None:
                    found[code] = hit[1]
                else:
                    missing[code] = "not fetched yet (rate limit budget); retry shortly"
                continue
            budget -= 1
            try:
                payload = client.research(
                    f"dividends:{code}",
                    lambda q, c=code: q.get_corporate_actions_dividends(c),
                    ttl=_TTL,
                )
                rows = list((payload or {}).get("dividend_list") or [])
                _histories[code] = (now, rows, None)
                found[code] = rows
            except MoomooError as exc:
                if hit and hit[1] is not None:
                    found[code] = hit[1]
                else:
                    _histories[code] = (now, None, str(exc))
                    missing[code] = str(exc)
    return found, missing


def _ledger(deals: Iterable[dict]) -> dict[str, list[tuple[str, float]]]:
    """Per code, (trade date, signed qty) in time order."""
    out: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for d in sorted(deals, key=lambda d: str(d.get("create_time") or "")):
        side = str(d.get("trd_side", "")).upper()
        try:
            qty = float(d.get("qty") or 0)
        except (TypeError, ValueError):
            continue
        if qty <= 0:
            continue
        sign = 1 if side in ("BUY", "BUY_BACK") else -1 if side in ("SELL", "SELL_SHORT") else 0
        if sign:
            out[d.get("code") or ""].append((str(d.get("create_time") or "")[:10], sign * qty))
    return out


def _held_before(entries: list[tuple[str, float]], day: str) -> float:
    """Shares held at the close of the session before *day*."""
    return sum(q for d, q in entries if d < day)


def _holding_since(entries: list[tuple[str, float]]) -> str:
    """Date the current holding was opened: the last time it went from flat to long."""
    running, since = 0.0, ""
    for d, q in entries:
        if running <= 1e-9 < running + q:
            since = d
        running += q
    return since


def estimate(
    positions: list[dict],
    deals: list[dict],
    since: str,
    base: str = "USD",
    rates: dict[str, float] | None = None,
    rate_source: str = "the broker's own conversion rate",
    histories: dict[str, list[dict]] | None = None,
    unavailable: dict[str, str] | None = None,
) -> dict:
    """Dividends received (estimated) since *since*, plus what is pending.

    ``histories`` / ``unavailable`` can be passed in for tests; otherwise they
    are fetched for every symbol held at some point in the window.
    """
    today = dt.date.today().isoformat()
    ledger = _ledger(deals)
    held_qty = {
        p.get("code") or "": float(p.get("qty") or 0) for p in positions if float(p.get("qty") or 0)
    }
    # Broker realized_pl on held rows. Non-zero means the row is carrying
    # banked money -- dividends included -- into net via pnl.py.
    broker_realized: dict[str, float] = {}
    for p in positions:
        try:
            broker_realized[p.get("code") or ""] = float(p.get("realized_pl") or 0)
        except (TypeError, ValueError):
            pass

    # Symbols worth asking about: held at the start of the window, bought in
    # it, or held now. Everything else cannot have earned a dividend here.
    relevant = set(held_qty)
    for code, entries in ledger.items():
        if _held_before(entries, since) > 0 or any(d >= since and q > 0 for d, q in entries):
            relevant.add(code)

    # Positions bigger than the deals can account for: dividends on the gap
    # are not counted, so name the symbol rather than understate silently.
    unreconciled = sorted(
        code
        for code, qty in held_qty.items()
        if _held_before(ledger.get(code, []), "9999-12-31") + 1e-9 < qty
    )

    if histories is None:
        histories, unavailable = _fetch_histories(relevant)
    unavailable = dict(unavailable or {})

    events: list[dict] = []
    upcoming: list[dict] = []
    unparsed: list[dict] = []
    for code in sorted(relevant):
        rows = histories.get(code)
        if rows is None:
            continue
        entries = ledger.get(code, [])
        for row in rows:
            ex = _iso(row.get("ex_date"))
            if not ex:
                # A date format not seen before must surface, not vanish.
                if row.get("ex_date") or row.get("statement"):
                    unparsed.append({"code": code, "ex_date": row.get("ex_date"),
                                     "statement": row.get("statement")})
                continue
            if ex < since:
                continue
            parsed = parse_statement(row.get("statement", ""))
            payable = _iso(row.get("dividend_payable_date"))
            if ex > today:
                shares = held_qty.get(code, 0.0)
                if shares and parsed:
                    upcoming.append({
                        "code": code,
                        "ex_date": ex,
                        "payable_date": payable,
                        "per_share": parsed[0],
                        "currency": parsed[1],
                        "shares_now": shares,
                        "amount_if_held": round(parsed[0] * shares, 2),
                    })
                continue
            shares = _held_before(entries, ex)
            if shares <= 0:
                continue
            if parsed is None:
                unparsed.append({"code": code, "ex_date": ex, "statement": row.get("statement")})
                continue
            per_share, ccy = parsed
            status = "paid" if payable and payable <= today else "pending"
            events.append({
                "code": code,
                "ex_date": ex,
                "payable_date": payable,
                "per_share": per_share,
                "currency": ccy,
                "shares": shares,
                "amount": round(per_share * shares, 2),
                # Ex-date passed but not paid yet: earned, not yet credited.
                "status": status,
                # Already inside net P&L through the broker's realized_pl on
                # the current holding (see module docstring).
                "in_net": bool(
                    status == "paid"
                    and held_qty.get(code)
                    and broker_realized.get(code)
                    and ex >= _holding_since(entries)
                ),
            })

    by_currency: dict[str, dict] = {}
    by_symbol: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for e in events:
        b = by_currency.setdefault(
            e["currency"],
            {"currency": e["currency"], "paid": 0.0, "paid_in_net": 0.0, "pending": 0.0},
        )
        b[e["status"]] += e["amount"]
        if e["in_net"]:
            b["paid_in_net"] += e["amount"]
        by_symbol[e["currency"]][e["code"]] += e["amount"]
    for b in by_currency.values():
        for key in ("paid", "paid_in_net", "pending"):
            b[key] = round(b[key], 2)
        b["paid_not_in_net"] = round(b["paid"] - b["paid_in_net"], 2)
        b["by_symbol"] = {
            k: round(v, 2)
            for k, v in sorted(by_symbol[b["currency"]].items(), key=lambda kv: kv[1], reverse=True)
        }

    usable = {base: 1.0, **{c: r for c, r in (rates or {}).items() if r}}
    paid = paid_in_net = pending = 0.0
    converted_from: list[str] = []
    unconverted: list[str] = []
    for ccy, b in by_currency.items():
        rate = usable.get(ccy)
        if rate is None:
            unconverted.append(ccy)
            continue
        if ccy != base:
            converted_from.append(ccy)
        paid += b["paid"] * rate
        paid_in_net += b["paid_in_net"] * rate
        pending += b["pending"] * rate

    events.sort(key=lambda e: e["ex_date"], reverse=True)
    upcoming.sort(key=lambda e: e["ex_date"])
    return {
        "base": base,
        "window": {"start": since, "end": today},
        "paid": round(paid, 2),
        # paid splits into these two. Only paid_not_in_net is missing from net
        # P&L; paid_in_net is already there via the broker's realized_pl.
        "paid_in_net": round(paid_in_net, 2),
        "paid_not_in_net": round(paid - paid_in_net, 2),
        "pending": round(pending, 2),
        "approximate": True,
        "derived": True,
        "basis": (
            "per-share dividends from moomoo's dividend history x shares held "
            "going into each ex-date, reconstructed from deal history"
        ),
        "excludes": _EXCLUDES,
        "by_currency": by_currency,
        "events": events,
        "upcoming": upcoming,
        "unparsed": unparsed,
        "unreconciled": unreconciled,
        "unavailable": unavailable,
        "conversion": {
            "base": base,
            "rates": {c: usable[c] for c in by_currency if c in usable},
            "converted_from": sorted(converted_from),
            "unconverted": sorted(unconverted),
            "source": rate_source,
            "note": "converted at today's rate, not the rate on the payable date",
        },
    }
