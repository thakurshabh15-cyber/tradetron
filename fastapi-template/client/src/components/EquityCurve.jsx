import { useMemo } from "react";

/**
 * TradeThrone Equity Curve — dependency-free responsive SVG area chart.
 * data: [{ ts?, equity }] | number[]   baseline: starting capital
 */
export default function EquityCurve({
  data = [],
  baseline,
  height = 180,
  strokeColor = "#34d399",
  fillColor = "rgba(52,211,153,0.14)",
}) {
  const { path, area, min, max, last } = useMemo(() => {
    const values = (data || [])
      .map((d) => (typeof d === "number" ? d : d?.equity))
      .filter((v) => typeof v === "number" && Number.isFinite(v));
    if (values.length === 0) {
      return { path: "", area: "", min: 0, max: 0, last: null };
    }
    const lo = Math.min(...values, baseline ?? Infinity);
    const hi = Math.max(...values, baseline ?? -Infinity);
    const span = hi - lo || Math.abs(hi) * 0.02 || 1;
    const W = 600;
    const H = height;
    const pad = 6;
    const pts = values.map((v, i) => [
      pad + (i / Math.max(1, values.length - 1)) * (W - 2 * pad),
      H - pad - ((v - lo) / span) * (H - 2 * pad),
    ]);
    const line = pts.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
    const fill = `${line} L${pts[pts.length - 1][0].toFixed(1)},${H - pad} L${pts[0][0].toFixed(1)},${H - pad} Z`;
    return {
      path: line,
      area: fill,
      min: lo,
      max: hi,
      last: values[values.length - 1],
    };
  }, [data, baseline, height]);

  if (!path) {
    return (
      <div
        className="flex items-center justify-center rounded-xl border border-slate-800 bg-surface-900/60 text-xs text-slate-500"
        style={{ height }}
      >
        Run a backtest to plot the equity curve
      </div>
    );
  }

  return (
    <svg viewBox={`0 0 600 ${height}`} className="w-full" preserveAspectRatio="none" role="img" aria-label="Strategy equity curve">
      <defs>
        <linearGradient id="eq-fill" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={fillColor} />
          <stop offset="100%" stopColor="rgba(0,0,0,0)" />
        </linearGradient>
      </defs>
      {baseline != null && (
        <line
          x1="0"
          x2="600"
          y1={height - 6 - ((baseline - min) / ((max - min) || 1)) * (height - 12)}
          y2={height - 6 - ((baseline - min) / ((max - min) || 1)) * (height - 12)}
          stroke="rgba(148,163,184,0.35)"
          strokeDasharray="4 4"
          strokeWidth="1"
        />
      )}
      <path d={area} fill="url(#eq-fill)" stroke="none" />
      <path d={path} fill="none" stroke={strokeColor} strokeWidth="2" strokeLinejoin="round" strokeLinecap="round" />
      <circle cx={598} cy={height - 6 - ((last - min) / ((max - min) || 1)) * (height - 12)} r="3.5" fill={strokeColor} />
      <text x="8" y="14" fontSize="10" fill="#64748b" fontFamily="monospace">₹{Math.round(max).toLocaleString("en-IN")}</text>
      <text x="8" y={height - 8} fontSize="10" fill="#64748b" fontFamily="monospace">₹{Math.round(min).toLocaleString("en-IN")}</text>
    </svg>
  );
}