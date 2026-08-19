/**
 * Indicator maths over a bar series.
 *
 * Pure functions on plain arrays: no charting types in here, so they stay
 * testable and the chart stays a rendering concern. These describe price
 * history; they do not judge it.
 */

export type Point = { time: string; value: number };

/** Simple moving average. Leading bars with no full window are omitted. */
export function sma(bars: { time: string; close: number }[], period: number): Point[] {
  if (period < 1 || bars.length < period) return [];
  const out: Point[] = [];
  let sum = 0;
  for (let i = 0; i < bars.length; i++) {
    sum += bars[i].close;
    if (i >= period) sum -= bars[i - period].close;
    if (i >= period - 1) out.push({ time: bars[i].time, value: sum / period });
  }
  return out;
}

/**
 * Wilder's RSI. The first value is a simple average of the opening `period`
 * changes; subsequent values smooth that forward, which is what every charting
 * package means by "RSI(14)".
 */
export function rsi(bars: { time: string; close: number }[], period = 14): Point[] {
  if (bars.length <= period) return [];
  const out: Point[] = [];
  let gain = 0;
  let loss = 0;

  for (let i = 1; i <= period; i++) {
    const change = bars[i].close - bars[i - 1].close;
    gain += Math.max(change, 0);
    loss += Math.max(-change, 0);
  }
  let avgGain = gain / period;
  let avgLoss = loss / period;
  out.push({ time: bars[period].time, value: toRsi(avgGain, avgLoss) });

  for (let i = period + 1; i < bars.length; i++) {
    const change = bars[i].close - bars[i - 1].close;
    avgGain = (avgGain * (period - 1) + Math.max(change, 0)) / period;
    avgLoss = (avgLoss * (period - 1) + Math.max(-change, 0)) / period;
    out.push({ time: bars[i].time, value: toRsi(avgGain, avgLoss) });
  }
  return out;
}

function toRsi(avgGain: number, avgLoss: number): number {
  // An unbroken run of up days leaves avgLoss at 0; RSI is 100 by definition
  // there rather than a division by zero.
  if (avgLoss === 0) return 100;
  return 100 - 100 / (1 + avgGain / avgLoss);
}
