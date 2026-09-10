"""Static and runtime guarantees that this package can only touch a paper account.

The mirror image of ``moomoo_client.assert_read_only``. That guard says a trade
method may appear *nowhere* in its file; this one says the order methods may
appear in exactly one file, ``broker.py``, and that nothing anywhere under
``paper/`` may reference the real trading environment or unlock a real account.

Three weaknesses of the original guard are fixed here, because this package is
the one that can actually send an order:

* It reads only its own ``__file__``. This walks every module in the package.
* It matches the literal text ``.name(``, so ``getattr(ctx, "place_order")``
  and ``f = ctx.place_order; f(...)`` both slip past. This parses the source
  and inspects AST nodes, so both are caught and comments and docstrings
  cannot produce a false positive.
* Its list omits ``change_order`` and ``place_combo_order``, which are separate
  public entry points into the same order pipeline. Both are covered here.

The names being denied have to be written down somewhere, and writing them down
would otherwise trip the scan. Rather than excluding lines by text - the
original's approach, and bypassable - the definitions live in one block below
and the walker skips those exact subtrees by node identity.
"""

import ast
import os
from pathlib import Path

import moomoo as mm

import config

# --- denylist definitions -----------------------------------------------------
# _skip_nodes() finds these assignments by target name and excludes their
# subtrees from the scan, so naming a forbidden symbol here is not a violation
# while naming it anywhere else in this package still is.
_DENYLIST_ASSIGNMENTS = frozenset(
    {"_DENY_ANYWHERE", "_ORDER_METHODS", "_EXPECTED_READ_ENV", "_REAL_ENV_ATTR"}
)

# Unlocking is what arms a real account. Simulated trading returns NoNeedUnlock
# for a non-REAL market, so there is no legitimate use of it in this repo.
_DENY_ANYWHERE = frozenset({"unlock_trade"})

# Every public entry point into the order pipeline. Permitted in broker.py only.
_ORDER_METHODS = frozenset(
    {
        "place_order",
        "modify_order",
        "change_order",
        "place_combo_order",
        "cancel_all_order",
    }
)

# The read client must stay pointed at the real account; if MOOMOO_TRD_ENV were
# ever flipped, the dashboard would silently report the paper account as "the
# portfolio". Held here so the scan below does not flag its own expectation.
_EXPECTED_READ_ENV = "REAL"
_REAL_ENV_ATTR = "REAL"

# --- the pin ------------------------------------------------------------------
# Deliberately module constants and not environment variables. An env var is
# exactly the thing that gets copied wrong between machines, and there is no
# use case for pointing this package at a different account. PAPER_ENABLED can
# turn the engine off; nothing can turn it somewhere else.
PAPER_ACC_ID = 5367857
PAPER_TRD_ENV = mm.TrdEnv.SIMULATE
PAPER_SECURITY_FIRM = mm.SecurityFirm.FUTUINC
PAPER_TRD_MARKET = mm.TrdMarket.US
REAL_ACC_ID = 286260077243931592

# Absolute ceiling on a single order, independent of anything in rules.json.
PAPER_MAX_NOTIONAL_USD = float(os.getenv("PAPER_MAX_NOTIONAL_USD", "25000"))
PAPER_MAX_RISK_PCT = float(os.getenv("PAPER_MAX_RISK_PCT", "2.0"))

_PKG_DIR = Path(__file__).resolve().parent
_SIDECAR_DIR = _PKG_DIR.parent
_BROKER_FILE = "broker.py"

# Read-path modules that must never gain a write call. moomoo_client.py has its
# own import-time guard; this covers the rest, and covers the forms its guard
# cannot see.
_READ_ONLY_MODULES = (
    "moomoo_client.py",
    "pnl.py",
    "klines.py",
    "research.py",
    "screener.py",
    "indicators.py",
    "scheduler.py",
    "app.py",
    "currency.py",
    "telegram.py",
)


class SimulationViolation(RuntimeError):
    """A static or runtime check proving paper-only operation has failed."""


def _skip_nodes(tree: ast.Module) -> set[int]:
    """Node ids belonging to the denylist definitions above.

    Identified by assignment target, never by line text, so the exclusion
    cannot be widened by writing a magic string into an unrelated line.
    """
    skip: set[int] = set()
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        names = {t.id for t in node.targets if isinstance(t, ast.Name)}
        if names & _DENYLIST_ASSIGNMENTS:
            for child in ast.walk(node):
                skip.add(id(child))
    return skip


def _collect(tree: ast.Module, skip: set[int]) -> tuple[set, set, set]:
    """(attribute names, string constants, getattr string arguments)."""
    attrs: set[str] = set()
    consts: set[str] = set()
    getattrs: set[str] = set()
    for node in ast.walk(tree):
        if id(node) in skip:
            continue
        if isinstance(node, ast.Attribute):
            attrs.add(node.attr)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            consts.add(node.value)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "getattr" and len(node.args) >= 2:
                arg = node.args[1]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    getattrs.add(arg.value)
    return attrs, consts, getattrs


def _parse(path: Path) -> tuple[ast.Module, set[int]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return tree, _skip_nodes(tree)


def _violation(path: Path, symbol: str, why: str) -> SimulationViolation:
    return SimulationViolation(
        f"simulation violation: {path.name} references {symbol!r} - {why}"
    )


def assert_simulated() -> None:
    """Refuse to import if this package could touch the real account.

    Raised at import time so a bad edit is a startup crash rather than a live
    order. Runs on every module in the package, including this one.
    """
    for path in sorted(_PKG_DIR.glob("*.py")):
        tree, skip = _parse(path)
        attrs, consts, getattrs = _collect(tree, skip)
        named = attrs | consts | getattrs

        for symbol in sorted(_DENY_ANYWHERE & named):
            raise _violation(
                path, symbol, "simulated trading never needs it and it arms the real account"
            )

        if _REAL_ENV_ATTR in named:
            raise _violation(
                path,
                _REAL_ENV_ATTR,
                "the real trading environment must not be nameable in this package",
            )

        if path.name != _BROKER_FILE:
            for symbol in sorted(_ORDER_METHODS & named):
                raise _violation(
                    path, symbol, f"order methods belong in {_BROKER_FILE} only"
                )

    # Config coherence. Both of these would silently repoint the read client.
    if config.TRD_ENV != _EXPECTED_READ_ENV:
        raise SimulationViolation(
            f"MOOMOO_TRD_ENV is {config.TRD_ENV!r}; the read client must stay on the "
            f"real account or the dashboard reports the paper account as the portfolio"
        )
    if config.ACC_ID == PAPER_ACC_ID:
        raise SimulationViolation(
            f"MOOMOO_ACC_ID is the paper account {PAPER_ACC_ID}; the read client must "
            f"point at the real account"
        )
    if PAPER_ACC_ID == 0:
        raise SimulationViolation("PAPER_ACC_ID is unset")


def assert_read_only_modules() -> None:
    """Close the original guard's blind spots without editing it.

    ``moomoo_client.assert_read_only`` stays exactly as written. This scans the
    same file and its siblings for attribute access and getattr indirection,
    which that guard's substring match cannot see. String constants are not
    checked here on purpose: moomoo_client's own ``_FORBIDDEN`` tuple is a list
    of these names and is not a call.
    """
    for name in _READ_ONLY_MODULES:
        path = _SIDECAR_DIR / name
        if not path.exists():
            continue
        tree, skip = _parse(path)
        attrs, _consts, getattrs = _collect(tree, skip)
        for symbol in sorted((_ORDER_METHODS | _DENY_ANYWHERE) & (attrs | getattrs)):
            raise _violation(path, symbol, "read-path modules must never write")


def verify_account_roster(acc_rows: list[dict]) -> None:
    """Runtime half of the guarantee, re-checked before every write.

    Static analysis cannot catch moomoo re-issuing an account id, or OpenD
    being pointed at a different login. The paper context is built with a
    security firm the real account does not match, so its account list should
    contain exactly one row; anything else means the isolation premise this
    package is built on no longer holds.
    """
    if len(acc_rows) != 1:
        raise SimulationViolation(
            f"paper context sees {len(acc_rows)} accounts, expected exactly 1: "
            f"{[r.get('acc_id') for r in acc_rows]}"
        )
    row = acc_rows[0]
    if row.get("acc_id") != PAPER_ACC_ID:
        raise SimulationViolation(
            f"paper context resolved acc_id {row.get('acc_id')}, expected {PAPER_ACC_ID}"
        )
    if row.get("trd_env") != PAPER_TRD_ENV:
        raise SimulationViolation(
            f"account {PAPER_ACC_ID} reports trd_env {row.get('trd_env')!r}, "
            f"expected {PAPER_TRD_ENV!r}"
        )
    auth = row.get("trdmarket_auth") or []
    if "US" not in auth:
        raise SimulationViolation(
            f"account {PAPER_ACC_ID} has trdmarket_auth {auth}, expected US"
        )


def tradable_markets(acc_rows: list[dict]) -> tuple[str, ...]:
    """Markets the paper account may trade, read from the account itself.

    Not from MKT_ENV_ENABLE_MAP: that map has (HK, SIMULATE) = True, so an HK
    symbol would pass a local check and then fail at the broker.
    """
    return tuple(acc_rows[0].get("trdmarket_auth") or ())
