"""Paper-trading engine: the only part of this app that can place an order.

Everything here is pinned to the simulated account. ``sidecar/moomoo_client.py``
is unchanged and still holds the only context pointed at the real account.

The guard runs at import, before anything else in the package can be reached,
so a bad edit crashes the sidecar on start rather than sending an order.
"""

from .guard import (  # noqa: F401
    PAPER_ACC_ID,
    PAPER_MAX_NOTIONAL_USD,
    PAPER_MAX_RISK_PCT,
    PAPER_SECURITY_FIRM,
    PAPER_TRD_ENV,
    PAPER_TRD_MARKET,
    SimulationViolation,
    assert_read_only_modules,
    assert_simulated,
    tradable_markets,
    verify_account_roster,
)

assert_simulated()
assert_read_only_modules()
