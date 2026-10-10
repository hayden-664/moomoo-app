import type { Account, Pnl } from "@/lib/types"
import Money, { fmt, fxLabel } from "./Money"

/**
 * Every figure on this page is stated in the reporting currency. Foreign
 * holdings are converted before they are summed — never added raw, which is
 * how a MYR round-trip once landed in a dollar total unnoticed.
 */
function Stat({
  label,
  value,
  currency,
  note,
  signed = true,
  emphasis = false,
}: {
  label: string
  value: number | null
  currency: string
  note?: string
  signed?: boolean
  emphasis?: boolean
}) {
  return (
    <div className="rounded-lg border border-border bg-surface p-4">
      <div className="text-xs uppercase tracking-wide text-muted">{label}</div>
      <div
        className={
          emphasis ? "mt-1 text-3xl font-semibold" : "mt-1 text-xl font-medium"
        }
      >
        <Money
          value={value}
          currency={currency}
          signed={signed}
          showSign={signed}
        />
      </div>
      {note && <div className="mt-1 text-xs text-muted">{note}</div>}
    </div>
  )
}

export default function PnlSummary({
  pnl,
  account,
}: {
  pnl: Pnl
  account: Account | null
}) {
  const base = pnl.base
  const {
    converted_from: converted,
    unconverted,
    rates,
    source,
  } = pnl.conversion

  // The positions table can show more rows than this counts: after an outright
  // sale the broker keeps returning the position at qty 0 until it drops the
  // row. Those are not holdings — they are excluded from the count, and from
  // net P&L, so the difference is named here rather than left to puzzle over.
  const positionNote =
    `${pnl.position_count} open position${pnl.position_count === 1 ? "" : "s"}` +
    (pnl.open.flat_count
      ? ` · ${pnl.open.flat_count} sold out, still listed by the broker`
      : "")

  const fx = converted.map((c) => fxLabel(c, base, rates[c])).join(" · ")

  return (
    <section className="space-y-3">
      {/* A currency with no solvable rate is left out of the totals entirely.
          It has to be said out loud rather than quietly rounding to zero. */}
      {unconverted.length > 0 && (
        <div className="rounded-lg border border-neg/40 bg-neg/10 px-4 py-2 text-sm text-neg">
          {unconverted.join(", ")} excluded from totals — no exchange rate
          available.
        </div>
      )}

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Stat
          label="Net P&L"
          value={pnl.net}
          currency={base}
          emphasis
          note={positionNote}
        />
        <Stat label="Today" value={pnl.open.today} currency={base} />
        <Stat
          label="Open unrealized"
          value={pnl.open.open_unrealized}
          currency={base}
          note="Broker-reported"
        />
        <Stat
          label="Realized"
          value={pnl.total_realized}
          currency={base}
          note={`Approximate${converted.length ? " · today's FX" : ""}`}
        />
      </div>

      <div className="flex flex-wrap gap-x-6 gap-y-1 rounded-lg border border-border bg-surface px-4 py-3 text-sm">
        <span className="text-muted">
          Market value{" "}
          <span className="tnum text-foreground">
            {fmt(pnl.open.market_value, base)}
          </span>
        </span>
        <span className="text-muted">
          Cost basis{" "}
          <span className="tnum text-foreground">
            {fmt(pnl.open.cost_basis, base)}
          </span>
        </span>
        {account?.total_assets != null && (
          <span className="text-muted">
            Total assets{" "}
            <span className="tnum text-foreground">
              {fmt(account.total_assets, base)}
            </span>
          </span>
        )}
        {account?.power != null && (
          <span className="text-muted">
            Buying power{" "}
            <span className="tnum text-foreground">
              {fmt(account.power, base)}
            </span>
          </span>
        )}
        {/* The totals are a conversion, so the rate stays on the page. */}
        {converted.length > 0 && (
          <span className="text-muted sm:ml-auto" title={source}>
            In {base} · {fx}
          </span>
        )}
      </div>

      {/* Provenance, one click away: only open unrealized is broker-reported;
          realized is reconstructed from deals. */}
      <details className="text-xs text-muted">
        <summary className="cursor-pointer select-none hover:text-foreground">
          How these numbers are built
        </summary>
        <div className="mt-2 space-y-1.5 leading-relaxed">
          <p>
            <strong className="text-foreground">Open unrealized</strong> comes
            straight from the broker.
          </p>
          <p>
            <strong className="text-foreground">Realized</strong> is
            reconstructed by FIFO-matching {pnl.window.deals} deals (
            {pnl.window.start} → {pnl.window.end}):{" "}
            {fmt(pnl.closed.closed_realized, base)} from positions fully
            exited, {fmt(pnl.total_realized - pnl.closed.closed_realized, base)}{" "}
            from partial sells. It excludes {pnl.closed.excludes.join(", ")} —
            except dividends on positions still held, which arrive inside the
            broker&apos;s realized figure.
          </p>
          {converted.length > 0 && (
            <p>
              <strong className="text-foreground">Currency:</strong> each
              figure is converted into {base} at {fx} ({source}). Realized
              figures use today&apos;s rate, not the trade-date rate.
            </p>
          )}
        </div>
      </details>
    </section>
  )
}
