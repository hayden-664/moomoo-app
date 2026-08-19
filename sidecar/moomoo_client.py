"""Thin, read-only wrapper around the moomoo Python SDK.

Design notes
------------
* The SDK contexts are expensive to build and hold a TCP connection to OpenD,
  so they are created once and reused for the process lifetime.
* Every SDK call returns ``(ret_code, payload)``. ``payload`` is a DataFrame on
  success and an error string on failure. ``_unwrap`` normalises that into
  either a list-of-dicts or a raised ``MoomooError``.
* Nothing here can place, modify or cancel an order. ``assert_read_only`` is
  executed at import time and fails the process if a write-capable method ever
  gets referenced from this module.
"""

from __future__ import annotations

import datetime as dt
import logging
import socket
import threading
import time
from typing import Any

import moomoo as mm
import pandas as pd

from config import (
    ACC_ID,
    ACC_INDEX,
    CURRENCY,
    OPEND_HOST,
    OPEND_PORT,
    SECURITY_FIRM,
    TRD_ENV,
    TRD_MARKET,
)

log = logging.getLogger(__name__)

# The SDK logs every connect/disconnect at INFO to the root logger, which
# drowns the sidecar's own output. Keep its warnings, drop the chatter.
logging.getLogger("moomoo").setLevel(logging.WARNING)


def _acc_kwargs() -> dict:
    """Account selector shared by every trade-context read.

    ``acc_id`` wins when set; the SDK ignores ``acc_index`` in that case.
    """
    return {"acc_id": ACC_ID} if ACC_ID else {"acc_index": ACC_INDEX}


class MoomooError(RuntimeError):
    """An SDK call returned a non-OK return code."""


class OpendUnreachable(MoomooError):
    """OpenD is not accepting connections on its configured port."""


def opend_reachable(timeout: float = 1.5) -> bool:
    """Fail-fast TCP probe.

    The SDK retries a refused connection every 6s forever and blocks the
    calling thread while it does, so every entry point probes the port first
    and raises instead of hanging when OpenD is not up.
    """
    try:
        with socket.create_connection((OPEND_HOST, OPEND_PORT), timeout=timeout):
            return True
    except OSError:
        return False


def require_opend() -> None:
    if not opend_reachable():
        raise OpendUnreachable(
            f"OpenD is not listening on {OPEND_HOST}:{OPEND_PORT}. "
            "Start OpenD and log in, then retry."
        )


# Methods that can move money or create orders. This module must never call
# them; the guard below turns an accidental future edit into a startup crash
# rather than a live trade.
_FORBIDDEN = (
    "place_order",
    "modify_order",
    "cancel_all_order",
    "unlock_trade",
)


def assert_read_only() -> None:
    source = __file__
    with open(source, "r", encoding="utf-8") as fh:
        body = fh.read()
    # Only inspect executable lines so the docstring/tuple above don't trip it.
    code_lines = [
        ln
        for ln in body.splitlines()
        if not ln.strip().startswith("#") and "_FORBIDDEN" not in ln
    ]
    code = "\n".join(code_lines)
    for name in _FORBIDDEN:
        if f".{name}(" in code:
            raise RuntimeError(
                f"read-only violation: {source} references trade method {name!r}"
            )


class MoomooClient:
    """Lazily-connected, thread-safe holder for the two SDK contexts."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._quote: mm.OpenQuoteContext | None = None
        self._trade: mm.OpenSecTradeContext | None = None
        self._history_cache: dict[tuple, tuple[float, list[dict]]] = {}

    # -- connection ---------------------------------------------------------
    @property
    def quote(self) -> mm.OpenQuoteContext:
        with self._lock:
            if self._quote is None:
                require_opend()
                log.info("opening quote context -> %s:%s", OPEND_HOST, OPEND_PORT)
                self._quote = mm.OpenQuoteContext(host=OPEND_HOST, port=OPEND_PORT)
            return self._quote

    @property
    def trade(self) -> mm.OpenSecTradeContext:
        with self._lock:
            if self._trade is None:
                require_opend()
                log.info("opening trade context -> %s:%s", OPEND_HOST, OPEND_PORT)
                self._trade = mm.OpenSecTradeContext(
                    filter_trdmarket=getattr(mm.TrdMarket, TRD_MARKET),
                    host=OPEND_HOST,
                    port=OPEND_PORT,
                    security_firm=getattr(mm.SecurityFirm, SECURITY_FIRM),
                )
            return self._trade

    def close(self) -> None:
        with self._lock:
            for ctx in (self._quote, self._trade):
                if ctx is not None:
                    try:
                        ctx.close()
                    except Exception:  # noqa: BLE001 - best effort on shutdown
                        log.warning("error closing context", exc_info=True)
            self._quote = None
            self._trade = None

    # -- helpers ------------------------------------------------------------
    @staticmethod
    def _unwrap(result: tuple[int, Any], what: str) -> list[dict]:
        ret, payload = result
        if ret != mm.RET_OK:
            raise MoomooError(f"{what} failed: {payload}")
        if isinstance(payload, pd.DataFrame):
            # NaN is not valid JSON; None round-trips cleanly to null.
            return payload.replace({float("nan"): None}).to_dict(orient="records")
        return payload

    # -- reads --------------------------------------------------------------
    def user_info(self) -> dict:
        """Quote entitlements. Option chains fail with a permission error when
        the matching *_option_qot_right is NO, which is a subscription state
        rather than anything the code can work around."""
        # Unlike most calls, get_user_info yields a plain dict rather than a
        # DataFrame, so it can come back either shape depending on SDK version.
        rows = self._unwrap(self.quote.get_user_info(), "get_user_info")
        if isinstance(rows, dict):
            return rows
        return rows[0] if rows else {}

    def account_list(self) -> list[dict]:
        """All sub-accounts visible to this OpenD session.

        Used to discover the correct MOOMOO_SECURITY_FIRM / MOOMOO_ACC_INDEX
        rather than guessing which moomoo entity the account sits under.
        """
        return self._unwrap(self.trade.get_acc_list(), "get_acc_list")

    def account_info(self) -> dict:
        rows = self._unwrap(
            self.trade.accinfo_query(
                trd_env=TRD_ENV,
                **_acc_kwargs(),
                refresh_cache=True,
                currency=CURRENCY,
            ),
            "accinfo_query",
        )
        return rows[0] if rows else {}

    def positions(self) -> list[dict]:
        return self._unwrap(
            self.trade.position_list_query(
                trd_env=TRD_ENV,
                **_acc_kwargs(),
                refresh_cache=True,
                currency=CURRENCY,
            ),
            "position_list_query",
        )

    # Deal history is rate-limited to 10 calls per 30s and only changes when a
    # trade fills, so it is cached rather than re-fetched on every dashboard
    # poll. On failure the last good value is served instead of an empty list,
    # so a transient rate-limit cannot silently zero out closed P&L.
    _HISTORY_TTL = 300.0

    def _cached_history(self, key: tuple, fetch) -> list[dict]:
        now = time.monotonic()
        hit = self._history_cache.get(key)
        if hit and now - hit[0] < self._HISTORY_TTL:
            return hit[1]
        try:
            rows = fetch()
        except MoomooError:
            if hit:
                log.warning("deal history fetch failed; serving cached result")
                return hit[1]
            raise
        self._history_cache[key] = (now, rows)
        return rows

    # The API rejects any window wider than 360 days, so longer lookbacks are
    # split into chunks and concatenated rather than failing outright.
    _MAX_WINDOW_DAYS = 359

    def deal_history(self, start: str, end: str) -> list[dict]:
        return self._cached_history((start, end), lambda: self._deal_history(start, end))

    def _deal_history(self, start: str, end: str) -> list[dict]:
        start_d = dt.date.fromisoformat(start)
        end_d = dt.date.fromisoformat(end)
        out: list[dict] = []
        seen: set = set()
        cursor = start_d
        while cursor <= end_d:
            chunk_end = min(cursor + dt.timedelta(days=self._MAX_WINDOW_DAYS), end_d)
            rows = self._unwrap(
                self.trade.history_deal_list_query(
                    start=cursor.isoformat(),
                    end=chunk_end.isoformat(),
                    trd_env=TRD_ENV,
                    **_acc_kwargs(),
                ),
                f"history_deal_list_query({cursor}..{chunk_end})",
            )
            # Chunk boundaries are inclusive on both ends, so a deal can be
            # returned twice; dedupe on deal_id before it reaches the P&L maths.
            for r in rows:
                key = r.get("deal_id") or (
                    r.get("code"), r.get("create_time"), r.get("qty"), r.get("price")
                )
                if key not in seen:
                    seen.add(key)
                    out.append(r)
            cursor = chunk_end + dt.timedelta(days=1)
        return out

    def option_chain(self, code: str, start: str, end: str, **kwargs) -> list[dict]:
        return self._unwrap(
            self.quote.get_option_chain(code=code, start=start, end=end, **kwargs),
            f"get_option_chain({code})",
        )

    def snapshot(self, codes: list[str]) -> list[dict]:
        """Snapshot in batches; OpenD caps a single request at 400 codes."""
        out: list[dict] = []
        for i in range(0, len(codes), 400):
            out.extend(
                self._unwrap(
                    self.quote.get_market_snapshot(codes[i : i + 400]),
                    "get_market_snapshot",
                )
            )
        return out


assert_read_only()
client = MoomooClient()
