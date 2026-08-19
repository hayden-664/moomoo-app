"""Bar cache for the position charts.

Why a cache at all
------------------
``request_history_kline`` is metered per symbol per period, and the dashboard
polls. Without a cache, leaving a chart open would re-pull on every tick.

Why full re-fetch instead of incremental append
-----------------------------------------------
Forward-adjusted prices (``AuType.QFQ``) are rewritten retroactively when a
split or dividend lands, so an appended series silently disagrees with itself
across the corporate action. Re-fetching the whole window avoids that entirely,
and it is free: the quota charge is per *symbol* per period, so a code already
pulled this period costs nothing to pull again. Cheap correctness beats clever
incrementalism here.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
import threading
import time

from moomoo_client import client

log = logging.getLogger(__name__)

_DIR = os.path.join(os.path.dirname(__file__), ".cache")
_DB = os.path.join(_DIR, "klines.db")

# Daily bars only change once a day, but the last bar moves during the session.
# 30 minutes keeps an open chart off the API without going visibly stale.
_TTL = float(os.getenv("KLINE_TTL_SECONDS", "1800"))

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bars (
  code     TEXT NOT NULL,
  ktype    TEXT NOT NULL,
  time_key TEXT NOT NULL,
  open REAL, high REAL, low REAL, close REAL,
  volume INTEGER, turnover REAL,
  PRIMARY KEY (code, ktype, time_key)
);
CREATE TABLE IF NOT EXISTS series_meta (
  code TEXT NOT NULL,
  ktype TEXT NOT NULL,
  start TEXT, end TEXT,
  fetched_at REAL,
  PRIMARY KEY (code, ktype)
);
"""


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        os.makedirs(_DIR, exist_ok=True)
        # FastAPI serves each request on a worker thread; the module-level lock
        # serialises access rather than opening a connection per request.
        _conn = sqlite3.connect(_DB, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(_SCHEMA)
        _conn.commit()
    return _conn


def _read(code: str, ktype: str, start: str, end: str) -> list[dict]:
    rows = _db().execute(
        "SELECT time_key, open, high, low, close, volume FROM bars "
        "WHERE code=? AND ktype=? AND time_key>=? AND time_key<=? ORDER BY time_key",
        (code, ktype, start, end + "￿"),
    ).fetchall()
    return [
        {
            "time": r["time_key"][:10],
            "open": r["open"],
            "high": r["high"],
            "low": r["low"],
            "close": r["close"],
            "volume": r["volume"],
        }
        for r in rows
    ]


def _fresh(code: str, ktype: str, start: str, end: str) -> bool:
    row = _db().execute(
        "SELECT start, end, fetched_at FROM series_meta WHERE code=? AND ktype=?",
        (code, ktype),
    ).fetchone()
    if not row:
        return False
    covers = (row["start"] or "9999") <= start and (row["end"] or "") >= end
    return covers and (time.time() - (row["fetched_at"] or 0)) < _TTL


def _write(code: str, ktype: str, start: str, end: str, rows: list[dict]) -> None:
    db = _db()
    db.executemany(
        "INSERT OR REPLACE INTO bars "
        "(code, ktype, time_key, open, high, low, close, volume, turnover) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        [
            (
                code,
                ktype,
                str(r.get("time_key")),
                r.get("open"),
                r.get("high"),
                r.get("low"),
                r.get("close"),
                r.get("volume"),
                r.get("turnover"),
            )
            for r in rows
        ],
    )
    db.execute(
        "INSERT OR REPLACE INTO series_meta (code, ktype, start, end, fetched_at) "
        "VALUES (?,?,?,?,?)",
        (code, ktype, start, end, time.time()),
    )
    db.commit()


def candles(code: str, days: int = 365, ktype: str = "K_DAY") -> dict:
    """Bars for ``code``, served from cache when it is warm enough."""
    today = dt.date.today()
    start = (today - dt.timedelta(days=days)).isoformat()
    end = today.isoformat()

    with _lock:
        if _fresh(code, ktype, start, end):
            return {
                "code": code,
                "ktype": ktype,
                "source": "cache",
                "bars": _read(code, ktype, start, end),
            }

    # Fetched outside the lock: the SDK call is slow and blocking, and holding
    # the DB lock across it would stall every other chart request.
    rows = client.history_kline(code, start=start, end=end, ktype=ktype)
    with _lock:
        _write(code, ktype, start, end, rows)
        bars = _read(code, ktype, start, end)
    return {"code": code, "ktype": ktype, "source": "api", "bars": bars}


def markers(code: str, days: int = 365) -> list[dict]:
    """Your own fills for ``code``, collapsed to one marker per day and side.

    A single order often fills in several prints at the same price and second.
    Charting libraries allow one marker per timestamp per series, so same-day
    fills on the same side are merged at their quantity-weighted price.
    """
    today = dt.date.today()
    start = (today - dt.timedelta(days=days)).isoformat()
    deals = client.deal_history(start, today.isoformat())

    agg: dict[tuple[str, str], dict] = {}
    for d in deals:
        if d.get("code") != code:
            continue
        day = str(d.get("create_time", ""))[:10]
        side = d.get("trd_side")
        if not day or side not in ("BUY", "SELL"):
            continue
        slot = agg.setdefault((day, side), {"qty": 0.0, "notional": 0.0})
        qty = float(d.get("qty") or 0)
        slot["qty"] += qty
        slot["notional"] += qty * float(d.get("price") or 0)

    out = [
        {
            "time": day,
            "side": side,
            "qty": round(v["qty"], 4),
            "price": round(v["notional"] / v["qty"], 4) if v["qty"] else None,
        }
        for (day, side), v in sorted(agg.items())
        if v["qty"]
    ]
    return out


def quota() -> dict:
    kl = client.kline_quota()
    try:
        sub = client.subscription_quota()
    except Exception:  # noqa: BLE001 - subscription quota is informational only
        sub = None
    return {"kline": kl, "subscription": sub}
