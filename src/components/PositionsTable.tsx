import { useMemo, useState } from "react";
import type { Position } from "@/lib/types";
import Money, { pct } from "./Money";

type Props = {
  positions: Position[];
  /** Code of the row whose chart is open, if any. */
  selected?: string | null;
  onSelect?: (code: string) => void;
};

type SortKey =
  | "code"
  | "qty"
  | "cost_price"
  | "nominal_price"
  | "market_val"
  | "today_pl_val"
  | "unrealized_pl"
  | "unrealized_pct"
  | "pl_ratio";

type SortDir = "asc" | "desc";

const columns: { key: SortKey; label: string; align: "left" | "right" }[] = [
  { key: "code", label: "Symbol", align: "left" },
  { key: "qty", label: "Qty", align: "right" },
  { key: "cost_price", label: "Cost", align: "right" },
  { key: "nominal_price", label: "Last", align: "right" },
  { key: "market_val", label: "Value", align: "right" },
  { key: "today_pl_val", label: "Today", align: "right" },
  { key: "unrealized_pl", label: "Unrealized", align: "right" },
  { key: "unrealized_pct", label: "Unrl %", align: "right" },
  { key: "pl_ratio", label: "%", align: "right" },
];

/** Unrealized P&L as a percentage of cost basis (qty × cost price). */
function unrealizedPct(p: Position): number {
  const costBasis = (p.qty ?? 0) * (p.cost_price ?? 0);
  if (!costBasis) return 0;
  return ((p.unrealized_pl ?? p.pl_val ?? 0) / costBasis) * 100;
}

function sortValue(p: Position, key: SortKey): number | string {
  switch (key) {
    case "code":
      return p.code;
    case "unrealized_pl":
      return p.unrealized_pl ?? p.pl_val ?? 0;
    case "unrealized_pct":
      return unrealizedPct(p);
    default:
      return p[key] ?? 0;
  }
}

export default function PositionsTable({ positions, selected, onSelect }: Props) {
  const [sortKey, setSortKey] = useState<SortKey>("market_val");
  const [sortDir, setSortDir] = useState<SortDir>("desc");

  const sorted = useMemo(() => {
    const dir = sortDir === "asc" ? 1 : -1;
    return [...positions].sort((a, b) => {
      const av = sortValue(a, sortKey);
      const bv = sortValue(b, sortKey);
      if (typeof av === "string" || typeof bv === "string") {
        return dir * String(av).localeCompare(String(bv));
      }
      return dir * (av - bv);
    });
  }, [positions, sortKey, sortDir]);

  if (positions.length === 0) {
    return (
      <div className="rounded-lg border border-border bg-surface p-8 text-center text-sm text-muted">
        No open positions.
      </div>
    );
  }

  function handleSort(key: SortKey) {
    if (key === sortKey) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      setSortDir(key === "code" ? "asc" : "desc");
    }
  }

  return (
    <div className="overflow-x-auto rounded-lg border border-border">
      <table className="w-full min-w-[820px] border-collapse text-sm">
        <thead>
          <tr className="border-b border-border bg-surface text-left text-xs uppercase tracking-wide text-muted">
            {columns.map((col) => (
              <th key={col.key} className={`px-3 py-2 font-medium ${col.align === "right" ? "text-right" : ""}`}>
                <button
                  type="button"
                  onClick={() => handleSort(col.key)}
                  aria-sort={sortKey === col.key ? (sortDir === "asc" ? "ascending" : "descending") : "none"}
                  className={`inline-flex items-center gap-1 hover:text-foreground ${
                    col.align === "right" ? "flex-row-reverse" : ""
                  } ${sortKey === col.key ? "text-foreground" : ""}`}
                >
                  {col.label}
                  <span className="text-[10px] leading-none">
                    {sortKey === col.key ? (sortDir === "asc" ? "▲" : "▼") : ""}
                  </span>
                </button>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {sorted.map((p) => (
            <tr
              key={p.code}
              onClick={() => onSelect?.(p.code)}
              aria-selected={selected === p.code}
              className={`border-b border-border last:border-0 hover:bg-surface ${
                onSelect ? "cursor-pointer" : ""
              } ${selected === p.code ? "bg-surface" : ""}`}
            >
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
                  unrealizedPct(p) > 0 ? "text-pos" : unrealizedPct(p) < 0 ? "text-neg" : ""
                }`}
              >
                {pct(unrealizedPct(p))}
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
