---
name: daily-review
description: Morning portfolio pass over the moomoo account — overnight moves, position P&L with its provenance intact, earnings landing this week, and currency buckets that fell out of the totals. Use for "daily review", "morning check", "how did we do overnight", "what should I look at today", or at the start of a trading day.
---

# Daily review

A fixed morning pass so the same things get checked every day in the same
order. The Telegram bot already sends raw net P&L at 08:30 — this is the layer
above that: what moved, what is scheduled, and what needs a caveat.

## Timing context

The account reports in USD and holds MYR. Local time is UTC+8, so by the time
this runs in the morning:

* **US cash session has already closed** (04:00–05:00 local, depending on DST).
  Overnight US moves are complete, not in progress.
* **HK and Bursa have not opened yet** (09:00–09:30 local). Anything you decide
  here is actionable at the Asian open.

Say which session a number came from. "NVDA -3%" means last night's close, not
something happening now.

## Steps

Run these in order. Stop and report if `health` says OpenD is down — every
other call will hang or fail, and a partial review is worse than a clear
"OpenD is not up".

1. **`health`** — confirm OpenD is reachable before anything else.
2. **`pnl`** — net P&L. Read the provenance rules below before reporting it.
3. **`positions`** — current holdings, per-position P&L.
4. **`technicals`** on each held symbol — this is free on a warm bar cache, so
   it costs nothing to run across every position. Report *changes in state*
   (RSI left the 30–70 band, price crossed an average, volume well above its
   20-day norm), not a recital of every field.
5. **`calendar`** for earnings in the next 7 days, then intersect with what you
   hold and what is on the watchlist. Flag holdings reporting inside the week.
6. **`capital_flow`** only on names that moved more than ~3% overnight. It is a
   per-symbol call against a rate-limited API — do not fan it out across the
   whole book.

## Reporting rules

These are not stylistic. They come from `AGENTS.md` and they are the difference
between a useful report and a misleading one.

**P&L provenance must survive into the summary.** `open_unrealized` is
broker-reported. `closed_realized` and `partial_realized` are *derived* by FIFO
matching over deal history — they exclude fees, dividends and corporate
actions. Never present a derived figure as broker truth. Label it approximate
every time, not just the first time.

**Name anything in `conversion.unconverted`.** Those buckets are excluded from
the totals entirely. A silently missing currency is the exact bug the currency
handling was written to fix — if that list is non-empty, say so prominently.

**Realized figures convert at today's rate, not the trade-date rate.** Carry
that caveat wherever a converted realized number appears.

**Describe, do not advise.** Report what the numbers are and what mechanically
follows from them ("RSI is 72, above the 70 line"; "price is 4% under the
50-day"). Do not say what to buy, sell, hold, trim or add, and do not rank
positions by attractiveness. If Morningstar or an analyst consensus comes up
via `profile`, attribute it — "Morningstar's fair value is X" — never adopt it.

## Output shape

This is delivered to Telegram and read on a phone. Write for that:

* **Bold sparingly** — one or two per section, on the numbers that matter. A
  paragraph where half the words are bold reads as noise.
* **No tables and no nested bullets.** Telegram renders neither. One level of
  bullets only.
* **No headings deeper than one level**, and no horizontal rules.
* **Short lines.** A sentence that wraps four times on a phone is a paragraph.
* Markdown is converted to Telegram's HTML subset on the way out — `**bold**`,
  `*italic*`, `` `code` `` and `-` bullets all survive. Anything more exotic
  does not.

Keep the whole thing short enough to read before the Asian open.

1. **Line one** — net P&L and the direction since yesterday, with the
   approximate/derived caveat attached.
2. **Movers** — positions that moved materially overnight, with the technical
   state that changed, if any.
3. **Diary** — earnings or events inside the next 7 days on names you hold or
   watch.
4. **Caveats** — unconverted currencies, stale cache, OpenD gaps, anything
   `health` flagged.

If nothing material changed, say that in one line. A quiet day should produce a
short report, not a padded one.
