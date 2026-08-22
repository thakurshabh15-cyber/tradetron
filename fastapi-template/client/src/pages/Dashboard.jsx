import { useState } from "react";
import MarketTicker from "../components/MarketTicker";
import TradeLog from "../components/TradeLog";
import RiskGauge from "../components/RiskGauge";
import SetupChecklist from "../components/SetupChecklist";
import TopStrategiesCard from "../components/TopStrategiesCard";
import PendingTasksList from "../components/PendingTasksList";
import TradingChart from "../components/TradingChart";
import FastOrderPanel from "../components/FastOrderPanel";
import { SkeletonCard, SkeletonChart } from "../components/SkeletonLoaders";
import { useApi } from "../hooks/useApi";
import { RefreshCw, TrendingUp, Calendar, Target, Activity, Layers, Monitor, ChevronRight } from "lucide-react";

const ASSET_CLASSES = [
  { id: "ALL", label: "All Markets" },
  { id: "INDIAN", label: "NSE / F&O (India)" },
  { id: "CRYPTO", label: "Crypto" },
  { id: "FOREX", label: "Forex & NSE-CDS" },
  { id: "GLOBAL", label: "US Tech" },
];

const SYMBOL_MAP = {
  ALL: ["NIFTY50", "BANKNIFTY", "RELIANCE", "BTCUSDT", "ETHUSDT", "USDINR", "AAPL", "NVDA"],
  INDIAN: ["NIFTY50", "BANKNIFTY", "FINNIFTY", "RELIANCE", "TCS", "INFY", "HDFCBANK", "CRUDEOIL"],
  CRYPTO: ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "MATICINR"],
  FOREX: ["USDINR", "EURINR", "GBPINR", "EURUSD", "GBPUSD"],
  GLOBAL: ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA"],
};

export default function Dashboard() {
  const [activeAssetTab, setActiveAssetTab] = useState("ALL");
  const [selectedSymbol, setSelectedSymbol] = useState("NIFTY50");

  const { data: marketData, loading: marketLoading, refetch: refetchMarket } = useApi("/api/market-data");
  const { data: riskData, loading: riskLoading, refetch: refetchRisk } = useApi("/api/risk-status");
  const { data: initialTrades, refetch: refetchTrades } = useApi("/api/trades?limit=20");
  const { data: summaryData, loading: summaryLoading, refetch: refetchSummary } = useApi("/api/dashboard/summary");

  const refreshAll = () => {
    refetchMarket();
    refetchRisk();
    refetchSummary();
    refetchTrades();
  };

  const marketMap = (marketData?.market || []).reduce((acc, curr) => {
    acc[curr.symbol] = curr;
    return acc;
  }, {});

  const displayedSymbols = SYMBOL_MAP[activeAssetTab] || SYMBOL_MAP.ALL;
  const currentSelectedData = marketMap[selectedSymbol] || { price: 24850.0 };

  const weekReturn = summaryData?.weekReturn ?? 3.42;
  const monthReturn = summaryData?.monthReturn ?? 11.85;

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-display font-bold tracking-tight text-white">
            Institutional Trading Terminal
          </h1>
          <p className="text-xs text-slate-400">
            Ultra-low latency execution stream, real-time DMA charts, and multi-asset risk management
          </p>
        </div>
        <button
          onClick={refreshAll}
          className="btn-ghost text-xs self-start sm:self-auto flex items-center gap-1.5 min-h-[40px]"
        >
          <RefreshCw size={14} className={marketLoading ? "animate-spin" : ""} /> Refresh Terminal
        </button>
      </div>

      {/* Summary KPI Cards */}
      {summaryLoading ? (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <SkeletonCard />
          <SkeletonCard />
          <SkeletonCard />
          <SkeletonCard />
        </div>
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <div className="glass-card-hover p-4">
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold text-slate-400">Week Return</span>
              <div className="p-1.5 rounded-lg bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">
                <TrendingUp size={14} />
              </div>
            </div>
            <div className="mt-2 flex items-baseline gap-2">
              <span className="text-xl font-bold font-mono tabular-nums text-emerald-400">
                +{weekReturn}%
              </span>
              <span className="text-[10px] text-slate-500">7-day rolling</span>
            </div>
          </div>

          <div className="glass-card-hover p-4">
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold text-slate-400">Month Return</span>
              <div className="p-1.5 rounded-lg bg-cyan-500/10 text-cyan-400 border border-cyan-500/20">
                <Calendar size={14} />
              </div>
            </div>
            <div className="mt-2 flex items-baseline gap-2">
              <span className="text-xl font-bold font-mono tabular-nums text-cyan-400">
                +{monthReturn}%
              </span>
              <span className="text-[10px] text-slate-500">30-day alpha</span>
            </div>
          </div>

          <div className="glass-card-hover p-4">
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold text-slate-400">Engine Win Rate</span>
              <div className="p-1.5 rounded-lg bg-brand-purple/10 text-brand-purple border border-brand-purple/20">
                <Target size={14} />
              </div>
            </div>
            <div className="mt-2 flex items-baseline gap-2">
              <span className="text-xl font-bold font-mono tabular-nums text-white">
                {summaryData?.winRate ?? 76.4}%
              </span>
              <span className="text-[10px] text-slate-500">across {summaryData?.totalTrades ?? 0} fills</span>
            </div>
          </div>

          <div className="glass-card-hover p-4">
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold text-slate-400">Engine Status</span>
              <div className="p-1.5 rounded-lg bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">
                <Activity size={14} />
              </div>
            </div>
            <div className="mt-2 flex items-baseline gap-2">
              <span className="text-xl font-bold font-mono text-emerald-400">
                {summaryData?.engineStatus ?? "OPERATIONAL"}
              </span>
              <span className="text-[10px] text-slate-500">DMA 0-latency</span>
            </div>
          </div>
        </div>
      )}

      {/* ── 3-COLUMN MULTI-PANEL DESKTOP TRADING TERMINAL ────────── */}
      <div className="grid gap-6 lg:grid-cols-12 items-start">
        {/* Left Column (3 Cols): Multi-Asset Stream / Watchlist Selector */}
        <div className="lg:col-span-3 space-y-3">
          <div className="flex items-center justify-between border-b border-slate-800 pb-2">
            <div className="flex items-center gap-2">
              <Layers size={15} className="text-brand-purple" />
              <h2 className="text-xs font-bold font-display text-white uppercase tracking-wider">Asset Feeds</h2>
            </div>
            <span className="text-[10px] font-mono text-slate-500">{displayedSymbols.length} pairs</span>
          </div>

          {/* Asset Category Switcher Pills */}
          <div className="flex flex-wrap gap-1 bg-surface-950 p-1 rounded-xl border border-slate-800">
            {ASSET_CLASSES.map((tab) => (
              <button
                key={tab.id}
                onClick={() => setActiveAssetTab(tab.id)}
                className={`px-2 py-1 text-[11px] font-semibold rounded-lg transition-all ${
                  activeAssetTab === tab.id
                    ? "bg-brand-purple/20 text-brand-purple border border-brand-purple/30 shadow-sm"
                    : "text-slate-400 hover:text-white"
                }`}
              >
                {tab.label}
              </button>
            ))}
          </div>

          {/* Clickable Quick Ticker List */}
          <div className="space-y-2 max-h-[440px] overflow-y-auto pr-1">
            {displayedSymbols.map((sym) => {
              const item = marketMap[sym] || { price: 0, change: 0, change_pct: 0 };
              const isSelected = selectedSymbol === sym;
              const isPositive = (item.change ?? 0) >= 0;
              const isINR = sym.includes("NIFTY") || sym.includes("RELIANCE") || sym.includes("INR") || sym.includes("TCS");
              const currSymbol = isINR ? "₹" : "$";

              return (
                <button
                  key={sym}
                  type="button"
                  onClick={() => setSelectedSymbol(sym)}
                  className={`w-full p-3 rounded-xl text-left border transition-all flex items-center justify-between ${
                    isSelected
                      ? "bg-brand-purple/15 border-brand-purple/50 shadow-md shadow-brand-purple/10"
                      : "glass-card-hover bg-surface-900/80 border-slate-800/80"
                  }`}
                >
                  <div>
                    <div className="flex items-center gap-1.5">
                      <span className="font-display font-bold text-xs text-white">{sym}</span>
                      {isSelected && <span className="w-1.5 h-1.5 rounded-full bg-brand-purple animate-pulse" />}
                    </div>
                    <div className="font-mono text-xs font-bold tabular-nums text-slate-200 mt-0.5">
                      {currSymbol}{Number(item.price || 0).toLocaleString(undefined, { minimumFractionDigits: 2 })}
                    </div>
                  </div>
                  <div className="text-right font-mono text-[11px] tabular-nums">
                    <span className={`font-semibold ${isPositive ? "text-emerald-400" : "text-rose-400"}`}>
                      {isPositive ? "+" : ""}{Number(item.change_pct || 0).toFixed(2)}%
                    </span>
                    <ChevronRight size={14} className="text-slate-600 ml-auto mt-1" />
                  </div>
                </button>
              );
            })}
          </div>
        </div>

        {/* Center Column (6 Cols): Interactive Candlestick DMA Chart */}
        <div className="lg:col-span-6 space-y-4">
          <TradingChart symbol={selectedSymbol} currentPrice={currentSelectedData.price} />
        </div>

        {/* Right Column (3 Cols): Fast Order Execution & Margin Gate */}
        <div className="lg:col-span-3 space-y-4">
          <FastOrderPanel
            symbol={selectedSymbol}
            currentPrice={currentSelectedData.price}
            onOrderPlaced={() => {
              refetchTrades();
              refetchRisk();
            }}
          />
        </div>
      </div>

      {/* User Onboarding / Setup Status Checklist */}
      <SetupChecklist />

      {/* Top Strategies & Pending Actionable Tasks */}
      <div className="grid gap-6 lg:grid-cols-2">
        <TopStrategiesCard strategies={summaryData?.topStrategies || []} />
        <PendingTasksList
          initialTasks={summaryData?.allTasks || []}
          onTaskToggled={refetchSummary}
        />
      </div>

      {/* Live Stream & Risk Metrics */}
      <div className="grid gap-6 lg:grid-cols-3">
        <div className="lg:col-span-2">
          <TradeLog initialTrades={initialTrades || []} />
        </div>
        <div>
          <RiskGauge riskData={riskData} />
        </div>
      </div>
    </div>
  );
}
