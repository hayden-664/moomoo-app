"""Journal invariants, including the concurrency bug that cost a fill.

No OpenD needed: the journal is pure SQLite.
"""

import os
import tempfile
import threading
import unittest

import paper
from paper import journal as J


class _FreshDB(unittest.TestCase):
    """Point the journal at a throwaway database for each test."""

    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self._orig_db, self._orig_dir = J._DB, J._DIR
        J._DB = os.path.join(self._dir, "journal.db")
        J._DIR = self._dir
        J._KILL_SENTINEL_orig = J._KILL_SENTINEL
        J._KILL_SENTINEL = os.path.join(self._dir, "KILLED")
        J.close()

    def tearDown(self):
        J.close()
        J._DB, J._DIR = self._orig_db, self._orig_dir
        J._KILL_SENTINEL = J._KILL_SENTINEL_orig

    def _order(self, order_id="o1", dealt=0.0):
        J.record_order({
            "order_id": order_id, "client_tag": f"tag-{order_id}",
            "trd_env": paper.PAPER_TRD_ENV, "acc_id": paper.PAPER_ACC_ID,
            "code": "US.AMD", "trd_side": "BUY", "qty": 2.0, "price": 100.0,
            "status": "SUBMITTED", "dealt_qty": dealt, "remark": f"pt-{order_id}",
        })


class TargetAccount(_FreshDB):
    def test_refuses_to_record_a_live_order(self):
        with self.assertRaises(paper.SimulationViolation):
            J.record_order({
                "order_id": "x", "client_tag": "y",
                "trd_env": "REAL", "acc_id": 286260077243931592,
            })

    def test_refuses_a_foreign_account_id(self):
        with self.assertRaises(paper.SimulationViolation):
            J.record_order({
                "order_id": "x", "client_tag": "y",
                "trd_env": paper.PAPER_TRD_ENV, "acc_id": 999,
            })


class Idempotency(_FreshDB):
    def test_client_tag_claimed_once(self):
        self.assertTrue(J.claim_client_tag("r:US.AMD:2026-01-01:1"))
        self.assertFalse(J.claim_client_tag("r:US.AMD:2026-01-01:1"))

    def test_fill_replay_is_dropped(self):
        f = {"deal_id": "d1", "order_id": "o1", "code": "US.AMD", "trd_side": "BUY",
             "qty": 1, "price": 1.0, "trd_env": paper.PAPER_TRD_ENV}
        self.assertTrue(J.record_fill(f))
        self.assertFalse(J.record_fill(f))


class OrderEvents(_FreshDB):
    def test_identical_push_is_not_relogged(self):
        self._order()
        push = {"order_status": "SUBMITTED", "dealt_qty": 0.0, "dealt_avg_price": 0.0}
        self.assertTrue(J.record_order_event("o1", "push", push)["changed"])
        self.assertFalse(J.record_order_event("o1", "push", push)["changed"])
        n = J._db().execute("SELECT COUNT(*) c FROM order_events").fetchone()["c"]
        self.assertEqual(n, 1)

    def test_fill_is_derived_from_dealt_qty_moving(self):
        self._order()
        out = J.record_order_event("o1", "push", {
            "order_status": "FILLED_ALL", "dealt_qty": 2.0, "dealt_avg_price": 101.5,
            "code": "US.AMD", "trd_side": "BUY", "trd_env": paper.PAPER_TRD_ENV,
        })
        self.assertTrue(out["changed"])
        self.assertIsNotNone(out["fill"])
        self.assertEqual(out["fill"]["qty"], 2.0)
        rows = J._db().execute("SELECT * FROM fills").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["derived"], 1, "a paper fill is reconstructed, not reported")

    def test_partial_then_full_books_each_increment_once(self):
        self._order()
        J.record_order_event("o1", "push", {
            "order_status": "FILLED_PART", "dealt_qty": 1.0, "dealt_avg_price": 100.0,
            "trd_env": paper.PAPER_TRD_ENV})
        J.record_order_event("o1", "push", {
            "order_status": "FILLED_ALL", "dealt_qty": 2.0, "dealt_avg_price": 100.5,
            "trd_env": paper.PAPER_TRD_ENV})
        qtys = sorted(r["qty"] for r in J._db().execute("SELECT qty FROM fills"))
        self.assertEqual(qtys, [1.0, 1.0], "each increment books once, no double count")

    def test_concurrent_duplicate_pushes_book_exactly_one_fill(self):
        """The regression.

        OpenD delivers the same push many times, concurrently. With the read and
        the write taking the lock separately, every copy saw the same "before",
        so all of them logged an event and none of them booked the fill.
        """
        self._order()
        push = {
            "order_status": "FILLED_ALL", "dealt_qty": 2.0, "dealt_avg_price": 101.5,
            "code": "US.AMD", "trd_side": "BUY", "trd_env": paper.PAPER_TRD_ENV,
        }
        barrier = threading.Barrier(8)
        results = []

        def hammer():
            barrier.wait()
            results.append(J.record_order_event("o1", "push", dict(push)))

        threads = [threading.Thread(target=hammer) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        events = J._db().execute("SELECT COUNT(*) c FROM order_events").fetchone()["c"]
        fills = J._db().execute("SELECT COUNT(*) c FROM fills").fetchone()["c"]
        total = J._db().execute("SELECT COALESCE(SUM(qty),0) s FROM fills").fetchone()["s"]
        self.assertEqual(events, 1, "one state change, one event")
        self.assertEqual(fills, 1, "one fill, not one per delivered copy")
        self.assertEqual(total, 2.0, "the shares that actually moved")
        self.assertEqual(sum(1 for r in results if r["changed"]), 1)


class KillSwitch(_FreshDB):
    def test_kill_and_resume(self):
        self.assertFalse(J.killed())
        J.kill("testing")
        self.assertTrue(J.killed())
        self.assertEqual(J.kill_reason(), "testing")
        J.resume()
        self.assertFalse(J.killed())

    def test_sentinel_alone_halts(self):
        with open(J._KILL_SENTINEL, "w") as fh:
            fh.write("halted from a shell\n")
        self.assertTrue(J.killed(), "a sentinel file must halt even with no DB row")


if __name__ == "__main__":
    unittest.main()


class TradeDate(_FreshDB):
    """The bug that silently disabled max_orders_per_day.

    Timestamps are stored in UTC; the machine's local date is UTC+8. Counting
    "orders today" by slicing the UTC string and comparing it against the local
    date matched nothing for most of the US session, so the cap never fired.
    """

    def test_trade_date_is_us_eastern(self):
        import datetime as dt
        from zoneinfo import ZoneInfo

        self.assertEqual(
            J.trade_date(),
            dt.datetime.now(ZoneInfo("America/New_York")).date().isoformat(),
        )

    def test_orders_today_counts_orders_placed_now(self):
        self.assertEqual(J.orders_today(), 0)
        self._order("o1")
        self.assertEqual(J.orders_today(), 1, "an order placed now must count today")
        self.assertEqual(J.orders_today("US.AMD"), 1)
        self.assertEqual(J.orders_today("US.NVDA"), 0)

    def test_next_seq_increments_within_the_session(self):
        self.assertEqual(J.next_seq("r1", "US.AMD"), 1)
        J.claim_client_tag("r1:US.AMD:x:1", rule_id="r1", code="US.AMD")
        self.assertEqual(
            J.next_seq("r1", "US.AMD"), 2,
            "a second order the same day needs a distinct tag, or the cap is stuck at 1",
        )

    def test_settle_claim_keeps_the_tag_reserved(self):
        tag = "r1:US.AMD:x:1"
        self.assertTrue(J.claim_client_tag(tag, decision_id="d1", rule_id="r1", code="US.AMD"))
        J.settle_claim(tag, {"decision_id": "d1", "cycle_id": "c1", "code": "US.AMD",
                             "rule_id": "r1", "kind": "ENTRY", "outcome": "PLACED"})
        self.assertFalse(J.claim_client_tag(tag), "the tag must stay spent after settling")
        row = J._db().execute("SELECT outcome FROM decisions WHERE client_tag=?", (tag,)).fetchone()
        self.assertEqual(row["outcome"], "PLACED")

    def test_release_frees_an_unsettled_claim(self):
        tag = "r1:US.AMD:x:9"
        self.assertTrue(J.claim_client_tag(tag))
        J.release_claim(tag)
        self.assertTrue(J.claim_client_tag(tag), "a failed placement must free its tag")


class Backfill(_FreshDB):
    def test_backfill_books_a_fill_a_dropped_push_lost(self):
        self._order("o9", dealt=0.0)
        J._db().execute("UPDATE orders SET dealt_qty=2.0, dealt_avg_price=100.0 WHERE order_id='o9'")
        J._db().commit()
        self.assertEqual(J.backfill_fills(), 1)
        rows = J._db().execute("SELECT qty, derived FROM fills").fetchall()
        self.assertEqual(rows[0]["qty"], 2.0)
        self.assertEqual(rows[0]["derived"], 1)

    def test_backfill_is_idempotent(self):
        self._order("o9", dealt=0.0)
        J._db().execute("UPDATE orders SET dealt_qty=2.0 WHERE order_id='o9'")
        J._db().commit()
        J.backfill_fills()
        self.assertEqual(J.backfill_fills(), 0, "running it again must not double-book")


class OrdersTodayBySide(_FreshDB):
    """The caps count entries. Counting exits against them trapped positions."""

    def _sell(self, order_id):
        J.record_order({
            "order_id": order_id, "client_tag": f"tag-{order_id}",
            "trd_env": paper.PAPER_TRD_ENV, "acc_id": paper.PAPER_ACC_ID,
            "code": "US.AMD", "trd_side": "SELL", "qty": 2.0, "price": 100.0,
            "status": "SUBMITTED", "dealt_qty": 0.0, "remark": f"pt-{order_id}",
        })

    def test_side_filter_separates_entries_from_exits(self):
        self._order("b1")
        self._sell("s1")
        self.assertEqual(J.orders_today(), 2)
        self.assertEqual(J.orders_today(side="BUY"), 1)
        self.assertEqual(J.orders_today(side="SELL"), 1)

    def test_side_filter_composes_with_the_symbol_filter(self):
        self._order("b1")
        self._sell("s1")
        self.assertEqual(J.orders_today("US.AMD", side="BUY"), 1)
        self.assertEqual(J.orders_today("US.NVDA", side="BUY"), 0)
