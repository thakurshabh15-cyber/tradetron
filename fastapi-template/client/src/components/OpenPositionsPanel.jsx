import React, { useState } from "react";
import { useMarket } from "../context/MarketContext";
import { API_BASE } from "../config";
import { authFetch } from "../services/apiClient";
import { ErrorState } from "./SkeletonLoaders";
import { useToast } from "./Toast";
import {
  Layers,
  XCircle,
  RefreshCw,
  ArrowUpRight,
  ArrowDownRight,
} from "lucide-react";

const PositionRow = React.memo(function PositionRow({
  pos,
  metric,
  isClosing,
  onClose,
  fmtCurrency,
}) {
  const isLong = pos.side === "LONG" || pos.side === "BUY";

  return (
    <tr className="hover:bg-white/[0.02] transition-colors">
      <td className="py-3 px-3 font-mono font-bold text-white">
        <span>{pos.symbol}</span>
      </td>

      <td className="py-3 px-3">
        <span
          className={`text-[10px] font-bold px-2 py-0.5 rounded border ${
            isLong
              ? "bg-emerald-500/15 text-emerald-300 border-emerald-500/30"
              : "bg-rose-500/15 text-rose-300 border-rose-500/30"
          }`}
        >
          {isLong ? "BUY / LONG" : "SELL / SHORT"}
        </span>
      </td>

      <td className="py-3 px-3 text-right font-mono text-white">
        {pos.quantity}
      </td>

      <td className="py-3 px-3 text-right font-mono text-slate-300">
        {fmtCurrency(pos.entry_price, pos.symbol)}
      </td>

      <td className="py-3 px-3 text-right font-mono font-bold text-white">
        <span className="flex items-center justify-end gap-1">
          <span className="w-1.5 h-1.5 rounded-full bg-cyan-400 animate-pulse" />
          {fmtCurrency(metric.livePrice, pos.symbol)}
        </span>
      </td>

      <td className="py-3 px-3 text-right font-mono font-bold">
        <span
          className={`inline-flex items-center gap-1 ${
            metric.isPositive ? "text-emerald-400" : "text-rose-400"
          }`}
        >
          {metric.isPositive ? <ArrowUpRight size={13} /> : <ArrowDownRight size={13} />}
          <span>
            {metric.isPositive ? "+" : ""}
            {fmtCurrency(metric.pnl, pos.symbol)} ({metric.isPositive ? "+" : ""}
            {metric.pnlPct.toFixed(2)}%)
          </span>
        </span>
      </td>

      <td className="py-3 px-3 text-center">
        <span
          className={`text-[9px] font-bold px-1.5 py-0.5 rounded border ${
            pos.mode === "LIVE"
              ? "bg-rose-500/20 text-rose-300 border-rose-500/40"
              : "bg-emerald-500/20 text-emerald-300 border-emerald-500/40"
          }`}
        >
          {pos.mode}
        </span>
      </td>

      <td className="py-3 px-3 text-right">
        <button
          onClick={() => onClose(pos.id, pos.symbol)}
          disabled={isClosing}
          className="px-2.5 py-1 rounded-lg text-xs font-semibold bg-rose-500/15 hover:bg-rose-500/25 border border-rose-500/30 text-rose-300 hover:text-white transition-all disabled:opacity-50 flex items-center gap-1 ml-auto"
          title="Close position at current market price"
        >
          {isClosing ? (
            <RefreshCw size={12} className="animate-spin" />
          ) : (
            <XCircle size={12} />
          )}
          <span>{isClosing ? "Closing..." : "Close"}</span>
        </button>
      </td>
    </tr>
  );
}, arePositionRowPropsEqual);

function arePositionRowPropsEqual(prev, next) {
  if (prev.pos?.id !== next.pos?.id) return false;
  if (prev.pos?.symbol !== next.pos?.symbol) return false;
  if (prev.pos?.quantity !== next.pos?.quantity) return false;
  if (prev.pos?.entry_price !== next.pos?.entry_price) return false;
  if (prev.pos?.mode !== next.pos?.mode) return false;
  if (prev.isClosing !== next.isClosing) return false;

  const pMet = prev.metric || {};
  const nMet = next.metric || {};
  if (pMet.livePrice !== nMet.livePrice) return false;
  if (pMet.pnl !== nMet.pnl) return false;
  if (pMet.pnlPct !== nMet.pnlPct) return false;

  return true;
}

function OpenPositionsPanel({
  positions = [],
  loading = false,
  error = null,
  onRetry = null,
  onPositionClosed,
}) {
  const { getQuote } = useMarket();
  const [closingId, setClosingId] = useState(null);
  const [actionError, setActionError] = useState(null);
  const toast = useToast();

  const handleClosePosition = async (posId, symbol) => {
    setClosingId(posId);
    setActionError(null);
    try {
      const res = await authFetch(`${API_BASE}/api/trades/positions/${posId}/close`, {
        method: "POST",
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || "Failed to close position");
      }
      toast.success(`${symbol} position closed`, { description: "Squared off at the latest market price." });
      if (onPositionClosed) {
        onPositionClosed();
      }
    } catch (err) {
      console.error("Close position error:", err);
      setActionError(err.message || "Failed to close position");
      toast.error("Could not close position", { description: err.message });
    } finally {
      setClosingId(null);
    }
  };

  // Compute live unrealized P&L for all positions
  let totalUnrealizedPnl = 0;
  const positionMetrics = {};

  positions.forEach((pos) => {
    const liveQuote = getQuote(pos.symbol);
    const livePrice = liveQuote?.price ?? pos.current_price ?? pos.entry_price;
    const isLong = pos.side === "LONG" || pos.side === "BUY";
    const delta = isLong ? livePrice - pos.entry_price : pos.entry_price - livePrice;
    const pnl = delta * pos.quantity;
    const pnlPct = pos.entry_price ? (delta / pos.entry_price) * 100 : 0;

    positionMetrics[pos.id] = {
      livePrice,
      pnl,
      pnlPct,
      isPositive: pnl >= 0,
    };
    totalUnrealizedPnl += pnl;
  });

  const isTotalPositive = totalUnrealizedPnl >= 0;

  const fmtCurrency = (val, sym) => {
    const isINR =
      sym?.includes("NIFTY") ||
      sym?.includes("RELIANCE") ||
      sym?.includes("INR") ||
      sym?.includes("TCS") ||
      sym?.includes("GOLD") ||
      sym?.includes("CRUDE");
    const sign = isINR ? "₹" : "$";
    return `${sign}${Number(val || 0).toLocaleString("en-IN", {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    })}`;
  };

  return (
    <div className="glass-card p-3.5 sm:p-5 space-y-4 overflow-hidden">
      {/* Header & Unrealized PnL Badge */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 border-b border-white/[0.06] pb-3">
        <div className="flex items-center gap-2.5">
          <div className="p-1.5 rounded-lg bg-cyan-500/10 text-cyan-400 border border-cyan-500/20 shrink-0">
            <Layers size={16} />
          </div>
          <div>
            <div className="flex items-center gap-2">
              <h2 className="text-sm font-semibold text-white">Live Open Positions</h2>
              <span className="text-[10px] font-mono font-bold px-2 py-0.5 rounded-full bg-surface-800 text-slate-400 border border-white/[0.06]">
                {positions.length} Active
              </span>
            </div>
            <p className="text-[11px] text-slate-400">
              Live mark-to-market positions ticking dynamically with the central WebSocket feed
            </p>
          </div>
        </div>

        {positions.length > 0 && (
          <div className="flex items-center gap-3 self-start sm:self-auto">
            <div className="text-left sm:text-right">
              <span className="text-[10px] text-slate-500 block">Total Unrealized P&L</span>
              <span
                className={`font-mono text-sm font-bold ${
                  isTotalPositive ? "text-emerald-400" : "text-rose-400"
                }`}
              >
                {isTotalPositive ? "+" : ""}
                {fmtCurrency(totalUnrealizedPnl, positions[0]?.symbol)}
              </span>
            </div>
          </div>
        )}
      </div>

      {actionError && (
        <div className="p-3 rounded-xl bg-rose-500/10 border border-rose-500/30 text-rose-300 text-xs">
          {actionError}
        </div>
      )}

      {/* Positions Table / Empty / Loading / Error State */}
      {error ? (
        <ErrorState
          title="Positions Synchronization Error"
          error={error}
          onRetry={onRetry}
        />
      ) : loading ? (
        <div className="space-y-2 p-2">
          {Array.from({ length: 3 }).map((_, i) => (
            <div key={i} className="h-11 w-full skeleton-box rounded-xl" />
          ))}
        </div>
      ) : positions.length === 0 ? (
        <div className="p-6 sm:p-8 text-center rounded-xl bg-surface-900/40 border border-dashed border-slate-800 space-y-2">
          <Layers size={28} className="mx-auto text-slate-600" />
          <p className="text-xs font-semibold text-slate-400">No Open Positions</p>
          <p className="text-[11px] text-slate-500 max-w-sm mx-auto">
            Execute a BUY or SELL order using the Fast DMA Panel to initiate a live tracked position.
          </p>
        </div>
      ) : (
        <div className="overflow-x-auto -mx-3.5 sm:mx-0 px-3.5 sm:px-0">
          <table className="w-full text-left text-xs min-w-[550px]">
            <thead className="bg-surface-800/40 text-slate-400 font-medium border-b border-white/[0.04]">
              <tr>
                <th className="py-2.5 px-3">Instrument</th>
                <th className="py-2.5 px-3">Side</th>
                <th className="py-2.5 px-3 text-right">Qty</th>
                <th className="py-2.5 px-3 text-right">Entry Price</th>
                <th className="py-2.5 px-3 text-right">Live Price</th>
                <th className="py-2.5 px-3 text-right">Unrealized P&L</th>
                <th className="py-2.5 px-3 text-center">Mode</th>
                <th className="py-2.5 px-3 text-right">Action</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-white/[0.04]">
              {positions.map((pos) => {
                const metric = positionMetrics[pos.id] || {
                  livePrice: pos.entry_price,
                  pnl: 0,
                  pnlPct: 0,
                  isPositive: true,
                };

                return (
                  <PositionRow
                    key={pos.id}
                    pos={pos}
                    metric={metric}
                    isClosing={closingId === pos.id}
                    onClose={handleClosePosition}
                    fmtCurrency={fmtCurrency}
                  />
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function areOpenPositionsPropsEqual(prev, next) {
  if (prev.loading !== next.loading) return false;
  if (prev.error !== next.error) return false;
  if (prev.onRetry !== next.onRetry) return false;
  if (prev.onPositionClosed !== next.onPositionClosed) return false;

  const pList = prev.positions || [];
  const nList = next.positions || [];
  if (pList.length !== nList.length) return false;

  for (let i = 0; i < pList.length; i++) {
    if (pList[i].id !== nList[i].id) return false;
    if (pList[i].current_price !== nList[i].current_price) return false;
    if (pList[i].unrealized_pnl !== nList[i].unrealized_pnl) return false;
    if (pList[i].status !== nList[i].status) return false;
  }

  return true;
}

export default React.memo(OpenPositionsPanel, areOpenPositionsPropsEqual);
