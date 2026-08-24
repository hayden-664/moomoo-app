---
name: watchlist-sweep
description: Scan every symbol on the moomoo watchlist for technical state changes — moving-average crosses, RSI band exits, volume spikes, 52-week extremes — and report only what changed since the last sweep. Use for "sweep the watchlist", "anything happening in my watchlist", "what changed", or "scan my names".
---

# Watchlist sweep

The watchlist runs to dozens of symbols across many groups. Checking them by
hand does not scale, and re-reading unchanged names every day is noise. This
sweeps all of them and reports only the deltas.

## Why this is cheap — and where it is not

`technicals` computes from the local SQLite bar cache, so a symbol whose bars
are already warm costs **zero broker calls**. That is what makes a full sweep
practical.

The exception matters: the *first* pull of a symbol at a given period is
metered against the historical-kline quota, charged **per symbol per period**.
A sweep over names never pulled before can burn a large slice of that budget in
one run.

**So: call `health` first, and check the kline quota before a first-ever sweep
or after adding many new names.** If remaining quota is lower than the number
of cold symbols, sweep the held positions first and report that the rest were
skipped for quota — do not silently truncate, and do not spend the whole budget
without saying so.

## Steps

1. **`health`** — stop here if OpenD is down.
2. **`watchlist`** — pull the groups and their symbols. Sweep every group
   unless the user named one.
3. **`technicals`** per symbol. Run them in sequence, not a burst — the quote
   APIs are rate-limited to 10 calls per 30 seconds and a burst trips them.
4. **Diff against the previous sweep**, if one exists (see below).
5. Report only symbols whose state changed, plus a one-line count of how many
   were checked and unchanged.

## State file

Persist each sweep to `sidecar/.cache/sweep-state.json` as
`{code: {as_of, rsi_zone, ma_order, macd_direction, pct_from_52w_high}}`.

On the next run, compare and report transitions. Without this file the sweep
can only report current state, which is far less useful — say explicitly that
it is a first run and there is no baseline yet.

The cache directory is already gitignored as a build artifact; keep the state
file there rather than in the repo proper.

## What counts as a change worth reporting

Report a symbol when any of these flipped since the last sweep:

* **RSI zone** — moved between `below_30`, `30_to_70`, `above_70`.
* **Moving-average order** — `ma_order` changed, e.g. the 20-day crossed the
  50-day. Report the specific pair that crossed, not just "MAs changed".
* **MACD cross** — `macd_cross.crossed_within` is 1 or 2 bars, meaning it
  crossed since the last sweep.
* **Volume** — `vs_avg_20d_pct` above roughly +100%, i.e. double its 20-day
  norm.
* **52-week extreme** — `pct_from_52w_high` within ~1% of zero, or
  `pct_from_52w_low` within ~1%.
* **Bollinger** — `percent_b` above 1 or below 0, meaning price closed outside
  the band.

Symbols with none of these are counted, not listed.

## Reporting rules

**These are measurements, not signals.** A golden cross is a fact about two
averages; it is not a reason to buy, and this skill must not present it as one.
Report the mechanical event and the numbers behind it. Do not rank the sweep
output by attractiveness, do not label symbols as opportunities, and do not
conclude with a suggested action.

Group the output by watchlist group, since the groups already encode how these
names are being thought about. Within each group, list changed symbols with the
transition and the current value:

```
Semis
  US.AMD   RSI 68 → 72 (entered above_70); volume 2.1× 20d avg
  US.NVDA  20d crossed above 50d; price 2.3% under 52w high
```

Close with: `Checked N symbols across M groups; K changed.`

If the sweep was truncated for quota or rate limits, say exactly which symbols
were not reached.
