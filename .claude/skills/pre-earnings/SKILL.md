---
name: pre-earnings
description: Factual setup description ahead of an earnings print — the date, how the stock has moved on past releases, what options are currently pricing, and your exposure if you hold it. Use for "earnings check on NVDA", "what's priced in", "how does X usually move on earnings", or before a report you care about.
---

# Pre-earnings check

Assembles what is *known* before a print: the scheduled date, the historical
reaction distribution, what implied volatility is currently pricing, and your
own exposure. It does not predict the result and does not suggest a trade.

## Steps

1. **`search`** if given a name rather than a code.
2. **`calendar`** with `kind="earnings"` — confirm the date and whether it is
   before or after the bell. Note the window is walked in 7-day slices because
   `get_earnings_calendar` rejects any span wider than that; if the print is
   further out, widen `days` rather than assuming there is no date.
3. **`earnings_reaction`** — how the stock actually moved around each past
   release. This is the core of the skill.
4. **`volatility`** — implied vol, IV rank, IV percentile, 30/60-day HV.
5. **`technicals`** — realized vol, where price sits in its range, recent trend.
6. **`positions`** — do you hold it, and how much. Skip if not held.
7. **`profile`** — only if fundamentals are relevant to the question asked.

## The comparison that carries the weight

Put three numbers side by side:

* **Historical reaction** — the typical absolute move on past prints, from
  `earnings_reaction`. Report the distribution, not just an average: the range
  and whether recent prints moved more or less than older ones.
* **Implied volatility now** — from `volatility`, with IV rank and percentile
  for context. IV rank says where current IV sits against its own past year,
  which is what makes "high IV" mean anything.
* **Realized volatility** — `realized_vol_20d_pct` and `realized_vol_60d_pct`
  from `technicals`.

The gap between implied and realized is the factual observation. State it as a
measurement — "implied is running about X points above 60-day realized" — and
stop there. Do not translate it into a view on whether options are cheap, rich,
or worth trading.

## Option chain reality on this account

`us_option_qot_right` is **`NO`**. US option chains fail with a permission
error no matter what — a subscription state, not a bug, not something to retry
or route around. Do not call `screen_options` on a US symbol as part of this
skill; it will fail and add nothing.

**The volatility question is still answerable.** `get_option_underlying_overview`
returns IV, IV rank, IV percentile and HV for US underlyings without any chain
entitlement, and that is what `volatility` uses. An option-shaped question does
not always need the chain.

HK options are LV1, so `screen_options` genuinely works there — use it for HK
names if the question is about specific contracts.

## Reporting rules

**Describe the setup, do not take a position on it.** Report the date, the
historical move distribution, current implied and realized vol, and the
exposure. Do not say whether to hold through the print, hedge, trim, or add;
do not characterise the setup as risky, attractive, or an opportunity; do not
estimate the probability of a beat or a direction.

**An implied move is arithmetic, not a forecast.** If you compute one from IV,
present it as what the options are currently pricing — a market-derived number
— and never as a prediction of what the stock will do.

**Third-party views stay attributed.** If `profile` returns analyst consensus
or a Morningstar rating, quote it as theirs. Never fold it into your own
summary line.

## Output shape

```
US.NVDA — earnings YYYY-MM-DD (after close)

Your exposure     qty · avg cost · unrealized P&L (broker-reported), or "not held"

Past reactions    last N prints: +X%, -Y%, +Z% …
                  typical absolute move ~X%; range X% to Y%

Priced now        IV XX% (rank XX, percentile XX)
                  realized 20d XX% / 60d XX%
                  implied minus realized: X points

Setup             price vs 20/50/200d, distance from 52w high/low
```

If there is no scheduled date inside the window, say so and stop — do not
speculate about when the print might land.
