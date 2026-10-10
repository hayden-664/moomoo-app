import type { Dividends } from "@/lib/types"
import { fmt, fxLabel } from "./Money"

/**
 * Dividends sit beside net P&L, not inside it — and part of them is already in
 * it. moomoo folds a held position's dividends into that row's realized_pl,
 * which net uses; dividends from holdings since closed are in net nowhere.
 * Showing the split is the point: adding the whole figure to net would
 * double-count the first part.
 */
export default function DividendsSummary({
  dividends: d,
}: {
  dividends: Dividends
}) {
  const base = d.base
  const unavailable = Object.keys(d.unavailable)
  const { converted_from: converted, unconverted, rates } = d.conversion
  if (d.events.length === 0 && unavailable.length === 0 && !d.upcoming.length)
    return null
  const skipped = unavailable.length + d.unparsed.length

  return (
    <section className="rounded-lg border border-border bg-surface px-4 py-3 text-sm">
      <div className="flex flex-wrap items-baseline gap-x-6 gap-y-1">
        <span className="font-medium">
          Dividends{" "}
          <span className="text-xs font-normal text-muted">est. · gross</span>
        </span>
        <span className="text-muted">
          Received{" "}
          <span className="tnum text-foreground">{fmt(d.paid, base)}</span>
        </span>
        <span
          className="text-muted"
          title="From holdings since closed. The rest is already in net P&L via the broker's realized figure."
        >
          Not in net{" "}
          <span className="tnum text-foreground">
            {fmt(d.paid_not_in_net, base)}
          </span>
        </span>
        {d.pending > 0 && (
          <span className="text-muted">
            Pending{" "}
            <span className="tnum text-foreground">{fmt(d.pending, base)}</span>
          </span>
        )}
        {unconverted.length > 0 && (
          <span className="text-xs text-neg">
            {unconverted.join(", ")} excluded — no rate
          </span>
        )}
      </div>

      <details className="mt-2 text-xs text-muted">
        <summary className="cursor-pointer select-none hover:text-foreground">
          Details{skipped > 0 && ` · ${skipped} not counted`}
        </summary>
        <div className="mt-2 space-y-1.5 leading-relaxed">
          <p>
            Per-share dividend history × shares held going into each ex-date,
            since {d.window.start}. Before withholding tax, so cash received is
            typically less. {fmt(d.paid_in_net, base)} is already in net P&amp;L
            through the broker&apos;s realized figure on positions you still
            hold.
            {converted.length > 0 &&
              ` Converted at ${converted.map((c) => fxLabel(c, base, rates[c])).join(", ")}, today's rate.`}
          </p>
          {d.upcoming.length > 0 && (
            <p>
              Upcoming:{" "}
              {d.upcoming
                .map(
                  (u) =>
                    `${u.code} ex ${u.ex_date} (${fmt(u.amount_if_held, u.currency)})`,
                )
                .join(" · ")}
            </p>
          )}
          {/* Named, not dropped: Bursa histories need the MY quote entitlement;
              others may be waiting on the 30-per-30s limit. */}
          {unavailable.length > 0 && (
            <p>
              Not counted, history unavailable: {unavailable.join(", ")}
              {unavailable.some((c) => c.startsWith("MY.")) &&
                " (MY needs a Bursa quote subscription)"}
            </p>
          )}
          {d.unparsed.length > 0 && (
            <p>
              Non-cash distributions not counted:{" "}
              {d.unparsed.map((u) => u.code).join(", ")}
            </p>
          )}
          {d.unreconciled.length > 0 && (
            <p>
              Understated — holdings not fully explained by deal history:{" "}
              {d.unreconciled.join(", ")}
            </p>
          )}
        </div>
      </details>
    </section>
  )
}
