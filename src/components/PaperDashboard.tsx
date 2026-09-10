"use client"

import { useCallback, useEffect, useState } from "react"
import { api } from "@/lib/api"
import type {
  PaperDecision,
  PaperOrder,
  PaperStatus,
  Position,
} from "@/lib/types"
import { fmt, num } from "./Money"
import PositionsTable from "./PositionsTable"

const POLL_MS = 30_000

/** Outcomes worth colouring. Everything else reads as neutral. */
const OUTCOME_TONE: Record<string, string> = {
  PLACED: "text-pos",
  PLACE_FAILED: "text-neg",
  BLOCKED: "text-muted",
  NO_MATCH: "text-muted",
}

function Row({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-4 py-1">
      <span className="text-muted">{label}</span>
      <span className="tnum">{value}</span>
    </div>
  )
}

export default function PaperDashboard() {
  const [status, setStatus] = useState<PaperStatus | null>(null)
  const [positions, setPositions] = useState<Position[] | null>(null)
  const [orders, setOrders] = useState<PaperOrder[] | null>(null)
  const [journal, setJournal] = useState<PaperDecision[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [updated, setUpdated] = useState<string | null>(null)
  const [expanded, setExpanded] = useState<string | null>(null)
  const [killing, setKilling] = useState(false)
  // Resume is two clicks on purpose. The first reveals what the engine halted
  // for; the second acts on it. A halt is usually something going wrong, so
  // reading the reason should not be skippable.
  const [resumeArmed, setResumeArmed] = useState(false)
  const [resuming, setResuming] = useState(false)

  // `alive` guards every setState so a request still in flight when the
  // component unmounts cannot write state.
  const load = useCallback(async (alive: () => boolean) => {
    try {
      const s = await api.paperStatus()
      if (!alive()) return
      setStatus(s)
      setError(null)

      const [p, o, j] = await Promise.all([
        api.paperPositions().catch(() => null),
        api.paperOrders().catch(() => null),
        api.paperJournal().catch(() => null),
      ])
      if (!alive()) return
      if (p) setPositions(p)
      if (o) setOrders(o)
      if (j) setJournal(j)
      setUpdated(new Date().toLocaleTimeString())
    } catch (err) {
      if (!alive()) return
      setError(err instanceof Error ? err.message : String(err))
    }
  }, [])

  useEffect(() => {
    let active = true
    const alive = () => active
    const tick = () => {
      void load(alive)
    }
    // Deferred so the first paint is never blocked by a state update
    // scheduled during the effect.
    const first = setTimeout(tick, 0)
    const id = setInterval(tick, POLL_MS)
    return () => {
      active = false
      clearTimeout(first)
      clearInterval(id)
    }
  }, [load])

  const kill = async () => {
    setKilling(true)
    setActionError(null)
    try {
      const r = await api.paperKill("stopped from the dashboard")
      if (!r?.killed) setActionError(r?.detail ?? "could not halt the engine")
      await load(() => true)
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err))
    } finally {
      setKilling(false)
    }
  }

  const resume = async () => {
    const reason = engine?.kill_reason
    if (!reason) return
    setResuming(true)
    setActionError(null)
    try {
      const { ok, body } = await api.paperResume(reason)
      if (!ok) {
        setActionError(body?.detail ?? "could not resume the engine")
      } else {
        setResumeArmed(false)
      }
      await load(() => true)
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err))
    } finally {
      setResuming(false)
    }
  }

  const engine = status?.engine
  const universe = status?.last_cycle?.universe

  // Open exposure as a share of the real account's equity - the same basis the
  // engine's open_notional_cap uses, so the number here is the one that gates
  // the next entry.
  const equityReal = num(status?.last_cycle?.equity_real)
  const openNotional = positions
    ? positions.reduce((t, p) => t + (num(p.market_val) ?? 0), 0)
    : null
  const deployed =
    openNotional != null && equityReal && equityReal > 0
      ? { usd: openNotional, pct: (openNotional / equityReal) * 100 }
      : null
  const excluded = universe?.excluded ?? []

  return (
    <div className="flex flex-col gap-6">
      {/* Load-bearing, not decoration: every number on this page is simulated,
          and the page must never be mistakable for the real portfolio. */}
      <div className="rounded-lg border border-accent/40 bg-accent/10 px-4 py-3 text-sm">
        <b>Simulated account {engine?.acc_id ?? 5367857}.</b> No real money moves
        here. Sizes are calculated from the real account&rsquo;s equity so the
        results are at a realistic scale, but every order is filled against
        moomoo&rsquo;s paper account, and the real account is not reachable from
        this page.
      </div>

      <header className="flex flex-wrap items-center gap-3">
        <h1 className="text-lg font-semibold tracking-tight">Paper engine</h1>
        <span
          className={`rounded border px-2 py-0.5 text-xs ${
            engine?.killed
              ? "border-neg/40 text-neg"
              : engine?.enabled
                ? "border-pos/40 text-pos"
                : "border-border text-muted"
          }`}
        >
          {engine?.killed ? "halted" : engine?.enabled ? "armed" : "disabled"}
        </span>
        <span className="text-xs text-muted">
          {updated
            ? `Updated ${updated}`
            : error
              ? "Not connected"
              : "Loading…"}
        </span>
        <button
          onClick={kill}
          disabled={killing || !!engine?.killed}
          title={
            engine?.killed
              ? "Already halted — resume from the banner below, where the reason is shown"
              : "Stop the engine placing any further orders"
          }
          className="ml-auto rounded border border-neg/40 px-3 py-1 text-sm text-neg disabled:opacity-40"
        >
          {engine?.killed ? "Halted" : killing ? "Stopping…" : "Stop engine"}
        </button>
      </header>

      {error && (
        <div className="rounded-lg border border-neg/40 bg-neg/10 px-4 py-3 text-sm">
          <b>Could not reach the paper engine.</b>
          <div className="text-muted">{error}</div>
        </div>
      )}
      {actionError && (
        <div className="rounded-lg border border-neg/40 bg-neg/10 px-4 py-3 text-sm">
          <b>That action failed.</b>
          <div className="text-muted">{actionError}</div>
        </div>
      )}
      {engine?.killed && (
        <div className="rounded-lg border border-neg/40 bg-neg/10 px-4 py-3 text-sm">
          <b>Engine halted.</b>
          <div className="text-muted">
            {engine.kill_reason || "no reason recorded"} — it will place nothing
            until resumed.
          </div>
          {/* A halt caused by the isolation guarantee failing is not clearable
              here; the sidecar refuses it and says what to do instead. */}
          <div className="mt-3 flex flex-wrap items-center gap-2">
            {resumeArmed ? (
              <>
                <button
                  onClick={resume}
                  disabled={resuming}
                  className="rounded border border-pos/50 px-3 py-1 text-pos disabled:opacity-40"
                >
                  {resuming ? "Resuming…" : "Confirm resume"}
                </button>
                <button
                  onClick={() => setResumeArmed(false)}
                  className="rounded border border-border px-3 py-1 text-muted"
                >
                  Cancel
                </button>
                <span className="text-xs text-muted">
                  This clears: “{engine.kill_reason}”
                </span>
              </>
            ) : (
              <button
                onClick={() => setResumeArmed(true)}
                className="rounded border border-border px-3 py-1"
              >
                Resume engine…
              </button>
            )}
          </div>
        </div>
      )}
      {status?.rules_error && (
        <div className="rounded-lg border border-neg/40 bg-neg/10 px-4 py-3 text-sm">
          <b>Strategy file invalid — nothing will trade.</b>
          <div className="text-muted">{status.rules_error}</div>
        </div>
      )}

      <section className="grid gap-4 sm:grid-cols-2">
        <div className="rounded-lg border border-border bg-surface px-4 py-3 text-sm">
          <div className="mb-1 font-medium">Engine</div>
          <Row label="Cycle" value={`${engine?.cycle_minutes ?? "—"} min`} />
          {/* Trading nothing all day is invisible otherwise: never reaching
              the window is not a halt, so `alerts` did not cover it. */}
          <Row
            label="Entries evaluated"
            value={
              engine?.missed_entry_window ? (
                <span className="text-neg">
                  {engine.evaluate_at} ET — missed today
                </span>
              ) : engine?.evaluate_at ? (
                `${engine.evaluate_at} ET`
              ) : (
                "every cycle"
              )
            }
          />
          {/* Says why Telegram is quiet — everything is here regardless. */}
          <Row
            label="Telegram"
            value={
              engine?.notify === "all"
                ? "orders + fills"
                : engine?.notify === "alerts"
                  ? "halts only"
                  : "silent"
            }
          />
          <Row label="Orders today" value={status?.orders_today ?? "—"} />
          {/* The cap budgets new exposure. Exits are not counted against it
              and are never refused by it, so the counter is entries. */}
          <Row
            label="Entry cap"
            value={
              status?.caps
                ? `${status.entries_today ?? 0} of ${status.caps.max_orders_per_day} used`
                : "—"
            }
          />
          {/* Computed from rows this page already has, rather than another
              position_list_query on a 30s poll. Measured against the real
              account's equity because that is what sized the positions. */}
          <Row
            label="Deployed"
            value={
              deployed != null && status?.caps
                ? `${fmt(deployed.usd, "USD")} · ${deployed.pct.toFixed(0)}% of ${status.caps.max_open_notional_pct}%`
                : "—"
            }
          />
          <Row
            label="Positions"
            value={
              positions && status?.caps
                ? `${positions.length} of ${status.caps.max_concurrent_positions}`
                : "—"
            }
          />
          <Row
            label="Max per order"
            value={
              status?.caps ? fmt(status.caps.max_notional_usd, "USD") : "—"
            }
          />
          <Row
            label="Rules"
            value={
              <span className="font-mono text-xs">
                {engine?.rules_sha?.slice(0, 12) ?? "—"}
              </span>
            }
          />
        </div>
        <div className="rounded-lg border border-border bg-surface px-4 py-3 text-sm">
          <div className="mb-1 font-medium">Last cycle</div>
          <Row
            label="Market"
            value={status?.last_cycle?.market_state ?? "—"}
          />
          <Row
            label="Blocked by"
            value={status?.last_cycle?.blocked_by ?? "nothing"}
          />
          {/* Exits and the pre-close cancel run regardless of this; it gates
              new entries only. */}
          <Row
            label="Entry window"
            value={
              <span className={engine?.entry_window?.open ? "text-pos" : "text-muted"}>
                {engine?.entry_window?.open ? "open" : "gated"}
              </span>
            }
          />
          <Row
            label="Considered"
            value={universe?.considered?.length ?? "—"}
          />
          <Row
            label="Real equity (sizing basis)"
            value={
              status?.last_cycle?.equity_real != null
                ? fmt(status.last_cycle.equity_real, "USD")
                : "—"
            }
          />
          <Row
            label="Paper equity"
            value={
              status?.last_cycle?.paper_total_assets != null
                ? fmt(status.last_cycle.paper_total_assets, "USD")
                : "—"
            }
          />
        </div>
      </section>

      {excluded.length > 0 && (
        // Named rather than dropped, for the same reason unconvertible
        // currencies are named in the P&L: a symbol that vanished silently
        // looks identical to one that simply did not qualify.
        <section className="rounded-lg border border-border bg-surface px-4 py-3 text-sm">
          <div className="mb-2 font-medium">
            Excluded from the universe ({excluded.length})
          </div>
          <ul className="flex flex-col gap-1 text-muted">
            {excluded.map((e) => (
              <li key={e.code}>
                <span className="text-foreground">{e.code}</span> — {e.reason}:{" "}
                {e.detail}
              </li>
            ))}
          </ul>
        </section>
      )}

      <section>
        <h2 className="mb-2 text-sm font-medium">Simulated positions</h2>
        {positions && positions.length > 0 ? (
          <PositionsTable positions={positions} />
        ) : (
          <div className="rounded-lg border border-border bg-surface px-4 py-6 text-center text-sm text-muted">
            No simulated positions.
          </div>
        )}
      </section>

      <section>
        <h2 className="mb-2 text-sm font-medium">Orders</h2>
        {orders && orders.length > 0 ? (
          <div className="overflow-x-auto rounded-lg border border-border">
            <table className="w-full text-sm">
              <thead className="bg-surface text-muted">
                <tr>
                  <th className="px-3 py-2 text-left font-normal">Time</th>
                  <th className="px-3 py-2 text-left font-normal">Symbol</th>
                  <th className="px-3 py-2 text-left font-normal">Side</th>
                  <th className="px-3 py-2 text-right font-normal">Qty</th>
                  <th className="px-3 py-2 text-right font-normal">Limit</th>
                  <th className="px-3 py-2 text-right font-normal">Filled</th>
                  <th className="px-3 py-2 text-left font-normal">Status</th>
                </tr>
              </thead>
              <tbody>
                {orders.map((o) => (
                  <tr key={o.order_id} className="border-t border-border">
                    <td className="px-3 py-2 tnum text-muted">
                      {o.created_at?.slice(11, 19) ?? "—"}
                    </td>
                    <td className="px-3 py-2">{o.code}</td>
                    <td
                      className={`px-3 py-2 ${o.trd_side === "BUY" ? "text-pos" : "text-neg"}`}
                    >
                      {o.trd_side}
                    </td>
                    <td className="px-3 py-2 text-right tnum">{o.qty}</td>
                    <td className="px-3 py-2 text-right tnum">
                      {fmt(o.price, "USD")}
                    </td>
                    <td className="px-3 py-2 text-right tnum">
                      {o.dealt_qty
                        ? `${o.dealt_qty} @ ${fmt(o.dealt_avg_price, "USD")}`
                        : "—"}
                    </td>
                    <td className="px-3 py-2">
                      {o.status}
                      {o.last_err_msg && (
                        <div className="text-xs text-neg">{o.last_err_msg}</div>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="rounded-lg border border-border bg-surface px-4 py-6 text-center text-sm text-muted">
            No orders yet.
          </div>
        )}
      </section>

      <section>
        <h2 className="mb-2 text-sm font-medium">Decision journal</h2>
        <p className="mb-2 text-xs text-muted">
          Every evaluation, including the ones that did nothing. Click a row to
          see which condition was true and what it read.
        </p>
        {journal && journal.length > 0 ? (
          <div className="flex flex-col gap-1">
            {journal.map((d) => (
              <div
                key={d.decision_id}
                className="rounded-lg border border-border bg-surface text-sm"
              >
                <button
                  onClick={() =>
                    setExpanded(
                      expanded === d.decision_id ? null : d.decision_id,
                    )
                  }
                  aria-expanded={expanded === d.decision_id}
                  className="flex w-full items-baseline gap-3 px-3 py-2 text-left"
                >
                  <span className="tnum text-muted">{d.ts.slice(11, 19)}</span>
                  <span>{d.code}</span>
                  <span className="text-muted">{d.rule_id}</span>
                  <span
                    className={`ml-auto ${OUTCOME_TONE[d.outcome] ?? ""}`}
                  >
                    {d.outcome}
                    {d.block_reason ? ` · ${d.block_reason}` : ""}
                  </span>
                </button>
                {expanded === d.decision_id && (
                  <div className="border-t border-border px-3 py-2 text-xs">
                    {d.block_detail && (
                      <p className="mb-2 text-muted">{d.block_detail}</p>
                    )}
                    {d.qty != null && (
                      <p className="mb-2 tnum">
                        {d.side} {d.qty} @ {fmt(d.limit_price, "USD")} · stop{" "}
                        {fmt(d.stop_price, "USD")} · risk{" "}
                        {fmt(d.risk_usd, "USD")}
                      </p>
                    )}
                    {d.trace && d.trace.length > 0 ? (
                      <ul className="flex flex-col gap-0.5 font-mono">
                        {d.trace.map((t, i) => (
                          <li
                            key={`${d.decision_id}-${i}`}
                            className={t.result ? "text-pos" : "text-muted"}
                          >
                            {t.result ? "✓" : "✗"} {t.path} ={" "}
                            {JSON.stringify(t.resolved)} ({t.op}
                            {t.target != null
                              ? ` ${JSON.stringify(t.target)}`
                              : ""}
                            )
                          </li>
                        ))}
                      </ul>
                    ) : (
                      <p className="text-muted">No condition trace recorded.</p>
                    )}
                  </div>
                )}
              </div>
            ))}
          </div>
        ) : (
          <div className="rounded-lg border border-border bg-surface px-4 py-6 text-center text-sm text-muted">
            Nothing evaluated yet.
          </div>
        )}
      </section>
    </div>
  )
}
