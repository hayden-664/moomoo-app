import { NextRequest, NextResponse } from "next/server";

const SIDECAR = process.env.SIDECAR_URL ?? "http://127.0.0.1:8788";

/**
 * Allowlisted passthrough to the Python sidecar.
 *
 * The sidecar has no order-placing endpoint, but the allowlist is kept
 * explicit anyway so a future sidecar route can never become reachable from
 * the browser just by existing.
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
]);

const POST_ROUTES = new Set(["notify/pnl", "notify/test"]);

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
    );
  }

  const url = new URL(`${SIDECAR}/${path}`);
  req.nextUrl.searchParams.forEach((v, k) => url.searchParams.append(k, v));

  try {
    const res = await fetch(url, { method, cache: "no-store" });
    const body = await res.text();
    return new NextResponse(body, {
      status: res.status,
      headers: { "content-type": res.headers.get("content-type") ?? "application/json" },
    });
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
    );
  }
}

export async function GET(
  req: NextRequest,
  ctx: { params: Promise<{ path: string[] }> },
) {
  const { path } = await ctx.params;
  return proxy(req, path.join("/"), GET_ROUTES, "GET");
}

export async function POST(
  req: NextRequest,
  ctx: { params: Promise<{ path: string[] }> },
) {
  const { path } = await ctx.params;
  return proxy(req, path.join("/"), POST_ROUTES, "POST");
}
