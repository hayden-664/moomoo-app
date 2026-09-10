import type {
  Account,
  Candles,
  Fill,
  Health,
  PaperDecision,
  PaperOrder,
  PaperStatus,
  Permissions,
  Pnl,
  Position,
  Quota,
  ScreenResult,
} from "./types"

async function get<T>(
  path: string,
  params?: Record<string, string | number>,
): Promise<T> {
  const qs = params
    ? "?" +
      new URLSearchParams(
        Object.entries(params).map(([k, v]) => [k, String(v)]),
      )
    : ""
  const res = await fetch(`/api/moomoo/${path}${qs}`, { cache: "no-store" })
  const body = await res.json()
  if (!res.ok)
    throw new Error(
      body?.detail ?? body?.error ?? `request failed (${res.status})`,
    )
  return body as T
}

export const api = {
  health: () => get<Health>("health"),
  permissions: () => get<Permissions>("permissions"),
  account: () => get<Account>("account"),
  positions: () => get<Position[]>("positions"),
  pnl: (days = 365) => get<Pnl>("pnl", { days }),
  quota: () => get<Quota>("quota"),
  candles: (code: string, days = 365) =>
    get<Candles>("candles", { code, days }),
  markers: (code: string, days = 365) => get<Fill[]>("markers", { code, days }),
  screen: (params: Record<string, string | number>) =>
    get<ScreenResult>("options/screen", params),
  notify: async (which: "pnl" | "test" | "review") => {
    const res = await fetch(`/api/moomoo/notify/${which}`, { method: "POST" })
    return res.json()
  },

  // Paper engine. Reads, plus stop and resume — the proxy allowlist does not
  // carry run-once or dry-run, so the browser cannot make it trade on demand.
  paperStatus: () => get<PaperStatus>("paper/status"),
  paperPositions: () => get<Position[]>("paper/positions"),
  paperOrders: (days = 7) => get<PaperOrder[]>("paper/orders", { days }),
  paperJournal: (days = 7, limit = 100) =>
    get<PaperDecision[]>("paper/journal", { days, limit }),
  // Must echo the reason the engine reported; the sidecar rejects a mismatch,
  // which is what stops a resume being a reflex.
  paperResume: async (reason: string) => {
    const res = await fetch(
      `/api/moomoo/paper/resume?reason=${encodeURIComponent(reason)}`,
      { method: "POST" },
    )
    return { ok: res.ok, body: await res.json() }
  },
  paperKill: async (reason: string) => {
    // Reason travels as a query param: the proxy forwards no request body.
    const res = await fetch(
      `/api/moomoo/paper/kill?reason=${encodeURIComponent(reason)}`,
      { method: "POST" },
    )
    return res.json()
  },
}
