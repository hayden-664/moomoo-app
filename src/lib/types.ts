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
};

export type OpenPnl = {
  open_unrealized: number;
  open_realized: number;
  today: number;
  market_value: number;
  cost_basis: number;
};

export type ClosedPnl = {
  closed_realized: number;
  by_symbol: Record<string, number>;
  /** Derived by FIFO-matching deal history, not reported by the broker. */
  approximate: true;
  excludes: string[];
};

/** Banked by selling part of a holding you still own. Derived, like ClosedPnl. */
export type PartialPnl = {
  partial_realized: number;
  by_symbol: Record<string, number>;
  approximate: true;
  excludes: string[];
  /** False when the broker reports its own figure and this one would double-count. */
  counted_in_net: boolean;
};

export type Pnl = {
  net: number;
  /** Everything cashed out: closed round-trips plus partial sells. */
  total_realized: number;
  open: OpenPnl;
  closed: ClosedPnl;
  partial: PartialPnl;
  window: {
    start: string;
    end: string;
    days: number;
    deals: number;
    /** Deals are matched from here even when reporting starts later. */
    matched_from: string;
  };
  position_count: number;
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
  currency: string | null;
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
