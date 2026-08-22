import { useEffect, useRef, useState, useMemo } from "react";
import {
  createChart,
  CandlestickSeries,
  HistogramSeries,
  LineSeries,
  ColorType,
  CrosshairMode,
  LineStyle,
} from "lightweight-charts";
import { BarChart2, TrendingUp, TrendingDown, Layers, Sliders, Activity, Clock } from "lucide-react";
import { useWebSocket } from "../hooks/useWebSocket";
import { API_BASE } from "../config";

const TIMEFRAMES = ["1m", "5m", "15m", "1h", "1D"];

export default function TradingChart({ symbol = "NIFTY50", currentPrice = 24850.0 }) {
  const chartContainerRef = useRef(null);
  const chartInstanceRef = useRef(null);
  const candleSeriesRef = useRef(null);
  const volumeSeriesRef = useRef(null);
  const smaSeriesRef = useRef(null);

  const [timeframe, setTimeframe] = useState("5m");
  const [showSMA, setShowSMA] = useState(true);
  const [showVolume, setShowVolume] = useState(true);
  const [activeLegend, setActiveLegend] = useState(null);
  const [chartData, setChartData] = useState({ candles: [], volumes: [], smaData: [] });
  const [loading, setLoading] = useState(true);

  const { lastMessage } = useWebSocket(`/ws/market/${symbol}`);
  const livePrice = lastMessage?.price ?? currentPrice;

  // Fetch authentic historical candles from backend API
  useEffect(() => {
    let isMounted = true;
    setLoading(true);

    fetch(`${API_BASE}/api/market/candles?symbol=${encodeURIComponent(symbol)}&timeframe=${encodeURIComponent(timeframe)}&limit=120`)
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        if (!isMounted) return;
        const rawCandles = data?.candles || [];
        if (rawCandles.length > 0) {
          const candles = [];
          const volumes = [];
          const smaData = [];

          // Sort strictly by timestamp ascending
          rawCandles.sort((a, b) => a.time - b.time);

          for (let i = 0; i < rawCandles.length; i++) {
            const c = rawCandles[i];
            const isGreen = c.close >= c.open;
            candles.push({
              time: c.time,
              open: c.open,
              high: c.high,
              low: c.low,
              close: c.close,
            });
            volumes.push({
              time: c.time,
              value: c.volume || 1000,
              color: isGreen ? "rgba(16, 185, 129, 0.35)" : "rgba(244, 63, 94, 0.35)",
            });
          }

          // Calculate 20 SMA on real closes
          for (let i = 0; i < candles.length; i++) {
            const start = Math.max(0, i - 19);
            const subset = candles.slice(start, i + 1);
            const avg = subset.reduce((acc, curr) => acc + curr.close, 0) / subset.length;
            smaData.push({ time: candles[i].time, value: avg });
          }

          setChartData({ candles, volumes, smaData });
        } else {
          // Fallback baseline candle at current price if market is initialising
          const now = Math.floor(Date.now() / 1000);
          setChartData({
            candles: [{ time: now, open: livePrice, high: livePrice, low: livePrice, close: livePrice }],
            volumes: [{ time: now, value: 100, color: "rgba(16, 185, 129, 0.35)" }],
            smaData: [{ time: now, value: livePrice }],
          });
        }
        setLoading(false);
      })
      .catch((err) => {
        console.error("Error fetching historical candles:", err);
        if (isMounted) setLoading(false);
      });

    return () => {
      isMounted = false;
    };
  }, [symbol, timeframe]);

  // Initialize TradingView Lightweight Chart
  useEffect(() => {
    if (!chartContainerRef.current) return;

    if (chartInstanceRef.current) {
      chartInstanceRef.current.remove();
      chartInstanceRef.current = null;
    }

    const container = chartContainerRef.current;
    const chart = createChart(container, {
      layout: {
        background: { type: ColorType.Solid, color: "transparent" },
        textColor: "#94a3b8",
        fontFamily: '"JetBrains Mono", monospace',
        fontSize: 11,
      },
      grid: {
        vertLines: { color: "rgba(255, 255, 255, 0.03)" },
        horzLines: { color: "rgba(255, 255, 255, 0.03)" },
      },
      crosshair: {
        mode: CrosshairMode.Normal,
        vertLine: {
          color: "rgba(139, 92, 246, 0.6)",
          width: 1,
          style: LineStyle.Dashed,
          labelBackgroundColor: "#7c3aed",
        },
        horzLine: {
          color: "rgba(139, 92, 246, 0.6)",
          width: 1,
          style: LineStyle.Dashed,
          labelBackgroundColor: "#7c3aed",
        },
      },
      rightPriceScale: {
        borderColor: "rgba(255, 255, 255, 0.08)",
        scaleMargins: {
          top: 0.1,
          bottom: 0.22,
        },
      },
      timeScale: {
        borderColor: "rgba(255, 255, 255, 0.08)",
        timeVisible: true,
        secondsVisible: false,
      },
      handleScroll: { mouseWheel: true, pressedMouseMove: true },
      handleScale: { axisPressedMouseMove: true, mouseWheel: true, pinch: true },
    });

    // 1. Candlestick Series (v5 API)
    const candleSeries = chart.addSeries(CandlestickSeries, {
      upColor: "#10b981",
      downColor: "#f43f5e",
      borderVisible: false,
      wickUpColor: "#10b981",
      wickDownColor: "#f43f5e",
    });

    if (chartData.candles.length > 0) {
      candleSeries.setData(chartData.candles);
    }
    candleSeriesRef.current = candleSeries;

    // 2. Volume Histogram Series (v5 API)
    if (showVolume) {
      const volumeSeries = chart.addSeries(HistogramSeries, {
        color: "#8b5cf6",
        priceFormat: { type: "volume" },
        priceScaleId: "",
      });
      volumeSeries.priceScale().applyOptions({
        scaleMargins: {
          top: 0.82,
          bottom: 0,
        },
      });
      if (chartData.volumes.length > 0) {
        volumeSeries.setData(chartData.volumes);
      }
      volumeSeriesRef.current = volumeSeries;
    }

    // 3. SMA 20 Overlay Line Series (v5 API)
    if (showSMA) {
      const smaSeries = chart.addSeries(LineSeries, {
        color: "#f59e0b",
        lineWidth: 2,
        priceLineVisible: false,
        crosshairMarkerVisible: true,
      });
      if (chartData.smaData.length > 0) {
        smaSeries.setData(chartData.smaData);
      }
      smaSeriesRef.current = smaSeries;
    }

    // Subscribe to Crosshair moves for interactive legend
    chart.subscribeCrosshairMove((param) => {
      if (!param.time || !param.seriesData || !candleSeries) {
        setActiveLegend(null);
        return;
      }
      const data = param.seriesData.get(candleSeries);
      if (data) {
        setActiveLegend({
          open: data.open,
          high: data.high,
          low: data.low,
          close: data.close,
        });
      }
    });

    chartInstanceRef.current = chart;

    // Handle container resize
    const resizeObserver = new ResizeObserver((entries) => {
      if (entries.length === 0 || !entries[0].contentRect) return;
      const { width, height } = entries[0].contentRect;
      chart.applyOptions({ width, height });
    });
    resizeObserver.observe(container);

    // Initial fit
    chart.timeScale().fitContent();

    return () => {
      resizeObserver.disconnect();
      chart.remove();
      chartInstanceRef.current = null;
    };
  }, [chartData, showSMA, showVolume]);

  // Real-time tick update on WebSocket price changes
  useEffect(() => {
    if (!candleSeriesRef.current || !livePrice || chartData.candles.length === 0) return;
    const lastCandle = chartData.candles[chartData.candles.length - 1];

    if (lastCandle) {
      candleSeriesRef.current.update({
        time: lastCandle.time,
        open: lastCandle.open,
        high: Math.max(lastCandle.high, livePrice),
        low: Math.min(lastCandle.low, livePrice),
        close: livePrice,
      });
    }
  }, [livePrice, chartData]);

  const displayData = activeLegend || chartData.candles[chartData.candles.length - 1] || {
    open: livePrice,
    high: livePrice,
    low: livePrice,
    close: livePrice,
  };

  const isPositive = displayData.close >= displayData.open;
  const isINR = symbol.includes("NIFTY") || symbol.includes("RELIANCE") || symbol.includes("INR") || symbol.includes("TCS");
  const currencySymbol = isINR ? "₹" : "$";

  return (
    <div className="glass-panel rounded-2xl p-5 border border-slate-800/80 shadow-glass-md flex flex-col justify-between space-y-4">
      {/* Header & Controls */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 border-b border-slate-800/60 pb-3">
        <div className="flex items-center gap-3">
          <div className="p-2 rounded-xl bg-brand-purple/10 text-brand-purple border border-brand-purple/25">
            <BarChart2 size={18} />
          </div>
          <div>
            <div className="flex items-center gap-2">
              <h3 className="font-display font-bold text-base text-white">{symbol} TradingView Chart</h3>
              <span className="badge-purple">LIVE DMA</span>
            </div>
            <div className="flex items-center gap-3 text-xs font-mono tabular-nums text-slate-400 mt-0.5 flex-wrap">
              <span>O: <strong className="text-white">{currencySymbol}{displayData.open.toFixed(2)}</strong></span>
              <span>H: <strong className="text-emerald-400">{currencySymbol}{displayData.high.toFixed(2)}</strong></span>
              <span>L: <strong className="text-rose-400">{currencySymbol}{displayData.low.toFixed(2)}</strong></span>
              <span>C: <strong className={isPositive ? "text-emerald-400" : "text-rose-400"}>{currencySymbol}{displayData.close.toFixed(2)}</strong></span>
            </div>
          </div>
        </div>

        {/* Timeframe & Indicators Pill Bar */}
        <div className="flex items-center gap-2 self-start sm:self-auto flex-wrap">
          {/* Timeframe Selector */}
          <div className="flex bg-surface-950 p-1 rounded-xl border border-slate-800 text-xs font-mono font-semibold">
            {TIMEFRAMES.map((tf) => (
              <button
                key={tf}
                onClick={() => setTimeframe(tf)}
                className={`px-2.5 py-1 rounded-lg transition-all ${
                  timeframe === tf
                    ? "bg-brand-violet text-white shadow-sm font-bold"
                    : "text-slate-400 hover:text-white"
                }`}
              >
                {tf}
              </button>
            ))}
          </div>

          {/* Indicator Toggles */}
          <div className="flex items-center gap-1 bg-surface-950 p-1 rounded-xl border border-slate-800 text-[11px] font-semibold">
            <button
              onClick={() => setShowSMA(!showSMA)}
              className={`px-2 py-1 rounded-lg transition-all ${
                showSMA ? "bg-amber-500/20 text-amber-300 border border-amber-500/30" : "text-slate-500 hover:text-slate-300"
              }`}
            >
              SMA(20)
            </button>
            <button
              onClick={() => setShowVolume(!showVolume)}
              className={`px-2 py-1 rounded-lg transition-all ${
                showVolume ? "bg-brand-purple/20 text-brand-purple border border-brand-purple/30" : "text-slate-500 hover:text-slate-300"
              }`}
            >
              Volume
            </button>
          </div>
        </div>
      </div>

      {/* TradingView Lightweight Chart Canvas Container */}
      <div className="relative w-full rounded-xl bg-surface-950/70 border border-slate-850 p-2 overflow-hidden">
        <div ref={chartContainerRef} className="w-full h-64 sm:h-72" />

        {/* Legend Indicator Overlay */}
        <div className="absolute top-3 left-4 flex items-center gap-3 text-[10px] font-mono text-slate-400 bg-surface-950/80 px-2.5 py-1 rounded-lg border border-slate-800 pointer-events-none">
          <span className="flex items-center gap-1">
            <span className="w-2 h-2 rounded-full bg-emerald-500 inline-block" /> Bull
          </span>
          <span className="flex items-center gap-1">
            <span className="w-2 h-2 rounded-full bg-rose-500 inline-block" /> Bear
          </span>
          {showSMA && (
            <span className="flex items-center gap-1 text-amber-400">
              <span className="w-2 h-0.5 bg-amber-400 inline-block" /> SMA 20
            </span>
          )}
          {showVolume && (
            <span className="flex items-center gap-1 text-brand-purple">
              <span className="w-2 h-2 rounded bg-brand-purple/40 inline-block" /> Vol
            </span>
          )}
        </div>
      </div>
    </div>
  );
}
