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

# Volatility survives the option entitlement gap

`us_option_qot_right` is `NO`, so chains fail — but
`get_option_underlying_overview` still returns IV, IV rank, IV percentile and
30/60-day HV for US underlyings. `research.volatility()` uses it, and it is
the only way to answer "is this name's option pricing rich" on this account.
Do not assume an option-shaped question needs the chain.

# Third-party ratings are quoted, never adopted

`research.profile()` returns analyst consensus and Morningstar's star rating
and fair value under `third_party_views`. Unlike everything else in this
codebase those are directional opinions published by outside firms. They are
carried with a `source` note so they can be reported as "Morningstar says X".
Never surface them as the app's own view, and never let them leak into a
surface that otherwise only reports derived numbers.

# Quote entitlements gate the screener

`us_option_qot_right` is `NO` on this account, so US option chains fail with a
permission error no matter what the code does — it is a subscription, not a
bug. `/permissions` exposes the entitlement map and the screener UI warns
before running. US *stocks* are LV3 and HK options are LV1.

# The real account is read-only; a separate module trades a simulated one

`sidecar/moomoo_client.py` is unchanged and stays that way. It holds the only
context pointed at the real account, and `assert_read_only()` still crashes the
process if a trade method appears in it. Everything that can place an order
lives under `sidecar/paper/`, behind its own `OpenSecTradeContext` pinned to
`TrdEnv.SIMULATE` and account `5367857`.

The pin is not configurable. `PAPER_ACC_ID` is a module constant, not an
environment variable, because an environment variable is exactly the thing that
gets copied wrong between machines. `PAPER_ENABLED` can turn the engine off;
nothing can point it somewhere else.

Four independent things have to fail before a real order is possible:

1. `paper/guard.py::assert_simulated()` parses every module under `paper/` with
   `ast` and refuses to import if `unlock_trade` appears anywhere, if the real
   trading environment is nameable at all, or if `place_order` / `modify_order`
   / `change_order` / `place_combo_order` / `cancel_all_order` appear anywhere
   except `paper/broker.py`. It walks the AST rather than matching `.name(` as
   text, so `getattr(ctx, "place_order")` and a bound method in a variable are
   both caught - the read-only guard's substring check catches neither, and its
   list omits `change_order` and `place_combo_order` entirely.
2. The paper context is built with `security_firm=SecurityFirm.FUTUINC`. The
   real account is FUTUMY, and `get_acc_list` keeps a live row only when its
   firm matches, while simulated rows bypass that filter entirely
   (`open_trade_context.py:94`). The real account is not visible to that
   context at all - verified: its account list returns exactly one row. The
   push subscription takes the whole list, so it is scoped by the same fact.
3. The SDK checks the pair. `_check_acc_id_exist` filters on `acc_id` *and*
   `trd_env` together (`open_trade_context.py:268`), so the real account id
   passed with `SIMULATE` comes back "Nonexisting acc_id" rather than trading.
   Confirmed against the live account.
4. `PaperBroker` re-verifies before every write that its account list is exactly
   one row and that row is simulated. That is the one thing static analysis
   cannot catch: moomoo re-issuing an id, or OpenD pointed at a different login.

`unlock_trade` must never appear anywhere in this repo. Simulated trading does
not need it, and its only other effect here would be to arm the real account.

`MOOMOO_TRD_ENV` must stay `REAL`. If it were ever flipped, the read client
would silently start reporting the paper account as "the portfolio".
`assert_simulated()` checks it, along with `MOOMOO_ACC_ID != PAPER_ACC_ID`.

# A rules file the user wrote is not the app forming a view

The screener describes and does not advise, and neither does `indicators.py`.
Nothing under `sidecar/paper/` scores a symbol, ranks candidates or decides that
a setup is good. It reads a JSON file of thresholds the user wrote, tests them
against `technicals()` output, and records which leaf of which condition was
true. The judgement is in the file; the code only evaluates it.

That distinction is what keeps the house rule coherent, so keep it:

- No rule may be generated, suggested, tuned or ranked by the app or by an
  agent. `sidecar/paper/rules.json` is user-authored input, like a screener
  filter - which is why it is version-controlled rather than sitting in
  `.cache/`.
- The condition grammar is a fixed operator table over dotted field paths. No
  `eval`, no `exec`, no expression strings, and deliberately no field-to-field
  comparison operator. Every threshold is a literal the user typed; that single
  omission is what keeps the grammar from being a program. "Close is below the
  50-day average" is written `trend.pct_from.sma50 < 0`.
- An unknown field path or a `None` value evaluates to False and is recorded,
  never raised and never coerced. `atr14`, `sma200` and
  `macd_cross.crossed_within` are legitimately null on short history.
- Nothing in the journal, the API or the UI says whether a decision was a good
  one. It says which rule fired, on what values, and what happened.
- No LLM in the decision loop. An agent may read the journal and run a dry run;
  it may not write a rule or place an order.

# A paper account reports no deal data at all

`deal_list_query` answers "Paper trading does not support deal data" and no
`TradeDealHandlerBase` push ever arrives, however the handler is registered.
Order pushes *do* arrive and carry `dealt_qty` and `dealt_avg_price`, so a
fill is reconstructed from an order's dealt quantity moving between status
pushes. Every row in `fills` is flagged `derived` for that reason - it is not
broker-reported, and nothing downstream may present it as if it were. Same
discipline as `closed_realized` in `pnl.py`.

Because push is not guaranteed, `reconcile()` also calls
`journal.backfill_fills()`, which makes `fills` agree with the order table.
The order table is authoritative; a fill row is a convenience derived from it.

# Order caps are counted per US session date, not per local date

`journal.trade_date()` returns the `America/New_York` date and every cap is
counted against a stored `trade_date` column. This is not a nicety. Timestamps
are stored in UTC and this machine's local date is UTC+8, so counting "orders
today" by slicing the UTC timestamp and comparing it against `date.today()`
matched nothing for most of the US session: `max_orders_per_day` silently never
fired, however it was configured, and every order after the first collided on a
duplicate client tag. Never reintroduce `date.today()` under `paper/`.

The order sequence in a client tag also comes from the journal, not from a
per-cycle counter. A per-cycle counter restarts every few minutes, so the second
order of the day on the same rule and symbol reused the first one's tag and was
rejected as a duplicate - capping `max_orders_per_symbol_per_day` at 1 whatever
it said.

# Every cap is a budget on new exposure, so none of them may refuse an exit

`max_orders_per_day`, `max_orders_per_symbol_per_day`, `cooldown_days`,
`max_concurrent_positions`, `max_fee_pct_of_notional` and the no-late-entries
window all sit inside the `side == "BUY"` branch of `_order_guardrails`, and
`max_orders_per_day` counts `journal.orders_today(side="BUY")`. Keep every one
of them there.

They were not all there to begin with, and the journal shows what that cost. On
2026-09-09 six entries filled early in the session and then **six exits on AMAT
and SVRA were journalled `daily_order_cap`** while both positions stayed open -
the rules said to close them and the engine declined, because the budget for
opening things had been spent. `max_orders_per_symbol_per_day: 1` made it worse
in a way that is easy to miss: it made selling a symbol on the day it was bought
impossible. `cooldown_days: 3` would have held a position for three days after
its exit signal.

The reasoning is the same one already written down for the fee guard: a limit on
how much new risk the engine takes on is not a limit on its ability to get out
of risk it already has. A cap that can trap a position is worse than no cap.

Two things stay on both sides, deliberately:

- **`order_outstanding`.** A second order while one is still resting is a double
  order, not a faster one.
- **`price_sanity`.** A limit far from both the last bar and the live quote is a
  bad input whichever way it points.

`tests/test_engine.py` pins this; six of its nine tests fail if the `BUY` gate is
removed.

Because exits no longer consume it, the practical ceiling on entries per day is
`max_concurrent_positions` and `cooldown_days`, not this cap - a slot has to be
free before anything new is bought. Raising `max_orders_per_day` past the
position count is an outer safety limit, not a throughput lever.

# max_open_notional_pct was declared for weeks and never read

It sat in `rules.json` and in `PortfolioLimits` and no guardrail consulted it,
so the file said "at most 40% of the account is deployed" and the engine had no
such limit. It is enforced now, in the `BUY` branch of `_order_guardrails`.
When you add a limit to `PortfolioLimits`, grep that it is actually read - a
declared cap that does nothing is worse than an absent one, because the reader
of the file believes it.

It is measured against the **real** account's equity, not the paper account's.
That follows from sizing: the position was sized off `client.account_info()`
`total_assets`, so the exposure has to be expressed on the same basis. Against
the paper account's own million it would never bind and would read as a limit
while being none.

`market_val` can arrive as the string `"N/A"` like every other moomoo numeric,
so it goes through `currency.num` and an unreadable row contributes zero. An
unmeasurable equity blocks rather than waving the order through, the same
posture `_price_sanity` takes when there is no snapshot.

The ceiling and the position count have to be set together or one makes the
other decorative. On ~13k of real equity, `risk_pct` 1% over a 2x ATR stop puts
most positions at 900-1,500, so eight of them is roughly 74%: at the old 40%
the notional ceiling would have bound at about four and
`max_concurrent_positions: 8` would have been a number that never applied.

# Idempotency is a database constraint, not care

Every order carries a `client_tag` of `rule_id + code + trade_date + sequence`,
stored in `remark` as a blake2s digest because `place_order` rejects a remark
over 64 UTF-8 bytes, and declared `UNIQUE` in
`sidecar/.cache/paper/journal.db`. The row is claimed before placement and
settled in place afterwards rather than deleted and re-inserted, so the tag is
never briefly free. A retried cycle, a double-clicked run-once and a restart
mid-placement all collide on that constraint instead of placing a second order.

Do not replace it with a "have I already done this" check in Python. That check
and the insert are not atomic, and several threads write here. The same mistake
already cost a fill once: `record_order_event` read the previous state, released
the lock, then wrote, so concurrent copies of one push each logged a duplicate
event and none of them saw `dealt_qty` move. The shares had moved and the
journal showed no fill. Check and act are one critical section now; keep them
that way. `tests/test_journal.py` has the regression.

# One process may hold a paper context, and now only one can

OpenD delivers an order push to every connected context that can see the
account, and each `get_acc_list` re-subscribes. Two processes holding a
`PaperBroker` therefore double every event and race on the journal. The sidecar
is the only thing that should hold one.

This was documented as a rule and enforced by nothing, so it was broken for
fourteen hours on 2026-09-10: `npm run dev` starts a sidecar through
`run-sidecar.sh` and the launchd agent starts another, and both were live. The
duplicate never even held the port - **uvicorn runs the app lifespan before it
binds**, so a second instance opens a paper context and runs a complete engine
loop, then fails to bind and keeps running. Nothing surfaces. It is the same
condition that produced the 29 orphan pushes in the first journal.

`broker._claim_single_instance()` now takes an exclusive `flock` on
`.cache/paper/context.lock` before the context is ever constructed, and
`PaperBroker.ctx` raises `PaperContextBusy` naming the holding pid when it
cannot. An advisory lock rather than a pidfile because the kernel releases it
however the process dies, including SIGKILL. The lock file is opened `"a+"`,
never `"w"` - `"w"` truncates on open, so the process being refused would erase
the pid of the holder it is being refused by.

`run-sidecar.sh` also health-checks the port first and exits cleanly, so the
dev-server path says so in English instead of failing at the bind. `app.py`
turns `PaperContextBusy` into a 503 with the `lsof` line that names the
survivor: two sidecars is a setup mistake, not a broker failure.

The moomoo SDK also starts non-daemon threads, so a script that imports
`paper.broker` never exits on its own even after its last line runs. Any script
or REPL that opens a broker must call `broker.close()` and exit explicitly, or
it keeps consuming pushes in the background for the rest of the session.

# Sizing reads the real account, execution reads the paper one

Position size comes from the real account's `total_assets`
(`client.account_info()`, still a read) so results are at the scale that
matters, and it is validated against the paper account's own
`acctradinginfo_query` before the order goes out. When they disagree the order
is rejected and both numbers are journalled and messaged - never silently
truncated to whatever the paper balance allows. `equity_real`, `usd_assets`,
the rate and its source are journalled on every decision, so any size is
reproducible.

A zero size is the interesting case, not an error: on a small account one share
of an expensive name can exceed the per-position cap outright. The block detail
names the binding constraint and the arithmetic behind it, rather than "0".

The paper account trades US only: its `trdmarket_auth` is `[US]`. Build the
universe filter from that list read at runtime, not from `MKT_ENV_ENABLE_MAP` -
that map has `(HK, SIMULATE) = True`, so an HK symbol would pass a local check
and then fail at the broker. `MY.*` and `HK.*` holdings and watchlist entries
are excluded and **named** in `universe.excluded` with a reason, for the same
reason unconverted currencies are named rather than dropped.

Rules run on `K_DAY` only. `klines._read` truncates `time_key` to its first ten
characters, so intraday bars come back date-only and `technicals()["as_of"]`
cannot express intraday staleness - with `KLINE_TTL_SECONDS` at 1800 a
15-minute rule would evaluate half-hour-old bars with no way to tell. Fix both
before enabling intraday.

# The browser can stop and resume the engine, never make it trade

`route.ts` allowlists the paper reads plus `POST paper/kill` and
`POST paper/resume`. It does not allowlist `paper/run-once` or
`paper/dry-run` - those are the two that place or evaluate orders on demand.

Resume is safe to expose only because the sidecar makes it deliberate rather
than one-click, in two ways that must both survive any edit: the caller has to
echo the current kill reason back, so the reason cannot go unread; and a halt
whose reason contains "simulation violation" cannot be cleared through the API
at all, because that means the account the engine was pinned to is no longer the
account it is talking to. That one is cleared by deleting
`sidecar/.cache/paper/KILLED` by hand, after looking at why.

The proxy forwards no request body (`route.ts`), which is why `paper/kill`
takes its reason as a query parameter - the same limitation that keeps
`research/screen` off the POST allowlist despite the endpoint existing.


# Fees are modelled, because the paper account is not charged them

`order_fee_query` refuses simulated orders outright ("Paper trading is not
supported") and a paper fill costs nothing, so an un-modelled round trip that
really loses money would be recorded as a win. `paper/fees.py` estimates the
cost from the *real* account's own fee history and is labelled `estimated`
everywhere it surfaces - never present it as broker-reported.

The shape on this account, from 40 real fills:

    Commission        0.00        this account pays none
    Platform Fees     0.99        flat, every order, both sides
    Stamp Duty        ~0.115%     stepped in $0.25, floor $0.25
    Settlement Fee    0.00-0.06   negligible
    SEC Fee           ~0.0023%    sells only
    Trading Activity  0.01        sells only

Because the cost is mostly flat, it is regressive: a round trip is 0.55% of a
$650 position and 0.28% of a $5,000 one. `max_fee_pct_of_notional` refuses an
entry whose round trip exceeds that share of the position.

Two things about that guard:

- **It applies to entries only.** Refusing an exit on cost grounds would trap a
  position that could never be closed.
- **`max_notional_pct` is usually not the binding constraint on this account.**
  `risk_pct` 1% over a 2x ATR stop already caps most positions near $900-1,500,
  well under the 15% ceiling, so raising the ceiling alone changes nothing. If
  the fee burden needs to be proportionally smaller the lever is a bigger
  position - a higher `risk_pct` or a tighter `atr_mult_stop` - not a looser fee
  limit.

`tests/test_fees.py` pins the model against a frozen sample of real fills, so a
change in moomoo's schedule fails a test instead of quietly skewing every
sizing decision. The model runs slightly conservative by design (mean +$0.12,
never more than $0.04 under across the sample); a guard that understates costs
is worse than one that overstates them.


# Daily-bar rules should be evaluated once, near the close

`PAPER_EVALUATE_AT` (US/Eastern `HH:MM`) restricts *entry* evaluation to one
cycle per session. Empty means every cycle, which is only correct if the rules
read intraday bars - and they cannot yet, because `klines._read` truncates
`time_key` to a date.

The reason is repainting. `technicals()` computes MACD, RSI and the moving
averages from the last bar in the series, and mid-session that bar is today's
and still open. A cross seen at 10:00 is derived from a close that is still
moving; by 16:00 it may not exist. An entry taken on it was taken on a signal
that never happened, and no amount of shortening the cycle helps - it only
gives more chances to catch a transient one.

Two things must stay true if you touch this:

- **Exits and the pre-close cancel are never gated by it.** A position already
  open has to be manageable on every cycle, whatever the entry policy is.
- **The time must fall before the no-late-entries window.**
  `_order_guardrails` refuses a BUY within `flatten_before_close_minutes` of the
  close, so setting `PAPER_EVALUATE_AT=15:45` against the default 20 evaluates
  the rules and then rejects every result as a late entry - an engine that looks
  like it is working and never trades. `evaluate_at_conflict()` detects that and
  logs it at startup; keep the check.

An unparseable value falls back to evaluating every cycle and says so. Trading
nothing all day because of a typo in a time string is a worse failure than
ignoring the setting.


# The whole US session happens while this machine is asleep

The engine runs on a laptop in UTC+8. The US session is 09:30-16:00 ET, which
is **21:30-04:00 local**, and `PAPER_EVALUATE_AT=15:35` puts the one entry
evaluation of the day at **03:35 local**. macOS sleeps; the sidecar is
suspended with it; `asyncio.sleep` does not fire.

This is not theoretical. On 2026-09-09 the last in-session cycle ran at 15:00 ET
and the next at 17:06 ET - a 126-minute gap straight through the window - and
`engine_state` has never once held an `entries_evaluated:<date>` row. Every
entry in the journal came from a manual `run-once`, not from the scheduled loop.
Cycle spacing on a nominal 15-minute cadence has been observed at 126, 175 and
225 minutes.

Two consequences to keep in mind:

- **A missed window is not a halt**, so `PAPER_NOTIFY=alerts` did not report it
  and the dashboard read "armed" all day. `missed_entry_window()` exists for
  that: once the session is over on a weekday and no cycle reached the window,
  it alerts once and the `/paper` Engine card marks the row. A market holiday
  reads identically from inside the process and the message says so rather than
  guessing.
- **Shortening `PAPER_CYCLE_MINUTES` does not help.** The machine is asleep for
  hours at a time; more frequent ticks that never execute are still zero ticks.
  The fix is at the OS level (a scheduled wake plus `caffeinate` across the
  session) or a host that does not sleep - not in this code.

# The daily review has two callers and only one of them may send

`run-daily-review.sh` is invoked by `start-dev.sh` and by
`local.moomoo-app.daily-review`. Both must pass `--if-due`. The launchd job did
not, so it ignored the once-per-calendar-day stamp and **two reviews went out on
2026-09-10** - one at 02:55 local and another at 08:48.

`--if-due` now also refuses to run before 06:00 local (`REVIEW_EARLIEST_HOUR`).
The skill's own "Timing context" section states that the US session has already
closed when the review runs, and every figure in it is read as an overnight
close. Local time is UTC+8, so the close lands at 04:00-05:00; the 02:55 run was
reporting a session still in progress as though it were final. Without the hour
guard, adding `--if-due` would have made that early, wrong report the one that
wins and suppressed the correct post-close one.

The day-of-week schedule is correct and does not need the treatment the paper
session note needed. Mon-Fri local at 08:45 maps onto all five New York
sessions with no gaps and no repeats (Monday reports Friday's close, across a
weekend in which nothing traded). The review reports current state - positions,
P&L, technicals - not a discrete session date, so there is no session to be off
by one about. Only the paper note, which reports one session's orders, has to
derive its date from the clock.

# The simulated session note is a separate message, never a section

`POST /paper/digest` sends the paper engine's daily note, and
`local.moomoo-app.paper-session-note` fires it at 08:50 on weekdays - five
minutes after `local.moomoo-app.daily-review`, as its own message.

Do not merge it into the daily review, and do not add a paper section to that
skill. The review reports a real brokerage account; this reports a simulated
account holding money that does not exist. Merging them would put a fabricated
P&L one line away from a real one under a single heading, which is the same
provenance rule `pnl.py` follows when it refuses to present `closed_realized`
as broker truth. The difference here is that a label can be misread at a
glance and two separate messages cannot.

The note is journal-backed, like `/paper/orders`, so it still arrives on a
morning when OpenD is down; the simulated equity line is best-effort decoration
on top and is simply absent when the broker cannot be reached.

It is not on the browser POST allowlist. That list is brakes-only, and this
sends a message.

`format_paper_session` leads with whether the entry window was evaluated at all,
because on this machine that is the failure mode - see the section on the US
session running while the machine is asleep. An empty day where the rules
matched nothing and an empty day where nothing ran look identical in a list of
orders, and only one of them is the engine working.

# Telegram carries the exceptions, not the activity

`PAPER_NOTIFY` is `off`, `alerts` or `all`, defaulting to `alerts`. At the
default caps `all` is roughly a dozen messages a day - an order receipt and a
fill receipt per trade - which buries the two or three a year that actually
need a human.

`alerts` keeps exactly those: the engine halting (a daily loss limit, a
simulation violation), a placement the broker refused, and a rules file that
will not load. Everything else lives in the journal and on `/paper`, which is
where it should be read anyway.

Two things to keep if you touch this:

- **A halt must always be able to reach a person.** Before this existed,
  `journal.kill()` only wrote a log line, so the engine could stop trading
  silently. Silencing receipts is fine; silencing failures is not, which is why
  `off` is not the default.
- **`drain_fills` marks a fill notified whether or not it sent one.** Otherwise
  every fill ever recorded is retried on every cycle for the life of the
  database.
