"""Technical indicators computed from the local bar cache.

Why compute these here instead of pulling them from somewhere
-------------------------------------------------------------
The bars are already in SQLite (``klines.py``) and they are moomoo's own
forward-adjusted prices. Deriving indicators from them costs no API call, no
rate-limit budget and no third-party key, and — the reason that matters most
here — the numbers stay reconcilable with every other figure the dashboard
shows. An external TA provider would quote a different adjusted series and the
chart would silently disagree with the indicator drawn on it.

No third-party TA library
-------------------------
``pandas-ta`` hard-pins ``numba==0.61.2``, which caps numpy below the 2.5 this
venv runs. Downgrading numpy inside the process that bridges a live brokerage
account to save ten formulas is a bad trade. These are hand-rolled.

TradingView parity — two silent mismatches
------------------------------------------
Both of these produce plausible-looking numbers that quietly differ from what
the chart shows, so they are deliberate:

* **Wilder smoothing is not an EMA.** RSI, ATR and ADX use Wilder's average
  (TradingView's ``ta.rma``), which is ``ewm(alpha=1/n)`` — *not*
  ``ewm(span=n)``. Using span here shifts RSI by several points. MACD is the
  exception: it genuinely is a span-based EMA.
* **Bollinger bands use population stdev.** TradingView uses ``ddof=0``;
  pandas' ``.std()`` defaults to ``ddof=1``. The difference is small but it
  moves %B enough to flip a band touch.

This module describes, it does not advise
-----------------------------------------
Same rule as ``screener.py``. Values and mechanical state only — an RSI zone is
reported as ``above_70``, never as "overbought", and no field says whether any
of this is a reason to buy or sell. The numbers are the deliverable; the
judgement is the user's.
"""

from __future__ import annotations

import math
from typing import Any

import pandas as pd

import klines

# SMA200 needs 200 *trading* days ≈ 290 calendar days. Defaulting wider than
# the longest window means the slowest average is populated rather than null.
DEFAULT_DAYS = 400

_TRADING_DAYS = 252


def _rma(series: pd.Series, length: int) -> pd.Series:
    """Wilder's smoothed average — TradingView's ``ta.rma``.

    Not interchangeable with ``ewm(span=length)``: Wilder's alpha is 1/n where
    an EMA's is 2/(n+1). See the module docstring.
    """
    return series.ewm(alpha=1.0 / length, adjust=False).mean()


def _clean(value: Any) -> Any:
    """Coerce a numpy/pandas scalar to a plain JSON-safe Python number.

    The unwrap step is not optional. ``np.float64`` subclasses Python ``float``,
    so it satisfies an ``isinstance`` check and looks handled — but ``np.int64``
    subclasses nothing, so it sails through untouched and only fails much later,
    in the response serializer, as "Unable to serialize unknown type". The bar
    cache stores volume as SQLite INTEGER, so pandas types that column int64 and
    every volume-derived field hits exactly this path.

    ``.item()`` unwraps any numpy scalar regardless of its subclassing, which is
    why it is used in preference to a type check.

    NaN/inf are not valid JSON either; None round-trips to null.
    """
    if hasattr(value, "item") and not isinstance(value, (str, bytes)):
        try:
            value = value.item()
        except (AttributeError, ValueError):
            pass
    if value is None or value is pd.NaT:
        return None
    # bool is an int subclass; leave it as a bool rather than rounding it to 1.
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        if math.isnan(value) or math.isinf(value):
            return None
        return round(float(value), 4)
    return value


def _last(series: pd.Series) -> float | None:
    if series is None or series.empty:
        return None
    return _clean(series.iloc[-1])


def _frame(bars: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(bars)
    for col in ("open", "high", "low", "close", "volume"):
        if col in df:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(subset=["close"]).reset_index(drop=True)


# -- individual indicators -------------------------------------------------
def rsi(close: pd.Series, length: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain, avg_loss = _rma(gain, length), _rma(loss, length)
    # A pure uptrend has zero average loss; RS is infinite and RSI pins at 100.
    rs = avg_gain / avg_loss.replace(0, float("nan"))
    return (100 - 100 / (1 + rs)).fillna(100 * (avg_gain > 0))


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    """Genuinely EMA-based, unlike RSI/ATR/ADX above."""
    line = close.ewm(span=fast, adjust=False).mean() - close.ewm(span=slow, adjust=False).mean()
    sig = line.ewm(span=signal, adjust=False).mean()
    return line, sig, line - sig


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev = close.shift(1)
    return pd.concat(
        [high - low, (high - prev).abs(), (low - prev).abs()], axis=1
    ).max(axis=1)


def atr(high, low, close, length: int = 14) -> pd.Series:
    return _rma(true_range(high, low, close), length)


def bollinger(close: pd.Series, length: int = 20, mult: float = 2.0):
    mid = close.rolling(length).mean()
    # ddof=0 to match TradingView; pandas defaults to 1. See module docstring.
    dev = close.rolling(length).std(ddof=0) * mult
    return mid - dev, mid, mid + dev


def stochastic(high, low, close, k: int = 14, d: int = 3):
    lowest, highest = low.rolling(k).min(), high.rolling(k).max()
    span = (highest - lowest).replace(0, float("nan"))
    pct_k = 100 * (close - lowest) / span
    return pct_k, pct_k.rolling(d).mean()


def adx(high, low, close, length: int = 14):
    """Trend *strength*, not trend direction — ADX is unsigned by construction."""
    up, down = high.diff(), -low.diff()
    plus_dm = ((up > down) & (up > 0)) * up.clip(lower=0)
    minus_dm = ((down > up) & (down > 0)) * down.clip(lower=0)
    tr = _rma(true_range(high, low, close), length).replace(0, float("nan"))
    plus_di = 100 * _rma(plus_dm, length) / tr
    minus_di = 100 * _rma(minus_dm, length) / tr
    total = (plus_di + minus_di).replace(0, float("nan"))
    return _rma((100 * (plus_di - minus_di).abs() / total), length), plus_di, minus_di


def obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    direction = close.diff().apply(lambda x: 0 if x == 0 or pd.isna(x) else (1 if x > 0 else -1))
    return (direction * volume.fillna(0)).cumsum()


def realized_vol(close: pd.Series, length: int) -> pd.Series:
    """Annualised stdev of log returns, in percent — comparable to an IV quote."""
    log_ret = (close / close.shift(1)).apply(
        lambda x: math.log(x) if x and x > 0 else float("nan")
    )
    return log_ret.rolling(length).std(ddof=0) * math.sqrt(_TRADING_DAYS) * 100


# -- assembly --------------------------------------------------------------
def _cross(fast: pd.Series, slow: pd.Series, lookback: int = 5) -> dict:
    """Did *fast* cross *slow* recently, and which way.

    A factual event with a date, not a signal: the caller decides what, if
    anything, it means.
    """
    diff = (fast - slow).dropna()
    if len(diff) < 2:
        return {"crossed_within": None, "direction": None}
    sign = diff.apply(lambda x: 1 if x > 0 else (-1 if x < 0 else 0))
    recent = sign.iloc[-(lookback + 1):]
    for i in range(len(recent) - 1, 0, -1):
        if recent.iloc[i] != 0 and recent.iloc[i - 1] != 0 and recent.iloc[i] != recent.iloc[i - 1]:
            return {
                "crossed_within": len(recent) - i,
                "direction": "above" if recent.iloc[i] > 0 else "below",
            }
    return {"crossed_within": None, "direction": None}


def _pct_from(current: float | None, reference: float | None) -> float | None:
    if not current or not reference:
        return None
    return _clean((current - reference) / reference * 100)


def technicals(code: str, days: int = DEFAULT_DAYS, ktype: str = "K_DAY") -> dict[str, Any]:
    """Full factual technical snapshot for one symbol.

    Served off the same cached bars the chart draws, so the indicator and the
    candle it sits on can never disagree.
    """
    series = klines.candles(code=code, days=days, ktype=ktype)
    bars = series.get("bars") or []
    df = _frame(bars)
    if len(df) < 2:
        return {
            "code": code,
            "ktype": ktype,
            "bars": len(df),
            "error": "not enough history to compute indicators",
        }

    close, high, low, vol = df["close"], df["high"], df["low"], df["volume"]
    price = _last(close)

    rsi14 = rsi(close)
    macd_line, macd_sig, macd_hist = macd(close)
    bb_low, bb_mid, bb_up = bollinger(close)
    k, d = stochastic(high, low, close)
    adx14, plus_di, minus_di = adx(high, low, close)
    atr14 = atr(high, low, close)
    obv_line = obv(close, vol)

    mas = {f"sma{n}": _last(close.rolling(n).mean()) for n in (20, 50, 100, 200) if len(df) >= n}
    ema = {f"ema{n}": _last(close.ewm(span=n, adjust=False).mean()) for n in (12, 26)}

    # 52-week extremes, capped at whatever history actually came back.
    window = min(len(df), _TRADING_DAYS)
    high_52 = _clean(high.tail(window).max())
    low_52 = _clean(low.tail(window).min())

    atr_val = _last(atr14)
    bb_l, bb_m, bb_u = _last(bb_low), _last(bb_mid), _last(bb_up)
    pct_b = None
    if None not in (price, bb_l, bb_u) and bb_u != bb_l:
        pct_b = _clean((price - bb_l) / (bb_u - bb_l))

    rsi_val = _last(rsi14)
    avg_vol_20 = _last(vol.rolling(20).mean())
    last_vol = _last(vol)

    return {
        "code": code,
        "ktype": ktype,
        "as_of": bars[-1].get("time") if bars else None,
        "bars": len(df),
        "source": series.get("source"),
        "price": price,
        "trend": {
            **mas,
            **ema,
            # Distance from each average, in percent — the raw geometry, with
            # no claim about what being above or below one implies.
            "pct_from": {name: _pct_from(price, val) for name, val in mas.items()},
            "ma_order": [
                name for name, _ in sorted(
                    ((n, v) for n, v in mas.items() if v is not None),
                    key=lambda kv: kv[1],
                    reverse=True,
                )
            ],
            "adx14": _last(adx14),
            "plus_di14": _last(plus_di),
            "minus_di14": _last(minus_di),
        },
        "momentum": {
            "rsi14": rsi_val,
            "rsi_zone": (
                None if rsi_val is None
                else "above_70" if rsi_val > 70
                else "below_30" if rsi_val < 30
                else "30_to_70"
            ),
            "macd": _last(macd_line),
            "macd_signal": _last(macd_sig),
            "macd_histogram": _last(macd_hist),
            "macd_cross": _cross(macd_line, macd_sig),
            "stoch_k": _last(k),
            "stoch_d": _last(d),
        },
        "volatility": {
            "atr14": atr_val,
            # ATR as a share of price is the comparable form: $3 of daily range
            # means something different on a $20 stock than on a $400 one.
            "atr_pct_of_price": _pct_from(price + (atr_val or 0), price) if atr_val else None,
            "bollinger": {
                "lower": bb_l,
                "middle": bb_m,
                "upper": bb_u,
                "percent_b": pct_b,
                "bandwidth_pct": (
                    _clean((bb_u - bb_l) / bb_m * 100)
                    if None not in (bb_u, bb_l, bb_m) and bb_m else None
                ),
            },
            "realized_vol_20d_pct": _last(realized_vol(close, 20)),
            "realized_vol_60d_pct": _last(realized_vol(close, 60)),
        },
        "volume": {
            "last": last_vol,
            "avg_20d": avg_vol_20,
            "vs_avg_20d_pct": _pct_from(last_vol, avg_vol_20),
            "obv": _last(obv_line),
            "obv_change_20d": (
                _clean(obv_line.iloc[-1] - obv_line.iloc[-21]) if len(obv_line) > 21 else None
            ),
        },
        "range": {
            "high_52w": high_52,
            "low_52w": low_52,
            "pct_from_52w_high": _pct_from(price, high_52),
            "pct_from_52w_low": _pct_from(price, low_52),
            "window_trading_days": window,
        },
        "returns_pct": {
            label: _pct_from(price, _clean(close.iloc[-(n + 1)]))
            for label, n in (("1d", 1), ("5d", 5), ("1m", 21), ("3m", 63), ("6m", 126), ("1y", 252))
            if len(close) > n
        },
        "note": (
            "Derived from moomoo's forward-adjusted bars. Values only — these "
            "describe price behaviour and imply no view on it."
        ),
    }
