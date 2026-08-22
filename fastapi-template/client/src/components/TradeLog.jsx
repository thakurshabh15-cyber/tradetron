import React, { useEffect, useState } from "react";
import { useWebSocket } from "../hooks/useWebSocket";
import StatusBadge from "./StatusBadge";
import { Activity } from "lucide-react";
import { ErrorState } from "./SkeletonLoaders";

const TradeItem = React.memo(function TradeItem({ trade }) {
  return (
    <div
      className="flex items-center justify-between rounded-lg bg-surface-800/60 p-3 text-xs border border-white/[0.03] transition-all hover:bg-surface-800"
    >
      <div className="flex items-center gap-3">
        <StatusBadge status={trade.side} />
        <div>
          <div className="font-semibold text-white">
            {trade.symbol}
          </div>
          <div className="text-[11px] text-slate-400">
            {trade.strategy_name || "SMA Strategy"}
          </div>
        </div>
      </div>

      <div className="text-right">
        <div className="font-mono font-medium text-white">
          {trade.quantity} @ ${Number(trade.price || 0).toFixed(2)}
        </div>
        <div className="text-[10px] text-slate-500">
          {trade.executed_at
            ? new Date(trade.executed_at).toLocaleTimeString()
            : "Just now"}
        </div>
      </div>
    </div>
  );
}, (prev, next) => {
  return (
    prev.trade?.id === next.trade?.id &&
    prev.trade?.price === next.trade?.price &&
    prev.trade?.executed_at === next.trade?.executed_at
  );
});

function TradeLog({ initialTrades = [], loading = false, error = null, onRetry = null }) {
  const [trades, setTrades] = useState(initialTrades);

  // Connect to live trades WebSocket feed
  useWebSocket("/ws/trades", {
    onMessage: (newTrade) => {
      setTrades((prev) => [newTrade, ...prev.slice(0, 49)]); // Keep latest 50
    },
  });

  useEffect(() => {
    if (initialTrades?.length) {
      setTrades(initialTrades);
    }
  }, [initialTrades]);

  return (
    <div className="glass-card p-3.5 sm:p-5 flex flex-col h-[380px] sm:h-[420px]">
      <div className="flex items-center justify-between border-b border-white/[0.06] pb-3">
        <div className="flex items-center gap-2">
          <Activity size={16} className="text-accent-400" />
          <h2 className="text-sm font-semibold uppercase tracking-wider text-slate-300">
            Real-Time Trade Stream
          </h2>
        </div>
        <span className="flex items-center gap-1.5 text-xs text-profit-400">
          <span className="h-2 w-2 rounded-full bg-profit-400 animate-pulse"></span>
          Live Feed
        </span>
      </div>

      <div className="flex-1 overflow-y-auto pt-2">
        {error ? (
          <ErrorState
            title="Trade Stream Error"
            error={error}
            onRetry={onRetry}
          />
        ) : loading ? (
          <div className="space-y-2 p-2">
            {Array.from({ length: 4 }).map((_, i) => (
              <div key={i} className="h-12 w-full skeleton-box rounded-xl" />
            ))}
          </div>
        ) : trades.length === 0 ? (
          <div className="flex h-full items-center justify-center text-xs text-slate-500">
            Waiting for strategy signals & executions...
          </div>
        ) : (
          <div className="space-y-2">
            {trades.map((trade, idx) => (
              <TradeItem key={trade.id || idx} trade={trade} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function areTradeLogPropsEqual(prev, next) {
  if (prev.loading !== next.loading) return false;
  if (prev.error !== next.error) return false;
  if (prev.onRetry !== next.onRetry) return false;

  const pList = prev.initialTrades || [];
  const nList = next.initialTrades || [];
  if (pList.length !== nList.length) return false;

  for (let i = 0; i < pList.length; i++) {
    if (pList[i].id !== nList[i].id) return false;
    if (pList[i].price !== nList[i].price) return false;
  }

  return true;
}

export default React.memo(TradeLog, areTradeLogPropsEqual);
