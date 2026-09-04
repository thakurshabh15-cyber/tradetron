import { useParams, Link } from "react-router-dom";
import { ArrowLeft } from "lucide-react";
import TradingChart from "../components/TradingChart";
import OptionChain from "../components/OptionChain";
import OrderTerminal from "../components/OrderTerminal";
import ErrorBoundary from "../components/ErrorBoundary";
import { useApi } from "../hooks/useApi";
import { useMarket } from "../context/MarketContext";

export default function MarketDetail() {
  const { symbol = "NIFTY50" } = useParams();
  const sym = decodeURIComponent(symbol).toUpperCase();
  const { getQuote } = useMarket();
  const q = getQuote(sym);
  const price = q?.price ?? 0;
  const isIndex = /NIFTY|BANKNIFTY|FINNIFTY|SENSEX/.test(sym);
  const { data: positions } = useApi("/api/trades/positions");
  const mine = (positions || []).filter((p) => p.symbol === sym);

  return (
    <div className="space-y-5">
      <Link to="/markets" className="inline-flex items-center gap-1.5 text-xs text-slate-400 hover:text-white">
        <ArrowLeft size={12} /> Back to Markets
      </Link>

      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="font-display text-2xl font-bold text-white">{sym}</h1>
          <div className="flex items-center gap-2 mt-0.5">
            <p className="text-[11px] text-slate-500 font-mono">Unified tape · {isIndex ? "index derivatives enabled" : "cash segment"}</p>
            {q && <ProvenanceBadge q={q} />}
          </div>
        </div>
        <div className="text-right">
          <p className="font-mono text-3xl font-bold text-white tabular-nums">{price ? price.toLocaleString("en-IN", { minimumFractionDigits: 2 }) : "—"}</p>
          {q && <p className={`text-xs font-mono ${(q.change ?? 0) >= 0 ? "text-profit-400" : "text-loss-400"}`}>
            {(q.change ?? 0) >= 0 ? "+" : ""}{fmt(q.change)} ({(q.change_pct ?? 0).toFixed(2)}%)
          </p>}
        </div>
      </div>

      <ErrorBoundary><TradingChart symbol={sym} currentPrice={price || undefined}
        positions={mine} /></ErrorBoundary>

      <div className="grid xl:grid-cols-5 gap-4">
        <div className="xl:col-span-3 space-y-4">
          {isIndex ? (
            <ErrorBoundary><OptionChain symbol={sym} /></ErrorBoundary>
          ) : (
            <p className="glass-panel rounded-xl p-4 text-xs text-slate-500 border border-edge">
              Option chain available for index underlyings (NIFTY / BANKNIFTY / FINNIFTY / SENSEX).
            </p>
          )}
        </div>
        <div className="xl:col-span-2">
          <ErrorBoundary><OrderTerminal symbol={sym} currentPrice={price || undefined} /></ErrorBoundary>
        </div>
      </div>
    </div>
  );
}

function fmt(v) { return Number(v || 0).toFixed(2); }

function ProvenanceBadge({ q }) {
  const status = q?.data_status;
  const isDemo = q?.feed_mode === "DEMO_SIMULATED" || status === "DEMO";
  const isStale = status === "STALE" ||
    (status !== "DEMO" && q?.is_stale === true && !isDemo);

  let label = "REAL FEED";
  let cls = "bg-emerald-500/15 text-emerald-300 border-emerald-500/30";
  if (isStale && !isDemo) {
    label = "STALE";
    cls = "bg-amber-500/15 text-amber-300 border-amber-500/40";
  } else if (isDemo) {
    label = "SIMULATED";
    cls = "bg-sky-500/15 text-sky-300 border-sky-500/30";
  } else if (status === "UNKNOWN") {
    label = "UNKNOWN";
    cls = "bg-slate-700/40 text-slate-400 border-slate-600";
  }

  return (
    <span
      title={`${q.data_source || "feed"} · ${status || "n/a"}`}
      className={`text-[9px] font-bold uppercase tracking-wider px-2 py-0.5 rounded border ${cls}`}
    >
      {label}
    </span>
  );
}
