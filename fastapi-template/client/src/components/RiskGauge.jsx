import { ShieldAlert, Activity } from "lucide-react";

export default function RiskGauge({ riskData }) {
  const data = riskData || {
    daily_pnl: 0,
    max_daily_loss: 10000,
    open_positions: 0,
    orders_this_minute: 0,
    max_orders_per_minute: 30,
    circuit_breaker_active: false,
  };

  const lossUsage = Math.min(
    100,
    Math.max(0, (Math.abs(Math.min(0, data.daily_pnl)) / (data.max_daily_loss || 1)) * 100)
  );

  const rateUsage = Math.min(
    100,
    (data.orders_this_minute / (data.max_orders_per_minute || 1)) * 100
  );

  return (
    <div className="glass-card flex flex-col justify-between">
      <div>
        <div className="flex items-center justify-between border-b border-white/[0.06] pb-3">
          <div className="flex items-center gap-2">
            <ShieldAlert size={16} className="text-accent-400" />
            <h2 className="text-sm font-semibold uppercase tracking-wider text-slate-300">
              Risk & Exposure
            </h2>
          </div>
          {data.circuit_breaker_active ? (
            <span className="badge-loss">Circuit Breaker Active</span>
          ) : (
            <span className="badge-profit">Risk Safe</span>
          )}
        </div>

        <div className="mt-4 space-y-4 text-xs">
          {/* Daily Drawdown Meter */}
          <div>
            <div className="flex justify-between text-slate-400 mb-1">
              <span>Daily Loss Buffer</span>
              <span className="font-mono text-white">
                ${Math.abs(Math.min(0, data.daily_pnl)).toFixed(2)} / ${Number(data.max_daily_loss).toFixed(2)}
              </span>
            </div>
            <div className="h-2 w-full rounded-full bg-surface-800 overflow-hidden">
              <div
                className={`h-full transition-all duration-300 ${
                  lossUsage > 80 ? "bg-loss-500" : lossUsage > 50 ? "bg-amber-500" : "bg-profit-500"
                }`}
                style={{ width: `${lossUsage}%` }}
              />
            </div>
          </div>

          {/* Rate Limit Meter */}
          <div>
            <div className="flex justify-between text-slate-400 mb-1">
              <span>Order Rate (1m)</span>
              <span className="font-mono text-white">
                {data.orders_this_minute} / {data.max_orders_per_minute} req/min
              </span>
            </div>
            <div className="h-2 w-full rounded-full bg-surface-800 overflow-hidden">
              <div
                className={`h-full transition-all duration-300 ${
                  rateUsage > 80 ? "bg-loss-500" : "bg-accent-500"
                }`}
                style={{ width: `${rateUsage}%` }}
              />
            </div>
          </div>
        </div>
      </div>

      <div className="mt-4 flex items-center justify-between rounded-lg bg-surface-800/40 p-3 text-xs">
        <span className="text-slate-400">Open Tracked Positions</span>
        <span className="font-mono text-base font-semibold text-white">
          {data.open_positions}
        </span>
      </div>
    </div>
  );
}
