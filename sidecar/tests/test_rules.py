"""Grammar and validation tests for the paper strategy file. No OpenD needed.

Run: sidecar/.venv/bin/python -m unittest discover -s sidecar/tests -t sidecar
"""

import json
import os
import tempfile
import unittest

from paper import rules as R


def _write(doc: dict) -> str:
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    return path


def _doc(**over) -> dict:
    doc = {
        "version": 1,
        "ktype": "K_DAY",
        "rules": [
            {
                "id": "r1",
                "side": "BUY",
                "when": {"all": [{"field": "momentum.rsi14", "op": "lt", "value": 65}]},
            }
        ],
    }
    doc.update(over)
    return doc


TECH = {
    "price": 100.0,
    "trend": {
        "sma50": 95.0,
        "sma200": None,
        "pct_from": {"sma50": 5.26, "sma200": None},
        "ma_order": ["sma20", "sma50", "sma100"],
    },
    "momentum": {
        "rsi14": 58.0,
        "rsi_zone": "30_to_70",
        "macd_cross": {"crossed_within": 1, "direction": "above"},
    },
    "volatility": {"atr14": None, "atr_pct_of_price": 2.4},
}


class Resolve(unittest.TestCase):
    def test_nested_path(self):
        self.assertEqual(R.resolve(TECH, "momentum.macd_cross.direction"), "above")

    def test_unknown_path_is_none_not_raise(self):
        self.assertIsNone(R.resolve(TECH, "momentum.nope.deeper"))

    def test_walking_through_a_scalar_is_none(self):
        self.assertIsNone(R.resolve(TECH, "price.something"))


class Evaluate(unittest.TestCase):
    def test_leaf_true_and_traced(self):
        ok, trace = R.evaluate({"field": "momentum.rsi14", "op": "lt", "value": 65}, TECH)
        self.assertTrue(ok)
        self.assertEqual(trace[0]["resolved"], 58.0)
        self.assertTrue(trace[0]["result"])

    def test_null_value_is_false_not_exception(self):
        ok, trace = R.evaluate({"field": "volatility.atr14", "op": "gt", "value": 1}, TECH)
        self.assertFalse(ok)
        self.assertIsNone(trace[0]["resolved"])

    def test_unknown_path_is_false_and_recorded(self):
        ok, trace = R.evaluate({"field": "momentum.zzz", "op": "gt", "value": 1}, TECH)
        self.assertFalse(ok)
        self.assertEqual(trace[0]["path"], "momentum.zzz")

    def test_is_null_still_works_on_missing(self):
        ok, _ = R.evaluate({"field": "volatility.atr14", "op": "is_null"}, TECH)
        self.assertTrue(ok)

    def test_starts_with_on_ma_order(self):
        ok, _ = R.evaluate(
            {"field": "trend.ma_order", "op": "starts_with", "value": ["sma20", "sma50"]},
            TECH,
        )
        self.assertTrue(ok)
        bad, _ = R.evaluate(
            {"field": "trend.ma_order", "op": "starts_with", "value": ["sma50", "sma20"]},
            TECH,
        )
        self.assertFalse(bad)

    def test_between_inclusive(self):
        ok, _ = R.evaluate(
            {"field": "volatility.atr_pct_of_price", "op": "between", "value": [2.4, 6.0]},
            TECH,
        )
        self.assertTrue(ok)

    def test_all_any_none(self):
        cond = {
            "all": [
                {"field": "momentum.rsi14", "op": "lt", "value": 65},
                {"any": [
                    {"field": "momentum.rsi_zone", "op": "eq", "value": "above_70"},
                    {"field": "momentum.macd_cross.direction", "op": "eq", "value": "above"},
                ]},
                {"none": [{"field": "momentum.rsi14", "op": "gt", "value": 90}]},
            ]
        }
        ok, trace = R.evaluate(cond, TECH)
        self.assertTrue(ok)
        self.assertEqual(len(trace), 4)

    def test_string_compared_with_number_is_false_not_raise(self):
        ok, _ = R.evaluate({"field": "momentum.rsi_zone", "op": "gt", "value": 5}, TECH)
        self.assertFalse(ok)


class Load(unittest.TestCase):
    def _err(self, doc, fragment):
        path = _write(doc)
        try:
            with self.assertRaises(R.RuleError) as cm:
                R.load(path)
            self.assertIn(fragment, str(cm.exception))
        finally:
            os.unlink(path)

    def test_valid(self):
        path = _write(_doc())
        try:
            rs = R.load(path)
            self.assertEqual(len(rs.rules), 1)
            self.assertEqual(len(rs.sha256), 64)
        finally:
            os.unlink(path)

    def test_unknown_field_path(self):
        self._err(
            _doc(rules=[{"id": "r", "side": "BUY",
                         "when": {"field": "momentum.rsi15", "op": "lt", "value": 1}}]),
            "unknown path",
        )

    def test_unknown_operator(self):
        self._err(
            _doc(rules=[{"id": "r", "side": "BUY",
                         "when": {"field": "momentum.rsi14", "op": "approx", "value": 1}}]),
            "unknown operator",
        )

    def test_between_arity(self):
        self._err(
            _doc(rules=[{"id": "r", "side": "BUY",
                         "when": {"field": "momentum.rsi14", "op": "between", "value": [1]}}]),
            "exactly [min, max]",
        )

    def test_between_inverted_bounds(self):
        self._err(
            _doc(rules=[{"id": "r", "side": "BUY",
                         "when": {"field": "momentum.rsi14", "op": "between", "value": [9, 1]}}]),
            "exceeds max",
        )

    def test_duplicate_rule_id(self):
        rule = {"id": "same", "side": "BUY",
                "when": {"field": "momentum.rsi14", "op": "lt", "value": 65}}
        self._err(_doc(rules=[rule, dict(rule)]), "duplicate id")

    def test_bad_side(self):
        self._err(
            _doc(rules=[{"id": "r", "side": "HOLD",
                         "when": {"field": "momentum.rsi14", "op": "lt", "value": 1}}]),
            "must be BUY or SELL",
        )

    def test_risk_pct_above_ceiling(self):
        self._err(
            _doc(rules=[{"id": "r", "side": "BUY", "size": {"risk_pct": 99},
                         "when": {"field": "momentum.rsi14", "op": "lt", "value": 1}}]),
            "outside",
        )

    def test_depth_cap(self):
        cond = {"field": "momentum.rsi14", "op": "lt", "value": 1}
        for _ in range(R.MAX_DEPTH + 2):
            cond = {"all": [cond]}
        self._err(_doc(rules=[{"id": "r", "side": "BUY", "when": cond}]), "nesting exceeds")

    def test_intraday_ktype_rejected(self):
        self._err(_doc(ktype="K_15M"), "not supported")

    def test_unknown_portfolio_key(self):
        self._err(_doc(portfolio={"max_positions": 3}), "unknown keys")

    def test_bad_insufficient_power_mode(self):
        self._err(_doc(portfolio={"on_insufficient_paper_power": "truncate"}),
                  "reject")

    def test_malformed_json(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as fh:
            fh.write("{not json")
        try:
            with self.assertRaises(R.RuleError) as cm:
                R.load(path)
            self.assertIn("not valid JSON", str(cm.exception))
        finally:
            os.unlink(path)

    def test_shipped_rules_file_is_valid(self):
        rs = R.load()
        self.assertGreaterEqual(len(rs.rules), 1)
        self.assertEqual(rs.ktype, "K_DAY")


if __name__ == "__main__":
    unittest.main()


class EntryWindow(unittest.TestCase):
    """PAPER_EVALUATE_AT gates entries only, and fails open."""

    def setUp(self):
        from paper import engine
        self.engine = engine
        self._orig = engine.EVALUATE_AT

    def tearDown(self):
        self.engine.EVALUATE_AT = self._orig

    def _at(self, hhmm):
        import datetime as dt
        from zoneinfo import ZoneInfo
        h, m = hhmm.split(":")
        return dt.datetime.now(ZoneInfo("America/New_York")).replace(
            hour=int(h), minute=int(m), second=0, microsecond=0
        )

    def test_unset_means_every_cycle(self):
        self.engine.EVALUATE_AT = ""
        ok, why = self.engine.entry_window(self._at("10:00"))
        self.assertTrue(ok)
        self.assertIn("every cycle", why)

    def test_gated_before_the_time(self):
        self.engine.EVALUATE_AT = "15:35"
        ok, why = self.engine.entry_window(self._at("09:31"))
        self.assertFalse(ok)
        self.assertIn("15:35", why)

    def test_open_after_the_time(self):
        self.engine.EVALUATE_AT = "15:35"
        ok, _ = self.engine.entry_window(self._at("15:36"))
        self.assertTrue(ok)

    def test_unparseable_value_fails_open(self):
        """Trading nothing all day because of a typo would be the worst outcome."""
        self.engine.EVALUATE_AT = "quarter to four"
        ok, why = self.engine.entry_window(self._at("10:00"))
        self.assertTrue(ok)
        self.assertIn("not HH:MM", why)

    def test_conflict_with_the_late_entry_window_is_reported(self):
        self.engine.EVALUATE_AT = "15:45"
        msg = self.engine.evaluate_at_conflict(20)
        self.assertIsNotNone(msg)
        self.assertIn("15:40", msg)

    def test_no_conflict_when_outside_that_window(self):
        self.engine.EVALUATE_AT = "15:35"
        self.assertIsNone(self.engine.evaluate_at_conflict(20))


class NotifyLevels(unittest.TestCase):
    """PAPER_NOTIFY silences receipts without silencing failures."""

    def setUp(self):
        from paper import engine
        self.engine = engine
        self._orig = engine.NOTIFY
        self.sent = []
        self._real = engine.telegram.notify

    def tearDown(self):
        self.engine.NOTIFY = self._orig
        self.engine.telegram.notify = self._real

    def _capture(self):
        async def fake(text, **kw):
            self.sent.append(text)
            return {"sent": True}
        self.engine.telegram.notify = fake

    def _emit(self):
        self._capture()
        self.engine._notify("routine receipt", level="all")
        self.engine._notify("engine halted", level="alerts")

    def test_all_sends_everything(self):
        self.engine.NOTIFY = "all"
        self._emit()
        self.assertEqual(len(self.sent), 2)

    def test_alerts_drops_receipts_keeps_halts(self):
        self.engine.NOTIFY = "alerts"
        self._emit()
        self.assertEqual(self.sent, ["engine halted"],
                         "a halt must survive the noise filter")

    def test_off_sends_nothing(self):
        self.engine.NOTIFY = "off"
        self._emit()
        self.assertEqual(self.sent, [])

    def test_unknown_value_falls_back_to_alerts(self):
        import importlib, os
        from paper import engine
        os.environ["PAPER_NOTIFY"] = "loud"
        importlib.reload(engine)
        try:
            self.assertEqual(engine.NOTIFY, "alerts")
        finally:
            os.environ.pop("PAPER_NOTIFY", None)
            importlib.reload(engine)
