"""Dividend estimates: ex-date eligibility, currency buckets, and what is named.

Histories are injected, so nothing here touches OpenD.

Run with ``pytest sidecar/tests`` or directly: ``python3 sidecar/tests/test_dividends.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dividends import estimate, parse_statement  # noqa: E402


def deal(code: str, side: str, qty: float, day: str) -> dict:
    market = code.split(".")[0]
    return {
        "code": code,
        "trd_side": side,
        "qty": qty,
        "price": 10.0,
        "create_time": f"{day} 10:00:00",
        "deal_market": market,
    }


def position(code: str, qty: float, currency: str = "USD") -> dict:
    return {"code": code, "qty": qty, "currency": currency, "position_market": code.split(".")[0]}


def div(amount: float, ccy: str, ex: str, payable: str) -> dict:
    """A dividend row shaped like get_corporate_actions_dividends (month-first dates)."""
    def us(d: str) -> str:
        y, m, dd = d.split("-")
        return f"{m}/{dd}/{y}"

    return {
        "statement": f"Cash Dividend: {amount} {ccy} Per Share",
        "ex_date": us(ex),
        "record_date": us(ex),
        "dividend_payable_date": us(payable),
    }


def test_parse_statement():
    assert parse_statement("Cash Dividend: 1.65 USD Per Share") == (1.65, "USD")
    assert parse_statement("Cash Dividend: 1.8226USD") == (1.8226, "USD")
    assert parse_statement("Stock Dividend: 1 for 10") is None
    assert parse_statement("") is None


def test_buy_on_ex_date_does_not_qualify():
    deals = [deal("US.JPM", "BUY", 3, "2025-07-03")]
    out = estimate(
        [position("US.JPM", 3)],
        deals,
        since="2025-01-01",
        histories={"US.JPM": [div(1.4, "USD", "2025-07-03", "2025-07-31"),
                              div(1.5, "USD", "2025-10-06", "2025-10-31")]},
    )
    assert [e["ex_date"] for e in out["events"]] == ["2025-10-06"]
    assert out["paid"] == 4.5
    # Broker reported no realized_pl on the row, so none of it is in net yet.
    assert out["paid_not_in_net"] == 4.5


def test_dividend_carried_by_broker_realized_pl_is_marked_in_net():
    """A held, never-sold row's realized_pl is its dividends; net already has them."""
    deals = [deal("US.NEE", "BUY", 5, "2026-08-21")]
    held = position("US.NEE", 5)
    held["realized_pl"] = 3.12
    out = estimate(
        [held], deals, since="2025-01-01",
        histories={"US.NEE": [div(0.624, "USD", "2026-08-28", "2026-09-15")]},
    )
    assert out["paid"] == 3.12
    assert out["paid_in_net"] == 3.12
    assert out["paid_not_in_net"] == 0


def test_dividend_from_a_closed_round_trip_is_not_in_net():
    """The broker row is gone and FIFO ignores dividends: in net nowhere."""
    deals = [deal("US.MRK", "BUY", 10, "2025-01-02"), deal("US.MRK", "SELL", 10, "2025-06-01")]
    out = estimate(
        [], deals, since="2025-01-01",
        histories={"US.MRK": [div(0.8, "USD", "2025-03-14", "2025-04-07")]},
    )
    assert out["paid"] == 8.0
    assert out["paid_not_in_net"] == 8.0


def test_dividend_before_the_holding_was_reopened_is_not_in_net():
    """realized_pl resets when a position goes flat, so older dividends fall out."""
    deals = [
        deal("US.T", "BUY", 10, "2025-01-02"),
        deal("US.T", "SELL", 10, "2025-03-01"),
        deal("US.T", "BUY", 10, "2025-05-01"),
    ]
    held = position("US.T", 10)
    held["realized_pl"] = 2.5
    out = estimate(
        [held], deals, since="2025-01-01",
        histories={"US.T": [div(0.3, "USD", "2025-02-01", "2025-02-15"),
                            div(0.25, "USD", "2025-07-01", "2025-07-15")]},
    )
    assert out["paid"] == 5.5
    assert out["paid_in_net"] == 2.5
    assert out["paid_not_in_net"] == 3.0


def test_sold_before_ex_date_earns_nothing():
    deals = [deal("US.MRK", "BUY", 10, "2025-01-02"), deal("US.MRK", "SELL", 10, "2025-03-01")]
    out = estimate(
        [], deals, since="2025-01-01",
        histories={"US.MRK": [div(0.8, "USD", "2025-03-14", "2025-04-07")]},
    )
    assert out["events"] == []
    assert out["paid"] == 0


def test_pending_and_upcoming_are_separate_from_paid():
    deals = [deal("US.VOO", "BUY", 2, "2025-01-02")]
    out = estimate(
        [position("US.VOO", 2)], deals, since="2025-01-01",
        histories={"US.VOO": [
            div(1.0, "USD", "2025-03-27", "2025-03-31"),   # paid
            div(2.0, "USD", "2026-01-05", "2099-01-01"),   # ex passed, not yet payable
            div(3.0, "USD", "2099-06-01", "2099-06-30"),   # not ex yet
        ]},
    )
    assert out["paid"] == 2.0
    assert out["pending"] == 4.0
    assert [u["ex_date"] for u in out["upcoming"]] == ["2099-06-01"]
    assert out["upcoming"][0]["amount_if_held"] == 6.0


def test_foreign_currency_converted_before_summing():
    deals = [deal("US.JPM", "BUY", 1, "2025-01-02"), deal("MY.1155", "BUY", 100, "2025-01-02")]
    histories = {
        "US.JPM": [div(1.0, "USD", "2025-04-04", "2025-04-30")],
        "MY.1155": [div(0.5, "MYR", "2025-05-01", "2025-05-20")],
    }
    out = estimate(
        [position("US.JPM", 1), position("MY.1155", 100, "MYR")],
        deals, since="2025-01-01", rates={"MYR": 0.25}, histories=histories,
    )
    assert out["by_currency"]["MYR"]["paid"] == 50.0
    assert out["paid"] == 1.0 + 50.0 * 0.25
    assert out["conversion"]["converted_from"] == ["MYR"]


def test_unpriced_currency_is_named_not_added_at_par():
    deals = [deal("MY.1155", "BUY", 100, "2025-01-02")]
    out = estimate(
        [position("MY.1155", 100, "MYR")], deals, since="2025-01-01",
        histories={"MY.1155": [div(0.5, "MYR", "2025-05-01", "2025-05-20")]},
    )
    assert out["paid"] == 0
    assert out["conversion"]["unconverted"] == ["MYR"]
    assert out["by_currency"]["MYR"]["paid"] == 50.0


def test_unexplained_holdings_and_unreadable_histories_are_named():
    out = estimate(
        [position("US.COST", 1), position("MY.5099", 100, "MYR")],
        [],
        since="2025-01-01",
        histories={"US.COST": [div(1.3, "USD", "2025-05-01", "2025-05-20")]},
        unavailable={"MY.5099": "No permission to get quotes for MY.5099."},
    )
    # No deals explain the COST share, so nothing is credited for it -- but it
    # is named rather than silently missing.
    assert out["events"] == []
    assert "US.COST" in out["unreconciled"]
    assert "MY.5099" in out["unavailable"]


def test_spelled_out_dates_are_read():
    deals = [deal("US.VOO", "BUY", 1, "2026-08-31")]
    held = position("US.VOO", 1)
    held["realized_pl"] = 1.82
    out = estimate(
        [held], deals, since="2025-01-01",
        histories={"US.VOO": [{"statement": "Cash Dividend: 1.8226USD", "ex_date": "Sep 28, 2026",
                               "dividend_payable_date": "Sep 30, 2026"}]},
    )
    assert out["paid"] == 1.82
    assert out["paid_in_net"] == 1.82


def test_unknown_date_format_is_listed_not_dropped():
    out = estimate(
        [position("US.X", 1)], [deal("US.X", "BUY", 1, "2025-01-02")], since="2025-01-01",
        histories={"US.X": [{"statement": "Cash Dividend: 1 USD", "ex_date": "2026.09.28"}]},
    )
    assert out["unparsed"][0]["ex_date"] == "2026.09.28"


def test_non_cash_statement_is_listed_unparsed():
    deals = [deal("US.X", "BUY", 5, "2025-01-02")]
    out = estimate(
        [position("US.X", 5)], deals, since="2025-01-01",
        histories={"US.X": [{"statement": "Stock Dividend: 1 for 20", "ex_date": "06/02/2025",
                             "dividend_payable_date": "06/20/2025"}]},
    )
    assert out["paid"] == 0
    assert out["unparsed"][0]["code"] == "US.X"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
