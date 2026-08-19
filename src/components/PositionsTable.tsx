import type { Position } from "@/lib/types";
import Money, { pct } from "./Money";

export default function PositionsTable({ positions }: { positions: Position[] }) {
  if (positions.length === 0) {
    return (
      <div className="rounded-lg border border-border bg-surface p-8 text-center text-sm text-muted">
        No open positions.
      </div>
    );
  }

  const sorted = [...positions].sort(
    (a, b) => (b.market_val ?? 0) - (a.market_val ?? 0),
  );

  return (
    <div className="overflow-x-auto rounded-lg border border-border">
      <table className="w-full min-w-[820px] border-collapse text-sm">
        <thead>
          <tr className="border-b border-border bg-surface text-left text-xs uppercase tracking-wide text-muted">
            <th className="px-3 py-2 font-medium">Symbol</th>
            <th className="px-3 py-2 text-right font-medium">Qty</th>
            <th className="px-3 py-2 text-right font-medium">Cost</th>
            <th className="px-3 py-2 text-right font-medium">Last</th>
            <th className="px-3 py-2 text-right font-medium">Value</th>
            <th className="px-3 py-2 text-right font-medium">Today</th>
            <th className="px-3 py-2 text-right font-medium">Unrealized</th>
            <th className="px-3 py-2 text-right font-medium">%</th>
          </tr>
        </thead>
        <tbody>
          {sorted.map((p) => (
            <tr key={p.code} className="border-b border-border last:border-0 hover:bg-surface">
              <td className="px-3 py-2">
                <div className="font-medium">{p.code}</div>
                <div className="truncate text-xs text-muted">{p.stock_name}</div>
              </td>
              <td className="tnum px-3 py-2 text-right">{p.qty ?? "—"}</td>
              <td className="tnum px-3 py-2 text-right"><Money value={p.cost_price} /></td>
              <td className="tnum px-3 py-2 text-right"><Money value={p.nominal_price} /></td>
              <td className="tnum px-3 py-2 text-right"><Money value={p.market_val} /></td>
              <td className="px-3 py-2 text-right"><Money value={p.today_pl_val} signed showSign /></td>
              <td className="px-3 py-2 text-right">
                <Money value={p.unrealized_pl ?? p.pl_val} signed showSign />
              </td>
              <td
                className={`tnum px-3 py-2 text-right ${
                  (p.pl_ratio ?? 0) > 0 ? "text-pos" : (p.pl_ratio ?? 0) < 0 ? "text-neg" : ""
                }`}
              >
                {pct(p.pl_ratio)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
