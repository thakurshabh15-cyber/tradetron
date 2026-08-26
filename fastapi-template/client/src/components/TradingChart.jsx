import React, { useEffect, useRef, useState, useCallback } from "react";
import {
  createChart,
  CandlestickSeries,
  HistogramSeries,
  LineSeries,
  ColorType,
  CrosshairMode,
  LineStyle,
} from "lightweight-charts";
import { BarChart2 } from "lucide-react";
import { useMarket } from "../context/MarketContext";
import { API_BASE } from "../config";

const TIMEFRAMES = ["1s", "1m", "5m", "15m", "1h", "1D"];
const TF_SECONDS = { "1s": 1, "1m": 60, "5m": 300, "15m": 900, "1h": 3600, "1D": 86400 };

const DEFAULT_TOGGLES = {
  ema20: true, ema50: true, ema200: false, sma20: false,
  vwap: false, bb: false, rsi: true, macd: false, volume: true,
};

// ── Indicator math (pure) ──────────────────────────────────────────
function calcSMA(candles, period) {
  const out = [];
  let sum = 0;
  for (let i = 0; i < candles.length; i++) {
    sum += candles[i].close;
    if (i >= period) sum -= candles[i - period].close;
    if (i >= period - 1) out.push({ time: candles[i].time, value: +(sum / period).toFixed(4) });
  }
  return out;
}

function calcEMA(candles, period) {
  const k = 2 / (period + 1);
  const out = [];
  let prev = null;
  for (let i = 0; i < candles.length; i++) {
    const c = candles[i].close;
    prev = prev === null ? c : c * k + prev * (1 - k);
    if (i >= period - 1) out.push({ time: candles[i].time, value: +prev.toFixed(4) });
  }
  return out;
}

function calcVWAP(candles) {
  let cumPV = 0, cumV = 0;
  const out = [];
  for (const c of candles) {
    const typical = (c.high + c.low + c.close) / 3;
    const v = c.volume || 1;
    cumPV += typical * v;
    cumV += v;
    out.push({ time: c.time, value: +(cumPV / cumV).toFixed(4) });
  }
  return out;
}

function calcBollinger(candles, period = 20, mult = 2) {
  const up = [], mid = [], low = [];
  for (let i = period - 1; i < candles.length; i++) {
    const slice = candles.slice(i - period + 1, i + 1);
    const mean = slice.reduce((a, c) => a + c.close, 0) / period;
    const variance = slice.reduce((a, c) => a + (c.close - mean) ** 2, 0) / period;
    const sd = Math.sqrt(variance);
    const t = candles[i].time;
    up.push({ time: t, value: +(mean + mult * sd).toFixed(4) });
    mid.push({ time: t, value: +mean.toFixed(4) });
    low.push({ time: t, value: +(mean - mult * sd).toFixed(4) });
  }
  return { upper: up, mid, lower: low };
}

function calcRSI(candles, period = 14) {
  const out = [];
  let gain = 0, loss = 0;
  for (let i = 1; i < candles.length; i++) {
    const diff = candles[i].close - candles[i - 1].close;
    const g = Math.max(diff, 0), l = Math.max(-diff, 0);
    if (i <= period) {
      gain += g; loss += l;
      if (i === period) {
        gain /= period; loss /= period;
        out.push({ time: candles[i].time, value: +(100 - 100 / (1 + gain / (loss || 1e-9))).toFixed(2) });
      }
    } else {
      gain = (gain * (period - 1) + g) / period;
      loss = (loss * (period - 1) + l) / period;
      out.push({ time: candles[i].time, value: +(100 - 100 / (1 + gain / (loss || 1e-9))).toFixed(2) });
    }
  }
  return out;
}

function calcMACD(candles, fast = 12, slow = 26, signalP = 9) {
  const closes = candles.map((c) => c.close);
  const ema = (arr, p) => {
    const k = 2 / (p + 1); let prev = null; const o = [];
    arr.forEach((v, i) => { prev = prev === null ? v : v * k + prev * (1 - k); if (i >= p - 1) o.push(prev); });
    return o;
  };
  const ef = ema(closes, fast), es = ema(closes, slow);
  const offset = closes.length - es.length;
  const macdLine = es.map((v, i) => ef[i + offset] - v);
  const sig = (() => { const k = 2 / (signalP + 1); let prev = null; return macdLine.map((v, i) => { prev = prev === null ? v : v * k + prev * (1 - k); return i >= signalP - 1 ? prev : null; }).filter((v) => v !== null); })();
  const sigOffset = macdLine.length - sig.length;
  const hist = [], macdOut = [], sigOut = [];
  for (let i = 0; i < macdLine.length; i++) {
    const t = candles[i + offset].time;
    macdOut.push({ time: t, value: +macdLine[i].toFixed(4) });
    if (i >= sigOffset) {
      sigOut.push({ time: t, value: +sig[i - sigOffset].toFixed(4) });
      hist.push({ time: t, value: +(macdLine[i] - sig[i - sigOffset]).toFixed(4), color: macdLine[i] >= sig[i - sigOffset] ? "rgba(16,185,129,0.6)" : "rgba(244,63,94,0.6)" });
    }
  }
  return { macdOut, sigOut, hist };
}

function TradingChart({ symbol = "NIFTY50", currentPrice = 24850.0, positions = [], onModifyRisk }) {
  const containerRef = useRef(null);
  const chartRef = useRef(null);
  const candleRef = useRef(null);
  const overlayRefs = useRef({});
  const priceLineRefs = useRef([]);
  const dragRef = useRef(null);
  const candlesRef = useRef([]);
  const [timeframe, setTimeframe] = useState("5m");
  const [toggles, setToggles] = useState(DEFAULT_TOGGLES);
  const [candles, setCandles] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [legend, setLegend] = useState(null);
  const [dragBadge, setDragBadge] = useState(null);

  const { getQuote } = useMarket();
  const liveQuote = getQuote(symbol);
  const livePrice = liveQuote?.price ?? currentPrice;
  const livePriceRef = useRef(livePrice);
  useEffect(() => { livePriceRef.current = livePrice; }, [livePrice]);

  const fetchCandles = useCallback(async () => {
    setLoading(true); setError(null);
    try {
      if (timeframe === "1s") {
        const res = await fetch(`${API_BASE}/api/market/candles?symbol=${encodeURIComponent(symbol)}&timeframe=1m&limit=3`);
        const data = res.ok ? await res.json() : { candles: [] };
        const last = (data.candles || []).slice(-1)[0];
        const seedTime = Math.floor(Date.now() / 1000);
        const seed = last
          ? { time: seedTime, open: last.close, high: last.close, low: last.close, close: last.close, volume: last.volume || 100 }
          : { time: seedTime, open: livePriceRef.current, high: livePriceRef.current, low: livePriceRef.current, close: livePriceRef.current, volume: 100 };
        candlesRef.current = [seed];
        setCandles([seed]);
      } else {
        const res = await fetch(`${API_BASE}/api/market/candles?symbol=${encodeURIComponent(symbol)}&timeframe=${timeframe}&limit=180`);
        if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `HTTP ${res.status}`);
        const data = await res.json();
        const raw = (data.candles || []).sort((a, b) => a.time - b.time)
          .map((c) => ({ time: c.time, open: c.open, high: c.high, low: c.low, close: c.close, volume: c.volume || 1000 }));
        if (raw.length === 0) throw new Error("No candle history returned by feed");
        candlesRef.current = raw;
        setCandles(raw);
      }
    } catch (err) {
      setError(err.message || "Candle feed unavailable");
      candlesRef.current = [];
      setCandles([]);
    } finally {
      setLoading(false);
    }
  }, [symbol, timeframe]);

  useEffect(() => { fetchCandles(); }, [fetchCandles]);

  // ── Chart build / rebuild on data or toggle change ──────────────
  useEffect(() => {
    const container = containerRef.current;
    if (!container || candles.length === 0) return undefined;

    const chart = createChart(container, {
      layout: {
        background: { type: ColorType.Solid, color: "transparent" },
        textColor: "#94a3b8",
        fontSize: 11,
        panes: { separatorColor: "#1e293b", separatorHoverColor: "#7c3aed40" },
      },
      grid: {
        vertLines: { color: "rgba(148,163,184,0.06)" },
        horzLines: { color: "rgba(148,163,184,0.06)" },
      },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: "rgba(148,163,184,0.15)", scaleMargins: { top: 0.08, bottom: 0.26 } },
      timeScale: { borderColor: "rgba(148,163,184,0.15)", timeVisible: true, secondsVisible: timeframe === "1s" },
      autoSize: true,
    });
    chartRef.current = chart;

    const candleSeries = chart.addSeries(CandlestickSeries, {
      upColor: "#10b981", downColor: "#f43f5e", borderVisible: false,
      wickUpColor: "#10b981", wickDownColor: "#f43f5e",
    });
    candleSeries.setData(candles);
    candleRef.current = candleSeries;

    if (toggles.volume) {
      const vol = chart.addSeries(HistogramSeries, { priceFormat: { type: "volume" }, priceScaleId: "vol" });
      vol.priceScale().applyOptions({ scaleMargins: { top: 0.78, bottom: 0 } });
      vol.setData(candles.map((c) => ({
        time: c.time, value: c.volume,
        color: c.close >= c.open ? "rgba(16,185,129,0.35)" : "rgba(244,63,94,0.35)",
      })));
      overlayRefs.current.volume = vol;
    }

    const addLine = (data, opts, pane = 0) => {
      const s = chart.addSeries(LineSeries, { priceLineVisible: false, lastValueVisible: false, ...opts }, pane);
      s.setData(data);
      return s;
    };

    if (toggles.ema20) overlayRefs.current.ema20 = addLine(calcEMA(candles, 20), { color: "#38bdf8", lineWidth: 1 });
    if (toggles.ema50) overlayRefs.current.ema50 = addLine(calcEMA(candles, 50), { color: "#a78bfa", lineWidth: 1 });
    if (toggles.ema200) overlayRefs.current.ema200 = addLine(calcEMA(candles, 200), { color: "#f472b6", lineWidth: 2 });
    if (toggles.sma20) overlayRefs.current.sma20 = addLine(calcSMA(candles, 20), { color: "#f59e0b", lineWidth: 2 });
    if (toggles.vwap) overlayRefs.current.vwap = addLine(calcVWAP(candles), { color: "#22d3ee", lineWidth: 1, lineStyle: LineStyle.Dashed });
    if (toggles.bb) {
      const bb = calcBollinger(candles, 20, 2);
      overlayRefs.current.bbU = addLine(bb.upper, { color: "rgba(148,163,184,0.7)", lineWidth: 1 });
      overlayRefs.current.bbM = addLine(bb.mid, { color: "rgba(148,163,184,0.45)", lineWidth: 1, lineStyle: LineStyle.Dotted });
      overlayRefs.current.bbL = addLine(bb.lower, { color: "rgba(148,163,184,0.7)", lineWidth: 1 });
    }

    if (toggles.rsi) {
      const rsiData = calcRSI(candles, 14);
      const rsi = chart.addSeries(LineSeries, { color: "#e879f9", lineWidth: 1, priceLineVisible: false }, 1);
      rsi.setData(rsiData);
      rsi.createPriceLine({ price: 70, color: "rgba(244,63,94,0.5)", lineStyle: LineStyle.Dashed, lineWidth: 1, axisLabelVisible: true, title: "70" });
      rsi.createPriceLine({ price: 30, color: "rgba(16,185,129,0.5)", lineStyle: LineStyle.Dashed, lineWidth: 1, axisLabelVisible: true, title: "30" });
      overlayRefs.current.rsi = rsi;
      if (rsiData.length) rsi.priceScale().applyOptions({ scaleMargins: { top: 0.12, bottom: 0.12 } });
    }

    if (toggles.macd) {
      const { macdOut, sigOut, hist } = calcMACD(candles);
      const h = chart.addSeries(HistogramSeries, { priceLineVisible: false, lastValueVisible: false }, 2);
      h.setData(hist);
      overlayRefs.current.macdHist = h;
      overlayRefs.current.macdLine = addLine(macdOut, { color: "#38bdf8", lineWidth: 1 }, 2);
      overlayRefs.current.macdSig = addLine(sigOut, { color: "#f59e0b", lineWidth: 1 }, 2);
    }

    // ── Position overlays: entry / SL / TP price lines ──
    const symPositions = positions.filter((p) => p.symbol === symbol && p.status !== "CLOSED");
    priceLineRefs.current = [];
    for (const p of symPositions) {
      const base = { lineWidth: 1, axisLabelVisible: true };
      priceLineRefs.current.push({ pos: p, kind: "ENTRY", line: candleSeries.createPriceLine({ ...base, price: p.entry_price, color: "#22d3ee", lineStyle: LineStyle.Dashed, title: `ENTRY ${p.quantity}` }) });
      if (p.stop_loss_price) {
        priceLineRefs.current.push({ pos: p, kind: "SL", line: candleSeries.createPriceLine({ ...base, price: p.stop_loss_price, color: "#f43f5e", lineStyle: LineStyle.Solid, title: "SL (drag)" }) });
      }
      if (p.take_profit_price) {
        priceLineRefs.current.push({ pos: p, kind: "TP", line: candleSeries.createPriceLine({ ...base, price: p.take_profit_price, color: "#10b981", lineStyle: LineStyle.Solid, title: "TP (drag)" }) });
      }
    }

    // ── Drag-to-modify SL/TP handles ──
    const threshold = () => {
      const ps = candleSeries.priceScale();
      const range = ps.getVisibleRange?.() || {};
      const span = (range.to ?? 0) - (range.from ?? 0);
      return Math.max((span || livePriceRef.current * 0.02) * 0.02, 1e-6);
    };

    const onMouseDown = (e) => {
      
      const rect = container.getBoundingClientRect();
      const y = e.clientY - rect.top;
      const price = candleSeries.coordinateToPrice(y);
      if (price == null) return;
      const th = threshold();
      let best = null;
      for (const pl of priceLineRefs.current) {
        if (pl.kind === "ENTRY") continue;
        const target = pl.kind === "SL" ? pl.pos.stop_loss_price : pl.pos.take_profit_price;
        if (target == null) continue;
        if (Math.abs(price - target) <= th && (!best || Math.abs(price - target) < Math.abs(price - best.target))) {
          best = { ...pl, target };
        }
      }
      if (best) {
        dragRef.current = { posId: best.pos.id, field: best.kind === "SL" ? "stop_loss_price" : "take_profit_price", line: best.line, kind: best.kind };
        setDragBadge({ kind: best.kind, price });
        container.style.cursor = "grabbing";
        e.preventDefault();
      }
    };

    const onMouseMove = (e) => {
      const d = dragRef.current;
      const rect = container.getBoundingClientRect();
      const crossY = e.clientY - rect.top;
      chart.setCrosshairPosition?.(candleSeries.coordinateToPrice(crossY), undefined, candleSeries);
      if (!d) return;
      const price = candleSeries.coordinateToPrice(crossY);
      if (price == null) return;
      d.line.applyOptions({ price, title: `${d.kind} → ${price.toFixed(2)}` });
      setDragBadge({ kind: d.kind, price });
    };

    const commitDrag = () => {
      const d = dragRef.current;
      container.style.cursor = "default";
      dragRef.current = null;
      setDragBadge(null);
      if (d && onModifyRisk) {
        const finalPrice = d.line.options().price;
        onModifyRisk(d.posId, d.field, finalPrice);
      }
    };

    container.addEventListener("mousedown", onMouseDown);
    window.addEventListener("mousemove", onMouseMove);
    window.addEventListener("mouseup", commitDrag);

    chart.subscribeCrosshairMove((param) => {
      if (!param.time || !param.seriesData) { setLegend(null); return; }
      const cd = param.seriesData.get(candleSeries);
      if (cd) setLegend(cd);
    });

    chart.timeScale().fitContent();

    const resizeObs = new ResizeObserver((entries) => {
      if (entries.length === 0 || !entries[0].contentRect) return;
      const { width, height } = entries[0].contentRect;
      chart.applyOptions({ width, height });
    });
    resizeObs.observe(container);



    return () => {
      container.removeEventListener("mousedown", onMouseDown);
      window.removeEventListener("mousemove", onMouseMove);
      window.removeEventListener("mouseup", commitDrag);
      resizeObs.disconnect();
      chart.remove();
      chartRef.current = null;
      overlayRefs.current = {};
      priceLineRefs.current = [];
    };
  }, [candles, toggles, timeframe, positions]);

  // ── Real-time tick updates (1s builds candles from ticks) ──
  useEffect(() => {
    if (!livePrice || !candleRef.current || candlesRef.current.length === 0) return;
    const arr = candlesRef.current;
    const last = arr[arr.length - 1];

    if (timeframe === "1s") {
      const bucket = Math.floor(Date.now() / 1000);
      if (bucket > last.time) {
        const fresh = { time: bucket, open: last.close, high: Math.max(last.close, livePrice), low: Math.min(last.close, livePrice), close: livePrice, volume: 10 };
        arr.push(fresh);
        if (arr.length > 600) arr.shift();
        candleRef.current.update(fresh);
      } else {
        last.high = Math.max(last.high, livePrice);
        last.low = Math.min(last.low, livePrice);
        last.close = livePrice;
        last.volume += 5;
        candleRef.current.update({ ...last });
      }
    } else {
      candleRef.current.update({
        time: last.time,
        open: last.open,
        high: Math.max(last.high, livePrice),
        low: Math.min(last.low, livePrice),
        close: livePrice,
      });
    }
  }, [livePrice, timeframe]);

  const lastCandle = candles[candles.length - 1];
  const displayData = legend || lastCandle || {
    open: livePrice * 0.9985, high: livePrice * 1.0025,
    low: livePrice * 0.9965, close: livePrice,
  };
  const isPositive = (displayData?.close ?? 0) >= (displayData?.open ?? 0);
  const isINR = /NIFTY|BANKNIFTY|SENSEX|FINNIFTY|RELIANCE|TCS|INFY|INR|GOLD|CRUDE|SILVER/i.test(symbol);
  const cur = isINR ? "₹" : "$";
  const fmt = (v) => (v == null || isNaN(v)) ? "0.00" : Number(v).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 4 });

  const toggle = (k) => setToggles((t) => ({ ...t, [k]: !t[k] }));
  const pillOn = "px-2 py-1 rounded-lg transition-all bg-brand-purple/20 text-brand-purple border border-brand-purple/30";
  const pillOff = "px-2 py-1 rounded-lg transition-all text-slate-500 hover:text-slate-300";

  return (
    <div className="glass-panel rounded-2xl p-3.5 sm:p-5 border border-slate-800/80 shadow-glass-md space-y-4">
      <div className="flex flex-col lg:flex-row lg:items-center justify-between gap-3 border-b border-slate-800/60 pb-3">
        <div className="flex items-center gap-2.5">
          <div className="p-1.5 sm:p-2 rounded-xl bg-brand-purple/10 text-brand-purple border border-brand-purple/25 shrink-0">
            <BarChart2 size={18} />
          </div>
          <div>
            <div className="flex items-center gap-2 flex-wrap">
              <h3 className="font-display font-bold text-sm sm:text-base text-white">{symbol} · Institutional</h3>
              <span className="badge-purple text-[10px]">LIVE DMA ({timeframe})</span>
            </div>
            <div className="flex items-center gap-2 sm:gap-3 text-[11px] font-mono tabular-nums text-slate-400 mt-0.5 flex-wrap">
              <span>O: <strong className="text-white">{cur}{fmt(displayData.open)}</strong></span>
              <span>H: <strong className="text-emerald-400">{cur}{fmt(displayData.high)}</strong></span>
              <span>L: <strong className="text-rose-400">{cur}{fmt(displayData.low)}</strong></span>
              <span>C: <strong className={isPositive ? "text-emerald-400" : "text-rose-400"}>{cur}{fmt(displayData.close)}</strong></span>
            </div>
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <div className="flex bg-surface-950 p-1 rounded-xl border border-slate-800 text-xs font-mono font-semibold">
            {TIMEFRAMES.map((tf) => (
              <button key={tf} onClick={() => setTimeframe(tf)}
                className={`px-2 py-1 rounded-lg transition-all ${timeframe === tf ? "bg-brand-violet text-white shadow-sm font-bold" : "text-slate-400 hover:text-white"}`}>
                {tf}
              </button>
            ))}
          </div>
          <div className="flex items-center gap-1 bg-surface-950 p-1 rounded-xl border border-slate-800 text-[11px] font-semibold flex-wrap">
            {["ema20","ema50","ema200","sma20","vwap","bb","rsi","macd","volume"].map((k) => (
              <button key={k} onClick={() => toggle(k)} className={toggles[k] ? pillOn : pillOff}>
                {k.toUpperCase()}
              </button>
            ))}
          </div>
        </div>
      </div>

      <div className="relative w-full rounded-xl bg-surface-950/70 border border-slate-800/70 p-1 overflow-hidden">
        {error && (
          <div className="absolute inset-0 z-20 bg-surface-950/90 backdrop-blur-sm flex flex-col items-center justify-center gap-3 text-center p-4">
            <BarChart2 size={22} className="text-rose-400" />
            <p className="text-[11px] text-rose-300 max-w-xs">{error}</p>
            <button onClick={fetchCandles} className="btn-danger text-xs px-3 py-1.5">Retry Feed</button>
          </div>
        )}
        {loading && !error && (
          <div className="absolute inset-0 z-10 bg-surface-950/60 backdrop-blur-[2px] flex items-center justify-center pointer-events-none">
            <div className="flex items-center gap-2 px-3 py-1.5 rounded-lg bg-surface-900 border border-slate-800 text-xs font-mono text-cyan-400">
              <span className="w-2 h-2 rounded-full bg-cyan-400 animate-ping" /> Loading {symbol} ({timeframe})…
            </div>
          </div>
        )}
        <div ref={containerRef} className="w-full h-[340px] sm:h-[420px] select-none touch-none" />

        {dragBadge && (
          <div className={`absolute top-3 right-3 z-30 px-2.5 py-1 rounded-lg text-[11px] font-mono font-bold border pointer-events-none ${dragBadge.kind === "SL" ? "bg-rose-500/20 border-rose-500/40 text-rose-300" : "bg-emerald-500/20 border-emerald-500/40 text-emerald-300"}`}>
            Dragging {dragBadge.kind} → {fmt(dragBadge.price)} · release to apply
          </div>
        )}

        <div className="absolute top-3 left-3 flex items-center gap-2.5 text-[10px] font-mono bg-surface-950/85 px-2.5 py-1 rounded-lg border border-slate-800 pointer-events-none z-0 flex-wrap max-w-[80%]">
          {toggles.ema20 && <span className="text-sky-400">EMA20</span>}
          {toggles.ema50 && <span className="text-violet-400">EMA50</span>}
          {toggles.ema200 && <span className="text-pink-400">EMA200</span>}
          {toggles.sma20 && <span className="text-amber-400">SMA20</span>}
          {toggles.vwap && <span className="text-cyan-300">VWAP</span>}
          {toggles.bb && <span className="text-slate-400">BB(20,2)</span>}
          {toggles.rsi && <span className="text-fuchsia-400">RSI14</span>}
          {toggles.macd && <span className="text-sky-300">MACD</span>}
          {positions.filter((p) => p.symbol === symbol).length > 0 && (
            <span className="text-cyan-300">◆ {positions.filter((p) => p.symbol === symbol).length} pos · drag SL/TP</span>
          )}
        </div>
      </div>
    </div>
  );
}

function areEqual(prev, next) {
  if (prev.symbol !== next.symbol) return false;
  if (prev.currentPrice !== next.currentPrice) return false;
  if ((prev.positions || []).length !== (next.positions || []).length) return false;
  const pp = prev.positions || [], np = next.positions || [];
  for (let i = 0; i < pp.length; i++) {
    if (pp[i].id !== np[i].id || pp[i].stop_loss_price !== np[i].stop_loss_price || pp[i].take_profit_price !== np[i].take_profit_price) return false;
  }
  return true;
}

export default React.memo(TradingChart, areEqual);
