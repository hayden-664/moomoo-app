"""Read-only research over securities the account does not hold.

Everything here answers "what is this company / what is trading" rather than
"what do I own", so none of it touches the trade context. The account-facing
modules stay unaware of it.

On attribution
--------------
Two of these surfaces carry *someone else's* opinion: ``analyst_consensus``
and Morningstar's star rating and fair value. They are third-party ratings,
not derived numbers, and unlike everything else in this codebase they are
inherently directional. They are returned under an explicit ``source`` so they
can be reported as "Morningstar says X" and never as this tool's own view. The
screener and the factual reads keep the same no-advice discipline as
``screener.py``.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import moomoo as mm
from moomoo.quote.quote_stockfilter_info import (
    AccumulateFilter,
    FinancialFilter,
    PatternFilter,
    SimpleFilter,
)

from moomoo_client import MoomooError, client

# Quote-only reads: a stale price is worse than a stale balance sheet, so the
# live-ish surfaces override the 15-minute research default.
_QUOTE_TTL = 15.0


def search(keyword: str, limit: int = 10) -> list[dict]:
    """Resolve a company name to moomoo codes.

    Every other call here needs a market-prefixed code (``US.NVDA``), which is
    not something a person says out loud. This is the front door.
    """
    return client.research(
        f"search:{keyword}:{limit}",
        lambda q: q.get_search_quote(keyword, limit),
    )


def watchlist(group: str = "All") -> dict:
    """The account's own moomoo watchlist, plus the groups it is filed under."""
    stocks = client.research(f"watchlist:{group}", lambda q: q.get_user_security(group), ttl=60.0)
    groups = client.research("watchlist_groups", lambda q: q.get_user_security_group(), ttl=60.0)
    return {
        "group": group,
        "groups": [g.get("group_name") for g in groups if g.get("group_name")],
        "count": len(stocks),
        "stocks": stocks,
    }


def quote(codes: list[str]) -> list[dict]:
    """Live snapshot for arbitrary codes, held or not."""
    return client.research(
        f"quote:{','.join(sorted(codes))}",
        lambda q: q.get_market_snapshot(codes),
        ttl=_QUOTE_TTL,
    )


def _optional(what: str, call, ttl: float | None = None) -> Any:
    """Run a research call, returning the error instead of raising.

    ``profile`` merges several independent sources. Coverage varies by market
    and by company -- a small-cap has no Morningstar report, HK names have no
    analyst consensus -- and one missing source must not blank the rest.
    """
    try:
        return client.research(what, call, ttl=ttl)
    except MoomooError as exc:
        return {"unavailable": str(exc)}


def profile(code: str) -> dict:
    """Everything known about one company, by source.

    ``third_party_views`` is deliberately a separate branch: those are ratings
    published by other firms, and must be reported with their attribution
    rather than folded in beside the factual fields as if this tool concluded
    them.
    """
    snap = quote([code])
    return {
        "code": code,
        "quote": snap[0] if snap else None,
        "profile": _optional(f"profile:{code}", lambda q: q.get_company_profile(code)),
        "sector_plates": _optional(f"plates:{code}", lambda q: q.get_owner_plate([code])),
        "third_party_views": {
            "note": (
                "Published by outside firms, not derived here. Attribute them "
                "when reporting; they are not this tool's assessment."
            ),
            "analyst_consensus": _optional(
                f"consensus:{code}", lambda q: q.get_research_analyst_consensus(code)
            ),
            "morningstar": _optional(
                f"morningstar:{code}", lambda q: q.get_research_morningstar_report(code)
            ),
        },
    }


def volatility(code: str) -> dict:
    """Implied and historical volatility for an underlying.

    ``get_option_underlying_overview`` reports IV, IV rank, IV percentile and
    30/60-day HV for the underlying without needing an option-chain
    entitlement, so it answers "is this name expensive right now" even for a
    market whose chains are not entitled.
    """
    rows = client.research(
        f"volatility:{code}",
        lambda q: q.get_option_underlying_overview([code]),
        ttl=_QUOTE_TTL,
    )
    return rows[0] if rows else {"code": code, "unavailable": "no volatility data returned"}


def capital_flow(code: str, period: str = "INTRADAY") -> dict:
    """Money flowing in and out, split by order size.

    ``get_capital_distribution`` is today's cumulative split; ``capital_flow``
    is the series. Both are returned because the ratio only means something
    against its own trend.
    """
    return {
        "code": code,
        "period": period,
        "distribution_today": _optional(
            f"capdist:{code}", lambda q: q.get_capital_distribution(code), ttl=_QUOTE_TTL
        ),
        "flow": _optional(
            f"capflow:{code}:{period}",
            lambda q: q.get_capital_flow(code, period_type=getattr(mm.PeriodType, period)),
            ttl=_QUOTE_TTL,
        ),
    }


def earnings_reaction(code: str) -> Any:
    """How the stock actually moved around each past earnings release."""
    return client.research(
        f"earnmove:{code}", lambda q: q.get_financials_earnings_price_move(code)
    )


# get_earnings_calendar rejects any begin/end span wider than 7 days, silently
# capping rather than erroring on some builds, so the window is walked in
# 7-day slices exactly like the option chain's 30-day cap in screener.py.
_EARNINGS_SPAN_DAYS = 7

# Markets each calendar accepts, from the API's own rejection messages rather
# than its docstrings (which list AU/CA for earnings; the server refuses them).
# Bursa has a dividend calendar but no earnings calendar at all.
_CALENDAR_MARKETS = {
    "earnings": {"US", "HK", "SH", "SZ", "SG", "JP"},
    "dividends": {"US", "HK", "MY", "SG", "JP"},
}


def calendar(kind: str, market: str = "US", days: int = 7) -> dict:
    """Upcoming earnings, dividends or macro events."""
    today = dt.date.today()
    end = today + dt.timedelta(days=days)

    supported = _CALENDAR_MARKETS.get(kind)
    if supported is not None and market not in supported:
        return {
            "kind": kind,
            "market": market,
            "rows": [],
            "unsupported": (
                f"moomoo has no {kind} calendar for {market}; "
                f"supported markets: {', '.join(sorted(supported))}"
            ),
        }

    if kind == "earnings":
        rows: list[dict] = []
        cursor = today
        while cursor <= end:
            slice_end = min(cursor + dt.timedelta(days=_EARNINGS_SPAN_DAYS - 1), end)
            rows.extend(
                client.research(
                    f"earncal:{market}:{cursor}:{slice_end}",
                    lambda q, a=cursor, b=slice_end: q.get_earnings_calendar(
                        market=getattr(mm.Market, market),
                        begin_date=a.isoformat(),
                        end_date=b.isoformat(),
                    ),
                )
            )
            cursor = slice_end + dt.timedelta(days=1)
        return {"kind": kind, "market": market, "from": today.isoformat(), "to": end.isoformat(), "rows": rows}

    if kind == "dividends":
        # This one takes a single date, not a range.
        return {
            "kind": kind,
            "market": market,
            "date": today.isoformat(),
            **client.research(
                f"divcal:{market}:{today}",
                lambda q: q.get_dividend_calendar(
                    market=getattr(mm.Market, market), date=today.isoformat()
                ),
            ),
        }

    if kind == "economic":
        return {
            "kind": kind,
            "from": today.isoformat(),
            "to": end.isoformat(),
            "rows": client.research(
                f"econcal:{today}:{end}",
                lambda q: q.get_economic_calendar(
                    begin_date=today.isoformat(), end_date=end.isoformat()
                ),
            ),
        }

    raise ValueError(f"unknown calendar kind {kind!r}")


def _build_filter(spec: dict):
    """Turn one ``{field, min, max, ...}`` spec into the right filter object.

    Which of the four filter classes a field belongs to is encoded in its
    numeric position in the ``StockField`` enum, not in its name, so the
    boundaries are read off the enum rather than hardcoded.

    A spec with neither ``min`` nor ``max`` sets ``is_no_filter``, which asks
    for the column to be *returned* without constraining it -- that is how a
    caller adds P/E to the output without also filtering on it.
    """
    field = str(spec.get("field", "")).upper()
    ok, num = mm.StockField.to_number(field)
    if not ok:
        raise ValueError(f"unknown screener field {field!r}")

    lo, hi = spec.get("min"), spec.get("max")

    if num < mm.StockField.acc_enum_begin:
        f = SimpleFilter()
    elif num < mm.StockField.financial_enum_begin:
        f = AccumulateFilter()
        f.days = int(spec.get("days", 1))
    elif num < mm.StockField.pattern_enum_begin:
        f = FinancialFilter()
        f.quarter = getattr(
            mm.FinancialQuarter, str(spec.get("quarter", "ANNUAL")).upper()
        )
    elif num < mm.StockField.indicator_enum_begin:
        # Pattern fields are signals, not ranges: presence is the whole filter.
        f = PatternFilter()
        f.ktype = getattr(mm.KLType, str(spec.get("ktype", "K_DAY")).upper())
        f.is_no_filter = False
        f.stock_field = field
        return f
    else:
        raise ValueError(
            f"{field} is a custom-indicator field, which needs two fields and a "
            "relative position; not supported here"
        )

    f.stock_field = field
    f.is_no_filter = lo is None and hi is None
    if lo is not None:
        f.filter_min = float(lo)
    if hi is not None:
        f.filter_max = float(hi)
    if spec.get("sort"):
        f.sort = getattr(mm.SortDir, str(spec["sort"]).upper())
    return f


def screen_fields() -> dict:
    """The screener's vocabulary, grouped by which filter kind each field is.

    Roughly ninety field names, none of them guessable, and each group takes a
    different extra argument (``days`` / ``quarter`` / ``ktype``). Returning
    the list lets a caller build a valid filter without a round-trip of
    failures.
    """
    groups: dict[str, list[str]] = {
        "simple": [],
        "accumulate": [],
        "financial": [],
        "pattern": [],
        "indicator": [],
    }
    for name in mm.StockField.get_all_key_list():
        ok, num = mm.StockField.to_number(name)
        # NONE and the *_enum_begin markers are enum bookkeeping, not fields.
        if not ok or name == "N/A" or name.endswith("_enum_begin"):
            continue
        if num < mm.StockField.acc_enum_begin:
            groups["simple"].append(name)
        elif num < mm.StockField.financial_enum_begin:
            groups["accumulate"].append(name)
        elif num < mm.StockField.pattern_enum_begin:
            groups["financial"].append(name)
        elif num < mm.StockField.indicator_enum_begin:
            groups["pattern"].append(name)
        else:
            groups["indicator"].append(name)
    return {
        "fields": groups,
        "usage": {
            "simple": "{'field': 'PE_TTM', 'min': 5, 'max': 30}",
            "accumulate": "{'field': 'CHANGE_RATE', 'min': 5, 'days': 5} - days is the lookback",
            "financial": "{'field': 'ROIC', 'min': 10, 'quarter': 'ANNUAL'}",
            "pattern": "{'field': 'MACD_GOLD_CROSS_LOW', 'ktype': 'K_DAY'} - a signal, no min/max",
            "indicator": "not supported: needs two fields and a relative position",
        },
        "notes": [
            "Percent fields take whole numbers: 20 means 20%, not 0.20.",
            "A filter with neither min nor max returns the column without constraining it.",
            "Add 'sort': 'DESCEND' or 'ASCEND' to one filter to order the results.",
        ],
    }


def screen_stocks(
    market: str = "US",
    filters: list[dict] | None = None,
    plate: str | None = None,
    begin: int = 0,
    num: int = 50,
) -> dict:
    """Screen a whole market on fundamentals, price action or chart patterns.

    The stock counterpart to ``screener.screen``, and it keeps the same
    contract: it returns the numbers each name matched on and nothing else. No
    score, no ranking beyond the caller's own requested sort, no verdict.
    """
    specs = filters or [{"field": "MARKET_VAL", "sort": "DESCEND"}]
    built = [_build_filter(s) for s in specs]
    result = client.stock_filter(
        market=getattr(mm.Market, market), filters=built, plate=plate, begin=begin, num=num
    )
    return {
        "market": market,
        "plate": plate,
        "filters": specs,
        **result,
    }
