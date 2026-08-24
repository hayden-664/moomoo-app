---
name: pnl-audit
description: Provenance-first P&L breakdown — separates broker-reported figures from the FIFO-derived reconstruction, names every currency bucket excluded from the totals, and attaches the conversion caveats. Use for "audit my P&L", "where does this number come from", "is my P&L accurate", "why don't these numbers add up", or before relying on P&L for anything that matters.
---

# P&L audit

The headline net P&L is assembled from sources of **different authority**. The
dashboard shows one number; this skill takes it apart and says which parts the
broker vouches for and which parts this codebase reconstructed.

Run this whenever the number is going to be used for something real — a tax
record, a performance claim, a decision — rather than glanced at.

## The four sources

`net` is built from these. They are not equally trustworthy:

| Component | Authority | Notes |
|---|---|---|
| `open.open_unrealized` | **Broker-reported** | Mark-to-market on open positions. Trust this. |
| `open.open_realized` | **Broker-reported** | Banked from partial closes. On this securities account the broker reports `realized_pl` as 0 on every position row, so this is **always zero** here. |
| `closed.closed_realized` | **DERIVED** | Positions closed outright vanish from `position_list_query`, so this is reconstructed by FIFO-matching deal history. |
| `partial.partial_realized` | **DERIVED** | Money banked selling *part* of a still-held position. Same quantity as `open_realized`, different source — only one may enter the net or it double-counts. |

`total_realized` = `closed_realized` + `partial_realized`. Both are derived, so
**`total_realized` is entirely a reconstruction.** Say that plainly.

## What the derived figures exclude

They do not account for:

* commissions and fees
* dividends
* stock splits and other corporate actions

and they are only as complete as the deal-history window they were given. A
position opened before that window has no matching buy, so its FIFO match is
incomplete.

**Never describe a derived figure as broker truth.** The API marks them
`approximate: true` precisely so this cannot happen by accident.

## Steps

1. **`pnl`** with the full lookback.
2. Report `net` and immediately decompose it into the four components above,
   labelling each as broker-reported or derived.
3. Read `window` — say what date range the FIFO matching actually saw, and note
   that trades before `matched_from` cannot be matched.
4. Read `conversion.unconverted`. **If it is non-empty, lead with it.** Those
   currencies are excluded from the totals entirely.
5. Read `by_currency` and show the unconverted per-currency truth alongside the
   converted totals.
6. Report `conversion` — which rate was used and where it came from.
7. Read `mixed_currency`. If true, the totals involved a conversion and every
   caveat below applies.

## The currency rules

This account settles in **USD and MYR**, and the currency handling exists
because of a specific bug: a Bursa round-trip once landed in the dollar total
untouched, invisible because the mixed-currency flag was derived from *open*
positions and that position had already closed.

**Figures are bucketed by settlement currency, converted, then summed — never
summed then converted.** If you are reconstructing or sanity-checking any
total, preserve that order.

**Realized figures convert at today's rate, not the trade-date rate.** The
broker reports no historical rates. A MYR gain banked months ago is stated in
today's dollars, and that can move the figure materially. Attach this caveat to
every converted realized number, every time.

**A currency with no solvable rate is left out, not folded in at 1:1.** It
appears in `conversion.unconverted`. Silently treating it as 1:1 is the
original bug — if the audit finds a non-empty `unconverted`, that is the
headline finding, not a footnote.

## Reporting rules

**This is an accounting audit, not a performance review.** Report what the
numbers are and where they came from. Do not assess whether the P&L is good,
whether the strategy is working, or what to do differently. Do not rank
positions by contribution as though that implies action.

**Do not offer tax conclusions.** The ledger is a record; what it means for any
tax filing is a question for a qualified professional. Say so if tax comes up.

## Output shape

```
Net P&L (USD)                          $X,XXX.XX
  ├─ Unrealized (broker-reported)      $X,XXX.XX
  └─ Realized (DERIVED, approximate)   $X,XXX.XX
       ├─ Closed positions             $X,XXX.XX
       └─ Partial sells                $X,XXX.XX

Derived figures exclude fees, dividends and corporate actions.
FIFO matched over YYYY-MM-DD → YYYY-MM-DD; earlier trades unmatched.

By currency (unconverted)
  USD   ...
  MYR   ...

Conversion   rate + source; realized converted at TODAY's rate
Excluded     <currencies in conversion.unconverted, or "none">
```

If `unconverted` is non-empty, put that line first and in plain language: these
currencies are missing from the total above.
