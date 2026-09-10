"""Guardrail ordering.

No OpenD needed: `_order_guardrails` is patched away from the broker and the
quote client, and everything else it reads is the SQLite journal.

The invariant under test is one thing: every cap in this pipeline is a budget
on *new* exposure, so none of it may refuse a SELL. On 2026-09-09 six exits on
AMAT and SVRA were journalled `daily_order_cap` because the day's entries had
spent the allowance, and both positions stayed open.
"""

import os
import tempfile
import unittest
from dataclasses import replace
from unittest import mock

import paper
from paper import engine as E
from paper import journal as J
from paper import rules as R

_RULES = {
    "version": 1,
    "ktype": "K_DAY",
    "defaults": {"risk_pct": 1.0, "atr_mult_stop": 2.0, "cooldown_days": 3},
    "portfolio": {
        "max_open_notional_pct": 50.0,
        "max_orders_per_day": 1,
        "max_orders_per_symbol_per_day": 1,
        "max_concurrent_positions": 1,
        "max_fee_pct_of_notional": 0.01,   # deliberately unmeetable
        "flatten_before_close_minutes": 20,
    },
    "rules": [{
        "id": "r1", "enabled": True, "side": "BUY",
        "when": {"all": [{"field": "price", "op": "gt", "value": 0}]},
        "exit": {"all": [{"field": "price", "op": "gt", "value": 0}]},
    }],
}


class Guardrails(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self._orig = (J._DB, J._DIR, J._KILL_SENTINEL)
        J._DB = os.path.join(self._dir, "journal.db")
        J._DIR = self._dir
        J._KILL_SENTINEL = os.path.join(self._dir, "KILLED")
        J.close()

        path = os.path.join(self._dir, "rules.json")
        with open(path, "w") as fh:
            import json
            json.dump(_RULES, fh)
        self.ruleset = R.load(path)
        self.rule = self.ruleset.rules[0]
        # Same limits with room for a held position and a meetable fee limit, so
        # the notional ceiling is what these tests reach rather than
        # max_concurrent_positions: 1 or the deliberately unmeetable fee cap.
        self.roomy = replace(
            self.ruleset,
            portfolio=replace(
                self.ruleset.portfolio,
                max_concurrent_positions=8,
                max_fee_pct_of_notional=100.0,
            ),
        )

        # Sizing, quotes and the broker are not what this file is about.
        for target, value in (
            ("_minutes_to_close", 300.0),
            ("_price_sanity", None),
        ):
            p = mock.patch.object(E, target, return_value=value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(E.broker, "positions", return_value=[])
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        J.close()
        J._DB, J._DIR, J._KILL_SENTINEL = self._orig

    def _order(self, order_id, side, code="US.AMD"):
        J.record_order({
            "order_id": order_id, "client_tag": f"tag-{order_id}",
            "trd_env": paper.PAPER_TRD_ENV, "acc_id": paper.PAPER_ACC_ID,
            "code": code, "trd_side": side, "qty": 1.0, "price": 100.0,
            "status": "FILLED_ALL", "dealt_qty": 1.0, "remark": f"pt-{order_id}",
        })

    def _check(self, side, code="US.AMD", equity=10_000.0, ruleset=None):
        return E._order_guardrails(
            self.rule, code, side, 1, 100.0, {"price": 100.0},
            ruleset or self.ruleset, equity,
        )

    # -- the entry budget must not reach an exit -----------------------------
    def test_daily_cap_blocks_a_further_entry(self):
        self._order("b1", "BUY", code="US.NVDA")
        block = self._check("BUY")
        self.assertIsNotNone(block)
        self.assertEqual(block.reason, "daily_order_cap")

    def test_daily_cap_never_blocks_an_exit(self):
        self._order("b1", "BUY", code="US.NVDA")
        self.assertIsNone(self._check("SELL"),
                          "an exit must not be refused because entries spent the cap")

    def test_per_symbol_cap_never_blocks_the_same_day_exit(self):
        self._order("b1", "BUY")
        self.assertIsNone(self._check("SELL"),
                          "buying and selling a symbol the same day must be possible")

    def test_cooldown_never_blocks_an_exit(self):
        J.record_decision({
            "decision_id": "d1", "cycle_id": "c1", "code": "US.AMD",
            "rule_id": "r1", "kind": "ENTRY", "outcome": "PLACED", "side": "BUY",
        })
        self.assertEqual(self._check("BUY").reason, "cooldown")
        self.assertIsNone(self._check("SELL"),
                          "a cooldown is a re-entry delay, not a holding period")

    def test_fee_burden_never_blocks_an_exit(self):
        self.assertEqual(self._check("BUY").reason, "fee_burden")
        self.assertIsNone(self._check("SELL"))

    def test_late_entry_never_blocks_an_exit(self):
        with mock.patch.object(E, "_minutes_to_close", return_value=5.0):
            self.assertEqual(self._check("BUY").reason, "late_entry")
            self.assertIsNone(self._check("SELL"))

    def test_open_notional_cap_never_blocks_an_exit(self):
        # 4,900 open + a 100 entry is 50.0% of 10,000; one more dollar is over.
        held = [{"code": "US.NVDA", "qty": 1, "market_val": 4_950.0}]
        with mock.patch.object(E.broker, "positions", return_value=held):
            self.assertEqual(self._check("BUY", ruleset=self.roomy).reason,
                             "open_notional_cap")
            self.assertIsNone(self._check("SELL", ruleset=self.roomy))

    def test_open_notional_cap_admits_an_entry_that_fits(self):
        held = [{"code": "US.NVDA", "qty": 1, "market_val": 4_800.0}]
        with mock.patch.object(E.broker, "positions", return_value=held):
            self.assertIsNone(self._check("BUY", ruleset=self.roomy))

    def test_open_notional_ignores_the_na_string_moomoo_returns(self):
        held = [{"code": "US.NVDA", "qty": 1, "market_val": "N/A"}]
        with mock.patch.object(E.broker, "positions", return_value=held):
            self.assertIsNone(self._check("BUY", ruleset=self.roomy),
                              "an unreadable market_val must not read as huge exposure")

    def test_open_notional_measured_against_real_equity_not_paper(self):
        held = [{"code": "US.NVDA", "qty": 1, "market_val": 4_950.0}]
        with mock.patch.object(E.broker, "positions", return_value=held):
            # The same book against a paper account's million would never bind;
            # the guard must be reading the real equity it was handed.
            self.assertEqual(
                self._check("BUY", equity=10_000.0, ruleset=self.roomy).reason,
                "open_notional_cap")
            self.assertIsNone(
                self._check("BUY", equity=1_000_000.0, ruleset=self.roomy))

    def test_unmeasurable_equity_blocks_rather_than_waves_through(self):
        self.assertEqual(self._check("BUY", equity=0.0).reason, "open_notional_cap")

    def test_max_concurrent_positions_never_blocks_an_exit(self):
        with mock.patch.object(E.broker, "positions",
                               return_value=[{"code": "US.NVDA", "qty": 1}]):
            self.assertEqual(self._check("BUY").reason, "max_concurrent_positions")
            self.assertIsNone(self._check("SELL"))

    # -- what still applies to both sides ------------------------------------
    def test_a_resting_order_blocks_both_sides(self):
        J.record_order({
            "order_id": "o9", "client_tag": "tag-o9",
            "trd_env": paper.PAPER_TRD_ENV, "acc_id": paper.PAPER_ACC_ID,
            "code": "US.AMD", "trd_side": "BUY", "qty": 1.0, "price": 100.0,
            "status": "SUBMITTED", "dealt_qty": 0.0, "remark": "pt-o9",
        })
        for side in ("BUY", "SELL"):
            self.assertEqual(self._check(side).reason, "order_outstanding",
                             f"{side}: stacking on a resting order is a double order")

    def test_a_missing_price_blocks_both_sides(self):
        for side in ("BUY", "SELL"):
            block = E._order_guardrails(
                self.rule, "US.AMD", side, None, None, {}, self.ruleset, 10_000.0,
            )
            self.assertEqual(block.reason, "no_price")


if __name__ == "__main__":
    unittest.main()


class MissedEntryWindow(unittest.TestCase):
    """Never evaluating is not a halt, so `alerts` did not cover it.

    The whole US session is 21:30-04:00 local on this machine, and
    PAPER_EVALUATE_AT gives the engine one chance a day inside it. On
    2026-09-09 the last in-session cycle was 15:00 ET and the next was 17:06 ET
    - the 15:35 window passed unevaluated and nothing said so.
    """

    ET = __import__("zoneinfo").ZoneInfo("America/New_York")

    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self._orig = (J._DB, J._DIR, J._KILL_SENTINEL)
        J._DB = os.path.join(self._dir, "journal.db")
        J._DIR = self._dir
        J._KILL_SENTINEL = os.path.join(self._dir, "KILLED")
        J.close()
        p = mock.patch.object(E, "EVALUATE_AT", "15:35")
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        J.close()
        J._DB, J._DIR, J._KILL_SENTINEL = self._orig

    def _at(self, y, m, d, hh, mm):
        import datetime as dt
        return dt.datetime(y, m, d, hh, mm, tzinfo=self.ET)

    def test_quiet_while_the_window_is_still_ahead(self):
        self.assertIsNone(E.missed_entry_window(self._at(2026, 9, 9, 15, 0)))

    def test_reports_once_the_session_has_ended_unevaluated(self):
        self.assertIsNotNone(E.missed_entry_window(self._at(2026, 9, 9, 16, 30)))

    def test_quiet_when_the_window_was_used(self):
        J.set_state("entries_evaluated:2026-09-09", "1")
        self.assertIsNone(E.missed_entry_window(self._at(2026, 9, 9, 16, 30)))

    def test_the_session_it_reports_is_the_clock_it_was_given(self):
        J.set_state("entries_evaluated:2026-09-09", "1")
        # A different session is still missed; the key must not be "today".
        self.assertIn("2026-09-08",
                      E.missed_entry_window(self._at(2026, 9, 8, 16, 30)) or "")

    def test_quiet_at_the_weekend(self):
        self.assertIsNone(E.missed_entry_window(self._at(2026, 9, 12, 18, 0)))

    def test_quiet_when_no_window_is_configured(self):
        with mock.patch.object(E, "EVALUATE_AT", ""):
            self.assertIsNone(E.missed_entry_window(self._at(2026, 9, 9, 16, 30)))


class LastCompletedSession(unittest.TestCase):
    """The note runs at 08:50 local, which is the previous evening in New York.

    Taking journal.trade_date() there names a session that has not started, and
    Friday's actual session would never be reported at all.
    """

    ET = __import__("zoneinfo").ZoneInfo("America/New_York")
    LOC = __import__("zoneinfo").ZoneInfo("Asia/Kuala_Lumpur")

    def _job(self, y, m, d):
        """What the 08:50 local job reports on that local date."""
        import datetime as dt
        local = dt.datetime(y, m, d, 8, 50, tzinfo=self.LOC)
        return E.last_completed_session(local.astimezone(self.ET))

    def test_the_morning_after_reports_the_session_that_closed(self):
        self.assertEqual(self._job(2026, 9, 10), "2026-09-09")

    def test_saturday_local_reports_fridays_session(self):
        # Friday ET closes at 04:00 Saturday local; without this the whole
        # Friday session was never reported.
        self.assertEqual(self._job(2026, 9, 12), "2026-09-11")

    def test_never_names_a_weekend_session(self):
        for d in (12, 13, 14):
            got = self._job(2026, 9, d)
            import datetime as dt
            self.assertLess(dt.date.fromisoformat(got).weekday(), 5, got)

    def test_before_the_close_reports_the_previous_day(self):
        import datetime as dt
        noon = dt.datetime(2026, 9, 10, 12, 0, tzinfo=self.ET)
        self.assertEqual(E.last_completed_session(noon), "2026-09-09")

    def test_after_the_close_reports_the_same_day(self):
        import datetime as dt
        evening = dt.datetime(2026, 9, 10, 17, 0, tzinfo=self.ET)
        self.assertEqual(E.last_completed_session(evening), "2026-09-10")
