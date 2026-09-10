import { NextRequest, NextResponse } from "next/server"

const SIDECAR = process.env.SIDECAR_URL ?? "http://127.0.0.1:8788"

/**
 * Allowlisted passthrough to the Python sidecar.
 *
 * A future sidecar route can never become reachable from the browser just by
 * existing; it has to be named here.
 *
 * The paper engine makes that rule load-bearing rather than precautionary, so
 * it is applied asymmetrically: the browser can stop the engine, resume a stop
 * it can see the reason for, and read everything the engine has done - but it
 * cannot make it trade. `paper/kill` and `paper/resume` are allowlisted;
 * `paper/run-once` and `paper/dry-run` deliberately are not, because those are
 * the two that place or evaluate orders on demand.
 *
 * Resume is safe to expose because the sidecar makes it deliberate rather than
 * one-click: the caller has to echo the current kill reason back, and a halt
 * caused by the simulation guarantee failing cannot be cleared through the API
 * at all. Keep both of those if you touch it.
 */
const GET_ROUTES = new Set([
  "health",
  "accounts",
  "permissions",
  "account",
  "positions",
  "pnl",
  "history",
  "options/screen",
  "quota",
  "candles",
  "markers",
  // Paper engine, all read-only.
  "paper/status",
  "paper/positions",
  "paper/orders",
  "paper/journal",
  "paper/rules",
])

const POST_ROUTES = new Set([
  "notify/pnl",
  "notify/test",
  "notify/review",
  // The two paper writes the browser may make. Neither can start a trade:
  // one halts the engine, the other lifts a halt whose reason the caller has
  // had to read and echo back. Reasons travel as query parameters because this
  // proxy forwards no request body - the same limitation that keeps
  // research/screen off the list.
  "paper/kill",
  "paper/resume",
])

async function proxy(
  req: NextRequest,
  path: string,
  allowed: Set<string>,
  method: "GET" | "POST",
) {
  if (!allowed.has(path)) {
    return NextResponse.json(
      { error: `route not permitted: ${method} ${path}` },
      { status: 404 },
    )
  }

  const url = new URL(`${SIDECAR}/${path}`)
  req.nextUrl.searchParams.forEach((v, k) => url.searchParams.append(k, v))

  try {
    const res = await fetch(url, { method, cache: "no-store" })
    const body = await res.text()
    return new NextResponse(body, {
      status: res.status,
      headers: {
        "content-type": res.headers.get("content-type") ?? "application/json",
      },
    })
  } catch (err) {
    // Sidecar down is the common case (OpenD not running yet); make it legible
    // rather than surfacing an opaque fetch failure in the UI.
    return NextResponse.json(
      {
        error: "sidecar unreachable",
        detail: err instanceof Error ? err.message : String(err),
        hint: `Start it with: cd sidecar && .venv/bin/python app.py`,
      },
      { status: 503 },
    )
  }
}

export async function GET(
  req: NextRequest,
  ctx: { params: Promise<{ path: string[] }> },
) {
  const { path } = await ctx.params
  return proxy(req, path.join("/"), GET_ROUTES, "GET")
}

export async function POST(
  req: NextRequest,
  ctx: { params: Promise<{ path: string[] }> },
) {
  const { path } = await ctx.params
  return proxy(req, path.join("/"), POST_ROUTES, "POST")
}
