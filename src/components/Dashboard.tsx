"use client"

import { useCallback, useEffect, useState } from "react"
import { api } from "@/lib/api"
import type { Account, Health, Pnl, Position } from "@/lib/types"
import { fmt } from "./Money"
import PnlSummary from "./PnlSummary"
import PositionChart from "./PositionChart"
import PositionsTable from "./PositionsTable"

const POLL_MS = 30_000

/** Codes are `MARKET.SYMBOL` (US.META, MY.MAYBANK), so the prefix is the market. */
function groupByMarket(positions: Position[]): [string, Position[]][] {
  const groups = new Map<string, Position[]>()
  for (const p of positions) {
    const market = p.code.includes(".") ? p.code.split(".")[0] : "OTHER"
    const rows = groups.get(market)
    if (rows) rows.push(p)
    else groups.set(market, [p])
  }
  return [...groups.entries()].sort((a, b) => a[0].localeCompare(b[0]))
}

const RANGES = [
  { label: "1Y", days: 365 },
  { label: "2Y", days: 730 },
  { label: "3Y", days: 1095 },
  { label: "5Y", days: 1825 },
]

export default function Dashboard() {
  const [health, setHealth] = useState<Health | null>(null)
  const [pnl, setPnl] = useState<Pnl | null>(null)
  const [positions, setPositions] = useState<Position[] | null>(null)
  const [account, setAccount] = useState<Account | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [updated, setUpdated] = useState<Date | null>(null)
  const [notifying, setNotifying] = useState(false)
  const [sendingReview, setSendingReview] = useState(false)
  // True from a successful "started" response until the skill has had time to
  // finish, so the button can't be clicked again mid-run and hit the
  // sidecar's "already running" guard.
  const [reviewInFlight, setReviewInFlight] = useState(false)
  const [reviewStatus, setReviewStatus] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [selected, setSelected] = useState<string | null>(null)
  // Widest by default: the sidecar pulls the full lookback regardless, so a
  // narrower range hides history rather than saving a call.
  const [days, setDays] = useState(1825)

  // `alive` guards every setState so a request still in flight when the
  // component unmounts (or when polling is torn down) cannot write state.
  const load = useCallback(
    async (alive: () => boolean = () => true) => {
      try {
        const h = await api.health()
        if (!alive()) return
        setHealth(h)
        if (h.opend !== "connected") {
          setError(h.error ?? "OpenD is not connected. Start OpenD and log in.")
          return
        }
        const [p, pos, acc] = await Promise.all([
          api.pnl(days),
          api.positions(),
          api.account().catch(() => null),
        ])
        if (!alive()) return
        setPnl(p)
        setPositions(pos)
        setAccount(acc)
        setError(null)
        setUpdated(new Date())
      } catch (e) {
        if (!alive()) return
        setError(e instanceof Error ? e.message : String(e))
      }
    },
    [days],
  )

  useEffect(() => {
    let active = true
    const alive = () => active
    const tick = () => {
      void load(alive)
    }
    // Deferred rather than called in the effect body so the first paint is
    // never blocked by a state update scheduled during the effect.
    const first = setTimeout(tick, 0)
    const id = setInterval(tick, POLL_MS)
    return () => {
      active = false
      clearTimeout(first)
      clearInterval(id)
    }
  }, [load])

  const sendToTelegram = async () => {
    setNotifying(true)
    setActionError(null)
    try {
      const r = await api.notify("pnl")
      if (!r.sent) setActionError(`Daily summary: ${r.reason ?? "send failed"}`)
    } finally {
      setNotifying(false)
    }
  }

  // The skill run behind this takes ~2 minutes, so the button stays disabled
  // for that long after a successful start — otherwise a second click lands
  // on the sidecar's "already running" guard, which reads as a failure even
  // though the first run is progressing normally.
  const REVIEW_RUN_MS = 150_000

  const sendDailyReview = async () => {
    setSendingReview(true)
    setReviewStatus(null)
    setActionError(null)
    try {
      const r = await api.notify("review")
      if (r.started) {
        setReviewInFlight(true)
        setReviewStatus("Started — check Telegram in a couple of minutes")
        setTimeout(() => {
          setReviewStatus(null)
          setReviewInFlight(false)
        }, REVIEW_RUN_MS)
      } else if (r.detail?.includes("already running")) {
        setActionError("Daily review: still sending the last one — try again in a minute or two")
      } else {
        setActionError(`Daily review: ${r.detail ?? r.reason ?? "failed to start"}`)
      }
    } finally {
      setSendingReview(false)
    }
  }

  const selectedPosition = positions?.find((p) => p.code === selected) ?? null

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Portfolio</h1>
          <p className="text-xs text-muted">
            {updated
              ? `Updated ${updated.toLocaleTimeString()}`
              : error
                ? "Not connected"
                : "Loading…"}
            {health?.read_only && " · read-only"}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <div
            className="flex items-center rounded-md border border-border bg-surface p-0.5"
            role="group"
            aria-label="P&L window"
          >
            {RANGES.map((r) => (
              <button
                key={r.days}
                onClick={() => setDays(r.days)}
                aria-pressed={days === r.days}
                className={`rounded px-2 py-1 text-xs ${
                  days === r.days
                    ? "bg-accent/15 text-accent"
                    : "text-muted hover:text-foreground"
                }`}
              >
                {r.label}
              </button>
            ))}
          </div>
          <StatusDot health={health} />
          <button
            onClick={sendToTelegram}
            disabled={notifying || !pnl || !health?.telegram}
            title={
              health?.telegram
                ? "Send the current net P&L numbers to Telegram"
                : "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to enable"
            }
            className="rounded-md border border-border bg-surface px-3 py-1.5 text-sm hover:border-accent disabled:cursor-not-allowed disabled:opacity-40"
          >
            {notifying ? "Sending…" : "Send Daily Summary"}
          </button>
          <button
            onClick={sendDailyReview}
            disabled={
              sendingReview ||
              reviewInFlight ||
              health?.opend !== "connected" ||
              !health?.telegram
            }
            title={
              health?.telegram
                ? "Run the /daily-review skill and send it to Telegram (~2 min)"
                : "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to enable"
            }
            className="rounded-md border border-border bg-surface px-3 py-1.5 text-sm hover:border-accent disabled:cursor-not-allowed disabled:opacity-40"
          >
            {sendingReview ? "Starting…" : reviewInFlight ? "Sending…" : "Send Daily Review"}
          </button>
          {reviewStatus && <span className="text-xs text-muted">{reviewStatus}</span>}
        </div>
      </header>

      {error && (
        <div className="rounded-lg border border-neg/40 bg-neg/10 px-4 py-3 text-sm">
          <div className="font-medium text-neg">Not connected</div>
          <div className="mt-1 text-muted">{error}</div>
        </div>
      )}

      {actionError && (
        <div className="rounded-lg border border-neg/40 bg-neg/10 px-4 py-3 text-sm">
          <div className="text-muted">{actionError}</div>
        </div>
      )}

      {pnl && <PnlSummary pnl={pnl} account={account} />}

      {positions && (
        <section className="space-y-4">
          <div className="flex items-baseline gap-3">
            <h2 className="text-sm font-medium text-muted">Positions</h2>
            <span className="text-xs text-muted">
              {selected
                ? "Click the row again to close the chart"
                : "Click a row to chart it"}
            </span>
          </div>
          {groupByMarket(positions).map(([market, rows]) => (
            <div key={market} className="space-y-2">
              <div className="flex items-baseline gap-2 text-xs text-muted">
                <span className="font-medium uppercase tracking-wide text-foreground">
                  {market}
                </span>
                <span>
                  {rows.length} {rows.length === 1 ? "position" : "positions"} ·{" "}
                  {/* Every row in a market shares its settlement currency, so this
                      subtotal is a single-currency sum even though the page is not. */}
                  {fmt(
                    rows.reduce((s, p) => s + (p.market_val ?? 0), 0),
                    rows[0].currency ?? "USD",
                  )}
                </span>
                <span className="rounded bg-border/60 px-1 py-px text-[10px] font-medium tracking-wide">
                  {rows[0].currency ?? "USD"}
                </span>
              </div>
              <PositionsTable
                positions={rows}
                selected={selected}
                onSelect={(code) =>
                  setSelected((cur) => (cur === code ? null : code))
                }
              />
            </div>
          ))}
        </section>
      )}

      {selectedPosition && (
        <section className="space-y-2">
          <h2 className="text-sm font-medium text-muted">Chart</h2>
          <PositionChart position={selectedPosition} />
        </section>
      )}
    </div>
  )
}

function StatusDot({ health }: { health: Health | null }) {
  const ok = health?.opend === "connected"
  const label = health
    ? ok
      ? `OpenD · ${health.market ?? ""}`
      : "OpenD down"
    : "…"
  return (
    <span className="flex items-center gap-1.5 rounded-md border border-border bg-surface px-2.5 py-1.5 text-xs text-muted">
      <span
        className={`inline-block size-2 rounded-full ${ok ? "bg-pos" : "bg-neg"}`}
        aria-hidden
      />
      {label}
    </span>
  )
}
