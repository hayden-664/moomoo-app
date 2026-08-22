export type Position = {
  code: string;
  stock_name: string;
  qty: number;
  can_sell_qty: number;
  cost_price: number | null;
  nominal_price: number | null;
  market_val: number | null;
  pl_val: number | null;
  pl_ratio: number | null;
  today_pl_val: number | null;
  unrealized_pl: number | null;
  realized_pl: number | null;
  position_side: string | null;
  position_market: string | null;
  /** Denomination moomoo reports for this row; not always the account currency. */
  currency: string | null;
};

export type OpenPnl = {
  currency: string;
  open_unrealized: number;
  open_realized: number;
  today: number;
  market_value: number;
  cost_basis: number;
  position_count: number;
};

export type ClosedPnl = {
  currency: string;
  closed_realized: number;
  by_symbol: Record<string, number>;
  /** Derived by FIFO-matching deal history, not reported by the broker. */
  approximate: true;
  excludes: string[];
};

/** Banked by selling part of a holding you still own. Derived, like ClosedPnl. */
export type PartialPnl = {
  currency: string;
  partial_realized: number;
  by_symbol: Record<string, number>;
  approximate: true;
  excludes: string[];
  /** False when the broker reports its own figure and this one would double-count. */
  counted_in_net: boolean;
};

/** Every P&L figure for one settlement currency. Never mixed with another. */
export type CurrencyPnl = {
  currency: string;
  net: number;
  /** Everything cashed out: closed round-trips plus partial sells. */
  total_realized: number;
  open: OpenPnl;
  closed: ClosedPnl;
  partial: PartialPnl;
  position_count: number;
};

/** How foreign figures were folded into the reporting currency. */
export type Conversion = {
  base: string;
  /** Value of one unit of each currency in `base`; base itself is 1. */
  rates: Record<string, number>;
  /** Non-base currencies actually converted and included in the totals. */
  converted_from: string[];
  /** Currencies with no available rate — excluded from the totals, not zeroed. */
  unconverted: string[];
  /** Human-readable provenance of the rate, rendered directly in the UI. */
  source: string;
  note: string;
};

export type Pnl = Omit<CurrencyPnl, "currency"> & {
  /** Reporting currency. The top-level figures are everything converted into it. */
  base: string;
  /** Every currency with money in it, base included. */
  currencies: string[];
  /** True when >1 currency is present. The totals are converted either way. */
  mixed_currency: boolean;
  /** The same figures per currency, unconverted. */
  by_currency: Record<string, CurrencyPnl>;
  conversion: Conversion;
  window: {
    start: string;
    end: string;
    days: number;
    deals: number;
    /** Deals are matched from here even when reporting starts later. */
    matched_from: string;
  };
  /** Open positions the broker reported, including any left unconverted. */
  total_position_count: number;
};

/** One currency's balances, exactly as the broker holds them — unconverted. */
export type CurrencyBalance = {
  currency: string;
  cash: number | null;
  assets: number | null;
  avl_withdrawal_cash: number | null;
  net_cash_power: number | null;
  /** 1 unit of this currency in base. Null when the rate is not solvable. */
  rate_to_base: number | null;
  assets_in_base: number | null;
};

export type CurrencySplit = {
  base: string;
  by_currency: Record<string, CurrencyBalance>;
  /** Read back out of the broker's own conversion, not quoted directly. */
  implied_fx: Record<string, number>;
  /** Account fields that arrive already converted into `base`. */
  converted_fields: string[];
};

export type Account = {
  total_assets: number | null;
  securities_assets: number | null;
  market_val: number | null;
  us_cash: number | null;
  cash: number | null;
  frozen_cash: number | null;
  avl_withdrawal_cash: number | null;
  power: number | null;
  risk_status: string | null;
  /** Reporting currency the totals above were converted into. */
  currency: string | null;
  currency_split: CurrencySplit;
};

export type OptionCandidate = {
  code: string;
  option_type: "CALL" | "PUT";
  strike_price: number;
  expiry: string | null;
  dte: number | null;
  delta: number | null;
  gamma: number | null;
  theta: number | null;
  vega: number | null;
  iv: number | null;
  open_interest: number | null;
  volume: number | null;
  bid: number | null;
  ask: number | null;
  mid: number | null;
  cost: number | null;
  breakeven: number | null;
  required_move_pct: number | null;
  spread_pct: number | null;
  characterisation: string;
};

export type ScreenResult = {
  underlying: string;
  spot: number | null;
  count: number;
  candidates: OptionCandidate[];
  filters: Record<string, unknown>;
};

export type Health = {
  opend: "connected" | "error" | "unreachable";
  market?: string;
  telegram?: boolean;
  read_only?: boolean;
  error?: string;
};

export type Permissions = {
  markets: Record<string, { stock: string | null; option: string | null }>;
  options_enabled: string[];
};

export type Bar = {
  time: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
};

export type Candles = {
  code: string;
  ktype: string;
  /** Whether this response came from the local bar cache or the moomoo API. */
  source: "cache" | "api";
  bars: Bar[];
};

/** One day's fills for a symbol, merged per side at the weighted price. */
export type Fill = {
  time: string;
  side: "BUY" | "SELL";
  qty: number;
  price: number | null;
};

export type Quota = {
  kline: { used: number; remain: number; symbols: string[] };
  subscription: {
    used: number | null;
    remain: number | null;
    option_used: number | null;
    option_remain: number | null;
  } | null;
};
