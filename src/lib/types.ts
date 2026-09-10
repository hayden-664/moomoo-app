export type Position = {
  code: string
  stock_name: string
  qty: number
  can_sell_qty: number
  /**
   * DILUTED cost — realized P&L subtracted from the basis, so it can sit below
   * what was paid, or go negative. Prefer `average_cost` for display.
   */
  cost_price: number | null
  /** False when the broker cannot establish a basis; `cost_price` is then junk. */
  cost_price_valid: boolean | null
  /** Average price actually paid. What moomoo's own app shows. */
  average_cost: number | null
  diluted_cost: number | null
  nominal_price: number | null
  market_val: number | null
  pl_val: number | null
  pl_val_valid: boolean | null
  pl_ratio: number | null
  today_pl_val: number | null
  unrealized_pl: number | null
  realized_pl: number | null
  position_side: string | null
  position_market: string | null
  /** Denomination moomoo reports for this row; not always the account currency. */
  currency: string | null
}

export type OpenPnl = {
  currency: string
  open_unrealized: number
  open_realized: number
  /** Same figure, broken out per symbol — what `partial.broker_symbols` is decided from. */
  open_realized_by_symbol: Record<string, number>
  today: number
  market_value: number
  cost_basis: number
  position_count: number
}

export type ClosedPnl = {
  currency: string
  closed_realized: number
  by_symbol: Record<string, number>
  /** Derived by FIFO-matching deal history, not reported by the broker. */
  approximate: true
  excludes: string[]
}

/** Banked by selling part of a holding you still own. Derived, like ClosedPnl. */
export type PartialPnl = {
  currency: string
  /** Pure FIFO reconstruction — not what necessarily went into `net`. */
  partial_realized: number
  by_symbol: Record<string, number>
  approximate: true
  excludes: string[]
  /**
   * Symbols where the broker's own `realized_pl` (not this reconstruction)
   * was used for that symbol's contribution to `net` — decided per symbol,
   * since the broker only starts reporting a figure once that specific
   * position has been partially sold.
   */
  broker_symbols: string[]
}

/** Every P&L figure for one settlement currency. Never mixed with another. */
export type CurrencyPnl = {
  currency: string
  net: number
  /** Everything cashed out: closed round-trips plus partial sells. */
  total_realized: number
  open: OpenPnl
  closed: ClosedPnl
  partial: PartialPnl
  position_count: number
}

/** How foreign figures were folded into the reporting currency. */
export type Conversion = {
  base: string
  /** Value of one unit of each currency in `base`; base itself is 1. */
  rates: Record<string, number>
  /** Non-base currencies actually converted and included in the totals. */
  converted_from: string[]
  /** Currencies with no available rate — excluded from the totals, not zeroed. */
  unconverted: string[]
  /** Human-readable provenance of the rate, rendered directly in the UI. */
  source: string
  note: string
}

export type Pnl = Omit<CurrencyPnl, "currency"> & {
  /** Reporting currency. The top-level figures are everything converted into it. */
  base: string
  /** Every currency with money in it, base included. */
  currencies: string[]
  /** True when >1 currency is present. The totals are converted either way. */
  mixed_currency: boolean
  /** The same figures per currency, unconverted. */
  by_currency: Record<string, CurrencyPnl>
  conversion: Conversion
  window: {
    start: string
    end: string
    days: number
    deals: number
    /** Deals are matched from here even when reporting starts later. */
    matched_from: string
  }
  /** Open positions the broker reported, including any left unconverted. */
  total_position_count: number
}

/** One currency's balances, exactly as the broker holds them — unconverted. */
export type CurrencyBalance = {
  currency: string
  cash: number | null
  assets: number | null
  avl_withdrawal_cash: number | null
  net_cash_power: number | null
  /** 1 unit of this currency in base. Null when the rate is not solvable. */
  rate_to_base: number | null
  assets_in_base: number | null
}

export type CurrencySplit = {
  base: string
  by_currency: Record<string, CurrencyBalance>
  /** Read back out of the broker's own conversion, not quoted directly. */
  implied_fx: Record<string, number>
  /** Account fields that arrive already converted into `base`. */
  converted_fields: string[]
}

export type Account = {
  total_assets: number | null
  securities_assets: number | null
  market_val: number | null
  us_cash: number | null
  cash: number | null
  frozen_cash: number | null
  avl_withdrawal_cash: number | null
  power: number | null
  risk_status: string | null
  /** Reporting currency the totals above were converted into. */
  currency: string | null
  currency_split: CurrencySplit
}

export type OptionCandidate = {
  code: string
  option_type: "CALL" | "PUT"
  strike_price: number
  expiry: string | null
  dte: number | null
  delta: number | null
  gamma: number | null
  theta: number | null
  vega: number | null
  iv: number | null
  open_interest: number | null
  volume: number | null
  bid: number | null
  ask: number | null
  mid: number | null
  cost: number | null
  breakeven: number | null
  required_move_pct: number | null
  spread_pct: number | null
  characterisation: string
}

export type ScreenResult = {
  underlying: string
  spot: number | null
  count: number
  candidates: OptionCandidate[]
  filters: Record<string, unknown>
}

export type Health = {
  opend: "connected" | "error" | "unreachable"
  market?: string
  telegram?: boolean
  read_only?: boolean
  error?: string
}

export type Permissions = {
  markets: Record<string, { stock: string | null; option: string | null }>
  options_enabled: string[]
}

export type Bar = {
  time: string
  open: number
  high: number
  low: number
  close: number
  volume: number
}

export type Candles = {
  code: string
  ktype: string
  /** Whether this response came from the local bar cache or the moomoo API. */
  source: "cache" | "api"
  bars: Bar[]
}

/** One day's fills for a symbol, merged per side at the weighted price. */
export type Fill = {
  time: string
  side: "BUY" | "SELL"
  qty: number
  price: number | null
}

export type Quota = {
  kline: { used: number; remain: number; symbols: string[] }
  subscription: {
    used: number | null
    remain: number | null
    option_used: number | null
    option_remain: number | null
  } | null
}

// -- paper engine --------------------------------------------------------------
// Simulated account only. Everything here describes orders against moomoo's
// paper account; nothing in this branch of the app can reach the real one.

/** One leaf of a rule condition, as it was actually evaluated. */
export type PaperTraceLeaf = {
  path: string
  /** Null where the technicals payload had no value — a real state on short history. */
  resolved: string | number | boolean | string[] | null
  op: string
  target: string | number | boolean | number[] | string[] | null
  result: boolean
}

export type PaperDecision = {
  decision_id: string
  cycle_id: string
  ts: string
  /** US/Eastern session date. Order caps are counted against this, not the local date. */
  trade_date: string | null
  code: string
  rule_id: string
  rules_sha: string | null
  kind: "ENTRY" | "EXIT"
  outcome: "MATCHED" | "NO_MATCH" | "BLOCKED" | "PLACED" | "PLACE_FAILED" | "CLAIM"
  block_reason: string | null
  block_detail: string | null
  trace: PaperTraceLeaf[] | null
  bar_as_of: string | null
  side: "BUY" | "SELL" | null
  qty: number | null
  limit_price: number | null
  ref_price: number | null
  atr14: number | null
  stop_price: number | null
  risk_usd: number | null
  /** Size the rule asked for, before the paper account's buying power was checked. */
  qty_intended: number | null
  qty_affordable: number | null
  /** 1 when the order was cut to fit paper buying power rather than rejected. */
  scaled: number | null
  client_tag: string | null
}

export type PaperOrder = {
  order_id: string
  client_tag: string
  trd_env: string
  acc_id: string
  trade_date: string | null
  code: string | null
  trd_side: "BUY" | "SELL" | null
  order_type: string | null
  qty: number | null
  price: number | null
  currency: string | null
  status: string | null
  dealt_qty: number | null
  dealt_avg_price: number | null
  /** The broker's own words on a rejection. The most useful field when one fails. */
  last_err_msg: string | null
  remark: string | null
  created_at: string | null
  updated_at: string | null
  terminal_at: string | null
}

export type PaperExclusion = {
  code: string
  reason:
    | "market_not_tradable"
    | "instrument_excluded"
    | "insufficient_history"
    | "stale_bars"
  detail: string
}

export type PaperUniverse = {
  considered: string[]
  sources: { positions: string[]; watchlist: string[] }
  /** Named, never silently dropped — as with unconvertible currencies in P&L. */
  excluded: PaperExclusion[]
}

export type PaperStatus = {
  engine: {
    enabled: boolean
    acc_id: number
    trd_env: string
    killed: boolean
    kill_reason?: string | null
    cycle_minutes: number
    /** US/Eastern "HH:MM" when entries are evaluated; null = every cycle. */
    evaluate_at: string | null
    /** off | alerts | all — how much reaches Telegram. */
    notify: string
    entry_window?: { open: boolean | null; reason: string }
    rules_sha?: string
    rules_error?: string
    last_cycle?: string | null
    /** Set when a session ended without the entry window being reached. */
    missed_entry_window?: string | null
    error?: string
  }
  orders_today: number
  /** BUY orders only: the daily cap is a budget on new exposure. */
  entries_today: number
  open_orders: PaperOrder[]
  rules?: {
    sha256: string
    ktype: string
    source_path: string
    rule_ids: string[]
    enabled_rule_ids: string[]
    portfolio: Record<string, number | string>
  }
  rules_error?: string
  caps?: {
    max_orders_per_day: number
    max_concurrent_positions: number
    /** Share of the REAL account's equity, the basis that sized the positions. */
    max_open_notional_pct: number
    max_notional_usd: number
    max_risk_pct: number
  }
  last_cycle?: {
    cycle_id: string
    started_at: string
    finished_at: string | null
    market_state: string | null
    equity_real: number | null
    paper_total_assets: number | null
    blocked_by: string | null
    error: string | null
    universe: PaperUniverse | null
  }
}
