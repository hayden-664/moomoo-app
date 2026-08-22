"""Which currency a row is denominated in, and how the account splits by it.

The account is multi-currency: US positions settle in USD, Bursa positions and
the MYR cash balance in MYR. Nothing downstream may add those together, so
every figure needs a currency attached to it before it is summed.

Position rows carry an explicit ``currency`` field. Deal rows do not -- but
they do carry ``deal_market``, and the market determines the settlement
currency, so it is recoverable rather than lost. An earlier version assumed it
was unrecoverable and summed deals across markets, which quietly counted a
MYR round-trip as dollars.

``accinfo_query`` is the other half: it reports ``total_assets``/``cash``/
``power`` already converted into the requested reporting currency, alongside
untouched per-currency fields (``us_cash``/``usd_assets``, ``my_cash``/
``myr_assets``, ...). The converted totals are the broker's own arithmetic and
are correct -- they just hide the split, so both are surfaced.

``account_breakdown`` also recovers the FX rate implied by that conversion,
but only as a *fallback*: it solves one equation from a single payload, so it
needs exactly one foreign currency to hold a balance.
``MoomooClient.fx_rates`` is the primary source -- it asks accinfo for the same
total in two currencies and divides, which works for any currency.
"""

from __future__ import annotations

from typing import Iterable

_NULLISH = ("", "N/A", "NONE", "NAN")

# moomoo market prefix -> settlement currency. Mainland China trades under
# SH./SZ. codes but reports as the CN market, so all three map to CNH.
MARKET_CURRENCY = {
    "US": "USD",
    "HK": "HKD",
    "CN": "CNH",
    "SH": "CNH",
    "SZ": "CNH",
    "MY": "MYR",
    "SG": "SGD",
    "JP": "JPY",
    "AU": "AUD",
    "CA": "CAD",
}

# currency -> (cash, assets, withdrawable, cash power) field names in accinfo.
# The prefixes are inconsistent in the API itself (``hk_cash`` but
# ``hkd_assets``), which is why this is a table rather than a format string.
_ACCOUNT_FIELDS = {
    "HKD": ("hk_cash", "hkd_assets", "hk_avl_withdrawal_cash", "hkd_net_cash_power"),
    "USD": ("us_cash", "usd_assets", "us_avl_withdrawal_cash", "usd_net_cash_power"),
    "CNH": ("cn_cash", "cnh_assets", "cn_avl_withdrawal_cash", "cnh_net_cash_power"),
    "JPY": ("jp_cash", "jpy_assets", "jp_avl_withdrawal_cash", "jpy_net_cash_power"),
    "SGD": ("sg_cash", "sgd_assets", "sg_avl_withdrawal_cash", "sgd_net_cash_power"),
    "AUD": ("au_cash", "aud_assets", "au_avl_withdrawal_cash", "aud_net_cash_power"),
    "CAD": ("ca_cash", "cad_assets", "ca_avl_withdrawal_cash", "cad_net_cash_power"),
    "MYR": ("my_cash", "myr_assets", "my_avl_withdrawal_cash", "myr_net_cash_power"),
}


def num(value) -> float | None:
    """Coerce an accinfo field to a float. The API writes "N/A", not null."""
    if value is None or (isinstance(value, str) and value.strip().upper() in _NULLISH):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def market_of(row: dict) -> str:
    """Market prefix for a position or deal row, from whichever field carries it."""
    for key in ("position_market", "deal_market"):
        v = row.get(key)
        if v is not None and str(v).strip().upper() not in _NULLISH:
            return str(v).strip().upper()
    return str(row.get("code") or "").split(".")[0].strip().upper()


def currency_of(row: dict, default: str = "USD") -> str:
    """Settlement currency for a position or deal row.

    Prefers the broker's own ``currency`` field when present (positions) and
    falls back to the market prefix (deals, which have no currency field).
    """
    ccy = row.get("currency")
    if ccy is not None and str(ccy).strip().upper() not in _NULLISH:
        return str(ccy).strip().upper()
    return MARKET_CURRENCY.get(market_of(row), default)


def currencies_in(rows: Iterable[dict], default: str = "USD") -> list[str]:
    """Every settlement currency appearing across position or deal rows.

    Used to decide which FX rates to fetch. It reads the rows rather than the
    account balances on purpose: a currency can matter to P&L through closed
    trades alone, long after its cash balance has gone to zero.
    """
    return sorted({currency_of(r, default) for r in rows})


def account_breakdown(acct: dict, base: str = "USD") -> dict:
    """Split the account's converted totals back into their currencies.

    Returns the per-currency cash/assets exactly as the broker reports them --
    each in its own currency, never added together -- plus the FX rate implied
    by the converted total.

    The rate is only solvable when exactly one non-base currency holds assets:
    ``total_assets`` is a single sum over every currency, so two unknown rates
    leave it underdetermined. It is reported as ``implied`` because it is read
    back out of the broker's arithmetic rather than quoted directly.
    """
    by_currency: dict[str, dict] = {}
    for ccy, (cash_f, assets_f, avl_f, power_f) in _ACCOUNT_FIELDS.items():
        cash = num(acct.get(cash_f))
        assets = num(acct.get(assets_f))
        if cash is None and assets is None:
            continue
        if not cash and not assets and ccy != base:
            continue  # currency the account simply does not hold
        by_currency[ccy] = {
            "currency": ccy,
            "cash": cash,
            "assets": assets,
            "avl_withdrawal_cash": num(acct.get(avl_f)),
            "net_cash_power": num(acct.get(power_f)),
        }

    total = num(acct.get("total_assets"))
    base_assets = by_currency.get(base, {}).get("assets")
    foreign = {
        c: b["assets"]
        for c, b in by_currency.items()
        if c != base and b["assets"]
    }

    implied_fx: dict[str, float] = {}
    if total is not None and base_assets is not None and len(foreign) == 1:
        (ccy, assets), = foreign.items()
        implied_fx[ccy] = round((total - base_assets) / assets, 6)

    for ccy, block in by_currency.items():
        rate = 1.0 if ccy == base else implied_fx.get(ccy)
        block["rate_to_base"] = rate
        block["assets_in_base"] = (
            round(block["assets"] * rate, 2)
            if rate is not None and block["assets"] is not None
            else None
        )

    # Base first, then the rest alphabetically — the same order net_pnl uses,
    # so the two blocks read down the page consistently.
    order = [base] + sorted(c for c in by_currency if c != base)
    return {
        "base": base,
        "by_currency": {c: by_currency[c] for c in order if c in by_currency},
        "implied_fx": implied_fx,
        # total_assets / cash / power / market_val on the parent object are all
        # broker-converted into `base`; say so rather than letting the UI read
        # them as pure USD.
        "converted_fields": ["total_assets", "securities_assets", "cash", "market_val", "power"],
    }
