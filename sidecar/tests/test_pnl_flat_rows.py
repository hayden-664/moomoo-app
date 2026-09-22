"""A sold-out position must not be counted twice while its broker row lingers.

After an outright sale ``position_list_query`` keeps returning the symbol at
``qty`` 0 -- with its ``realized_pl`` still populated -- until the broker drops
the row. The FIFO reconstruction has already booked that exit as a closed
round-trip, so for as long as the row is there the same trade is visible from
both sides. Adding both inflated net P&L by the size of the trade just closed
and then "corrected" itself a day later when the row vanished.

Run with ``pytest sidecar/tests`` or directly: ``python3 sidecar/tests/test_pnl_flat_rows.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pnl import net_pnl  # noqa: E402


def position(code: str, qty: float, avg_cost: float, realized: float = 0.0, **kw) -> dict:
    """A position row shaped like the broker's, including its qty-0 form."""
    return {
        "code": code,
        "qty": qty,
        "average_cost": avg_cost,
        "cost_price": avg_cost,
        "market_val": kw.get("market_val", 0.0),
        "unrealized_pl": kw.get("unrealized_pl", 0.0),
        "today_pl_val": kw.get("today_pl_val", 0.0),
        "realized_pl": realized,
        "currency": "USD",
        "position_market": "US",
    }


def deal(code: str, side: str, qty: float, price: float, day: str) -> dict:
    return {
        "code": code,
        "trd_side": side,
        "qty": qty,
        "price": price,
        "create_time": f"{day} 10:00:00.000",
        "deal_market": "US",
    }


# META, as it actually was: two lifecycles, the second closed out today. The
# broker's realized_pl resets when a position goes flat and is re-opened, so it
# reports only the second round-trip (4 shares off 534.4825 average cost), while
# the reconstruction covers both (793.71 FIFO).
META_DEALS = [
    deal("US.META", "BUY", 1, 565.0, "2026-06-12"),
    deal("US.META", "BUY", 1, 560.0, "2026-06-24"),
    deal("US.META", "SELL", 1, 620.0, "2026-07-07"),
    deal("US.META", "SELL", 1, 672.0, "2026-07-10"),
    deal("US.META", "BUY", 3, 530.0, "2026-07-30"),
    deal("US.META", "BUY", 1, 547.93, "2026-08-18"),
    deal("US.META", "SELL", 1, 650.0, "2026-09-09"),
    deal("US.META", "SELL", 1, 668.0, "2026-09-14"),
    deal("US.META", "SELL", 1, 680.0, "2026-09-16"),
    deal("US.META", "SELL", 1, 753.0, "2026-09-22"),
]
META_FIFO = 55.0 + 112.0 + 120.0 + 138.0 + 150.0 + 205.07  # 780.07
META_BROKER = 613.07  # 2751 proceeds - 4 * 534.4825 average cost

HELD = position("US.VOO", 1, 705.0, market_val=712.84, unrealized_pl=7.84)
FLAT_META = position("US.META", 0, 534.483, realized=META_BROKER, today_pl_val=11.76)


def test_flat_row_does_not_double_count_its_exit():
    """The lingering row's realized_pl is dropped, not added on top."""
    out = net_pnl([HELD, FLAT_META], META_DEALS, base="USD")
    assert out["closed"]["closed_realized"] == round(META_FIFO, 2)
    # Not META_FIFO + META_BROKER, which is what the bug produced.
    assert out["total_realized"] == round(META_FIFO, 2)
    assert out["net"] == round(META_FIFO + 7.84, 2)
    # And it says so rather than dropping the broker figure silently.
    assert out["closed"]["superseded_broker"] == ["US.META"]


def test_net_is_stable_when_the_broker_drops_the_row():
    """The number must not move just because the row aged out of the payload."""
    with_row = net_pnl([HELD, FLAT_META], META_DEALS, base="USD")
    without_row = net_pnl([HELD], META_DEALS, base="USD")
    assert with_row["net"] == without_row["net"]
    assert with_row["total_realized"] == without_row["total_realized"]


def test_flat_rows_are_not_counted_as_open_positions():
    out = net_pnl([HELD, FLAT_META], META_DEALS, base="USD")
    assert out["position_count"] == 1
    assert out["open"]["flat_count"] == 1
    # The row's own figures still belong to today; only the count excludes it.
    assert out["open"]["today"] == 11.76
    assert out["open"]["cost_basis"] == 705.0


def test_partial_sell_still_prefers_the_broker_figure():
    """The existing per-symbol reconciliation is untouched for held positions."""
    deals = [
        deal("US.TMDX", "BUY", 10, 72.0, "2026-08-05"),
        deal("US.TMDX", "SELL", 5, 94.0, "2026-08-27"),
    ]
    held = position("US.TMDX", 5, 72.0, realized=110.0, market_val=434.83, unrealized_pl=74.83)
    out = net_pnl([held], deals, base="USD")
    assert out["partial"]["broker_symbols"] == ["US.TMDX"]
    assert out["closed"]["superseded_broker"] == []
    assert out["total_realized"] == 110.0
    assert out["position_count"] == 1


def test_broker_figure_survives_when_the_window_cannot_close_the_symbol():
    """A flat row whose opening buys predate deal history keeps its figure.

    There is no reconstruction to prefer here -- the unmatched sell produces no
    FIFO event at all -- so dropping the broker row would lose the trade.
    """
    deals = [deal("US.ARM", "SELL", 10, 150.0, "2026-09-22")]
    flat = position("US.ARM", 0, 120.0, realized=300.0)
    out = net_pnl([flat], deals, base="USD")
    assert out["closed"]["closed_realized"] == 0.0
    assert out["total_realized"] == 300.0
    assert out["partial"]["broker_symbols"] == ["US.ARM"]


def test_dividend_on_a_never_sold_position_is_still_banked():
    """realized_pl with no sell behind it (a distribution) is broker-only."""
    deals = [deal("US.NEE", "BUY", 5, 84.0, "2026-08-21")]
    held = position("US.NEE", 5, 84.0, realized=3.12, market_val=398.25, unrealized_pl=-21.75)
    out = net_pnl([held], deals, base="USD")
    assert out["total_realized"] == 3.12


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except Exception as exc:  # assertion or a missing key alike
                failures += 1
                print(f"FAIL {name}: {exc!r}")
            else:
                print(f"ok   {name}")
    sys.exit(1 if failures else 0)
