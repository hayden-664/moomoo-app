"""Loading, validation and evaluation of the user's strategy file.

What this module is not
-----------------------
It does not decide anything. It reads thresholds a person wrote down and tests
them against ``indicators.technicals()`` output, then reports which leaf of
which condition was true. The judgement lives in ``rules.json``; the code only
evaluates it. That is the distinction that keeps this consistent with the rest
of the app - the screener describes and does not advise, and neither does this.

Why there is no expression language
-----------------------------------
A condition is a group (``all`` / ``any`` / ``none``) or a leaf
(``field``/``op``/``value``), and there is deliberately no operator comparing
one field to another. Every threshold is a literal the user typed. That single
omission is what keeps the grammar from being a program, which is why ``eval``
is structurally unnecessary here rather than merely avoided.

Everything fails closed. An unknown path or a ``None`` value makes a leaf false
and records why, instead of raising: ``atr14``, ``sma200`` and
``macd_cross.crossed_within`` are all legitimately null on short history, and a
naive comparison against them would kill a cycle mid-way.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Any

_HERE = os.path.dirname(__file__)
_DEFAULT_PATH = os.path.join(_HERE, "rules.json")

MAX_DEPTH = 4
MAX_LEAVES = 32

# Only K_DAY in v1. klines._read truncates time_key to its first 10 characters,
# so an intraday bar comes back date-only and technicals()["as_of"] cannot
# express intraday staleness - with a 30 minute cache TTL a 15-minute rule
# would silently evaluate half-hour-old bars. Fix both before widening this.
ALLOWED_KTYPES = ("K_DAY",)


class RuleError(ValueError):
    """The strategy file is malformed. The engine refuses to trade on it."""


# -- the field whitelist -------------------------------------------------------
# Mirrors indicators.technicals(). Held explicitly so a typo in rules.json is a
# load-time error naming the bad path, rather than a leaf that is quietly false
# forever because the walker never finds anything there.
_FIELDS = frozenset(
    {
        "price", "bars", "as_of", "code", "ktype", "source",
        "trend.sma20", "trend.sma50", "trend.sma100", "trend.sma200",
        "trend.ema12", "trend.ema26",
        "trend.pct_from.sma20", "trend.pct_from.sma50",
        "trend.pct_from.sma100", "trend.pct_from.sma200",
        "trend.ma_order", "trend.adx14", "trend.plus_di14", "trend.minus_di14",
        "momentum.rsi14", "momentum.rsi_zone",
        "momentum.macd", "momentum.macd_signal", "momentum.macd_histogram",
        "momentum.macd_cross.crossed_within", "momentum.macd_cross.direction",
        "momentum.stoch_k", "momentum.stoch_d",
        "volatility.atr14", "volatility.atr_pct_of_price",
        "volatility.bollinger.lower", "volatility.bollinger.middle",
        "volatility.bollinger.upper", "volatility.bollinger.percent_b",
        "volatility.bollinger.bandwidth_pct",
        "volatility.realized_vol_20d_pct", "volatility.realized_vol_60d_pct",
        "volume.last", "volume.avg_20d", "volume.vs_avg_20d_pct",
        "volume.obv", "volume.obv_change_20d",
        "range.high_52w", "range.low_52w",
        "range.pct_from_52w_high", "range.pct_from_52w_low",
        "range.window_trading_days",
        "returns_pct.1d", "returns_pct.5d", "returns_pct.1m",
        "returns_pct.3m", "returns_pct.6m", "returns_pct.1y",
    }
)


# -- operators -----------------------------------------------------------------
def _num(a, b) -> bool:
    return isinstance(a, (int, float)) and not isinstance(a, bool) and isinstance(
        b, (int, float)
    ) and not isinstance(b, bool)


def _starts_with(actual, target) -> bool:
    """Prefix test over a list or a string.

    Exists for trend.ma_order, which is a list of average names sorted highest
    first: ``["sma20", "sma50"]`` is exactly "SMA20 above SMA50 above the rest".
    """
    if isinstance(actual, (list, tuple)):
        t = target if isinstance(target, (list, tuple)) else [target]
        return len(actual) >= len(t) and list(actual[: len(t)]) == list(t)
    if isinstance(actual, str) and isinstance(target, str):
        return actual.startswith(target)
    return False


def _contains(actual, target) -> bool:
    if isinstance(actual, (list, tuple)):
        return target in actual
    if isinstance(actual, str) and isinstance(target, str):
        return target in actual
    return False


OPS = {
    "eq": lambda a, b: a == b,
    "ne": lambda a, b: a != b,
    "lt": lambda a, b: _num(a, b) and a < b,
    "lte": lambda a, b: _num(a, b) and a <= b,
    "gt": lambda a, b: _num(a, b) and a > b,
    "gte": lambda a, b: _num(a, b) and a >= b,
    "between": lambda a, b: _num(a, b[0]) and _num(a, b[1]) and b[0] <= a <= b[1],
    "in": lambda a, b: a in b,
    "not_in": lambda a, b: a not in b,
    "starts_with": _starts_with,
    "contains": _contains,
    "is_null": lambda a, b: a is None,
    "not_null": lambda a, b: a is not None,
}

_LIST_VALUE_OPS = frozenset({"in", "not_in"})
_NO_VALUE_OPS = frozenset({"is_null", "not_null"})


# -- shapes --------------------------------------------------------------------
@dataclass(frozen=True)
class PortfolioLimits:
    max_concurrent_positions: int = 5
    max_open_notional_pct: float = 40.0
    max_orders_per_day: int = 6
    max_orders_per_symbol_per_day: int = 1
    max_daily_loss_pct: float = 2.0
    flatten_before_close_minutes: int = 20
    on_insufficient_paper_power: str = "reject"
    # Refuse an entry whose round-trip cost exceeds this share of the position.
    # Fees here are nearly flat, so a small position can owe more in costs than
    # the move the rule was trying to capture - and the paper account, which
    # charges nothing, would record that as a win.
    max_fee_pct_of_notional: float = 0.25


@dataclass(frozen=True)
class Rule:
    id: str
    side: str
    when: dict
    exit: dict | None
    enabled: bool = True
    risk_pct: float = 0.5
    atr_mult_stop: float = 2.0
    max_notional_pct: float = 5.0
    cooldown_days: int = 3
    time_in_force: str = "DAY"
    session: str = "RTH"
    limit_offset_pct: float = 0.15


@dataclass(frozen=True)
class RuleSet:
    version: int
    ktype: str
    rules: tuple[Rule, ...]
    portfolio: PortfolioLimits
    source_path: str
    sha256: str
    codes: tuple[str, ...] = field(default=())

    def enabled(self) -> tuple[Rule, ...]:
        return tuple(r for r in self.rules if r.enabled)


# -- resolution and evaluation -------------------------------------------------
_MISSING = object()


def resolve(tech: dict, path: str) -> Any:
    """Walk a dotted path into a technicals payload. Missing is None, never a raise."""
    node: Any = tech
    for part in path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part, _MISSING)
        if node is _MISSING:
            return None
    return node


def evaluate(cond: dict, tech: dict, _depth: int = 0) -> tuple[bool, list[dict]]:
    """Evaluate a condition tree. Returns (result, trace).

    The trace is the point: every leaf records the path, what it resolved to,
    the operator and the outcome, so "why did it fire" is answerable from the
    journal without re-running anything.
    """
    if "all" in cond:
        results, trace = [], []
        for sub in cond["all"]:
            ok, t = evaluate(sub, tech, _depth + 1)
            results.append(ok)
            trace.extend(t)
        return all(results), trace
    if "any" in cond:
        results, trace = [], []
        for sub in cond["any"]:
            ok, t = evaluate(sub, tech, _depth + 1)
            results.append(ok)
            trace.extend(t)
        return any(results), trace
    if "none" in cond:
        results, trace = [], []
        for sub in cond["none"]:
            ok, t = evaluate(sub, tech, _depth + 1)
            results.append(ok)
            trace.extend(t)
        return not any(results), trace

    path = cond["field"]
    op = cond["op"]
    target = cond.get("value")
    actual = resolve(tech, path)

    if actual is None and op not in _NO_VALUE_OPS:
        # Null is a real state here, not an error: short history leaves atr14,
        # sma200 and macd_cross.crossed_within empty.
        result = False
    else:
        try:
            result = bool(OPS[op](actual, target))
        except TypeError:
            result = False

    return result, [
        {"path": path, "resolved": actual, "op": op, "target": target, "result": result}
    ]


# -- loading and validation ----------------------------------------------------
def _validate_cond(cond: Any, where: str, depth: int, leaves: list[int]) -> None:
    if not isinstance(cond, dict):
        raise RuleError(f"{where}: condition must be an object, got {type(cond).__name__}")
    if depth > MAX_DEPTH:
        raise RuleError(f"{where}: condition nesting exceeds {MAX_DEPTH}")

    groups = [k for k in ("all", "any", "none") if k in cond]
    if groups:
        if len(groups) > 1:
            raise RuleError(f"{where}: a group takes exactly one of all/any/none")
        members = cond[groups[0]]
        if not isinstance(members, list) or not members:
            raise RuleError(f"{where}.{groups[0]}: must be a non-empty list")
        for i, sub in enumerate(members):
            _validate_cond(sub, f"{where}.{groups[0]}[{i}]", depth + 1, leaves)
        return

    if "field" not in cond or "op" not in cond:
        raise RuleError(f"{where}: leaf needs both 'field' and 'op'")
    leaves.append(1)
    if len(leaves) > MAX_LEAVES:
        raise RuleError(f"{where}: more than {MAX_LEAVES} conditions in one rule")

    path, op = cond["field"], cond["op"]
    if path not in _FIELDS:
        raise RuleError(
            f"{where}.field: unknown path {path!r}; it is not produced by technicals()"
        )
    if op not in OPS:
        raise RuleError(
            f"{where}.op: unknown operator {op!r}; valid: {', '.join(sorted(OPS))}"
        )
    if op in _NO_VALUE_OPS:
        return
    if "value" not in cond:
        raise RuleError(f"{where}: operator {op!r} needs a 'value'")
    value = cond["value"]
    if op == "between":
        if not isinstance(value, list) or len(value) != 2:
            raise RuleError(f"{where}.value: 'between' takes exactly [min, max]")
        if not all(isinstance(v, (int, float)) for v in value):
            raise RuleError(f"{where}.value: 'between' bounds must be numbers")
        if value[0] > value[1]:
            raise RuleError(f"{where}.value: 'between' min {value[0]} exceeds max {value[1]}")
    if op in _LIST_VALUE_OPS and not isinstance(value, list):
        raise RuleError(f"{where}.value: operator {op!r} takes a list")


def _rule_from(raw: dict, defaults: dict, index: int) -> Rule:
    where = f"rules[{index}]"
    rid = raw.get("id")
    if not rid or not isinstance(rid, str):
        raise RuleError(f"{where}.id: every rule needs a unique string id")
    side = raw.get("side")
    if side not in ("BUY", "SELL"):
        raise RuleError(f"{where}.side: must be BUY or SELL, got {side!r}")
    if "when" not in raw:
        raise RuleError(f"{where}.when: an entry condition is required")

    _validate_cond(raw["when"], f"{where}.when", 0, [])
    if raw.get("exit") is not None:
        _validate_cond(raw["exit"], f"{where}.exit", 0, [])

    size = {**defaults, **(raw.get("size") or {})}
    risk = float(size.get("risk_pct", 0.5))
    from .guard import PAPER_MAX_RISK_PCT

    if not 0 < risk <= PAPER_MAX_RISK_PCT:
        raise RuleError(
            f"{where}.size.risk_pct: {risk} is outside (0, {PAPER_MAX_RISK_PCT}]"
        )
    atr_mult = float(size.get("atr_mult_stop", 2.0))
    if atr_mult <= 0:
        raise RuleError(f"{where}.size.atr_mult_stop: must be positive, got {atr_mult}")

    return Rule(
        id=rid,
        side=side,
        when=raw["when"],
        exit=raw.get("exit"),
        enabled=bool(raw.get("enabled", True)),
        risk_pct=risk,
        atr_mult_stop=atr_mult,
        max_notional_pct=float(size.get("max_notional_pct", 5.0)),
        cooldown_days=int(size.get("cooldown_days", 3)),
        time_in_force=str(size.get("time_in_force", "DAY")),
        session=str(size.get("session", "RTH")),
        limit_offset_pct=float(size.get("limit_offset_pct", 0.15)),
    )


def load(path: str | None = None) -> RuleSet:
    """Parse and validate the strategy file.

    Raises RuleError naming the offending rule and JSON pointer. The engine
    treats that as "do not trade" rather than falling back to a default: a
    strategy nobody wrote is not a safe default, it is an unowned one.
    """
    src = path or os.getenv("PAPER_RULES_PATH") or _DEFAULT_PATH
    try:
        with open(src, "rb") as fh:
            raw_bytes = fh.read()
    except OSError as exc:
        raise RuleError(f"cannot read rules file {src}: {exc}") from exc

    try:
        doc = json.loads(raw_bytes)
    except json.JSONDecodeError as exc:
        raise RuleError(f"{src} is not valid JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise RuleError(f"{src}: top level must be an object")

    ktype = doc.get("ktype", "K_DAY")
    if ktype not in ALLOWED_KTYPES:
        raise RuleError(
            f"ktype {ktype!r} is not supported; only {', '.join(ALLOWED_KTYPES)} - "
            f"intraday bars come back date-only from the cache, so staleness "
            f"cannot be detected"
        )

    raw_rules = doc.get("rules")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise RuleError("rules: at least one rule is required")

    defaults = doc.get("defaults") or {}
    rules, seen = [], set()
    for i, raw in enumerate(raw_rules):
        rule = _rule_from(raw, defaults, i)
        if rule.id in seen:
            raise RuleError(f"rules[{i}].id: duplicate id {rule.id!r}")
        seen.add(rule.id)
        rules.append(rule)

    pf_raw = doc.get("portfolio") or {}
    allowed = set(PortfolioLimits.__dataclass_fields__)
    unknown = set(pf_raw) - allowed
    if unknown:
        raise RuleError(f"portfolio: unknown keys {sorted(unknown)}")
    portfolio = PortfolioLimits(**pf_raw)
    if portfolio.on_insufficient_paper_power not in ("reject", "scale"):
        raise RuleError(
            "portfolio.on_insufficient_paper_power: must be 'reject' or 'scale'"
        )

    codes = tuple(doc.get("codes") or ())
    if not all(isinstance(c, str) for c in codes):
        raise RuleError("codes: must be a list of strings")

    return RuleSet(
        version=int(doc.get("version", 1)),
        ktype=ktype,
        rules=tuple(rules),
        portfolio=portfolio,
        source_path=src,
        sha256=hashlib.sha256(raw_bytes).hexdigest(),
        codes=codes,
    )
