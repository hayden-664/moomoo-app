---
name: position-check
description: Full factual workup on a single symbol — your cost basis and P&L if you hold it, technicals, realized vs implied volatility, upcoming earnings, past earnings reactions, capital flow and company profile. Use for "check NVDA", "look at AMD", "what's the story on X", or before an earnings print.
---

# Position check

Everything the account knows about one symbol, assembled in one pass. Works
whether or not the symbol is held — for an unheld name the position and cost
sections are simply absent.

## Resolving the symbol

Accept a plain name or a ticker. If the user says "Nvidia" or "AMD" rather than
`US.NVDA`, resolve it with `search` first. Do not guess a market prefix:
`search` returns the correct code, and guessing `US.` for a Hong Kong or Bursa
listing produces a confusing empty result rather than an error.

If `search` returns several matches, ask which one rather than picking.

## Steps

1. **`search`** — resolve to a canonical code, unless already given one.
2. **`quote`** — current price and the day's numbers.
3. **`positions`** — is it held? If so, pull quantity, cost basis and
   per-position P&L. Skip cleanly if not.
4. **`technicals`** — trend, momentum, volatility, volume, 52-week range.
5. **`volatility`** — implied vol, IV rank, IV percentile, 30/60-day HV.
6. **`profile`** — company fundamentals and, if present, third-party views.
7. **`calendar`** — is there an earnings date inside the next few weeks?
8. **`earnings_reaction`** — how the stock has historically moved on prints.
9. **`capital_flow`** — recent money flow.
10. **`deal_history`** — your own past trades in this name, if any.

Run these in sequence rather than all at once; the quote APIs are rate-limited
to 10 calls per 30 seconds.

## The volatility comparison is the useful part

`technicals` gives **realized** volatility (what the stock actually did) and
`volatility` gives **implied** (what options are pricing). Put them side by
side — that comparison is available even on US names where the option chain
itself is not.

This matters because `us_option_qot_right` is `NO` on this account, so
`screen_options` fails on every US symbol. That is a subscription state, not a
bug, and not something to retry or work around. But
`get_option_underlying_overview` still returns IV, IV rank, IV percentile and
HV for US underlyings, which is what `volatility` uses. So an option-shaped
question about a US name is still answerable — just not via the chain.

For HK names the chain does work (HK options are LV1), so `screen_options` is
worth running there.

## Reporting rules

**Describe, do not advise.** Report the mechanics: cost, breakeven, required
move, IV versus HV, where price sits relative to its averages and its 52-week
range. Do not conclude with a view on whether to buy, sell, hold, add or trim,
and do not characterise the setup as good, attractive or risky.

**Third-party opinions are quoted, never adopted.** `profile` may return
analyst consensus and Morningstar's star rating and fair value. Those are
directional opinions published by outside firms, and they are the only
directional content in this whole system. Attribute them explicitly —
"Morningstar puts fair value at $X" — and never restate them as the app's own
assessment or fold them into a summary line.

**If the symbol is held, keep P&L provenance intact.** Per-position unrealized
P&L is broker-reported. Anything realized is derived by FIFO matching and
excludes fees, dividends and corporate actions — label it approximate.

## Output shape

```
US.NVDA — NVIDIA          $XXX.XX  (+X.X% today)

Position     qty · avg cost · unrealized P&L (broker-reported)
Trend        price vs 20/50/200d, ADX, MA order
Momentum     RSI + zone, MACD state
Volatility   realized 20d/60d vs implied, IV rank/percentile
Range        distance from 52w high and low
Flow         recent capital flow direction
Diary        next earnings date, if inside a few weeks
History      how it moved on the last several prints
Your trades  past fills in this name, if any
Third party  attributed, if profile returned any
```

Omit sections with no data rather than printing empty headings.
