<!-- BEGIN:nextjs-agent-rules -->

# This is NOT the Next.js you know

This version has breaking changes — APIs, conventions, and file structure may all differ from your training data. Read the relevant guide in `node_modules/next/dist/docs/` (resolved from this file's directory; in monorepos the `next` package may not be visible from the repo root) before writing any code. Heed deprecation notices.

This block is written and re-added by `next dev` — verify at `node_modules/next/dist/server/lib/generate-agent-files.js`. Removing it from a diff only re-creates the uncommitted change; committing it with your work keeps the tree clean.

<!-- END:nextjs-agent-rules -->

# This app is read-only against a live brokerage account

Do not add order placement, modification, or cancellation — not to the sidecar,
not to the Next.js proxy, not "behind a flag". `sidecar/moomoo_client.py` runs
`assert_read_only()` at import and will crash the process if `place_order`,
`modify_order`, or `unlock_trade` appear in it. The proxy at
`src/app/api/moomoo/[...path]/route.ts` uses an explicit route allowlist for
the same reason. Both guards are deliberate; do not relax them to make a
feature work.

# OpenD blocks forever when it is down

The moomoo SDK retries a refused connection every 6 seconds indefinitely and
blocks the calling thread while it does. Any new code path that touches an SDK
context must go through `require_opend()` / `opend_reachable()` first, or the
endpoint will hang instead of returning an error.

# P&L provenance must stay visible

Net P&L mixes broker-reported figures with a FIFO reconstruction over deal
history. Never present the derived `closed_realized` as broker truth — it
excludes fees, dividends and corporate actions. If you add P&L surfaces, carry
the approximate/derived labelling with them.

# Currencies are converted before they are summed, never after

This account settles in USD *and* MYR. `pnl.py` buckets every figure by
settlement currency, converts each bucket into `MOOMOO_CURRENCY`, then sums.
Adding buckets without converting them is the bug this was written to fix — a
Bursa round-trip counted as dollars, invisible because the mixed-currency flag
was derived from open positions and that position was already closed.

`currency_of()` recovers a deal row's currency from `deal_market`; deal rows
have no `currency` field, which is what made the mixing look unavoidable.

The rate is moomoo's own. There is no forex feed to quote — the `FX` market
returns "Unsupported quote market" and no entitlement exists for it — but
`accinfo_query` converts the whole account into whichever currency it is asked
for, so `MoomooClient.fx_rates()` asks twice and divides the `total_assets`.
Do not replace this with an external FX API without saying so: it would stop
the dashboard reconciling with moomoo's own numbers.

`currency.account_breakdown()`'s `implied_fx` is the fallback only. It solves
one equation from a single payload, so it needs exactly one foreign currency to
hold a balance. When neither source can price a currency it goes into
`conversion.unconverted` and is left out of the totals — never folded in at
1:1. Keep that behaviour and keep it visible in the UI.

Realized figures are converted at today's rate, not the trade-date rate. Carry
that caveat wherever you present them.

# The screener describes, it does not advise

`characterise()` in `sidecar/screener.py` emits factual contract mechanics —
cost, breakeven, required move, IV, liquidity. Keep it that way. Do not add
directional language, scoring, or "best trade" ranking.

# moomoo API limits that fail silently

All four of these were hit during the initial build. None produce an obvious
error at the call site, and three of them return plausible-looking empty or
zero results instead:

- **`history_deal_list_query` caps the window at 360 days.** Anything wider
  errors. `deal_history()` chunks into 359-day slices and dedupes on `deal_id`
  because chunk boundaries are inclusive at both ends.
- **`position_list_query` and `accinfo_query` are rate-limited to 10 calls per
  30 seconds too**, not just deal history. The dashboard polls and the P&L
  range selector refetches, so bursts are easy to produce; both are served from
  a 5s cache in `moomoo_client` with the last good value on failure. Without
  that, tripping the limit 502s `/pnl` and blanks the whole dashboard.
- **`history_deal_list_query` is rate-limited to 10 calls per 30 seconds.**
  A 15s dashboard poll trips this. Results are cached for 5 minutes and the
  last good value is served on failure — otherwise a transient limit silently
  zeroes closed P&L and net P&L visibly flickers.
- **`get_corporate_actions_dividends` is rate-limited to 30 calls per 30
  seconds**, fewer than the symbols this account has held. `dividends.py`
  caches each history for 12h and fetches at most 25 uncached per request;
  the rest show under `unavailable` until a later request fills them. Its
  payload is free text in two shapes — `"Cash Dividend: 1.65 USD Per Share"`
  with `10/06/2026` dates, and `"Cash Dividend: 1.8226USD"` with
  `Sep 28, 2026` dates (VOO). Anything else lands in `unparsed`, never dropped.
- **`get_option_chain` caps the expiry span at 30 days.** `screen()` walks the
  requested window in 29-day slices.
- **`OptionDataFilter.delta_min/max` take RAW delta (0.15), not percent.**
  Passing `15` matches nothing and returns an empty chain with no error. This
  one is invisible: it looks like "no contracts qualified".
- **`get_earnings_calendar` caps begin/end at 7 days.** `research.calendar()`
  walks longer windows in 7-day slices.
- **Research payload shapes are not uniform.** Most quote calls return
  `(ret, DataFrame)`, but the ranked/paged ones return `(ret, (count, frame))`
  and `get_economic_calendar` returns a 4-tuple with a paging cursor.
  `_unwrap_any` reads only the first two slots for that reason; `_unwrap`
  raises on unpack against either.
- **`get_stock_filter` results are objects, not rows.** Values live in
  `FilterStockData.__dict__`, keyed by plain strings for simple fields but by
  *tuples* for accumulate/financial ones — `(field, days)`, `(field, quarter)`.
  Those keys are not JSON-serialisable, so `MoomooClient.stock_filter`
  flattens them. Screener percent fields also take whole numbers: `20` is 20%.

# Volatility does not need the option chain

`get_option_underlying_overview` returns IV, IV rank, IV percentile and
30/60-day HV for an underlying without any option-chain entitlement.
`research.volatility()` uses it. Do not assume an option-shaped question needs
the chain — it was the only route while US options were unentitled, and is
still the cheaper one.

# Third-party ratings are quoted, never adopted

`research.profile()` returns analyst consensus and Morningstar's star rating
and fair value under `third_party_views`. Unlike everything else in this
codebase those are directional opinions published by outside firms. They are
carried with a `source` note so they can be reported as "Morningstar says X".
Never surface them as the app's own view, and never let them leak into a
surface that otherwise only reports derived numbers.

# Quote entitlements gate what works per market

Entitlements change with subscriptions, so read `/permissions` rather than
trusting this file. As of 2026-10: US stocks LV3, US options LV1 (was `NO`
until then; chains now work), HK stocks and options LV1, and **MY, SG and JP
stocks `NO`**. A missing entitlement is a subscription, not a bug — the
screener UI and `scheduler._entitled_markets()` read the map live.

For Bursa (`MY.*`) that means positions, P&L, deal history, `search` and the
stock screener work, but `quote`, `candles`, `technicals`, `profile`,
`capital_flow` and dividend history fail with "No permission to get quotes".
moomoo has no earnings calendar for MY at all, regardless of entitlement;
`research.calendar()` returns `unsupported` for it rather than an error.

# Dividends are partly inside net P&L already

`dividends.py` estimates dividends as per-share history x shares held going
into each ex-date. The broker folds a held position's dividends into that
row's `realized_pl` on the payable date (NEE, AVGO and VOO reconcile to the
cent), and `pnl.py` puts that figure into net. Dividends from holdings since
closed or re-opened are in net nowhere. So the result splits `paid` into
`paid_in_net` and `paid_not_in_net` — never add `paid` to net. Figures are
gross of withholding tax; keep saying so.
