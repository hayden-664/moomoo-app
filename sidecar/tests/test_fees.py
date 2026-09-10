"""The estimated fee model, checked against the real account's own fills.

The sample below is 40 real filled orders (2026-06 to 2026-09) with the fee
moomoo actually charged. It is frozen here on purpose: if moomoo changes its
schedule, or someone retunes the constants, these assertions fail rather than
every sizing decision silently skewing.

Fees are the reason this engine can lie to you. A paper fill costs nothing, so
an un-modelled round trip that really loses money is recorded as a win.
"""

import statistics
import unittest

from paper import fees

# (side, notional, fee actually charged)
REAL_FILLS = [
    ("SELL", 650.00, 1.75), ("BUY", 360.00, 1.50), ("BUY", 370.00, 1.51),
    ("BUY", 330.00, 1.48), ("BUY", 333.94, 1.48), ("SELL", 1500.00, 2.77),
    ("SELL", 505.15, 1.77), ("SELL", 500.00, 1.77), ("SELL", 1010.95, 2.29),
    ("BUY", 730.00, 1.76), ("BUY", 337.97, 1.49), ("BUY", 340.00, 1.49),
    ("SELL", 1000.00, 2.26), ("SELL", 229.00, 1.26), ("BUY", 430.00, 1.51),
    ("BUY", 450.00, 1.49), ("BUY", 460.00, 1.49), ("BUY", 350.00, 1.55),
    ("BUY", 705.00, 1.73), ("BUY", 909.95, 1.98), ("SELL", 470.00, 1.53),
    ("SELL", 830.00, 2.01), ("BUY", 420.00, 1.50), ("SELL", 276.00, 1.50),
    ("BUY", 170.00, 1.27), ("BUY", 900.00, 2.01), ("BUY", 460.00, 1.48),
    ("BUY", 465.00, 1.48), ("BUY", 455.00, 1.50), ("BUY", 810.00, 1.98),
    ("SELL", 350.00, 1.50), ("BUY", 492.00, 1.48), ("SELL", 237.00, 1.26),
    ("SELL", 150.44, 1.26), ("BUY", 360.00, 1.48), ("BUY", 547.93, 1.72),
    ("SELL", 966.00, 2.00), ("BUY", 277.96, 1.48), ("BUY", 274.00, 1.48),
    ("BUY", 720.00, 1.99),
]


class ModelFit(unittest.TestCase):
    def setUp(self):
        self.errors = [
            fees.estimate(n, side) - actual for side, n, actual in REAL_FILLS
        ]

    def test_mean_absolute_error_under_20_cents(self):
        mae = statistics.mean(abs(e) for e in self.errors)
        self.assertLess(mae, 0.20, f"mean absolute error {mae:.3f} is too large")

    def test_errs_on_the_safe_side(self):
        """A guard that under-states costs is worse than one that over-states."""
        self.assertGreater(statistics.mean(self.errors), 0)

    def test_never_materially_underestimates(self):
        worst = min(self.errors)
        self.assertGreater(
            worst, -0.10, f"model under-charges by {worst:.2f} on a real fill"
        )

    def test_commission_free_account_still_costs_a_dollar(self):
        """Commission is zero here; the flat platform fee is the real cost."""
        self.assertGreaterEqual(fees.estimate(1.0, "BUY"), fees.PLATFORM_FEE)


class RoundTrip(unittest.TestCase):
    def test_cost_is_regressive_on_small_positions(self):
        small = fees.round_trip_pct(330)
        large = fees.round_trip_pct(5000)
        self.assertGreater(small, large)
        self.assertGreater(small, 0.8, "a $330 round trip should cost well under 1%")
        self.assertLess(large, 0.35)

    def test_a_650_dollar_position_must_move_about_half_a_percent(self):
        """The situation on this account at a 5% cap — the reason for the guard."""
        self.assertAlmostEqual(fees.round_trip_pct(650), 0.55, delta=0.08)

    def test_breakeven_is_above_entry(self):
        be = fees.breakeven_price(100.0, 10)
        self.assertIsNotNone(be)
        self.assertGreater(be, 100.0)
        # 10 shares at 100 is $1000 of notional; the round trip is a few dollars.
        self.assertLess(be, 101.0)

    def test_zero_notional_is_not_a_division_error(self):
        self.assertEqual(fees.round_trip(0), 0.0)
        self.assertEqual(fees.round_trip_pct(0), float("inf"))

    def test_describe_carries_its_provenance(self):
        d = fees.describe(1000)
        self.assertTrue(d["estimated"])
        self.assertIn("paper fills are free", d["source"])


if __name__ == "__main__":
    unittest.main()
