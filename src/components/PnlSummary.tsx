import type { Account, Pnl } from "@/lib/types";
import Money, { fmt, fxLabel } from "./Money";

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
  label: string;
  value: number | null;
  currency: string;
  note?: string;
  signed?: boolean;
  emphasis?: boolean;
}) {
  return (
    <div className="rounded-lg border border-border bg-surface p-4">
      <div className="text-xs uppercase tracking-wide text-muted">{label}</div>
      <div className={emphasis ? "mt-1 text-3xl font-semibold" : "mt-1 text-xl font-medium"}>
        <Money value={value} currency={currency} signed={signed} showSign={signed} />
      </div>
      {note && <div className="mt-1 text-xs text-muted">{note}</div>}
    </div>
  );
}

export default function PnlSummary({ pnl, account }: { pnl: Pnl; account: Account | null }) {
  const base = pnl.base;
  const { converted_from: converted, unconverted, rates, source } = pnl.conversion;

  return (
    <section className="space-y-3">
      {/* The totals are a conversion, so say at what rate. Without this the
          page looks like a pure dollar account, which it is not. */}
      {converted.length > 0 && (
        <div className="rounded-lg border border-border bg-surface px-4 py-3 text-sm">
          <div className="font-medium">
            {converted.join(", ")} converted into {base}
          </div>
          <div className="mt-1 text-muted">
            Every figure below is one {base} total, at{" "}
            {converted.map((c) => fxLabel(c, base, rates[c])).join(" · ")} — {source}. Realized
            figures use today&apos;s rate rather than the rate on each trade date, so they drift
            with the currency.
          </div>
        </div>
      )}

      {/* A currency with no solvable rate is left out of the totals entirely.
          It has to be said out loud rather than quietly rounding to zero. */}
      {unconverted.length > 0 && (
        <div className="rounded-lg border border-neg/40 bg-neg/10 px-4 py-3 text-sm">
          <div className="font-medium text-neg">
            {unconverted.join(", ")} not included — no exchange rate available
          </div>
          <div className="mt-1 text-muted">
            Figures in {unconverted.join(", ")} are excluded from the totals below rather than being
            added at face value. Rates come from the broker; if this persists, check that OpenD is
            connected and the account reports a balance.
          </div>
        </div>
      )}

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Stat
          label="Net P&L"
          value={pnl.net}
          currency={base}
          emphasis
          note={`${pnl.position_count} open position${pnl.position_count === 1 ? "" : "s"}`}
        />
        <Stat label="Today" value={pnl.open.today} currency={base} />
        <Stat
          label="Open unrealized"
          value={pnl.open.open_unrealized}
          currency={base}
          note="Mark-to-market, broker-reported"
        />
        <Stat
          label="Realized"
          value={pnl.total_realized}
          currency={base}
          note={`Cashed out since ${pnl.window.start} · approximate`}
        />
      </div>

      <div className="flex flex-wrap gap-x-6 gap-y-1 rounded-lg border border-border bg-surface px-4 py-3 text-sm">
        <span className="text-muted">
          Market value{" "}
          <span className="tnum text-foreground">{fmt(pnl.open.market_value, base)}</span>
        </span>
        <span className="text-muted">
          Cost basis <span className="tnum text-foreground">{fmt(pnl.open.cost_basis, base)}</span>
        </span>
        {account?.total_assets != null && (
          <span className="text-muted">
            Total assets{" "}
            <span className="tnum text-foreground">{fmt(account.total_assets, base)}</span>
          </span>
        )}
        {account?.power != null && (
          <span className="text-muted">
            Buying power <span className="tnum text-foreground">{fmt(account.power, base)}</span>
          </span>
        )}
      </div>

      {/* The provenance of each number matters more than usual here: only open
          unrealized is broker-reported; realized is reconstructed from deals. */}
      <p className="text-xs leading-relaxed text-muted">
        <strong className="text-foreground">How net P&amp;L is built:</strong> open unrealized comes
        straight from the broker. Realized is reconstructed by FIFO-matching {pnl.window.deals}{" "}
        deals over {pnl.window.start} → {pnl.window.end}, because this account reports no realized
        P&amp;L of its own — it combines {fmt(pnl.closed.closed_realized, base)} from positions you
        are fully out of with {fmt(pnl.total_realized - pnl.closed.closed_realized, base)} banked
        from selling part of holdings you still own. It excludes {pnl.closed.excludes.join(", ")},
        so treat it as an estimate rather than a statement.
        {converted.length > 0 &&
          ` Each deal is attributed to its market's currency and converted into ${base} at ${converted
            .map((c) => fxLabel(c, base, rates[c]))
            .join(", ")}.`}
      </p>
    </section>
  );
}
