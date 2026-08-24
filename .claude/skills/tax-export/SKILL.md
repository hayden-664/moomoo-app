---
name: tax-export
description: Export executed deals to a spreadsheet ledger — every fill with date, side, quantity, price, settlement currency and FIFO round-trip matching, with the FX and fee limitations stated on the sheet itself. Use for "export my trades", "trade journal", "tax export", "give me a ledger of my fills", or at financial year end.
---

# Trade ledger export

Turns deal history into a spreadsheet that can be handed to an accountant or
kept as a record. The point of this skill is that the **limitations travel with
the data** — a ledger that silently misstates FX or omits fees is worse than no
ledger.

Use the `xlsx` skill to produce the file.

## Steps

1. **`health`** — stop if OpenD is down.
2. **`deal_history`** over the requested window. Note the hard limits below.
3. **`account`** — capture the current FX rate and reporting currency for the
   rate sheet.
4. FIFO-match buys against sells per symbol to build round-trips, using the
   same ordering `pnl.py` uses so the ledger reconciles with the dashboard.
5. Build the workbook (below) with the `xlsx` skill.
6. State every limitation in the chat response *as well as* on the sheet.

## Hard limits to respect

**`history_deal_list_query` caps the window at 360 days.** The client chunks
into 359-day slices and dedupes on `deal_id` because chunk boundaries are
inclusive at both ends. A multi-year export is therefore several calls — and
deal history is rate-limited to 10 calls per 30 seconds, so do not loop
aggressively. Results are cached 5 minutes.

**The export is only as complete as the window.** A position opened before the
first date has no matching buy in the data, so its round-trip cannot be
completed. Mark those rows explicitly as unmatched rather than dropping them or
guessing a cost basis.

## The two things this export cannot do

Both must appear on the sheet, not just in conversation.

### 1. There is no trade-date FX rate

moomoo publishes **no historical exchange rates**. `MoomooClient.fx_rates()`
reads *today's* rate by asking `accinfo_query` for the same total in two
currencies and dividing — a live rate, not a historical one. The `FX` market
returns "Unsupported quote market" and no entitlement exists for it.

So a MYR trade from eight months ago **cannot** be converted at the rate that
applied on its trade date using anything in this codebase.

Handle it this way:

* Record every deal in its **own settlement currency**, unconverted. That
  column is the authoritative one.
* Add a converted column only if asked, computed at **today's** rate, with the
  rate and the date it was read stamped in the header.
* Put a note on the sheet: converted figures use the rate as at `<date>`, not
  the trade date.
* If trade-date accuracy is actually needed, the rates have to come from
  outside — the user's own broker contract notes, or a published daily series
  such as the central bank's. Say that rather than producing a number that
  looks precise and is not.

`currency_of()` recovers a deal's currency from `deal_market`; deal rows carry
no `currency` field of their own.

### 2. Fees and dividends are not in the deal rows

The FIFO reconstruction excludes commissions, fees, dividends and corporate
actions — the same limitation `pnl-audit` reports. Check whether the deal rows
carry any fee field; if they do not, say so explicitly rather than leaving a
blank column that reads as zero cost.

Fees live on the broker's contract notes and monthly statements. If a
fee-accurate ledger is needed, those documents are the source.

## Workbook shape

**Sheet 1 — Fills.** One row per executed deal, in date order:
`date · code · name · side · quantity · price · settlement currency · deal_id · market`

**Sheet 2 — Round trips.** FIFO-matched pairs:
`code · open date · close date · quantity · avg buy · avg sell · gross P&L · currency · holding days`
Unmatched positions get a row flagged `unmatched — opened before window`.

**Sheet 3 — Notes.** Written before the data is considered done:
* Window covered, and that earlier trades are unmatched
* FX rate used, the date it was read, and that it is **not** the trade-date rate
* That fees, dividends and corporate actions are excluded
* That round-trip P&L is derived by FIFO reconstruction, not broker-reported

Freeze the header row and format dates and currency columns properly — this is
a document someone else may read.

## Reporting rules

**This produces a record, not tax advice.** Do not calculate tax owed, classify
gains as short or long term for any jurisdiction, apply exemptions, or advise
on treatment. If the user asks what any of it means for their filing, say
plainly that it is a question for a qualified tax professional and hand them
the ledger.

**Do not assess the trading.** No win rate framed as performance, no best and
worst trades leaderboard, no observations about what is working. It is a
ledger.
