import { Award, TrendingUp, Zap } from "lucide-react";

export default function TopStrategiesCard({ strategies = [] }) {
  if (!strategies || strategies.length === 0) return null;

  return (
    <div className="card p-5 bg-slate-900 border border-slate-800 rounded-xl shadow-lg">
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-2.5">
          <div className="p-2 rounded-lg bg-amber-500/10 text-amber-400 border border-amber-500/20">
            <Award size={18} />
          </div>
          <div>
            <h2 className="text-sm font-bold text-white tracking-tight">
              Top Performing Strategies
            </h2>
            <p className="text-[11px] text-slate-400">
              Live algorithmic alpha & Sharpe ranking
            </p>
          </div>
        </div>
        <span className="text-[10px] uppercase font-semibold px-2 py-0.5 rounded-full bg-slate-800 text-slate-400 border border-slate-700">
          Ranked by ROI
        </span>
      </div>

      <div className="space-y-2.5">
        {strategies.map((strat, idx) => (
          <div
            key={strat.id || idx}
            className="flex items-center justify-between p-3 rounded-lg bg-slate-850 hover:bg-slate-800/80 border border-slate-800/80 transition-all group"
          >
            <div className="flex items-center gap-3">
              <div className="flex items-center justify-center w-6 h-6 rounded-md bg-slate-800 text-xs font-bold text-slate-400 group-hover:text-cyan-400 border border-slate-700">
                #{idx + 1}
              </div>
              <div>
                <div className="flex items-center gap-2">
                  <span className="text-xs font-semibold text-white group-hover:text-cyan-300 transition-colors">
                    {strat.name}
                  </span>
                  <span className="text-[9px] px-1.5 py-0.2 rounded bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 font-medium">
                    {strat.status}
                  </span>
                </div>
                <div className="flex items-center gap-2 mt-0.5 text-[11px] text-slate-400">
                  <span>{(strat.symbols || []).join(", ")}</span>
                  <span>•</span>
                  <span>{strat.tradesCount || 0} trades</span>
                  <span>•</span>
                  <span className="text-cyan-400 font-mono">
                    Win: {strat.winRate}%
                  </span>
                </div>
              </div>
            </div>

            <div className="text-right">
              <div className="flex items-center justify-end gap-1 text-xs font-bold text-emerald-400 font-mono">
                <TrendingUp size={12} />
                +${strat.pnl ? strat.pnl.toLocaleString() : "0.00"}
              </div>
              <span className="text-[10px] text-slate-500 font-mono">
                Net Alpha
              </span>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
