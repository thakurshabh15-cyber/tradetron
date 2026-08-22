import { useWebSocket } from "../hooks/useWebSocket";
import { TrendingUp, TrendingDown, Shield } from "lucide-react";

export default function MarketTicker({ symbol, initialData, assetClass }) {
  const { lastMessage } = useWebSocket(`/ws/market/${symbol}`);
  const data = lastMessage || initialData || { price: 0, change: 0, change_pct: 0 };

  const isPositive = (data.change ?? 0) >= 0;
  const isINR = symbol.includes("NIFTY") || symbol.includes("RELIANCE") || symbol.includes("TCS") || symbol.includes("INFY") || symbol.includes("HDFC") || symbol.includes("INR") || symbol.includes("GOLD") || symbol.includes("CRUDEOIL");
  const currencySymbol = isINR ? "₹" : "$";

  return (
    <div className="glass-card-hover group relative overflow-hidden transition-all duration-300 bg-slate-900/90 border border-slate-800/80 rounded-xl p-4">
      <div className="flex items-center justify-between">
        <div>
          <div className="flex items-center gap-1.5">
            <span className="text-xs font-bold uppercase tracking-wider text-slate-300">
              {symbol}
            </span>
            {data.asset_class && (
              <span className="text-[9px] font-semibold px-1.5 py-0.5 rounded bg-slate-800 border border-slate-700 text-slate-400">
                {data.asset_class}
              </span>
            )}
          </div>
          <h3 className="mt-1 font-mono text-xl font-bold tracking-tight text-white">
            {currencySymbol}{Number(data.price || 0).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 4 })}
          </h3>
        </div>
        <div
          className={`flex h-9 w-9 items-center justify-center rounded-xl transition-transform duration-300 group-hover:scale-110 ${
            isPositive
              ? "bg-emerald-500/10 text-emerald-400 border border-emerald-500/20"
              : "bg-rose-500/10 text-rose-400 border border-rose-500/20"
          }`}
        >
          {isPositive ? <TrendingUp size={18} /> : <TrendingDown size={18} />}
        </div>
      </div>

      <div className="mt-3 flex items-center justify-between border-t border-slate-800/60 pt-2.5 text-[11px]">
        <span className="text-slate-500">24h Change</span>
        <span
          className={`font-mono font-medium ${
            isPositive ? "text-emerald-400" : "text-rose-400"
          }`}
        >
          {isPositive ? "+" : ""}
          {Number(data.change || 0).toFixed(2)} ({isPositive ? "+" : ""}
          {Number(data.change_pct || 0).toFixed(2)}%)
        </span>
      </div>

      {data.data_source && (
        <div className="mt-1.5 flex items-center gap-1 text-[9px] text-slate-500">
          <Shield size={10} className="text-cyan-400/80" />
          <span className="truncate">{data.data_source}</span>
        </div>
      )}
    </div>
  );
}
