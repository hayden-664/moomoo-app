"""Runtime configuration for the moomoo sidecar.

Everything is read from the environment so no credentials or account
identifiers are ever committed. OpenD itself holds the brokerage session;
this process only ever talks to it over localhost.
"""

import os

# --- OpenD connection -------------------------------------------------------
OPEND_HOST = os.getenv("OPEND_HOST", "127.0.0.1")
OPEND_PORT = int(os.getenv("OPEND_PORT", "11111"))

# --- Account selection ------------------------------------------------------
# TRD_MARKET: US | HK | CN | SG | AU | JP
TRD_MARKET = os.getenv("MOOMOO_TRD_MARKET", "US")
# SECURITY_FIRM: FUTUINC (moomoo US) | FUTUSG | FUTUAU | FUTUSECURITIES (HK)
SECURITY_FIRM = os.getenv("MOOMOO_SECURITY_FIRM", "FUTUINC")
# Preferred: pin the exact account by id. Selecting by list index is fragile
# because a single security-firm/market filter can return both the real and
# the simulated account, and their order is not guaranteed.
ACC_ID = int(os.getenv("MOOMOO_ACC_ID", "0"))
# Fallback only, used when ACC_ID is 0.
ACC_INDEX = int(os.getenv("MOOMOO_ACC_INDEX", "0"))
# Reporting currency for account/position aggregation.
CURRENCY = os.getenv("MOOMOO_CURRENCY", "USD")

# How far back to pull deal history for P&L. Every caller uses this same value
# so they share one cache entry: the reporting window is narrowed afterwards in
# pnl.py, which costs nothing, whereas a second lookback would mean a second
# round of chunked history calls against a 10-per-30s rate limit.
PNL_LOOKBACK_DAYS = int(os.getenv("MOOMOO_PNL_LOOKBACK_DAYS", "1825"))

# --- Safety -----------------------------------------------------------------
# This sidecar is read-only by construction; see moomoo_client.assert_read_only.
# TrdEnv.REAL is used for reads only. No order-placing call exists in this
# codebase, and the guard below fails loudly if one is ever introduced.
TRD_ENV = os.getenv("MOOMOO_TRD_ENV", "REAL")

# --- Sidecar HTTP -----------------------------------------------------------
SIDECAR_HOST = os.getenv("SIDECAR_HOST", "127.0.0.1")
SIDECAR_PORT = int(os.getenv("SIDECAR_PORT", "8788"))
