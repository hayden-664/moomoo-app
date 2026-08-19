"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  CandlestickSeries,
  CrosshairMode,
  HistogramSeries,
  LineSeries,
  LineStyle,
  createChart,
  createSeriesMarkers,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type Time,
} from "lightweight-charts";
import { api } from "@/lib/api";
import type { Bar, Fill, Position } from "@/lib/types";
import { rsi } from "@/lib/indicators";
import { fmt } from "./Money";

const RANGES = [
  { label: "3M", days: 90 },
  { label: "6M", days: 180 },
  { label: "1Y", days: 365 },
  { label: "3Y", days: 1095 },
];

/** Chart colours track the app's CSS variables so the two themes stay in step. */
function palette() {
  const css = getComputedStyle(document.documentElement);
  const v = (name: string, fallback: string) =>
    css.getPropertyValue(name).trim() || fallback;
  return {
    bg: v("--background", "#0b0d10"),
    text: v("--muted", "#8b939e"),
    border: v("--border", "#242931"),
    pos: v("--pos", "#2ecc84"),
    neg: v("--neg", "#ff5f56"),
    accent: v("--accent", "#5b9dff"),
  };
}

export default function PositionChart({ position }: { position: Position }) {
  const code = position.code;
  const [days, setDays] = useState(365);
  const [error, setError] = useState<string | null>(null);

  // Bars are stored together with the (code, days) they were fetched for.
  // Comparing against the current key marks them stale without a setState in
  // the effect body, which would trigger a cascading render.
  const key = `${code}:${days}`;
  const [data, setData] = useState<{
    key: string;
    bars: Bar[];
    fills: Fill[];
    source: string;
  } | null>(null);
  const current = data?.key === key ? data : null;
  const bars = current?.bars ?? null;
  const fills = useMemo(() => current?.fills ?? [], [current]);
  const source = current?.source ?? null;

  const wrapRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const rsiRef = useRef<ISeriesApi<"Line"> | null>(null);
  const markerRef = useRef<ISeriesMarkersPluginApi<Time> | null>(null);
  const costLineRef = useRef<IPriceLine | null>(null);

  const load = useCallback(
    async (alive: () => boolean) => {
      try {
        const [c, m] = await Promise.all([
          api.candles(code, days),
          api.markers(code, days).catch(() => [] as Fill[]),
        ]);
        if (!alive()) return;
        setData({ key: `${code}:${days}`, bars: c.bars, fills: m, source: c.source });
        setError(null);
      } catch (e) {
        if (!alive()) return;
        setError(e instanceof Error ? e.message : String(e));
      }
    },
    [code, days],
  );

  useEffect(() => {
    let active = true;
    const t = setTimeout(() => void load(() => active), 0);
    return () => {
      active = false;
      clearTimeout(t);
    };
  }, [load]);

  // Build the chart once. Series data is pushed by the effect below so a
  // refresh never tears down and rebuilds the canvas.
  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const p = palette();

    const chart = createChart(el, {
      width: el.clientWidth,
      height: 400,
      layout: {
        background: { color: p.bg },
        textColor: p.text,
        fontSize: 11,
        panes: { separatorColor: p.border, separatorHoverColor: p.border },
      },
      grid: {
        vertLines: { color: p.border, style: LineStyle.Dotted },
        horzLines: { color: p.border, style: LineStyle.Dotted },
      },
      rightPriceScale: { borderColor: p.border },
      timeScale: { borderColor: p.border, rightOffset: 4 },
      crosshair: { mode: CrosshairMode.Normal },
    });

    const candle = chart.addSeries(CandlestickSeries, {
      upColor: p.pos,
      downColor: p.neg,
      borderUpColor: p.pos,
      borderDownColor: p.neg,
      wickUpColor: p.pos,
      wickDownColor: p.neg,
    });
    const vol = chart.addSeries(HistogramSeries, {
      priceFormat: { type: "volume" },
      priceScaleId: "vol",
    });
    chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.85, bottom: 0 } });

    const rsiSeries = chart.addSeries(
      LineSeries,
      { color: p.accent, lineWidth: 1, priceLineVisible: false, title: "RSI(14)" },
      1,
    );
    chart.panes()[1]?.setHeight(96);

    chartRef.current = chart;
    candleRef.current = candle;
    volRef.current = vol;
    rsiRef.current = rsiSeries;
    markerRef.current = createSeriesMarkers(candle, []);

    const ro = new ResizeObserver(() => chart.applyOptions({ width: el.clientWidth }));
    ro.observe(el);

    return () => {
      ro.disconnect();
      chart.remove();
      chartRef.current = null;
      candleRef.current = null;
      volRef.current = null;
      rsiRef.current = null;
      markerRef.current = null;
      costLineRef.current = null;
    };
  }, []);

  useEffect(() => {
    const candle = candleRef.current;
    const vol = volRef.current;
    const rsiSeries = rsiRef.current;
    if (!candle || !vol || !rsiSeries || !bars?.length) return;
    const p = palette();

    candle.setData(bars.map((b) => ({ ...b, time: b.time as Time })));
    vol.setData(
      bars.map((b) => ({
        time: b.time as Time,
        value: b.volume,
        color: b.close >= b.open ? `${p.pos}44` : `${p.neg}44`,
      })),
    );
    rsiSeries.setData(rsi(bars).map((r) => ({ time: r.time as Time, value: r.value })));

    // Markers only render on timestamps that exist in the series, so fills on
    // non-trading timestamps (or outside the window) are dropped rather than
    // silently shifting to a neighbouring bar.
    const barDays = new Set(bars.map((b) => b.time));
    markerRef.current?.setMarkers(
      fills
        .filter((f) => barDays.has(f.time))
        .map((f) => ({
          time: f.time as Time,
          position: f.side === "BUY" ? ("belowBar" as const) : ("aboveBar" as const),
          color: f.side === "BUY" ? p.pos : p.neg,
          shape: f.side === "BUY" ? ("arrowUp" as const) : ("arrowDown" as const),
          text: `${f.side === "BUY" ? "B" : "S"} ${f.qty} @ ${f.price ?? "?"}`,
        })),
    );

    if (costLineRef.current) {
      candle.removePriceLine(costLineRef.current);
      costLineRef.current = null;
    }
    const avg = position.cost_price;
    if (avg) {
      costLineRef.current = candle.createPriceLine({
        price: avg,
        color: p.text,
        lineWidth: 1,
        lineStyle: LineStyle.Dashed,
        axisLabelVisible: true,
        title: "avg cost",
      });
    }
    chartRef.current?.timeScale().fitContent();
  }, [bars, fills, position.cost_price]);

  return (
    <div className="rounded-lg border border-border">
      <div className="flex flex-wrap items-center gap-3 border-b border-border px-3 py-2">
        <div className="font-medium">{position.stock_name}</div>
        <div className="text-xs text-muted">{code}</div>
        <div className="ml-auto flex items-center gap-1">
          {RANGES.map((r) => (
            <button
              key={r.days}
              onClick={() => setDays(r.days)}
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
      </div>

      {error ? (
        <div className="px-3 py-8 text-center text-sm text-neg">{error}</div>
      ) : !bars ? (
        <div className="px-3 py-8 text-center text-sm text-muted">Loading bars…</div>
      ) : null}

      <div ref={wrapRef} className={error ? "hidden" : ""} />

      <div className="flex flex-wrap items-center gap-4 border-t border-border px-3 py-2 text-xs text-muted">
        <span className="flex items-center gap-1.5">
          <span className="inline-block size-2 rounded-sm bg-pos" /> buy fill
        </span>
        <span className="flex items-center gap-1.5">
          <span className="inline-block size-2 rounded-sm bg-neg" /> sell fill
        </span>
        <span>dashed line · your average cost {fmt(position.cost_price)}</span>
        <span className="ml-auto">
          {fills.length} fill{fills.length === 1 ? "" : "s"} in window
          {source ? ` · bars from ${source}` : ""}
        </span>
      </div>
    </div>
  );
}
