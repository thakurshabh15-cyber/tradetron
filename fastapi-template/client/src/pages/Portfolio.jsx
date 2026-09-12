import { useMemo } from "react";
import { Briefcase, PieChart, Wallet, AlertTriangle } from "lucide-react";
import { useApi } from "../hooks/useApi";
import { useAuthStore } from "../stores/useAuthStore";
import { deriveEquityDisplay } from "../utils/brokerEquity";

const inr = (v) => `₹${Number(v || 0).toLocaleString("en-IN", { maximumFractionDigits: 2 })}`;

export default function Portfolio() {
  const paperBalance = useAuthStore((s) => s.paperBalance);
  const { data: positions, loading } = useApi("/api/trades/positions");
  // Phase 15B: the dashboard summary exposes the persisted, freshness-labelled
  // broker_state snapshot (`equity`).  LIVE/STALE tenants get BROKER truth;
  // PAPER tenants keep the paper ledger; ERROR/UNAVAILABLE tenants get an
  // honest "—" instead of a fabricated or substituted number.
  const { data: summaryData } = useApi("/api/dashboard/summary");
  const equity = summaryData?.equity || null;

  const rows = positions || [];
  const upnl = rows.reduce((a, p) => a + (p.unrealized_pnl || 0), 0);
  const netWorth = paperBalance + upnl;
  const grossExposure = rows.reduce((a, p) => a + Math.abs((p.quantity || 0) * (p.current_price || p.entry_price || 0)), 0);

  // Single source of truth for the hero (pure + unit-tested): broker-truth for
  // LIVE/STALE, paper ledger for PAPER, honest "—" for ERROR/UNAVAILABLE.
  const view = deriveEquityDisplay({ equity, paperBalance, upnl, rowsLength: rows.length });
  // Exposure denominator: broker total equity when it is broker truth.
  const exposureBase = view.isBrokerTruth && view.headValue != null ? view.headValue : netWorth;
  const exposurePct = exposureBase > 0 ? Math.min(999, Math.round((grossExposure / exposureBase) * 100)) : 0;

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
  const badgeTone =
    view.badge === "LIVE"
      ? "bg-emerald-500/15 border-emerald-500/30 text-emerald-300"
      : view.badge === "STALE"
        ? "bg-amber-500/15 border-amber-500/30 text-amber-300"
        : view.badge === "PAPER"
          ? "bg-cyan-500/15 border-cyan-500/30 text-cyan-300"
          : view.badge === "ERROR"
            ? "bg-rose-500/15 border-rose-500/30 text-rose-300"
            : view.badge === "UNAVAILABLE"
              ? "bg-slate-700/30 border-slate-700 text-slate-400"
              : null;

  return (
    <div className="space-y-5">
      <div className="flex items-center gap-2">
        <Briefcase size={20} className="text-brand-electric" />
        <h1 className="font-display text-xl font-bold text-white">Portfolio & Risk</h1>
      </div>

      {/* Hero */}
      <div className="glass-panel rounded-2xl p-5 border border-edge grid sm:grid-cols-3 gap-4">
        <div>
          <p className="text-[10px] uppercase tracking-wider text-slate-500 font-semibold flex items-center flex-wrap">
            {view.headTitle}
            {view.badge && badgeTone ? (
              <span className={`ml-2 px-2 py-0.5 rounded-full text-[9px] font-bold border ${badgeTone}`}>
                {view.badge}
              </span>
            ) : null}
          </p>
          <p className={`font-mono text-3xl font-bold tabular-nums mt-1 ${view.headTone}`}>
            {view.headValue != null ? inr(view.headValue) : "—"}
          </p>
          <p className="text-[11px] text-slate-500 mt-0.5">{view.headSub}</p>
        </div>
        <div>
          <p className="text-[10px] uppercase tracking-wider text-slate-500 font-semibold">Open P&L</p>
          <p className={`font-mono text-2xl font-bold tabular-nums mt-1 ${view.displayUpnl >= 0 ? "text-profit-400" : "text-loss-400"}`}>{view.displayUpnl >= 0 ? "+" : ""}{inr(view.displayUpnl)}</p>
          <p className="text-[11px] text-slate-500 mt-0.5">{view.openPnlSub}</p>
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
          <Row
            k="Margin usage"
            v={view.brokerUtilizedMargin != null ? `${inr(view.brokerUtilizedMargin)} used` : "—"}
            sub={view.brokerUtilizedMargin == null}
            live={view.brokerUtilizedMargin != null}
          />
          <Row k="Correlation matrix" v="—" sub />
          <p className="text-[9px] text-slate-600 mt-3 leading-relaxed">
            {view.isBrokerTruth
              ? `Heuristic = exposure×0.55 + concentration×0.45. Margin usage is broker-truth (${view.brokerName || "live"} snapshot).`
              : "Heuristic = exposure×0.55 + concentration×0.45. Margin/correlation render once broker margin endpoints return data."}
          </p>
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

function Row({ k, v, sub, live }) {
  return (
    <div className="flex justify-between items-center py-1.5 border-b border-slate-800/40 last:border-0">
      <span className="text-[11px] text-slate-400">{k}</span>
      <span className={`text-[11px] font-mono ${live ? "text-cyan-400" : "text-slate-200"}`}>
        {v}
        {sub && <span className="ml-1 text-[9px] text-slate-600">(pending)</span>}
      </span>
    </div>
  );
}