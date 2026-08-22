import React, { useEffect, useRef, useState } from "react";
import { useMarket } from "../context/MarketContext";
import { TrendingUp, TrendingDown, Shield, Activity } from "lucide-react";

function MarketTicker({ symbol, initialData, isSelected, onSelect }) {
  const { getQuote } = useMarket();
  const liveQuote = getQuote(symbol);
  const data = liveQuote || initialData || { price: 0, change: 0, change_pct: 0 };

  const [priceFlash, setPriceFlash] = useState(null); // 'UP' | 'DOWN' | null
  const prevPriceRef = useRef(data.price);

  useEffect(() => {
    if (data.price && prevPriceRef.current !== undefined) {
      if (data.price > prevPriceRef.current) {
        setPriceFlash("UP");
      } else if (data.price < prevPriceRef.current) {
        setPriceFlash("DOWN");
      }
      prevPriceRef.current = data.price;

      const timer = setTimeout(() => setPriceFlash(null), 600);
      return () => clearTimeout(timer);
    }
  }, [data.price]);

  const isPositive = (data.change ?? 0) >= 0;
  const sym = symbol || "";
  const isINR =
    sym.includes("NIFTY") ||
    sym.includes("RELIANCE") ||
    sym.includes("TCS") ||
    sym.includes("INFY") ||
    sym.includes("HDFC") ||
    sym.includes("INR") ||
    sym.includes("GOLD") ||
    sym.includes("CRUDEOIL");
  const currencySymbol = isINR ? "₹" : "$";

  return (
    <div
      onClick={() => onSelect && onSelect(symbol)}
      className={`group relative overflow-hidden transition-all duration-300 rounded-xl p-3.5 cursor-pointer select-none ${
        isSelected
          ? "bg-slate-900 border-2 border-cyan-500 shadow-lg shadow-cyan-500/10 ring-1 ring-cyan-500/30"
          : "bg-slate-900/90 hover:bg-slate-850 border border-slate-800/80 hover:border-slate-700 shadow-sm"
      }`}
    >
      {/* Top Symbol & Type Badge */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-1.5">
          <span className="text-xs font-bold uppercase tracking-wider text-white font-mono">{symbol}</span>
          {data.asset_class && (
            <span className="text-[9px] font-semibold px-1.5 py-0.5 rounded bg-slate-800 border border-slate-700 text-slate-400">
              {data.asset_class}
            </span>
          )}
          {isSelected && (
            <span className="flex h-1.5 w-1.5 rounded-full bg-cyan-400 animate-ping" title="Active on Chart" />
          )}
        </div>
        <div
          className={`flex h-7 w-7 items-center justify-center rounded-lg transition-transform duration-300 group-hover:scale-110 ${
            isPositive
              ? "bg-emerald-500/10 text-emerald-400 border border-emerald-500/20"
              : "bg-rose-500/10 text-rose-400 border border-rose-500/20"
          }`}
        >
          {isPositive ? <TrendingUp size={14} /> : <TrendingDown size={14} />}
        </div>
      </div>

      {/* Live Moving Price with Flash Animation */}
      <div className="mt-1.5 flex items-baseline justify-between">
        <h3
          className={`font-mono text-lg font-bold tracking-tight transition-colors duration-300 rounded px-1 -mx-1 ${
            priceFlash === "UP"
              ? "bg-emerald-500/25 text-emerald-300 shadow-sm shadow-emerald-500/20"
              : priceFlash === "DOWN"
              ? "bg-rose-500/25 text-rose-300 shadow-sm shadow-rose-500/20"
              : "text-white"
          }`}
        >
          {currencySymbol}
          {Number(data.price || 0).toLocaleString(undefined, {
            minimumFractionDigits: 2,
            maximumFractionDigits: 4,
          })}
        </h3>
      </div>

      {/* 24h Delta and Day Low/High */}
      <div className="mt-2.5 flex items-center justify-between border-t border-slate-800/60 pt-2 text-[11px]">
        <span className="text-slate-500">24h Delta</span>
        <span className={`font-mono font-medium ${isPositive ? "text-emerald-400" : "text-rose-400"}`}>
          {isPositive ? "+" : ""}
          {Number(data.change || 0).toFixed(2)} ({isPositive ? "+" : ""}
          {Number(data.change_pct || 0).toFixed(2)}%)
        </span>
      </div>

      {/* Data Source & Volume */}
      <div className="mt-1 flex items-center justify-between text-[9px] text-slate-500">
        <div className="flex items-center gap-1">
          <Shield size={10} className="text-cyan-400/80" />
          <span className="truncate max-w-[110px]">{data.data_source || "Live Pipeline Feed"}</span>
        </div>
        {data.volume ? (
          <span className="font-mono text-slate-400">Vol: {Number(data.volume).toLocaleString()}</span>
        ) : null}
      </div>
    </div>
  );
}

function areMarketTickerPropsEqual(prevProps, nextProps) {
  if (prevProps.symbol !== nextProps.symbol) return false;
  if (prevProps.isSelected !== nextProps.isSelected) return false;
  if (prevProps.onSelect !== nextProps.onSelect) return false;

  const prev = prevProps.initialData || {};
  const next = nextProps.initialData || {};

  if (prev.price !== next.price) return false;
  if (prev.change !== next.change) return false;
  if (prev.change_pct !== next.change_pct) return false;
  if (prev.high !== next.high) return false;
  if (prev.low !== next.low) return false;
  if (prev.volume !== next.volume) return false;

  return true;
}

export default React.memo(MarketTicker, areMarketTickerPropsEqual);
