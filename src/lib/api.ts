import type {
  Account,
  Health,
  Permissions,
  Pnl,
  Position,
  ScreenResult,
} from "./types";

async function get<T>(path: string, params?: Record<string, string | number>): Promise<T> {
  const qs = params
    ? "?" + new URLSearchParams(Object.entries(params).map(([k, v]) => [k, String(v)]))
    : "";
  const res = await fetch(`/api/moomoo/${path}${qs}`, { cache: "no-store" });
  const body = await res.json();
  if (!res.ok) throw new Error(body?.detail ?? body?.error ?? `request failed (${res.status})`);
  return body as T;
}

export const api = {
  health: () => get<Health>("health"),
  permissions: () => get<Permissions>("permissions"),
  account: () => get<Account>("account"),
  positions: () => get<Position[]>("positions"),
  pnl: (days = 365) => get<Pnl>("pnl", { days }),
  screen: (params: Record<string, string | number>) =>
    get<ScreenResult>("options/screen", params),
  notify: async (which: "pnl" | "test") => {
    const res = await fetch(`/api/moomoo/notify/${which}`, { method: "POST" });
    return res.json();
  },
};
