import { useMemo } from "react";
import { Briefcase, PieChart, Wallet, AlertTriangle } from "lucide-react";
import { useApi } from "../hooks/useApi";
import { useAuthStore } from "../stores/useAuthStore";

const inr = (v) => `₹${Number(v || 0).toLocaleString("en-IN", { maximumFractionDigits: 2 })}`;

export default function Portfolio() {
  const paperBalance = useAuthStore((s) => s.paperBalance);
  const { data: positions, loading } = useApi("/api/trades/positions");

  const rows = positions || [];
  const upnl = rows.reduce((a, p) => a + (p.unrealized_pnl || 0), 0);
  const netWorth = paperBalance + upnl;
  const grossExposure = rows.reduce((a, p) => a + Math.abs((p.quantity || 0) * (p.current_price || p.entry_price || 0)), 0);
  const exposurePct = netWorth > 0 ? Math.min(999, Math.round((grossExposure / netWorth) * 100)) : 0;

  const bySymbol = useMemo(() => {
    const m = new Map();
    for (const p of rows) {
      const cur = m.get(p.symbol) || { symbol: p.symbol, qty: 0, upnl: 0, notional: 0 };
      cur.qty += p.quantity * (p.side === "SHORT" ? -1 : 1);
      cur.upnl += p.unrealized_pnl || 0;
      cur.notional += Math.abs((p.quantity || 0) * (p.current_price || p.entry_price || 0));
      m.set(p.symbol, cur);
    }
    return [...m.values()].sort((a, b) => b.notional - a.notional);
  }, [rows]);

  const topWeight = bySymbol.length ? Math.round((bySymbol[0].notional / Math.max(grossExposure, 1)) * 100) : 0;
  // UI heuristic risk score: exposure + concentration composite (documented on-screen)
  const riskScore = Math.min(100, Math.round(exposurePct * 0.55 + topWeight * 0.45));
  const riskTone = riskScore < 35 ? "text-profit-400" : riskScore < 65 ? "text-warning-400" : "text-loss-400";

  return (
    <div className="space-y-5">
      <div className="flex items-center gap-2">
        <Briefcase size={20} className="text-brand-electric" />
        <h1 className="font-display text-xl font-bold text-white">Portfolio & Risk</h1>
      </div>

      {/* Hero */}
      <div className="glass-panel rounded-2xl p-5 border border-edge grid sm:grid-cols-3 gap-4">
        <div>
          <p className="text-[10px] uppercase tracking-wider text-slate-500 font-semibold">Net Worth (Paper)</p>
          <p className="font-mono text-3xl font-bold text-white tabular-nums mt-1">{inr(netWorth)}</p>
          <p className="text-[11px] text-slate-500 mt-0.5">cash {inr(paperBalance)} + open P&L {inr(upnl)}</p>
        </div>
        <div>
          <p className="text-[10px] uppercase tracking-wider text-slate-500 font-semibold">Open P&L</p>
          <p className={`font-mono text-2xl font-bold tabular-nums mt-1 ${upnl >= 0 ? "text-profit-400" : "text-loss-400"}`}>{upnl >= 0 ? "+" : ""}{inr(upnl)}</p>
          <p className="text-[11px] text-slate-500 mt-0.5">across {rows.length} positions</p>
        </div>
        <div>
          <p className="text-[10px] uppercase tracking-wider text-slate-500 font-semibold">Gross Exposure</p>
          <p className="font-mono text-2xl font-bold text-white tabular-nums mt-1">{exposurePct}%</p>
          <p className="text-[11px] text-slate-500 mt-0.5">{inr(grossExposure)} notional</p>
        </div>
      </div>

      <div className="grid lg:grid-cols-3 gap-4">
        {/* Risk cockpit */}
        <section className="glass-panel rounded-2xl p-4 border border-edge">
          <h2 className="flex items-center gap-2 text-sm font-bold text-white mb-3"><PieChart size={14} className="text-brand-electric" /> RISK COCKPIT</h2>
          <div className="flex items-end gap-2 mb-3">
            <span className={`font-mono text-4xl font-bold ${riskTone}`}>{riskScore}</span>
            <span className="text-xs text-slate-500 mb-1.5">/ 100 · {riskScore < 35 ? "LOW" : riskScore < 65 ? "MODERATE" : "HIGH"}</span>
          </div>
          <Row k="Exposure weight" v={`${Math.min(100, exposurePct)}%`} />
          <Row k="Top concentration" v={`${topWeight}% (${bySymbol[0]?.symbol ?? "—"})`} />
          <Row k="Margin usage" v="—" sub />
          <Row k="Correlation matrix" v="—" sub />
          <p className="text-[9px] text-slate-600 mt-3 leading-relaxed">Heuristic = exposure×0.55 + concentration×0.45. Margin/correlation render once broker margin endpoints return data.</p>
        </section>

        {/* Attribution */}
        <section className="lg:col-span-2 glass-panel rounded-2xl p-4 border border-edge">
          <h2 className="flex items-center gap-2 text-sm font-bold text-white mb-3"><Wallet size={14} className="text-profit-400" /> P&L ATTRIBUTION BY SYMBOL</h2>
          {loading ? <p className="text-xs text-slate-500 py-6 text-center">Loading positions…</p> : rows.length === 0 ? (
            <div className="py-10 text-center space-y-2">
              <AlertTriangle size={20} className="mx-auto text-slate-600" />
              <p className="text-xs text-slate-500">No open positions — attribute P&L after executing via the DMA terminal.</p>
            </div>
          ) : (
            <table className="w-full text-[11px] font-mono tabular-nums">
              <thead>
                <tr className="text-slate-500 border-b border-slate-800/70">
                  <th className="text-left py-1.5 font-medium">Symbol</th>
                  <th className="text-right font-medium">Qty</th>
                  <th className="text-right font-medium">Notional</th>
                  <th className="text-right font-medium">Weight</th>
                  <th className="text-right font-medium">Open P&L</th>
                </tr>
              </thead>
              <tbody>
                {bySymbol.map((r) => (
                  <tr key={r.symbol} className="border-b border-slate-800/40 last:border-0">
                    <td className="py-1.5 text-white font-bold">{r.symbol}</td>
                    <td className="text-right text-slate-300">{r.qty}</td>
                    <td className="text-right text-slate-300">{inr(r.notional)}</td>
                    <td className="text-right text-cyan-400">{Math.round((r.notional / Math.max(grossExposure, 1)) * 100)}%</td>
                    <td className={`text-right font-bold ${r.upnl >= 0 ? "text-profit-400" : "text-loss-400"}`}>{inr(r.upnl)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      </div>
    </div>
  );
}

function Row({ k, v, sub }) {
  return (
    <div className="flex justify-between items-center py-1.5 border-b border-slate-800/40 last:border-0">
      <span className="text-[11px] text-slate-400">{k}</span>
      <span className="text-[11px] font-mono text-slate-200">{v}{sub && <span className="ml-1 text-[9px] text-slate-600">(pending)</span>}</span>
    </div>
  );
}
