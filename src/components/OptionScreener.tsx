"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { OptionCandidate, Permissions, ScreenResult } from "@/lib/types";
import { fmt, pct } from "./Money";

const PRESETS = [
  { label: "Near-dated directional", dte_min: 7, dte_max: 30, delta_min: 0.3, delta_max: 0.55 },
  { label: "Swing (1–2 months)", dte_min: 30, dte_max: 60, delta_min: 0.2, delta_max: 0.45 },
  { label: "Cheap convexity", dte_min: 21, dte_max: 90, delta_min: 0.1, delta_max: 0.25 },
];

export default function OptionScreener() {
  const [code, setCode] = useState("US.AAPL");
  const [type, setType] = useState<"ALL" | "CALL" | "PUT">("ALL");
  const [preset, setPreset] = useState(1);
  const [minOi, setMinOi] = useState(250);
  const [result, setResult] = useState<ScreenResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [perms, setPerms] = useState<Permissions | null>(null);

  // Option chains fail with a permission error when the market's option data
  // is not subscribed, so surface that up front rather than after a failed run.
  useEffect(() => {
    let active = true;
    const t = setTimeout(() => {
      void api
        .permissions()
        .then((p) => active && setPerms(p))
        .catch(() => {});
    }, 0);
    return () => {
      active = false;
      clearTimeout(t);
    };
  }, []);

  const market = code.trim().toUpperCase().split(".")[0];
  const marketBlocked =
    perms !== null && market.length > 0 && !perms.options_enabled.includes(market);

  const run = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError(null);
    try {
      const p = PRESETS[preset];
      setResult(
        await api.screen({
          code: code.trim().toUpperCase(),
          dte_min: p.dte_min,
          dte_max: p.dte_max,
          delta_min: p.delta_min,
          delta_max: p.delta_max,
          option_type: type,
          min_open_interest: minOi,
          limit: 25,
        }),
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setResult(null);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="space-y-5">
      <header>
        <h1 className="text-xl font-semibold">Options screener</h1>
        <p className="mt-1 text-xs text-muted">
          Filters the live chain and describes what each contract requires to break even. These
          are screening results, not recommendations — the call on whether any of them is worth
          taking is yours.
        </p>
      </header>

      <form onSubmit={run} className="flex flex-wrap items-end gap-3 rounded-lg border border-border bg-surface p-4">
        <label className="flex flex-col gap-1 text-xs text-muted">
          Underlying
          <input
            value={code}
            onChange={(e) => setCode(e.target.value)}
            placeholder="US.AAPL"
            className="tnum w-36 rounded-md border border-border bg-background px-2.5 py-1.5 text-sm text-foreground"
          />
        </label>
        <label className="flex flex-col gap-1 text-xs text-muted">
          Type
          <select
            value={type}
            onChange={(e) => setType(e.target.value as typeof type)}
            className="rounded-md border border-border bg-background px-2.5 py-1.5 text-sm text-foreground"
          >
            <option value="ALL">Calls + puts</option>
            <option value="CALL">Calls</option>
            <option value="PUT">Puts</option>
          </select>
        </label>
        <label className="flex flex-col gap-1 text-xs text-muted">
          Profile
          <select
            value={preset}
            onChange={(e) => setPreset(Number(e.target.value))}
            className="rounded-md border border-border bg-background px-2.5 py-1.5 text-sm text-foreground"
          >
            {PRESETS.map((p, i) => (
              <option key={p.label} value={i}>
                {p.label} · {p.dte_min}–{p.dte_max}d · {p.delta_min}–{p.delta_max}Δ
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-xs text-muted">
          Min OI
          <input
            type="number"
            min={0}
            value={minOi}
            onChange={(e) => setMinOi(Number(e.target.value))}
            className="tnum w-24 rounded-md border border-border bg-background px-2.5 py-1.5 text-sm text-foreground"
          />
        </label>
        <button
          type="submit"
          disabled={loading}
          className="rounded-md border border-accent bg-accent/10 px-4 py-1.5 text-sm text-accent hover:bg-accent/20 disabled:opacity-40"
        >
          {loading ? "Screening…" : "Screen"}
        </button>
      </form>

      {marketBlocked && (
        <div className="rounded-lg border border-border bg-surface px-4 py-3 text-sm">
          <div className="font-medium">No {market} option data on this account</div>
          <div className="mt-1 text-muted">
            moomoo reports <span className="tnum">{market}</span> option quotes as{" "}
            <span className="tnum">
              {perms?.markets?.[market]?.option ?? "unavailable"}
            </span>
            . Option chains for this market will fail until the subscription is added
            in the moomoo app.
            {perms && perms.options_enabled.length > 0 && (
              <> Options data is currently enabled for {perms.options_enabled.join(", ")}.</>
            )}
          </div>
        </div>
      )}

      {error && (
        <div className="rounded-lg border border-neg/40 bg-neg/10 px-4 py-3 text-sm">
          <div className="font-medium text-neg">Screen failed</div>
          <div className="mt-1 text-muted">{error}</div>
        </div>
      )}

      {result && (
        <section className="space-y-3">
          <div className="flex items-baseline gap-3 text-sm">
            <span className="font-medium">{result.underlying}</span>
            <span className="tnum text-muted">spot {fmt(result.spot)}</span>
            <span className="text-muted">
              {result.count} contract{result.count === 1 ? "" : "s"} passed
              {result.count > result.candidates.length && ` · showing ${result.candidates.length}`}
            </span>
          </div>
          {result.candidates.length === 0 ? (
            <div className="rounded-lg border border-border bg-surface p-8 text-center text-sm text-muted">
              Nothing matched. Widen the delta band, extend the expiry window, or lower min OI.
            </div>
          ) : (
            <ul className="space-y-2">
              {result.candidates.map((c) => (
                <Candidate key={c.code} c={c} />
              ))}
            </ul>
          )}
        </section>
      )}
    </div>
  );
}

function Candidate({ c }: { c: OptionCandidate }) {
  const isCall = c.option_type === "CALL";
  return (
    <li className="rounded-lg border border-border bg-surface p-4">
      <div className="flex flex-wrap items-center gap-2">
        <span
          className={`rounded px-1.5 py-0.5 text-xs font-medium ${
            isCall ? "bg-pos/15 text-pos" : "bg-neg/15 text-neg"
          }`}
        >
          {c.option_type}
        </span>
        <span className="tnum font-medium">${c.strike_price}</span>
        <span className="text-xs text-muted">
          {c.expiry} {c.dte !== null && `· ${c.dte}d`}
        </span>
        <span className="tnum ml-auto text-sm">{fmt(c.cost)}<span className="text-muted"> /contract</span></span>
      </div>

      {/* The one-line characterisation: purely mechanical, derived from live numbers. */}
      <p className="mt-2 text-sm leading-relaxed">{c.characterisation}</p>

      <dl className="mt-3 flex flex-wrap gap-x-5 gap-y-1 text-xs text-muted">
        <Field label="Δ" value={c.delta?.toFixed(3)} />
        <Field label="Θ" value={c.theta?.toFixed(3)} />
        <Field label="Vega" value={c.vega?.toFixed(3)} />
        <Field label="IV" value={c.iv != null ? `${c.iv.toFixed(1)}%` : undefined} />
        <Field label="OI" value={c.open_interest?.toLocaleString()} />
        <Field label="Vol" value={c.volume?.toLocaleString()} />
        <Field label="Bid/Ask" value={c.bid != null && c.ask != null ? `${c.bid} / ${c.ask}` : undefined} />
        <Field label="Breakeven" value={c.breakeven != null ? fmt(c.breakeven) : undefined} />
        <Field label="Move needed" value={c.required_move_pct != null ? pct(c.required_move_pct, 1) : undefined} />
      </dl>
    </li>
  );
}

function Field({ label, value }: { label: string; value?: string }) {
  if (!value) return null;
  return (
    <div className="flex gap-1">
      <dt>{label}</dt>
      <dd className="tnum text-foreground">{value}</dd>
    </div>
  );
}
