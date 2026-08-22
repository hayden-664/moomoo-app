type Props = {
  value: number | null | undefined;
  /** Colour green/red by sign. Off for neutral figures like market value. */
  signed?: boolean;
  showSign?: boolean;
  currency?: string;
  className?: string;
};

const formatters = new Map<string, Intl.NumberFormat>();

function build(currency: string, currencyDisplay: "symbol" | "narrowSymbol") {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency,
    currencyDisplay,
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

/**
 * The account holds USD and MYR, so a figure that renders as a bare "$" when it
 * is not dollars is a real hazard, not a cosmetic one.
 *
 * `narrowSymbol` is what turns MYR into "RM" instead of "MYR", but it also
 * collapses HKD/SGD/AUD/CAD to a plain "$". Fall back to the full symbol
 * whenever that happens, so every non-USD amount stays distinguishable.
 */
function formatter(currency: string): Intl.NumberFormat | null {
  const hit = formatters.get(currency);
  if (hit) return hit;
  try {
    let f = build(currency, "narrowSymbol");
    if (currency !== "USD" && f.format(0).includes("$")) f = build(currency, "symbol");
    formatters.set(currency, f);
    return f;
  } catch {
    // Intl rejects codes that are not three letters; CNH and friends are fine,
    // anything malformed falls through to a plain "<CODE> 1,234.56".
    return null;
  }
}

export function fmt(value: number | null | undefined, currency = "USD") {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  const f = formatter(currency);
  if (f) return f.format(value);
  return `${currency} ${value.toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

export function pct(value: number | null | undefined, digits = 2) {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return `${value >= 0 ? "+" : ""}${value.toFixed(digits)}%`;
}

/**
 * e.g. "1 USD = 4.0388 MYR". Quoted base-per-foreign, which is how an FX rate
 * is normally read, and at four decimals — `fmt` rounds to cents and would
 * turn a rate of 0.2476 into "$0.25".
 */
export function fxLabel(currency: string, base: string, rate: number) {
  if (!rate) return `${currency} rate unavailable`;
  return `1 ${base} = ${(1 / rate).toFixed(4)} ${currency}`;
}

export default function Money({
  value,
  signed = false,
  showSign = false,
  currency = "USD",
  className = "",
}: Props) {
  const tone =
    signed && value !== null && value !== undefined && !Number.isNaN(value)
      ? value > 0
        ? "text-pos"
        : value < 0
          ? "text-neg"
          : "text-muted"
      : "";
  const body = fmt(value, currency);
  const prefix = showSign && typeof value === "number" && value > 0 ? "+" : "";
  return (
    <span className={`tnum ${tone} ${className}`}>
      {prefix}
      {body}
    </span>
  );
}
