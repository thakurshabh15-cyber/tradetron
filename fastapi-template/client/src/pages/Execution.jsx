import { useState } from "react";
import { Radar, Activity, Gauge, ShieldAlert, Radio } from "lucide-react";
import { useApi } from "../hooks/useApi";
import { useWebSocket } from "../hooks/useWebSocket";
import { useAuthStore } from "../stores/useAuthStore";

const inr = (v) => `₹${Number(v || 0).toLocaleString("en-IN", { maximumFractionDigits: 2 })}`;

export default function Execution() {
  const user = useAuthStore((s) => s.user);
  const [stream, setStream] = useState([]);

  const { data: health } = useApi("/api/health");
  const { data: risk } = useApi("/api/risk-status");
  const { data: positions, loading: posLoading } = useApi("/api/trades/positions");

  useWebSocket("/ws/trades", {
    onMessage: (msg) => {
      setStream((prev) => [{ ...msg, _t: new Date().toLocaleTimeString("en-IN") }, ...prev].slice(0, 40));
    },
  });

  const openPos = positions || [];
  const upnl = openPos.reduce((a, p) => a + (p.unrealized_pnl || 0), 0);
  const lossPct = risk ? Math.min(100, Math.round((Math.abs(Math.min(0, risk.daily_pnl)) / (risk.max_daily_loss || 1)) * 100)) : 0;
  const ratePct = risk ? Math.min(100, Math.round(((risk.orders_this_minute || 0) / (risk.max_orders_per_minute || 1)) * 100)) : 0;
  const sentinelTone = lossPct > 80 ? "CRITICAL" : lossPct > 50 ? "WARNING" : "HEALTHY";
  const toneCls = lossPct > 80 ? "text-rose-400" : lossPct > 50 ? "text-amber-400" : "text-emerald-400";

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
            <Radar size={20} className="text-brand-electric" />
            <h1 className="font-display text-xl font-bold text-white">Live Execution · Mission Control</h1>
          </div>
          <p className="text-xs text-slate-400 mt-0.5">Real-time algorithmic execution audit trail, engine telemetry and Risk Sentinel.</p>
        </div>
        <span className="inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-[11px] font-bold border border-emerald-500/30 bg-emerald-500/10 text-emerald-400">
          <Radio size={10} className="animate-pulse" /> STREAM CONNECTED
        </span>
      </div>

      {/* KPI strip */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <Kpi label="Open Positions" value={openPos.length} sub={`${openPos.filter((p) => p.mode === "LIVE").length} live`} />
        <Kpi label="Unrealized P&L" value={inr(upnl)} tone={upnl >= 0 ? "text-profit-400" : "text-loss-400"} sub="mark-to-market" />
        <Kpi label="Engine" value={health?.engine_running ? "RUNNING" : "STOPPED"} tone={health?.engine_running ? "text-profit-400" : "text-loss-400"} sub={`broker: ${health?.broker_mode ?? "—"}`} />
        <Kpi label="Sentinel" value={sentinelTone} tone={toneCls} sub={`daily loss ${lossPct}% of limit`} />
      </div>

      <div className="grid lg:grid-cols-5 gap-4">
        {/* Execution Stream */}
        <section className="lg:col-span-3 glass-panel rounded-2xl p-4 border border-edge">
          <h2 className="flex items-center gap-2 text-sm font-bold text-white mb-3"><Activity size={14} className="text-brand-electric" /> EXECUTION STREAM</h2>
          <div className="max-h-[420px] overflow-y-auto space-y-1.5 font-mono text-[11px]">
            {stream.length === 0 && <p className="text-slate-500 py-6 text-center">Awaiting executions… fills broadcast here in real-time.</p>}
            {stream.map((e, i) => (
              <div key={i} className="flex items-center justify-between rounded-lg bg-surface-900/70 border border-slate-800/60 px-3 py-1.5">
                <span className="text-slate-500">{e._t}</span>
                <span className={e.side === "BUY" ? "text-emerald-400 font-bold" : "text-rose-400 font-bold"}>{e.side || e.event || "EVT"}</span>
                <span className="text-slate-200">{e.symbol || ""} {e.quantity ?? ""}</span>
                <span className="text-white">{e.price ? inr(e.price) : ""}</span>
                <span className="text-cyan-400">{e.status || ""}</span>
              </div>
            ))}
          </div>
        </section>

        <div className="lg:col-span-2 space-y-4">
          {/* Risk Sentinel */}
          <section className="glass-panel rounded-2xl p-4 border border-edge">
            <h2 className="flex items-center gap-2 text-sm font-bold text-white mb-3"><ShieldAlert size={14} className="text-amber-400" /> RISK SENTINEL</h2>
            <Bar label="Daily Loss Limit" pct={lossPct} tone={lossPct > 80 ? "bg-loss-500" : lossPct > 50 ? "bg-warning-500" : "bg-profit-500"} right={`${inr(risk?.daily_pnl)} / ${inr(risk?.max_daily_loss)}`} />
            <Bar label="Order Rate (1m)" pct={ratePct} tone={ratePct > 80 ? "bg-loss-500" : "bg-accent-500"} right={`${risk?.orders_this_minute ?? 0}/${risk?.max_orders_per_minute ?? 30}`} />
            <div className="mt-3 flex items-center justify-between text-xs">
              <span className="flex items-center gap-1.5 text-slate-400"><Gauge size={12} /> Circuit breaker</span>
              <span className={risk?.circuit_breaker_active ? "text-rose-400 font-bold" : "text-profit-400 font-bold"}>{risk?.circuit_breaker_active ? "TRIGGERED" : "ARMED"}</span>
            </div>
          </section>

          {/* Open positions snapshot */}
          <section className="glass-panel rounded-2xl p-4 border border-edge">
            <h2 className="text-sm font-bold text-white mb-2">OPEN POSITIONS</h2>
            {posLoading ? <p className="text-xs text-slate-500">Loading…</p> : openPos.length === 0 ? (
              <p className="text-xs text-slate-500 py-4 text-center">No open positions.</p>
            ) : (
              <table className="w-full text-[11px] font-mono">
                <tbody>
                  {openPos.slice(0, 8).map((p) => (
                    <tr key={p.id} className="border-b border-slate-800/50 last:border-0">
                      <td className="py-1.5 text-white font-bold">{p.symbol}</td>
                      <td className={p.side === "LONG" ? "text-emerald-400" : "text-rose-400"}>{p.side}</td>
                      <td className="text-right text-slate-300">{p.quantity}</td>
                      <td className={`text-right ${ (p.unrealized_pnl||0)>=0 ? "text-profit-400":"text-loss-400"}`}>{inr(p.unrealized_pnl)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>
        </div>
      </div>
      <p className="text-[10px] text-slate-600 font-mono">Environment: {user ? "authenticated session" : "anonymous"} · all values mark-to-market from live unified tape.</p>
    </div>
  );
}

function Kpi({ label, value, sub, tone = "text-white" }) {
  return (
    <div className="glass-panel rounded-xl p-3.5 border border-edge">
      <p className="text-[10px] uppercase tracking-wider text-slate-500 font-semibold">{label}</p>
      <p className={`font-mono text-lg font-bold mt-1 tabular-nums ${tone}`}>{value}</p>
      <p className="text-[10px] text-slate-500 mt-0.5">{sub}</p>
    </div>
  );
}

function Bar({ label, pct, tone, right }) {
  return (
    <div className="mb-3">
      <div className="flex justify-between text-[11px] text-slate-400 mb-1"><span>{label}</span><span className="font-mono text-slate-300">{right}</span></div>
      <div className="h-1.5 w-full rounded-full bg-surface-700 overflow-hidden">
        <div className={`h-full rounded-full ${tone} transition-all duration-500`} style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}
