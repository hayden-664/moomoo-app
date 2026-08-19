type Props = {
  value: number | null | undefined;
  /** Colour green/red by sign. Off for neutral figures like market value. */
  signed?: boolean;
  showSign?: boolean;
  currency?: string;
  className?: string;
};

export function fmt(value: number | null | undefined, currency = "USD") {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency,
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(value);
}

export function pct(value: number | null | undefined, digits = 2) {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return `${value >= 0 ? "+" : ""}${value.toFixed(digits)}%`;
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
  return <span className={`tnum ${tone} ${className}`}>{prefix}{body}</span>;
}
