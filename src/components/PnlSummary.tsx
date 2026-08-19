import type { Account, Pnl } from "@/lib/types";
import Money, { fmt } from "./Money";

function Stat({
  label,
  value,
  note,
  signed = true,
  emphasis = false,
}: {
  label: string;
  value: number | null;
  note?: string;
  signed?: boolean;
  emphasis?: boolean;
}) {
  return (
    <div className="rounded-lg border border-border bg-surface p-4">
      <div className="text-xs uppercase tracking-wide text-muted">{label}</div>
      <div className={emphasis ? "mt-1 text-3xl font-semibold" : "mt-1 text-xl font-medium"}>
        <Money value={value} signed={signed} showSign={signed} />
      </div>
      {note && <div className="mt-1 text-xs text-muted">{note}</div>}
    </div>
  );
}

export default function PnlSummary({ pnl, account }: { pnl: Pnl; account: Account | null }) {
  return (
    <section className="space-y-3">
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Stat
          label="Net P&L"
          value={pnl.net}
          emphasis
          note={`${pnl.position_count} open position${pnl.position_count === 1 ? "" : "s"}`}
        />
        <Stat label="Today" value={pnl.open.today} />
        <Stat
          label="Open unrealized"
          value={pnl.open.open_unrealized}
          note="Mark-to-market, broker-reported"
        />
        <Stat
          label="Realized"
          value={pnl.total_realized}
          note={`Cashed out since ${pnl.window.start} · approximate`}
        />
      </div>

      <div className="flex flex-wrap gap-x-6 gap-y-1 rounded-lg border border-border bg-surface px-4 py-3 text-sm">
        <span className="text-muted">
          Market value <span className="tnum text-foreground">{fmt(pnl.open.market_value)}</span>
        </span>
        <span className="text-muted">
          Cost basis <span className="tnum text-foreground">{fmt(pnl.open.cost_basis)}</span>
        </span>
        {account?.total_assets != null && (
          <span className="text-muted">
            Total assets <span className="tnum text-foreground">{fmt(account.total_assets)}</span>
          </span>
        )}
        {account?.power != null && (
          <span className="text-muted">
            Buying power <span className="tnum text-foreground">{fmt(account.power)}</span>
          </span>
        )}
      </div>

      {/* The provenance of each number matters more than usual here: only open
          unrealized is broker-reported; realized is reconstructed from deals. */}
      <p className="text-xs leading-relaxed text-muted">
        <strong className="text-foreground">How net P&amp;L is built:</strong> open unrealized comes
        straight from the broker. Realized is reconstructed by FIFO-matching {pnl.window.deals}{" "}
        deals over {pnl.window.start} → {pnl.window.end}, because this account reports no realized
        P&amp;L of its own — it combines {fmt(pnl.closed.closed_realized)} from positions you are
        fully out of with {fmt(pnl.total_realized - pnl.closed.closed_realized)} banked from
        selling part of holdings you still own. It excludes {pnl.closed.excludes.join(", ")}, so
        treat it as an estimate rather than a statement.
      </p>
    </section>
  );
}
