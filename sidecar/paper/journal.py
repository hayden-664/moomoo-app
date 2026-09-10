"""Durable record of every decision, order and fill the paper engine makes.

Why SQLite rather than a JSONL append log
-----------------------------------------
Duplicate orders are the expensive failure here, and suppressing them needs a
``UNIQUE`` constraint. In a JSONL file that check is a full scan plus a race
between reading and appending; three threads write to this store - the SDK
push handlers, the FastAPI workers and the engine loop - so that race is real
rather than theoretical. The queries the guardrails ask ("orders today", "last
fill for this rule and symbol", "open notional") are also relational. JSONL's
one advantage is that ``tail -f`` reads it, and ``GET /paper/journal`` gives
that back.

WAL mode, unlike ``klines.py``, because a push handler writes while a request
thread reads. klines has a single logical writer and does not need it.

Every write asserts its target account. A real order cannot be recorded here
even if one somehow existed, which is what makes the verification query
``SELECT DISTINCT trd_env, acc_id FROM orders`` meaningful rather than
circular.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import datetime as dt
from zoneinfo import ZoneInfo

from .guard import PAPER_ACC_ID, PAPER_TRD_ENV, SimulationViolation

log = logging.getLogger(__name__)

_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".cache", "paper")
_DB = os.path.join(_DIR, "journal.db")
_KILL_SENTINEL = os.path.join(_DIR, "KILLED")

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cycles (
  cycle_id TEXT PRIMARY KEY,
  started_at TEXT NOT NULL, finished_at TEXT,
  rules_sha TEXT, market_state TEXT,
  equity_real REAL, equity_real_usd_only REAL,
  fx_rates_json TEXT, fx_source TEXT,
  paper_total_assets REAL, paper_cash REAL, paper_power REAL,
  universe_json TEXT, blocked_by TEXT, error TEXT,
  -- Whether new entries were evaluated this cycle, and why not if they were
  -- not. Exits and the pre-close cancel run regardless.
  entry_window TEXT
);

CREATE TABLE IF NOT EXISTS decisions (
  decision_id TEXT PRIMARY KEY,
  cycle_id TEXT NOT NULL,
  ts TEXT NOT NULL,
  code TEXT NOT NULL,
  rule_id TEXT NOT NULL,
  rules_sha TEXT,
  kind TEXT NOT NULL,
  outcome TEXT NOT NULL,
  block_reason TEXT, block_detail TEXT,
  trace_json TEXT, tech_json TEXT, bar_as_of TEXT,
  side TEXT, qty REAL, limit_price REAL, ref_price REAL,
  atr14 REAL, stop_price REAL, risk_usd REAL,
  qty_intended REAL, qty_affordable REAL, paper_max_cash_buy REAL,
  -- Estimated, never broker-reported: a paper account cannot be charged, and
  -- order_fee_query refuses simulated orders outright.
  est_fee_round_trip REAL, est_fee_pct REAL, breakeven_price REAL,
  scaled INTEGER NOT NULL DEFAULT 0,
  client_tag TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS decisions_by_day ON decisions(ts, code, rule_id);

CREATE TABLE IF NOT EXISTS orders (
  order_id TEXT PRIMARY KEY,
  client_tag TEXT UNIQUE NOT NULL,
  decision_id TEXT,
  trd_env TEXT NOT NULL,
  acc_id TEXT NOT NULL,
  code TEXT, trd_side TEXT, order_type TEXT,
  qty REAL, price REAL, currency TEXT, time_in_force TEXT, session TEXT,
  status TEXT, dealt_qty REAL, dealt_avg_price REAL, last_err_msg TEXT,
  remark TEXT,
  created_at TEXT, updated_at TEXT, terminal_at TEXT
);
CREATE INDEX IF NOT EXISTS orders_by_day ON orders(created_at);
CREATE INDEX IF NOT EXISTS orders_by_remark ON orders(remark);

CREATE TABLE IF NOT EXISTS order_events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id TEXT NOT NULL, ts TEXT NOT NULL, source TEXT NOT NULL,
  status TEXT, dealt_qty REAL, dealt_avg_price REAL, last_err_msg TEXT,
  raw_json TEXT
);
CREATE INDEX IF NOT EXISTS order_events_by_order ON order_events(order_id, seq);

CREATE TABLE IF NOT EXISTS fills (
  deal_id TEXT PRIMARY KEY,
  order_id TEXT, code TEXT, trd_side TEXT,
  qty REAL, price REAL, create_time TEXT,
  trd_env TEXT NOT NULL,
  -- Paper accounts return "Paper trading does not support deal data", so every
  -- fill here is reconstructed from an order's dealt_qty moving. Flagged, not
  -- presented as broker-reported: same provenance discipline as derived P&L.
  derived INTEGER NOT NULL DEFAULT 0,
  notified INTEGER NOT NULL DEFAULT 0,
  recorded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orphans (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL, kind TEXT NOT NULL, raw_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS engine_state (
  key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL
);
"""

# Statuses that mean the broker is done with an order.
TERMINAL = frozenset(
    {
        "FILLED_ALL",
        "CANCELLED_ALL",
        "CANCELLED_PART",
        "SUBMIT_FAILED",
        "FAILED",
        "DELETED",
        "DISABLED",
        "FILL_CANCELLED",
    }
)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


# The engine trades US equities, so "today" is the US/Eastern session date.
# Not UTC (which rolls at 20:00 ET, mid-session) and not the machine's local
# date (UTC+8 here, a day ahead for most of the session). Getting this wrong is
# not cosmetic: order caps are counted per trading day, and comparing a local
# date against a UTC timestamp silently matched nothing, so max_orders_per_day
# never fired however it was configured.
_ET = ZoneInfo("America/New_York")


def cache_dir() -> str:
    """The paper cache directory, created on demand.

    Exposed so the context lock lives beside the journal rather than duplicating
    the path, and so a test that rebinds `_DIR` moves both together.
    """
    os.makedirs(_DIR, exist_ok=True)
    return _DIR


def trade_date() -> str:
    return dt.datetime.now(_ET).date().isoformat()


def today() -> str:
    """Deprecated alias; kept so callers cannot silently get the wrong day."""
    return trade_date()


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        os.makedirs(_DIR, exist_ok=True)
        _conn = sqlite3.connect(_DB, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        # A push handler writes while a request thread reads; WAL lets both
        # proceed instead of the reader blocking behind the writer.
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.executescript(_SCHEMA)
        _migrate(_conn)
        _conn.commit()
    return _conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive column migrations; CREATE TABLE IF NOT EXISTS cannot do these."""
    have = {r["name"] for r in conn.execute("PRAGMA table_info(fills)")}
    if "derived" not in have:
        conn.execute("ALTER TABLE fills ADD COLUMN derived INTEGER NOT NULL DEFAULT 0")
    ccols = {r["name"] for r in conn.execute("PRAGMA table_info(cycles)")}
    if "entry_window" not in ccols:
        conn.execute("ALTER TABLE cycles ADD COLUMN entry_window TEXT")
    dcols = {r["name"] for r in conn.execute("PRAGMA table_info(decisions)")}
    for col in ("est_fee_round_trip", "est_fee_pct", "breakeven_price"):
        if col not in dcols:
            conn.execute(f"ALTER TABLE decisions ADD COLUMN {col} REAL")
    for table in ("decisions", "orders"):
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if "trade_date" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN trade_date TEXT")
            # Existing rows predate the column; the UTC date is the best available
            # approximation and these are all from before the caps worked anyway.
            stamp = "ts" if table == "decisions" else "created_at"
            conn.execute(
                f"UPDATE {table} SET trade_date=substr({stamp},1,10) WHERE trade_date IS NULL"
            )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS decisions_by_trade_date"
        " ON decisions(trade_date, code, rule_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS orders_by_trade_date ON orders(trade_date, code)"
    )


def _assert_target(trd_env: str, acc_id) -> None:
    """Refuse to record anything that is not the paper account."""
    if trd_env != PAPER_TRD_ENV or int(acc_id) != PAPER_ACC_ID:
        raise SimulationViolation(
            f"refusing to journal an order for trd_env={trd_env!r} acc_id={acc_id!r}; "
            f"this store records {PAPER_TRD_ENV} account {PAPER_ACC_ID} only"
        )


def _j(value) -> str | None:
    return None if value is None else json.dumps(value, default=str)


def _rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


# -- cycles --------------------------------------------------------------------
def start_cycle(cycle_id: str, **fields) -> None:
    with _lock:
        db = _db()
        db.execute(
            "INSERT OR REPLACE INTO cycles (cycle_id, started_at, rules_sha, market_state,"
            " equity_real, equity_real_usd_only, fx_rates_json, fx_source,"
            " paper_total_assets, paper_cash, paper_power, universe_json, blocked_by, error)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                cycle_id, _now(), fields.get("rules_sha"), fields.get("market_state"),
                fields.get("equity_real"), fields.get("equity_real_usd_only"),
                _j(fields.get("fx_rates")), fields.get("fx_source"),
                fields.get("paper_total_assets"), fields.get("paper_cash"),
                fields.get("paper_power"), _j(fields.get("universe")),
                fields.get("blocked_by"), fields.get("error"),
            ),
        )
        db.commit()


def finish_cycle(cycle_id: str, **fields) -> None:
    sets, vals = ["finished_at=?"], [_now()]
    for col, key in (
        ("market_state", "market_state"), ("equity_real", "equity_real"),
        ("equity_real_usd_only", "equity_real_usd_only"), ("fx_source", "fx_source"),
        ("paper_total_assets", "paper_total_assets"), ("paper_cash", "paper_cash"),
        ("paper_power", "paper_power"), ("blocked_by", "blocked_by"), ("error", "error"),
        ("entry_window", "entry_window"),
    ):
        if key in fields:
            sets.append(f"{col}=?")
            vals.append(fields[key])
    for col, key in (("fx_rates_json", "fx_rates"), ("universe_json", "universe")):
        if key in fields:
            sets.append(f"{col}=?")
            vals.append(_j(fields[key]))
    vals.append(cycle_id)
    with _lock:
        db = _db()
        db.execute(f"UPDATE cycles SET {', '.join(sets)} WHERE cycle_id=?", vals)
        db.commit()


def last_cycle() -> dict | None:
    with _lock:
        rows = _rows(
            _db().execute("SELECT * FROM cycles ORDER BY started_at DESC LIMIT 1")
        )
    return rows[0] if rows else None


# -- decisions -----------------------------------------------------------------
def record_decision(decision: dict) -> None:
    cols = (
        "decision_id", "cycle_id", "ts", "code", "rule_id", "rules_sha", "kind",
        "outcome", "block_reason", "block_detail", "trace_json", "tech_json",
        "bar_as_of", "side", "qty", "limit_price", "ref_price", "atr14",
        "stop_price", "risk_usd", "qty_intended", "qty_affordable",
        "paper_max_cash_buy", "est_fee_round_trip", "est_fee_pct", "breakeven_price", "scaled", "client_tag", "trade_date",
    )
    row = dict(decision)
    row.setdefault("ts", _now())
    row.setdefault("trade_date", trade_date())
    row["trace_json"] = _j(row.pop("trace", None))
    row["tech_json"] = _j(row.pop("tech", None))
    row["scaled"] = int(bool(row.get("scaled")))
    with _lock:
        db = _db()
        db.execute(
            f"INSERT INTO decisions ({','.join(cols)}) "
            f"VALUES ({','.join('?' * len(cols))})",
            tuple(row.get(c) for c in cols),
        )
        db.commit()


def next_seq(rule_id: str, code: str) -> int:
    """Next order sequence for this rule and symbol today.

    Counted from the journal rather than from a per-cycle counter. A per-cycle
    counter restarts at 1 every few minutes, so the second order of the day on
    the same rule and symbol would reuse the first one's tag and be rejected as
    a duplicate - which silently capped max_orders_per_symbol_per_day at 1
    however it was configured.
    """
    with _lock:
        row = _db().execute(
            "SELECT COUNT(*) c FROM decisions WHERE rule_id=? AND code=?"
            " AND trade_date=? AND client_tag IS NOT NULL",
            (rule_id, code, trade_date()),
        ).fetchone()
    return int(row["c"]) + 1


def claim_client_tag(client_tag: str, **fields) -> bool:
    """Reserve a tag before placing, returning False if it is already taken.

    The pre-check and the insert are not atomic in Python, so the UNIQUE
    constraint is what actually prevents a duplicate order; this turns the
    collision into a boolean rather than an exception at the call site.

    The row stays in place and is filled in by ``settle_claim`` once the broker
    has answered. Deleting it first and re-inserting afterwards would leave a
    window in which the tag was free again.
    """
    try:
        with _lock:
            db = _db()
            db.execute(
                "INSERT INTO decisions (decision_id, cycle_id, ts, code, rule_id,"
                " kind, outcome, client_tag, trade_date) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    fields.get("decision_id") or f"claim:{client_tag}",
                    fields.get("cycle_id", ""), _now(),
                    fields.get("code", ""), fields.get("rule_id", ""),
                    fields.get("kind", "CLAIM"), "CLAIM", client_tag, trade_date(),
                ),
            )
            db.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def settle_claim(client_tag: str, decision: dict) -> None:
    """Fill in a claimed row with the outcome, keeping the tag reserved throughout."""
    row = dict(decision)
    row["trace_json"] = _j(row.pop("trace", None))
    row["tech_json"] = _j(row.pop("tech", None))
    row["scaled"] = int(bool(row.get("scaled")))
    cols = (
        "decision_id", "cycle_id", "ts", "code", "rule_id", "rules_sha", "kind",
        "outcome", "block_reason", "block_detail", "trace_json", "tech_json",
        "bar_as_of", "side", "qty", "limit_price", "ref_price", "atr14",
        "stop_price", "risk_usd", "qty_intended", "qty_affordable",
        "paper_max_cash_buy", "est_fee_round_trip", "est_fee_pct", "breakeven_price", "scaled", "trade_date",
    )
    row.setdefault("ts", _now())
    row.setdefault("trade_date", trade_date())
    with _lock:
        db = _db()
        db.execute(
            f"UPDATE decisions SET {', '.join(f'{c}=?' for c in cols)}"
            f" WHERE client_tag=?",
            tuple(row.get(c) for c in cols) + (client_tag,),
        )
        db.commit()


def release_claim(client_tag: str) -> None:
    """Drop an unsettled claim, so a failure before placement frees the tag."""
    with _lock:
        db = _db()
        db.execute(
            "DELETE FROM decisions WHERE client_tag=? AND outcome='CLAIM'",
            (client_tag,),
        )
        db.commit()


def orders_today(code: str | None = None, side: str | None = None) -> int:
    """Orders placed this US session date, optionally for one symbol or side.

    ``side`` exists because the caps count entries, not activity. Counting
    exits against a budget the entries had already spent left a position the
    rules wanted closed sitting open with ``daily_order_cap`` in the journal.
    """
    sql = "SELECT COUNT(*) c FROM orders WHERE trade_date=?"
    args: list = [trade_date()]
    if code:
        sql += " AND code=?"
        args.append(code)
    if side:
        sql += " AND trd_side=?"
        args.append(side)
    with _lock:
        return _db().execute(sql, args).fetchone()["c"]


def last_activity(rule_id: str, code: str) -> str | None:
    """Newest placement timestamp for this rule and symbol, for the cooldown."""
    with _lock:
        row = _db().execute(
            "SELECT MAX(trade_date) t FROM decisions WHERE rule_id=? AND code=?"
            " AND outcome IN ('PLACED','MATCHED')",
            (rule_id, code),
        ).fetchone()
    return row["t"] if row and row["t"] else None


# -- orders --------------------------------------------------------------------
def record_order(order: dict) -> None:
    _assert_target(order.get("trd_env"), order.get("acc_id"))
    cols = (
        "order_id", "client_tag", "decision_id", "trd_env", "acc_id", "code",
        "trd_side", "order_type", "qty", "price", "currency", "time_in_force",
        "session", "status", "dealt_qty", "dealt_avg_price", "last_err_msg",
        "remark", "created_at", "updated_at", "terminal_at", "trade_date",
    )
    row = dict(order)
    row.setdefault("created_at", _now())
    row.setdefault("trade_date", trade_date())
    row["updated_at"] = _now()
    row["acc_id"] = str(row["acc_id"])
    if row.get("status") in TERMINAL:
        row.setdefault("terminal_at", _now())
    with _lock:
        db = _db()
        db.execute(
            f"INSERT OR REPLACE INTO orders ({','.join(cols)}) "
            f"VALUES ({','.join('?' * len(cols))})",
            tuple(row.get(c) for c in cols),
        )
        db.commit()


def record_order_event(order_id: str, source: str, payload: dict) -> dict:
    """Record a status change and derive any fill it implies, atomically.

    Returns ``{"changed": bool, "fill": dict | None}``.

    Everything happens under one lock on purpose. OpenD re-delivers the same
    push several times - each ``get_acc_list`` re-subscribes the account - and
    the copies arrive concurrently. An earlier version read the previous state,
    released the lock, then wrote: all the copies read the same "before", so
    every one of them logged a duplicate event and none of them saw a change in
    ``dealt_qty`` large enough to book the fill. The shares had moved and the
    journal showed no fill at all. Check and act have to be one step.

    Fills are derived, not reported: a paper account answers ``deal_list_query``
    with "Paper trading does not support deal data" and sends no deal push, so
    an order's ``dealt_qty`` moving is the only evidence that exists.
    """
    status = payload.get("order_status")
    dealt = payload.get("dealt_qty")
    avg = payload.get("dealt_avg_price")

    with _lock:
        db = _db()
        prior = db.execute(
            "SELECT status, dealt_qty, dealt_avg_price FROM order_events"
            " WHERE order_id=? ORDER BY seq DESC LIMIT 1",
            (order_id,),
        ).fetchone()
        if prior and (
            prior["status"], prior["dealt_qty"], prior["dealt_avg_price"]
        ) == (status, dealt, avg):
            return {"changed": False, "fill": None}

        row = db.execute(
            "SELECT dealt_qty, code, trd_side FROM orders WHERE order_id=?", (order_id,)
        ).fetchone()
        before = float((row["dealt_qty"] if row else 0) or 0)
        after = float(dealt or 0)

        db.execute(
            "INSERT INTO order_events (order_id, ts, source, status, dealt_qty,"
            " dealt_avg_price, last_err_msg, raw_json) VALUES (?,?,?,?,?,?,?,?)",
            (order_id, _now(), source, status, dealt, avg,
             payload.get("last_err_msg"), _j(payload)),
        )
        db.execute(
            "UPDATE orders SET status=?, dealt_qty=?, dealt_avg_price=?,"
            " last_err_msg=?, updated_at=?,"
            " terminal_at=COALESCE(terminal_at, ?) WHERE order_id=?",
            (status, dealt, avg, payload.get("last_err_msg"), _now(),
             _now() if status in TERMINAL else None, order_id),
        )

        fill = None
        if after > before:
            fill = {
                # Deterministic, so a replayed push collides on the primary key
                # instead of booking the same shares twice.
                "deal_id": f"{order_id}:{after:g}",
                "order_id": order_id,
                "code": payload.get("code") or (row["code"] if row else None),
                "trd_side": payload.get("trd_side") or (row["trd_side"] if row else None),
                "qty": after - before,
                "price": avg,
                "create_time": payload.get("updated_time") or payload.get("create_time"),
                "trd_env": payload.get("trd_env") or PAPER_TRD_ENV,
                "derived": True,
            }
            _assert_target(fill["trd_env"], PAPER_ACC_ID)
            db.execute(
                "INSERT OR IGNORE INTO fills (deal_id, order_id, code, trd_side, qty,"
                " price, create_time, trd_env, derived, notified, recorded_at)"
                " VALUES (?,?,?,?,?,?,?,?,1,0,?)",
                (fill["deal_id"], fill["order_id"], fill["code"], fill["trd_side"],
                 fill["qty"], fill["price"], fill["create_time"], fill["trd_env"], _now()),
            )
        db.commit()
    return {"changed": True, "fill": fill}


def order_by_remark(remark: str) -> dict | None:
    with _lock:
        rows = _rows(_db().execute("SELECT * FROM orders WHERE remark=?", (remark,)))
    return rows[0] if rows else None


def open_orders() -> list[dict]:
    with _lock:
        return _rows(
            _db().execute(
                "SELECT * FROM orders WHERE terminal_at IS NULL ORDER BY created_at DESC"
            )
        )


def orders(days: int = 7, limit: int = 200) -> list[dict]:
    since = (dt.date.fromisoformat(trade_date()) - dt.timedelta(days=days)).isoformat()
    with _lock:
        return _rows(
            _db().execute(
                "SELECT * FROM orders WHERE trade_date>=?"
                " ORDER BY created_at DESC LIMIT ?",
                (since, limit),
            )
        )


def held_codes() -> set[str]:
    """Symbols with a non-terminal order, so the engine does not stack orders."""
    return {r["code"] for r in open_orders() if r.get("code")}


# -- fills ---------------------------------------------------------------------
def record_fill(fill: dict) -> bool:
    """Insert a fill. Returns False if this deal_id was already recorded.

    Push can replay; the primary key makes that free rather than something the
    caller has to reason about.
    """
    _assert_target(fill.get("trd_env"), PAPER_ACC_ID)
    try:
        with _lock:
            db = _db()
            db.execute(
                "INSERT INTO fills (deal_id, order_id, code, trd_side, qty, price,"
                " create_time, trd_env, derived, notified, recorded_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,0,?)",
                (
                    fill.get("deal_id"), fill.get("order_id"), fill.get("code"),
                    fill.get("trd_side"), fill.get("qty"), fill.get("price"),
                    fill.get("create_time"), fill.get("trd_env"),
                    int(bool(fill.get("derived"))), _now(),
                ),
            )
            db.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def backfill_fills() -> int:
    """Ensure every order's booked quantity is represented in ``fills``.

    Push is the fast path, not the only one. A dropped push, or two processes
    holding a context at once, can leave an order showing ``dealt_qty`` with no
    fill beside it - the shares moved and the journal did not say so. This
    reconciles the two from the order table, which is authoritative, and is
    idempotent because the synthesised ``deal_id`` is deterministic.
    """
    added = 0
    with _lock:
        db = _db()
        rows = db.execute(
            "SELECT o.order_id, o.code, o.trd_side, o.dealt_qty, o.dealt_avg_price,"
            " o.trd_env, o.updated_at,"
            " COALESCE((SELECT SUM(qty) FROM fills f WHERE f.order_id=o.order_id), 0) booked"
            " FROM orders o WHERE COALESCE(o.dealt_qty,0) > 0"
        ).fetchall()
        for r in rows:
            missing = float(r["dealt_qty"] or 0) - float(r["booked"] or 0)
            if missing <= 0:
                continue
            _assert_target(r["trd_env"], PAPER_ACC_ID)
            db.execute(
                "INSERT OR IGNORE INTO fills (deal_id, order_id, code, trd_side, qty,"
                " price, create_time, trd_env, derived, notified, recorded_at)"
                " VALUES (?,?,?,?,?,?,?,?,1,0,?)",
                (f"{r['order_id']}:{float(r['dealt_qty']):g}", r["order_id"], r["code"],
                 r["trd_side"], missing, r["dealt_avg_price"], r["updated_at"],
                 r["trd_env"], _now()),
            )
            added += 1
        db.commit()
    return added


def unnotified_fills() -> list[dict]:
    with _lock:
        return _rows(
            _db().execute("SELECT * FROM fills WHERE notified=0 ORDER BY recorded_at")
        )


def mark_notified(deal_id: str) -> None:
    with _lock:
        db = _db()
        db.execute("UPDATE fills SET notified=1 WHERE deal_id=?", (deal_id,))
        db.commit()


def record_orphan(kind: str, payload: dict) -> None:
    """A push we cannot attribute to a journalled order. Reported, never acted on."""
    with _lock:
        db = _db()
        db.execute(
            "INSERT INTO orphans (ts, kind, raw_json) VALUES (?,?,?)",
            (_now(), kind, _j(payload)),
        )
        db.commit()


# -- engine state --------------------------------------------------------------
def get_state(key: str, default=None):
    with _lock:
        row = _db().execute(
            "SELECT value FROM engine_state WHERE key=?", (key,)
        ).fetchone()
    return row["value"] if row else default


def set_state(key: str, value) -> None:
    with _lock:
        db = _db()
        db.execute(
            "INSERT OR REPLACE INTO engine_state (key, value, updated_at) VALUES (?,?,?)",
            (key, str(value), _now()),
        )
        db.commit()


def killed() -> bool:
    """True if the engine is halted.

    Two sources deliberately: a row survives a database restore, and a sentinel
    file can be created or removed from a shell when the sidecar is not running.
    Either one halts it; both must be cleared to resume.
    """
    return os.path.exists(_KILL_SENTINEL) or get_state("kill_switch") == "1"


def kill(reason: str) -> None:
    os.makedirs(_DIR, exist_ok=True)
    with open(_KILL_SENTINEL, "w", encoding="utf-8") as fh:
        fh.write(f"{_now()} {reason}\n")
    set_state("kill_switch", "1")
    set_state("kill_reason", reason)
    log.warning("paper engine killed: %s", reason)


def kill_reason() -> str | None:
    return get_state("kill_reason")


def resume() -> None:
    if os.path.exists(_KILL_SENTINEL):
        os.remove(_KILL_SENTINEL)
    set_state("kill_switch", "0")
    set_state("kill_reason", "")


# -- reporting -----------------------------------------------------------------
def journal(days: int = 7, limit: int = 100, outcome: str | None = None) -> list[dict]:
    since = (dt.date.fromisoformat(trade_date()) - dt.timedelta(days=days)).isoformat()
    sql = "SELECT * FROM decisions WHERE trade_date>=? AND outcome!='CLAIM'"
    args: list = [since]
    if outcome:
        sql += " AND outcome=?"
        args.append(outcome)
    sql += " ORDER BY ts DESC LIMIT ?"
    args.append(limit)
    with _lock:
        rows = _rows(_db().execute(sql, args))
    for r in rows:
        r["trace"] = json.loads(r.pop("trace_json") or "null")
        r["tech"] = json.loads(r.pop("tech_json") or "null")
    return rows


def session_summary(session: str | None = None) -> dict:
    """Everything the daily note needs, from the journal alone.

    Journal-backed on purpose, like `orders()`: the note fires hours after the
    close, and OpenD may well be down by then. A summary that cannot be produced
    without the broker is a summary that goes missing on exactly the mornings
    something went wrong.
    """
    session = session or trade_date()
    with _lock:
        db = _db()
        orders_ = _rows(db.execute(
            "SELECT * FROM orders WHERE trade_date=? ORDER BY created_at", (session,)
        ))
        blocked = _rows(db.execute(
            "SELECT block_reason reason, COUNT(*) n FROM decisions"
            " WHERE trade_date=? AND outcome='BLOCKED'"
            " GROUP BY block_reason ORDER BY n DESC", (session,)
        ))
        counts = _rows(db.execute(
            "SELECT outcome, COUNT(*) n FROM decisions WHERE trade_date=?"
            " GROUP BY outcome", (session,)
        ))
        fees_ = db.execute(
            "SELECT COALESCE(SUM(est_fee_round_trip),0) f FROM decisions"
            " WHERE trade_date=? AND outcome='PLACED' AND side='BUY'", (session,)
        ).fetchone()["f"]
        cycles_ = db.execute(
            "SELECT COUNT(*) n FROM cycles WHERE substr(started_at,1,10)"
            " IN (?, date(?, '+1 day'))", (session, session)
        ).fetchone()["n"]

    return {
        "session": session,
        "entries_evaluated": get_state(f"entries_evaluated:{session}") is not None,
        "cycles": cycles_,
        "orders": orders_,
        "entries": [o for o in orders_ if o.get("trd_side") == "BUY"],
        "exits": [o for o in orders_ if o.get("trd_side") == "SELL"],
        "blocked": {r["reason"]: r["n"] for r in blocked if r["reason"]},
        "outcomes": {r["outcome"]: r["n"] for r in counts if r["outcome"]},
        # Estimated, never broker-reported: a paper fill is charged nothing.
        "est_fees_round_trip": round(float(fees_ or 0), 2),
        "day_open_equity": get_state(f"day_open_equity:{session}"),
    }


def close() -> None:
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None
