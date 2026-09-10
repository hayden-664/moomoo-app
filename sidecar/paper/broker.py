"""The only file in this repository permitted to name an order method.

``guard.assert_simulated`` enforces that: ``place_order``, ``modify_order``,
``change_order``, ``place_combo_order`` and ``cancel_all_order`` may appear
here and nowhere else under ``paper/``, and ``moomoo_client.py`` still refuses
to import if any of them appears in it.

The context is built with ``SecurityFirm.FUTUINC`` deliberately. ``get_acc_list``
keeps a live account only when its firm matches the context's, while simulated
accounts bypass that filter entirely (``open_trade_context.py:94``). The live
account on this login is FUTUMY, so it is not merely untargeted by this
context - it is invisible to it, and the push subscription, which takes the
whole account list, is scoped by the same fact.

A paper account reports no deal data at all: ``deal_list_query`` answers
"Paper trading does not support deal data" and no ``TradeDealHandlerBase`` push
ever arrives. Fills are therefore reconstructed from an order's ``dealt_qty``
moving between status pushes, and flagged ``derived`` in the journal so nothing
downstream mistakes them for broker-reported deals.

Fills arrive by push rather than polling. ``place_order`` already spends up to
three internal ``order_list_query`` calls to build the frame it returns, and
that query shares the 10-calls-per-30-seconds ceiling with the position and
account reads the dashboard is already polling - a ceiling documented in
AGENTS.md as failing silently. Push costs nothing and needs no unlocking.
"""

from __future__ import annotations

import fcntl
import logging
import os
import queue
import threading
import time
from typing import Any

import moomoo as mm
import pandas as pd

from config import OPEND_HOST, OPEND_PORT
from moomoo_client import MoomooError, require_opend

from .guard import (
    PAPER_ACC_ID,
    PAPER_SECURITY_FIRM,
    PAPER_TRD_ENV,
    PAPER_TRD_MARKET,
    verify_account_roster,
)
from . import journal

log = logging.getLogger(__name__)

# Push events land here from the SDK's callback thread and are drained by the
# engine's event loop. SimpleQueue is unbounded and lock-free for put(), which
# is what a handler that must never block needs.
events: "queue.SimpleQueue[tuple[str, dict]]" = queue.SimpleQueue()

_ROSTER_TTL = 60.0


def _rows(result: tuple[int, Any], what: str) -> list[dict]:
    ret, payload = result
    if ret != mm.RET_OK:
        raise MoomooError(f"{what} failed: {payload}")
    if isinstance(payload, pd.DataFrame):
        return payload.replace({float("nan"): None}).to_dict(orient="records")
    return payload


def _one(result: tuple[int, Any], what: str) -> dict:
    rows = _rows(result, what)
    if not rows:
        raise MoomooError(f"{what} returned no rows")
    return rows[0]


class _PaperOrderHandler(mm.TradeOrderHandlerBase):
    """Order status pushes.

    Runs on the SDK's callback executor thread. It may write SQLite and enqueue;
    it must not call the SDK, block, or raise - an exception here can kill the
    executor and end every push silently, which would look exactly like a quiet
    market.
    """

    def on_recv_rsp(self, rsp_pb):
        ret, data = super().on_recv_rsp(rsp_pb)
        try:
            if ret == mm.RET_OK and isinstance(data, pd.DataFrame):
                for row in data.replace({float("nan"): None}).to_dict(orient="records"):
                    _absorb_order(row)
        except Exception:  # noqa: BLE001 - a raising handler stops all pushes
            log.warning("order push handler failed", exc_info=True)
        return ret, data


class _PaperDealHandler(mm.TradeDealHandlerBase):
    """Fill pushes. Same thread rules as the order handler."""

    def on_recv_rsp(self, rsp_pb):
        ret, data = super().on_recv_rsp(rsp_pb)
        try:
            if ret == mm.RET_OK and isinstance(data, pd.DataFrame):
                for row in data.replace({float("nan"): None}).to_dict(orient="records"):
                    _absorb_deal(row)
        except Exception:  # noqa: BLE001
            log.warning("deal push handler failed", exc_info=True)
        return ret, data


def _absorb_order(row: dict) -> None:
    """Attribute an order push, record the state change, and derive any fill.

    Pushes carry ``trd_env`` and ``remark`` but no ``acc_id``, so attribution is
    the environment plus a remark matching a tag we journalled when placing.
    Anything matching neither is recorded and reported, never acted on.

    Fills are derived here rather than read from deal pushes: a paper account
    answers ``deal_list_query`` with "Paper trading does not support deal data"
    and sends no deal push at all, so an order's ``dealt_qty`` moving is the
    only evidence a fill happened.
    """
    if row.get("trd_env") != PAPER_TRD_ENV:
        return
    order_id = str(row.get("order_id") or "")
    remark = row.get("remark") or ""
    known = journal.order_by_remark(remark) if remark else None
    if not known:
        journal.record_orphan("order", row)
        return

    outcome = journal.record_order_event(order_id, "push", row)
    if not outcome["changed"]:
        return  # a duplicate delivery, not a new state
    events.put(("order", row))


def _absorb_deal(row: dict) -> None:
    if row.get("trd_env") != PAPER_TRD_ENV:
        return
    if journal.record_fill(row):
        events.put(("deal", row))


class PaperContextBusy(RuntimeError):
    """Another process already holds the paper trade context."""


_LOCK_PATH = os.path.join(journal.cache_dir(), "context.lock")
_lock_fh = None


def _claim_single_instance() -> bool:
    """Take an exclusive advisory lock for the life of this process.

    OpenD delivers an order push to every connected context that can see the
    account, so two processes holding a PaperBroker double every event and race
    each other on the journal. That is not hypothetical: `npm run dev` starts a
    sidecar and so does the launchd agent, and on 2026-09-10 both were live for
    fourteen hours. The second one cannot even bind the port - uvicorn runs the
    lifespan before the bind fails, so it opens a paper context and runs a full
    engine loop anyway, invisibly.

    An advisory lock is the right shape because the kernel releases it when the
    process dies, however it dies. A pidfile would need cleanup that a SIGKILL
    never gets to run.
    """
    global _lock_fh
    if _lock_fh is not None:
        return True
    # "a+", not "w": "w" truncates on open, so a process that is about to be
    # refused would first erase the pid of the holder it is being refused by,
    # and the error message could not name it.
    fh = open(_LOCK_PATH, "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return False
    fh.seek(0)
    fh.truncate()
    fh.write(f"{os.getpid()}\n")
    fh.flush()
    _lock_fh = fh
    return True


def holder_pid() -> str | None:
    """Which process owns the paper context, for an error message worth reading."""
    try:
        with open(_LOCK_PATH) as fh:
            return fh.read().strip() or None
    except OSError:
        return None


class PaperBroker:
    """Simulated-account trade context. Every call names its target explicitly."""

    def __init__(self, host: str = OPEND_HOST, port: int = OPEND_PORT) -> None:
        self._host = host
        self._port = port
        self._lock = threading.Lock()
        self._ctx: mm.OpenSecTradeContext | None = None
        self._roster: list[dict] | None = None
        self._roster_read_at = 0.0
        self._roster_at = 0.0

    # -- connection ------------------------------------------------------------
    @property
    def ctx(self) -> mm.OpenSecTradeContext:
        with self._lock:
            if self._ctx is None:
                require_opend()
                if not _claim_single_instance():
                    raise PaperContextBusy(
                        f"another process (pid {holder_pid()}) already holds the "
                        f"paper trade context; only one may, or every order push "
                        f"is delivered twice. Stop the duplicate sidecar."
                    )
                log.info(
                    "opening paper trade context -> %s:%s (acc %s)",
                    self._host, self._port, PAPER_ACC_ID,
                )
                ctx = mm.OpenSecTradeContext(
                    filter_trdmarket=PAPER_TRD_MARKET,
                    host=self._host,
                    port=self._port,
                    security_firm=PAPER_SECURITY_FIRM,
                )
                ctx.set_handler(_PaperOrderHandler())
                ctx.set_handler(_PaperDealHandler())
                self._ctx = ctx
            return self._ctx

    def roster(self, max_age: float = _ROSTER_TTL) -> list[dict]:
        """Account list, memoised.

        Not just to save a round trip: ``get_acc_list`` re-subscribes the push
        feed for every account it returns, so calling it freely multiplies the
        number of copies of each order push OpenD delivers.
        """
        now = time.monotonic()
        if self._roster is not None and now - self._roster_read_at < max_age:
            return self._roster
        rows = _rows(self.ctx.get_acc_list(), "get_acc_list")
        self._roster = rows
        self._roster_read_at = now
        return rows

    def _assert_target(self, force: bool = False) -> None:
        """Re-verify the account list before writing.

        Static analysis cannot catch moomoo re-issuing an account id or OpenD
        being pointed at a different login; this can. Memoised so a burst of
        orders does not spend a round trip each.
        """
        now = time.monotonic()
        if not force and now - self._roster_at < _ROSTER_TTL:
            return
        verify_account_roster(self.roster(max_age=0.0 if force else _ROSTER_TTL))
        self._roster_at = now

    def close(self) -> None:
        with self._lock:
            if self._ctx is not None:
                try:
                    self._ctx.close()
                except Exception:  # noqa: BLE001 - best effort on shutdown
                    log.warning("error closing paper context", exc_info=True)
                self._ctx = None

    # -- reads -----------------------------------------------------------------
    def account(self) -> dict:
        return _one(
            self.ctx.accinfo_query(
                trd_env=PAPER_TRD_ENV,
                acc_id=PAPER_ACC_ID,
                currency=mm.Currency.USD,
                refresh_cache=True,
            ),
            "paper accinfo_query",
        )

    def positions(self) -> list[dict]:
        return _rows(
            self.ctx.position_list_query(trd_env=PAPER_TRD_ENV, acc_id=PAPER_ACC_ID),
            "paper position_list_query",
        )

    def max_buy_sell(self, code: str, price: float) -> dict:
        """Broker's own view of what this account can afford."""
        return _one(
            self.ctx.acctradinginfo_query(
                order_type=mm.OrderType.NORMAL,
                code=code,
                price=float(price),
                trd_env=PAPER_TRD_ENV,
                acc_id=PAPER_ACC_ID,
            ),
            f"paper acctradinginfo_query {code}",
        )

    def resting_orders(self) -> list[dict]:
        """Non-terminal orders, straight from the broker. One call, for reconciliation."""
        return _rows(
            self.ctx.order_list_query(
                status_filter_list=[
                    mm.OrderStatus.WAITING_SUBMIT,
                    mm.OrderStatus.SUBMITTING,
                    mm.OrderStatus.SUBMITTED,
                    mm.OrderStatus.FILLED_PART,
                ],
                trd_env=PAPER_TRD_ENV,
                acc_id=PAPER_ACC_ID,
            ),
            "paper order_list_query",
        )

    # -- writes ----------------------------------------------------------------
    def place(
        self,
        *,
        code: str,
        side: str,
        qty: float,
        price: float,
        remark: str,
        time_in_force: str = "DAY",
        session: str = "RTH",
    ) -> dict:
        """Place one limit order on the simulated account.

        A limit rather than a market order: the engine computes the limit from a
        bar close plus an offset it can journal, so a bad quote produces a
        rejected order rather than an arbitrary fill.
        """
        self._assert_target()
        row = _one(
            self.ctx.place_order(
                price=float(price),
                qty=float(qty),
                code=code,
                trd_side=getattr(mm.TrdSide, side),
                order_type=mm.OrderType.NORMAL,
                trd_env=PAPER_TRD_ENV,
                acc_id=PAPER_ACC_ID,
                remark=remark,
                time_in_force=getattr(mm.TimeInForce, time_in_force),
                session=getattr(mm.Session, session),
            ),
            f"place {side} {qty} {code}",
        )
        row["trd_env"] = PAPER_TRD_ENV
        row["acc_id"] = PAPER_ACC_ID
        return row

    def cancel(self, order_id: str) -> dict:
        """Cancel one order.

        Per order rather than a blanket cancel: the blanket call is scoped by
        market and produces no per-order accounting, and an explicit cancel
        raises a push, so our record and the broker's converge on an event
        rather than on an assumption.
        """
        self._assert_target()
        return _one(
            self.ctx.modify_order(
                modify_order_op=mm.ModifyOrderOp.CANCEL,
                order_id=order_id,
                qty=0,
                price=0,
                trd_env=PAPER_TRD_ENV,
                acc_id=PAPER_ACC_ID,
            ),
            f"cancel {order_id}",
        )


broker = PaperBroker()
