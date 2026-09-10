"""The evaluation cycle: universe, rules, sizing, guardrails, orders.

Nothing here forms a view. It reads thresholds from ``rules.json``, tests them
against ``indicators.technicals()`` and records which leaf of which condition
was true. Every rejection is journalled with a reason, because a strategy that
silently declines to trade is indistinguishable from one that is broken.

Position semantics in v1, deliberately narrow:

* a BUY rule's ``when`` opens a long, and its ``exit`` closes it
* a SELL rule's ``when`` closes a long if one is held
* nothing opens a short, even though the simulated account permits it

Sizing reads the real account; execution reads the paper one. That is the whole
point of the exercise - position sizes at the scale that actually matters, with
fills that cost nothing - so where the two disagree the order is rejected and
both numbers are recorded rather than the size being quietly cut to fit.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import logging
import math
import os
import time
import uuid
from dataclasses import dataclass
from zoneinfo import ZoneInfo

import currency
import indicators
import research
import telegram
from moomoo_client import OpendUnreachable, client, require_opend

from . import fees, journal, rules as rulesmod
from .broker import PaperContextBusy, broker, events
from .guard import (
    PAPER_ACC_ID,
    PAPER_MAX_NOTIONAL_USD,
    SimulationViolation,
    tradable_markets,
)

log = logging.getLogger(__name__)

ENABLED = os.getenv("PAPER_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")
CYCLE_MINUTES = float(os.getenv("PAPER_CYCLE_MINUTES", "5"))

# Time of day (US/Eastern, "HH:MM") at which new entries are evaluated, once per
# session. Empty means evaluate entries on every cycle, which is the older
# behaviour and is what you want only if the rules read intraday bars.
#
# Why this exists: the rules run on K_DAY bars, and mid-session that bar is not
# finished. A MACD cross seen at 10:00 is computed from a moving close and can
# be gone by 16:00 - the signal repaints, and an entry taken on it was taken on
# something that never happened at the close. Evaluating once, late in the
# session, acts on a bar that is essentially final.
#
# Exits and the pre-close cancel are deliberately NOT gated by this. A position
# already open has to be manageable on every cycle.
EVALUATE_AT = os.getenv("PAPER_EVALUATE_AT", "").strip()

# How much of the engine's activity reaches Telegram.
#
#   all     every order and every fill, plus halts   (roughly 12 messages a day
#           at the current caps - enough that the useful ones get buried)
#   alerts  halts and failures only: the engine stopping, a rules file that will
#           not load, a placement the broker refused. Rare by construction.
#   off     nothing at all
#
# "alerts" is the default because the noise is the order and fill receipts, not
# the halts. An engine that stops trading and says nothing is a worse failure
# than one that says too much - everything it did is still in the journal and
# on /paper either way.
NOTIFY = os.getenv("PAPER_NOTIFY", "alerts").strip().lower()
_NOTIFY_LEVELS = ("off", "alerts", "all")
if NOTIFY not in _NOTIFY_LEVELS:
    NOTIFY = "alerts"

_ET = ZoneInfo("America/New_York")
_OPEN = dt.time(9, 30)
_CLOSE = dt.time(16, 0)

# A long weekend is three days; four tolerates one public holiday on top.
_MAX_BAR_AGE_DAYS = 4
# SMA200 needs 200 trading days; 210 leaves room for the cache's calendar window.
_MIN_BARS = 210
_PRICE_SANITY_PCT = 5.0
_OPEN_MARKET_STATES = ("MORNING", "AFTERNOON")

_OPTION_CODE = __import__("pnl")._OPTION_CODE


@dataclass(frozen=True)
class Block:
    reason: str
    detail: str


def _now_et() -> dt.datetime:
    return dt.datetime.now(_ET)


def _minutes_to_close(now: dt.datetime | None = None) -> float:
    now = now or _now_et()
    close = now.replace(hour=_CLOSE.hour, minute=_CLOSE.minute, second=0, microsecond=0)
    return (close - now).total_seconds() / 60.0


def _clock_says_open(now: dt.datetime | None = None) -> bool:
    now = now or _now_et()
    return now.weekday() < 5 and _OPEN <= now.time() <= _CLOSE


def _parse_hhmm(value: str) -> dt.time | None:
    try:
        hh, mm = value.split(":")
        return dt.time(int(hh), int(mm))
    except (ValueError, AttributeError):
        return None


def entry_window(now: dt.datetime | None = None) -> tuple[bool, str]:
    """May new entries be evaluated this cycle, and the reason either way.

    Fails open: an unparseable PAPER_EVALUATE_AT reverts to evaluating every
    cycle rather than silently trading nothing, and says so.
    """
    if not EVALUATE_AT:
        return True, "every cycle"
    target = _parse_hhmm(EVALUATE_AT)
    if target is None:
        return True, f"PAPER_EVALUATE_AT={EVALUATE_AT!r} is not HH:MM; evaluating every cycle"
    now = now or _now_et()
    if now.time() < target:
        return False, f"entries evaluate at {EVALUATE_AT} ET, it is {now:%H:%M}"
    if journal.get_state(f"entries_evaluated:{journal.trade_date()}"):
        return False, f"entries already evaluated today at {EVALUATE_AT} ET"
    return True, f"entry window open ({EVALUATE_AT} ET)"


def evaluate_at_conflict(flatten_minutes: int) -> str | None:
    """Is PAPER_EVALUATE_AT set so late that every entry is refused anyway?

    ``_order_guardrails`` blocks a BUY within ``flatten_before_close_minutes``
    of the close. Setting the evaluation time inside that window evaluates the
    rules and then rejects every result as a late entry - the engine would look
    like it was working and never trade. Worth saying out loud rather than
    leaving someone to find it in the journal.
    """
    if not EVALUATE_AT:
        return None
    target = _parse_hhmm(EVALUATE_AT)
    if target is None:
        return None
    close_minutes = _CLOSE.hour * 60 + _CLOSE.minute
    target_minutes = target.hour * 60 + target.minute
    slack = close_minutes - target_minutes
    if slack < flatten_minutes:
        cutoff = close_minutes - flatten_minutes
        return (
            f"PAPER_EVALUATE_AT={EVALUATE_AT} is {slack} minutes before the close, "
            f"inside the {flatten_minutes}-minute no-late-entries window, so every "
            f"entry it evaluates will be refused. Move it before "
            f"{cutoff // 60:02d}:{cutoff % 60:02d} ET, or lower "
            f"flatten_before_close_minutes."
        )
    return None


def last_completed_session(now: dt.datetime | None = None) -> str:
    """The most recent US session date whose close has passed.

    The daily note runs at 08:50 local, which is 20:50 ET the *previous* day, so
    `journal.trade_date()` names a session that has not happened yet - Friday
    morning would report Friday's unstarted session and Friday's actual session
    would never be reported at all. Deriving the date from the clock rather than
    from the job's schedule also survives launchd running a missed job late on
    wake, which on this machine is the normal case rather than the exception.

    Holidays are not modelled: the worst case is a note saying nothing happened
    on a day nothing was open, which is true.
    """
    now = now or _now_et()
    day = now.date()
    if now.time() < _CLOSE:
        day -= dt.timedelta(days=1)
    while day.weekday() >= 5:  # Saturday, Sunday
        day -= dt.timedelta(days=1)
    return day.isoformat()


def missed_entry_window(now: dt.datetime | None = None) -> str | None:
    """The session has ended for this trade date and entries never evaluated.

    `PAPER_EVALUATE_AT` gives the engine exactly one chance a day. If no cycle
    runs during it, the engine trades nothing and - before this existed - said
    nothing: `alerts` covered halts, and never evaluating is not a halt. On this
    machine the whole US session falls between 21:30 and 04:00 local, so a
    sleeping laptop misses the window silently, which is what happened on
    2026-09-09 (last in-session cycle 15:00 ET, next 17:06 ET, window 15:35).

    Returns a reason string when the day was missed, else None. A market holiday
    reads the same way from here; the message says so rather than pretending to
    know the difference.
    """
    if not EVALUATE_AT:
        return None
    now = now or _now_et()
    if now.weekday() >= 5:
        return None
    # Only once the session is over, so a cycle can still land in the window.
    if now.time() < _CLOSE:
        return None
    # Keyed off `now`, not journal.trade_date(), so the clock this was asked
    # about and the session it reports on cannot disagree.
    session = now.date().isoformat()
    if journal.get_state(f"entries_evaluated:{session}"):
        return None
    return (
        f"the {EVALUATE_AT} ET entry window passed with no cycle in it "
        f"(session {session})"
    )


def market_gate(state: str | None) -> Block | None:
    """Both the broker's view and an independent clock must agree.

    MarketState is an enum the SDK can extend, so the observed value is carried
    into the block reason - an unexpected state should be visible rather than
    quietly halting the engine forever.
    """
    if not _clock_says_open():
        return Block("market_closed", f"US/Eastern clock says closed ({_now_et():%a %H:%M})")
    if state not in _OPEN_MARKET_STATES:
        return Block("market_closed", f"broker reports market_us={state!r}")
    return None


# -- universe ------------------------------------------------------------------
def build_universe(ruleset, markets: tuple[str, ...], tech_by_code: dict) -> dict:
    """Considered and excluded symbols, with a reason for every exclusion.

    Excluded and named, never silently dropped - the same discipline
    ``pnl.conversion.unconverted`` follows, and for the same reason: a symbol
    that vanished without explanation looks identical to one that never
    qualified.
    """
    held = [p.get("code") for p in client.positions() if p.get("code")]
    try:
        watch = [s.get("code") for s in research.watchlist().get("stocks", []) if s.get("code")]
    except Exception:  # noqa: BLE001 - a watchlist failure must not stop holdings
        log.warning("watchlist unavailable; continuing with holdings only", exc_info=True)
        watch = []

    scoped = set(ruleset.codes) if ruleset.codes else None
    candidates, excluded, considered = [], [], []
    for code in dict.fromkeys([*held, *watch]):
        if scoped is not None and code not in scoped:
            continue
        candidates.append(code)

    for code in candidates:
        market = code.split(".")[0] if "." in code else ""
        if market not in markets:
            excluded.append({
                "code": code, "reason": "market_not_tradable",
                "detail": f"paper account trdmarket_auth is {list(markets)}",
            })
            continue
        if _OPTION_CODE.match(code):
            excluded.append({
                "code": code, "reason": "instrument_excluded",
                "detail": "option contract; the 100x multiplier changes sizing",
            })
            continue
        tech = tech_by_code.get(code)
        if tech is None or tech.get("error"):
            excluded.append({
                "code": code, "reason": "insufficient_history",
                "detail": (tech or {}).get("error", "no bars"),
            })
            continue
        if (tech.get("bars") or 0) < _MIN_BARS:
            excluded.append({
                "code": code, "reason": "insufficient_history",
                "detail": f"{tech.get('bars')} bars; {_MIN_BARS} required for sma200",
            })
            continue
        age = _bar_age_days(tech.get("as_of"))
        if age is None or age > _MAX_BAR_AGE_DAYS:
            excluded.append({
                "code": code, "reason": "stale_bars",
                "detail": f"last bar {tech.get('as_of')}, {age} days old, max {_MAX_BAR_AGE_DAYS}",
            })
            continue
        considered.append(code)

    return {
        "considered": considered,
        "sources": {"positions": held, "watchlist": watch},
        "excluded": excluded,
    }


def _bar_age_days(as_of: str | None) -> int | None:
    if not as_of:
        return None
    try:
        # Against the US session date: bar dates are market dates, and the local
        # clock here is a day ahead of the market for most of the session.
        return (dt.date.fromisoformat(journal.trade_date())
                - dt.date.fromisoformat(as_of[:10])).days
    except ValueError:
        return None


# -- sizing --------------------------------------------------------------------
def size_order(rule, tech: dict, equity_real: float, side: str) -> dict:
    """Risk-based sizing off the real account, capped three ways."""
    ref = tech.get("price")
    atr = (tech.get("volatility") or {}).get("atr14")
    if not ref or not atr:
        return {"qty": 0, "reason": "no price or atr14"}

    offset = rule.limit_offset_pct / 100.0
    limit = ref * (1 + offset) if side == "BUY" else ref * (1 - offset)
    limit = round(limit, 2)

    stop_dist = rule.atr_mult_stop * atr
    risk_usd = equity_real * rule.risk_pct / 100.0
    qty_risk = math.floor(risk_usd / stop_dist) if stop_dist > 0 else 0
    qty_cap = math.floor(equity_real * rule.max_notional_pct / 100.0 / limit)
    qty_abs = math.floor(PAPER_MAX_NOTIONAL_USD / limit)
    qty = max(0, min(qty_risk, qty_cap, qty_abs))

    caps = {"risk": qty_risk, "notional_pct": qty_cap, "absolute_cap": qty_abs}
    binding = min(caps, key=caps.get)

    return {
        "qty": qty,
        "ref_price": ref,
        "limit_price": limit,
        "atr14": atr,
        "stop_price": round(limit - stop_dist if side == "BUY" else limit + stop_dist, 2),
        "risk_usd": round(risk_usd, 2),
        "caps": caps,
        "binding": binding,
        # Why a size came out zero is the interesting case, not the arithmetic:
        # on a small account a single share of an expensive name can exceed the
        # per-position cap outright, and "0" alone does not say that.
        "explain": (
            f"risk {rule.risk_pct}% of {equity_real:,.0f} = {risk_usd:,.2f} over a "
            f"{rule.atr_mult_stop}x ATR stop of {stop_dist:,.2f} allows {qty_risk}; "
            f"the {rule.max_notional_pct}% position cap "
            f"({equity_real * rule.max_notional_pct / 100:,.2f}) allows {qty_cap} "
            f"at {limit:,.2f}; the absolute cap allows {qty_abs}. "
            f"Binding constraint: {binding}."
        ),
    }


def _client_tag(rule_id: str, code: str, seq: int) -> str:
    return f"{rule_id}:{code}:{journal.trade_date()}:{seq}"


def _remark(client_tag: str) -> str:
    """Correlation key carried on the order.

    ``place_order`` rejects a remark over 64 UTF-8 bytes, and pushes carry the
    remark but no account id, so this digest is what ties a push back to the
    decision that produced it.
    """
    return "pt" + hashlib.blake2s(client_tag.encode(), digest_size=8).hexdigest()


# -- the cycle -----------------------------------------------------------------
def cycle(dry_run: bool = False, ignore_market_hours: bool = False) -> dict:
    """One full evaluation pass. Blocking; call it on a worker thread."""
    cycle_id = f"{dt.datetime.now(dt.timezone.utc):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}"
    result: dict = {"cycle_id": cycle_id, "dry_run": dry_run, "placed": [], "decisions": []}

    if journal.killed() and not dry_run:
        journal.start_cycle(cycle_id, blocked_by="kill_switch")
        result["blocked_by"] = "kill_switch"
        result["detail"] = journal.kill_reason()
        return result

    try:
        ruleset = rulesmod.load()
    except rulesmod.RuleError as exc:
        journal.start_cycle(cycle_id, blocked_by="rules_invalid", error=str(exc))
        _notify_once_daily("rules_invalid", f"⚠️ Paper engine idle — rules file invalid:\n{exc}")
        result["blocked_by"] = "rules_invalid"
        result["detail"] = str(exc)
        return result

    require_opend()
    broker._assert_target(force=True)
    markets = tradable_markets(broker.roster())

    state = None
    try:
        state = (client.quote.get_global_state()[1] or {}).get("market_us")
    except Exception:  # noqa: BLE001 - the clock check still applies
        log.warning("global state unavailable", exc_info=True)

    gate = None if (dry_run and ignore_market_hours) else market_gate(state)

    acct = client.account_info()
    equity_real = currency.num(acct.get("total_assets")) or 0.0
    equity_usd_only = currency.num(acct.get("usd_assets"))
    paper_acct = broker.account()
    paper_positions = broker.positions()
    held = {p.get("code"): p for p in paper_positions}

    journal.start_cycle(
        cycle_id,
        rules_sha=ruleset.sha256,
        market_state=state,
        equity_real=equity_real,
        equity_real_usd_only=equity_usd_only,
        fx_source="moomoo accinfo_query conversion to the reporting currency",
        paper_total_assets=currency.num(paper_acct.get("total_assets")),
        paper_cash=currency.num(paper_acct.get("cash")),
        paper_power=currency.num(paper_acct.get("power")),
    )

    _record_day_open(paper_acct)

    tech_by_code: dict = {}
    for code in _candidate_codes(ruleset):
        try:
            tech_by_code[code] = indicators.technicals(code, ktype=ruleset.ktype)
        except Exception as exc:  # noqa: BLE001 - one bad symbol must not end the cycle
            log.warning("technicals failed for %s: %s", code, exc)
            tech_by_code[code] = {"error": str(exc)}

    universe = build_universe(ruleset, markets, tech_by_code)
    result["universe"] = universe

    if gate is not None:
        if not dry_run:
            missed = missed_entry_window()
            if missed:
                _notify_once_daily(
                    "missed_entry_window",
                    f"\u26a0\ufe0f <b>Paper engine evaluated no entries today</b>\n{missed}.\n"
                    f"<i>Either nothing ran at that time - a sleeping machine is the "
                    f"usual cause, the US session is 21:30-04:00 local - or the market "
                    f"was closed for a holiday.</i>",
                )
                result["missed_entry_window"] = missed
        journal.finish_cycle(cycle_id, universe=universe, blocked_by=gate.reason, error=gate.detail)
        result["blocked_by"] = gate.reason
        result["detail"] = gate.detail
        return result

    daily_block = _daily_loss_check(ruleset, paper_acct)
    if daily_block and not dry_run:
        journal.kill(f"{daily_block.reason}: {daily_block.detail}")
        _notify(
            f"\U0001f6d1 <b>Paper engine halted</b>\n{daily_block.detail}\n"
            f"<i>It will place nothing further until resumed from /paper.</i>",
            level="alerts",
        )
        journal.finish_cycle(cycle_id, universe=universe, blocked_by=daily_block.reason)
        result["blocked_by"] = daily_block.reason
        result["detail"] = daily_block.detail
        return result

    entries_open, window_note = entry_window()
    result["entry_window"] = window_note
    if not entries_open:
        log.debug("entries gated: %s", window_note)

    seq = 0
    for code in universe["considered"]:
        tech = tech_by_code[code]
        position = held.get(code)
        held_qty = float((position or {}).get("can_sell_qty") or 0)

        for rule in ruleset.enabled():
            if held_qty > 0:
                cond, kind, side = (rule.exit, "EXIT", "SELL") if rule.side == "BUY" else (rule.when, "EXIT", "SELL")
            else:
                if rule.side != "BUY":
                    continue
                if not entries_open:
                    # Skipped silently rather than journalled per symbol: at 46
                    # names and a 15 minute cycle that would be hundreds of
                    # identical rows a day. The reason is on the cycle instead.
                    continue
                cond, kind, side = rule.when, "ENTRY", "BUY"
            if not cond:
                continue

            matched, trace = rulesmod.evaluate(cond, tech)
            base = {
                "decision_id": uuid.uuid4().hex,
                "cycle_id": cycle_id,
                "code": code,
                "rule_id": rule.id,
                "rules_sha": ruleset.sha256,
                "kind": kind,
                "side": side,
                "trace": trace,
                "bar_as_of": tech.get("as_of"),
                "ref_price": tech.get("price"),
            }
            if not matched:
                journal.record_decision({**base, "outcome": "NO_MATCH"})
                continue

            seq += 1
            decision = _prepare(
                base, rule, tech, side, held_qty, equity_real, ruleset,
                paper_acct, universe, seq, dry_run,
            )
            result["decisions"].append(decision)
            # WOULD_PLACE counts here too: a dry run's whole purpose is to
            # answer "what would it send", so its summary must not read zero
            # while the decisions below it say otherwise.
            if decision.get("outcome") in ("PLACED", "WOULD_PLACE"):
                result["placed"].append(decision)
            break  # one rule per symbol per cycle

    _cancel_near_close(ruleset, dry_run, result)

    # Mark the session's entry evaluation done only after it actually ran, and
    # only for real cycles - a dry run must not consume the day's window.
    if entries_open and EVALUATE_AT and not dry_run:
        journal.set_state(f"entries_evaluated:{journal.trade_date()}", "1")

    journal.finish_cycle(cycle_id, universe=universe, entry_window=window_note)
    return result


def _candidate_codes(ruleset) -> list[str]:
    held = [p.get("code") for p in client.positions() if p.get("code")]
    try:
        watch = [s.get("code") for s in research.watchlist().get("stocks", []) if s.get("code")]
    except Exception:  # noqa: BLE001
        watch = []
    codes = list(dict.fromkeys([*held, *watch]))
    if ruleset.codes:
        codes = [c for c in codes if c in set(ruleset.codes)]
    markets_hint = ("US",)
    return [
        c for c in codes
        if c.split(".")[0] in markets_hint and not _OPTION_CODE.match(c)
    ]


def _prepare(base, rule, tech, side, held_qty, equity_real, ruleset,
             paper_acct, universe, seq, dry_run) -> dict:
    """Size a matched rule, run every per-order guardrail, then place it."""
    sizing = size_order(rule, tech, equity_real, side)
    limit = sizing.get("limit_price")
    qty_intended = held_qty if side == "SELL" else sizing["qty"]

    notional = (qty_intended or 0) * (limit or 0)
    cost = fees.describe(notional) if notional > 0 else None

    row = {
        **base,
        "est_fee_round_trip": (cost or {}).get("round_trip"),
        "est_fee_pct": (cost or {}).get("round_trip_pct"),
        "breakeven_price": fees.breakeven_price(limit, qty_intended)
        if side == "BUY" and limit else None,
        "qty_intended": qty_intended,
        "limit_price": limit,
        "atr14": sizing.get("atr14"),
        "stop_price": sizing.get("stop_price"),
        "risk_usd": sizing.get("risk_usd"),
        "sizing": {k: sizing.get(k) for k in ("caps", "binding", "explain")},
    }

    block = _order_guardrails(
        rule, base["code"], side, qty_intended, limit, tech, ruleset, equity_real
    )
    if block:
        journal.record_decision({**row, "outcome": "BLOCKED",
                                 "block_reason": block.reason, "block_detail": block.detail})
        return {**row, "outcome": "BLOCKED", "block_reason": block.reason,
                "block_detail": block.detail}

    qty = qty_intended
    affordable = None
    scaled = False
    if side == "BUY":
        info = broker.max_buy_sell(base["code"], limit)
        affordable = float(info.get("max_cash_buy") or 0)
        if qty > affordable:
            mode = ruleset.portfolio.on_insufficient_paper_power
            detail = (
                f"sized {qty} off real equity {equity_real:,.0f} but the paper "
                f"account affords {affordable:.0f} at {limit}"
            )
            if mode == "reject":
                journal.record_decision({
                    **row, "outcome": "BLOCKED", "block_reason": "paper_buying_power",
                    "block_detail": detail, "qty_affordable": affordable,
                    "paper_max_cash_buy": affordable,
                })
                return {**row, "outcome": "BLOCKED", "block_reason": "paper_buying_power",
                        "block_detail": detail, "qty_affordable": affordable}
            qty = math.floor(affordable)
            scaled = True

    if qty <= 0:
        detail = sizing.get("explain") or f"sizing produced {qty}"
        journal.record_decision({**row, "outcome": "BLOCKED", "block_reason": "zero_quantity",
                                 "block_detail": detail})
        return {**row, "outcome": "BLOCKED", "block_reason": "zero_quantity",
                "block_detail": detail}

    # Sequence comes from the journal, not the cycle: a per-cycle counter would
    # reuse today's first tag on every later cycle.
    client_tag = _client_tag(rule.id, base["code"], journal.next_seq(rule.id, base["code"]))
    row.update({"qty": qty, "qty_affordable": affordable, "scaled": scaled,
                "client_tag": client_tag})

    if dry_run:
        return {**row, "outcome": "WOULD_PLACE", "remark": _remark(client_tag)}

    if not journal.claim_client_tag(
        client_tag, decision_id=row["decision_id"], cycle_id=row["cycle_id"],
        code=base["code"], rule_id=rule.id, kind=base["kind"],
    ):
        return {**row, "outcome": "BLOCKED", "block_reason": "duplicate",
                "block_detail": f"{client_tag} already placed"}

    remark = _remark(client_tag)
    try:
        placed = broker.place(
            code=base["code"], side=side, qty=qty, price=limit, remark=remark,
            time_in_force=rule.time_in_force, session=rule.session,
        )
    except Exception as exc:  # noqa: BLE001 - a broker refusal is data, not a crash
        # Settle rather than release: a refusal is a fact worth keeping, and the
        # tag stays spent so a retry loop cannot hammer the broker.
        journal.settle_claim(client_tag, {**row, "outcome": "PLACE_FAILED",
                                          "block_reason": "broker_error",
                                          "block_detail": str(exc)})
        log.warning("placement failed for %s: %s", base["code"], exc)
        _notify(
            f"\u26a0\ufe0f <b>Paper order refused</b> — {base['code']} {side} {qty}\n"
            f"<code>{exc}</code>",
            level="alerts",
        )
        return {**row, "outcome": "PLACE_FAILED", "block_detail": str(exc)}

    journal.settle_claim(client_tag, {**row, "outcome": "PLACED"})
    journal.record_order({
        "order_id": str(placed.get("order_id")),
        "client_tag": client_tag,
        "decision_id": row["decision_id"],
        "trd_env": placed["trd_env"],
        "acc_id": placed["acc_id"],
        "code": placed.get("code"), "trd_side": placed.get("trd_side"),
        "order_type": placed.get("order_type"), "qty": placed.get("qty"),
        "price": placed.get("price"), "currency": placed.get("currency"),
        "time_in_force": placed.get("time_in_force"), "session": placed.get("session"),
        "status": placed.get("order_status"), "dealt_qty": placed.get("dealt_qty"),
        "dealt_avg_price": placed.get("dealt_avg_price"),
        "last_err_msg": placed.get("last_err_msg"), "remark": remark,
    })
    journal.record_order_event(str(placed.get("order_id")), "place", placed)

    out = {**row, "outcome": "PLACED", "order_id": placed.get("order_id"),
           "status": placed.get("order_status"), "remark": remark}
    _notify(telegram.format_paper_order(out, rule.id, base["trace"]))
    return out


def _order_guardrails(rule, code, side, qty, limit, tech, ruleset,
                      equity_real: float | None = None) -> Block | None:
    pf = ruleset.portfolio
    if qty is None or limit is None:
        return Block("no_price", "sizing produced no limit price")

    # Applies to both sides: a second order while one is still resting is a
    # double order, not a faster one.
    if code in journal.held_codes():
        return Block("order_outstanding", f"an order on {code} is still open")

    if side == "BUY":
        # Everything from here to the fee check is a budget on *new* exposure.
        # None of it may reach a SELL. An exit refused because the day's
        # entries spent the allowance is a position the rules said to close and
        # the engine held anyway - which is what happened on 2026-09-09, where
        # six exits on AMAT and SVRA were journalled `daily_order_cap` while the
        # positions stayed open. Same principle as the fee guard below.
        if _minutes_to_close() < pf.flatten_before_close_minutes:
            return Block("late_entry",
                         f"within {pf.flatten_before_close_minutes}m of the close")

        entries = journal.orders_today(side="BUY")
        if entries >= pf.max_orders_per_day:
            return Block("daily_order_cap",
                         f"{entries} entries already today, cap {pf.max_orders_per_day}"
                         " (exits are not counted and are never capped)")

        on_symbol = journal.orders_today(code, side="BUY")
        if on_symbol >= pf.max_orders_per_symbol_per_day:
            return Block("per_symbol_cap",
                         f"{on_symbol} entry(s) on {code} today, cap "
                         f"{pf.max_orders_per_symbol_per_day}")

        last = journal.last_activity(rule.id, code)
        if last:
            age = (dt.date.fromisoformat(journal.trade_date())
                   - dt.date.fromisoformat(last[:10])).days
            if age < rule.cooldown_days:
                return Block("cooldown",
                             f"{rule.id} fired on {code} {age}d ago, cooldown "
                             f"{rule.cooldown_days}d")

        try:
            positions = broker.positions()
        except Exception:  # noqa: BLE001
            positions = []
        if len([p for p in positions if float(p.get("qty") or 0) > 0]) >= pf.max_concurrent_positions:
            return Block("max_concurrent_positions", f"{pf.max_concurrent_positions} already open")

        # Measured against the REAL account's equity, because that is what
        # sized the position: against the paper account's own million it would
        # never bind and would read as a limit while being none. `market_val`
        # can arrive as the string "N/A", so it goes through currency.num.
        if not equity_real or equity_real <= 0:
            return Block("open_notional_cap",
                         "no real equity to measure open exposure against")
        open_notional = sum(
            currency.num(p.get("market_val")) or 0.0
            for p in positions
            if float(p.get("qty") or 0) > 0
        )
        proposed = open_notional + (qty or 0) * limit
        pct = proposed / equity_real * 100
        if pct > pf.max_open_notional_pct:
            return Block(
                "open_notional_cap",
                f"{open_notional:,.0f} already open plus {code} at "
                f"{(qty or 0) * limit:,.0f} is {pct:.1f}% of {equity_real:,.0f}, "
                f"over the {pf.max_open_notional_pct}% limit",
            )

        # A position whose fees look unattractive must still be closable.
        notional = (qty or 0) * limit
        burden = fees.round_trip_pct(notional)
        if burden > pf.max_fee_pct_of_notional:
            return Block(
                "fee_burden",
                f"{code} at {qty} x {limit:,.2f} = {notional:,.0f} would owe "
                f"{fees.round_trip(notional):,.2f} round trip, {burden:.2f}% of the "
                f"position, over the {pf.max_fee_pct_of_notional}% limit. It has to "
                f"move {burden:.2f}% before it earns anything. Estimated: the paper "
                f"account is not charged.",
            )

    sanity = _price_sanity(code, limit, tech)
    if sanity:
        return sanity
    return None


def _price_sanity(code: str, limit: float, tech: dict) -> Block | None:
    """A limit far from both the last bar and the live quote is a bad input."""
    close = tech.get("price")
    if close and abs(limit - close) / close * 100 > _PRICE_SANITY_PCT:
        return Block("price_sanity", f"limit {limit} is >{_PRICE_SANITY_PCT}% from close {close}")
    try:
        snap = client.snapshot([code])
    except Exception:  # noqa: BLE001 - no quote is not a reason to place blind
        return Block("price_sanity", "no live snapshot to validate the limit against")
    last = (snap[0].get("last_price") if snap else None)
    if not last:
        return Block("price_sanity", "snapshot carried no last_price")
    if abs(limit - last) / last * 100 > _PRICE_SANITY_PCT:
        return Block("price_sanity", f"limit {limit} is >{_PRICE_SANITY_PCT}% from last {last}")
    return None


def _record_day_open(paper_acct: dict) -> None:
    key = f"day_open_equity:{journal.trade_date()}"
    if journal.get_state(key) is None:
        journal.set_state(key, currency.num(paper_acct.get("total_assets")) or 0.0)


def _daily_loss_check(ruleset, paper_acct: dict) -> Block | None:
    key = f"day_open_equity:{journal.trade_date()}"
    opening = journal.get_state(key)
    now = currency.num(paper_acct.get("total_assets"))
    if opening is None or now is None:
        return None
    opening = float(opening)
    if opening <= 0:
        return None
    drop = (opening - now) / opening * 100
    if drop > ruleset.portfolio.max_daily_loss_pct:
        return Block("daily_loss_limit",
                     f"paper equity down {drop:.2f}% today, limit {ruleset.portfolio.max_daily_loss_pct}%")
    return None


def _cancel_near_close(ruleset, dry_run: bool, result: dict) -> None:
    """Pull resting orders before the close.

    DAY orders expire anyway, but an explicit cancel produces a push and a
    journal row, so our record and the broker's agree on an event rather than
    on an assumption.
    """
    if dry_run:
        return
    if _minutes_to_close() > ruleset.portfolio.flatten_before_close_minutes:
        return
    cancelled = []
    for order in journal.open_orders():
        oid = order.get("order_id")
        try:
            broker.cancel(oid)
            cancelled.append(oid)
        except Exception as exc:  # noqa: BLE001
            log.warning("cancel failed for %s: %s", oid, exc)
    if cancelled:
        result["cancelled"] = cancelled
        _notify(
            f"🧹 Paper engine cancelled {len(cancelled)} resting order(s) before the close."
        )


def reconcile() -> dict:
    """Repair anything a dropped push lost. One bounded call per cycle.

    Only ``order_list_query``: a paper account rejects ``deal_list_query`` with
    "Paper trading does not support deal data" and sends no deal push either, so
    an order's own ``dealt_qty`` is the only record of a fill that exists. That
    is why every fill in the journal is flagged ``derived``.
    """
    repaired = 0
    for order in broker.resting_orders():
        oid = str(order.get("order_id") or "")
        if not journal.order_by_remark(order.get("remark") or ""):
            continue
        if journal.record_order_event(oid, "reconcile", order)["changed"]:
            repaired += 1
    # Self-heal: an order can show dealt quantity with no fill beside it if a
    # push was dropped. The order table is authoritative; make fills agree.
    backfilled = journal.backfill_fills()
    return {"repaired": repaired, "backfilled": backfilled}


# -- notification --------------------------------------------------------------
def _notify(text: str, level: str = "all") -> None:
    """Send a Telegram message from whichever thread is calling.

    ``level`` is the minimum verbosity this message belongs to: "alerts" for
    things that need a human (a halt, a refusal, an unloadable rules file) and
    "all" for routine receipts.

    ``telegram.notify`` is a coroutine and the cycle runs on a worker thread, so
    it needs a loop of its own here. Calling it bare silently produced an
    un-awaited coroutine and no message at all.
    """
    if NOTIFY == "off":
        return
    if level == "all" and NOTIFY != "all":
        return
    try:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(telegram.notify(text))
            return
        loop.create_task(telegram.notify(text))
    except Exception:  # noqa: BLE001 - delivery must never break the engine
        log.warning("telegram notify failed", exc_info=True)


def _notify_once_daily(key: str, text: str) -> None:
    state_key = f"notified:{key}:{journal.trade_date()}"
    if journal.get_state(state_key):
        return
    _notify(text, level="alerts")
    journal.set_state(state_key, "1")


def drain_fills() -> int:
    """Send a receipt for every fill not yet reported.

    Driven off the journal rather than the queue so a restart mid-flight
    re-sends nothing already delivered and drops nothing not yet delivered.
    """
    sent = 0
    for fill in journal.unnotified_fills():
        # Marked either way. If receipts are suppressed the row must still be
        # cleared, or every fill ever recorded is retried on every cycle
        # forever.
        if NOTIFY == "all":
            try:
                _notify(telegram.format_paper_fill(fill), level="all")
            except Exception:  # noqa: BLE001
                log.warning("fill notify failed", exc_info=True)
                break
            sent += 1
        journal.mark_notified(fill["deal_id"])
    return sent


# -- the loop ------------------------------------------------------------------
async def run() -> None:
    """Second scheduler task, independent of the alert scheduler.

    Deliberately not folded into ``scheduler.run``: that loop does blocking P&L
    work on its own period, and this one must be separately killable without
    taking the daily alerts down with it.
    """
    if not ENABLED:
        log.info("paper engine disabled (set PAPER_ENABLED=true to arm it)")
        return

    log.info(
        "paper engine armed: account %s, %.0f minute cycle, max notional $%s, entries %s",
        PAPER_ACC_ID, CYCLE_MINUTES, f"{PAPER_MAX_NOTIONAL_USD:,.0f}",
        f"at {EVALUATE_AT} ET" if EVALUATE_AT else "every cycle",
    )
    try:
        conflict = evaluate_at_conflict(rulesmod.load().portfolio.flatten_before_close_minutes)
        if conflict:
            log.warning(conflict)
    except rulesmod.RuleError:
        pass  # an invalid rules file is reported by the first cycle

    while True:
        try:
            out = await asyncio.to_thread(cycle)
            if out.get("placed"):
                log.info("paper cycle %s placed %d order(s)", out["cycle_id"], len(out["placed"]))
            await asyncio.to_thread(reconcile)
        except OpendUnreachable:
            log.debug("OpenD unreachable; paper engine idling")
        except SimulationViolation:
            # The account is not what this package was built to trade. Stop.
            log.error("simulation violation; killing the paper engine", exc_info=True)
            await asyncio.to_thread(journal.kill, "simulation violation")
            await asyncio.to_thread(
                _notify,
                "\U0001f6d1 <b>Paper engine halted: simulation violation</b>\n"
                "The account it is pinned to is not the account it is talking to. "
                "This one cannot be resumed from the dashboard - investigate, then "
                "remove sidecar/.cache/paper/KILLED by hand.",
                "alerts",
            )
        except Exception:  # noqa: BLE001 - the loop must outlive any single failure
            log.warning("paper cycle failed", exc_info=True)

        await _sleep_draining(CYCLE_MINUTES * 60)


async def _sleep_draining(seconds: float) -> None:
    """Sleep, but keep reporting fills while doing it.

    Placed outside the try above so a crashing cycle cannot spin the loop.
    """
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        try:
            while True:
                events.get_nowait()
        except Exception:  # noqa: BLE001 - empty queue
            pass
        try:
            await asyncio.to_thread(drain_fills)
        except Exception:  # noqa: BLE001
            log.warning("fill drain failed", exc_info=True)
        await asyncio.sleep(min(2.0, remaining))
