import { useMemo } from "react";
import { NotebookPen } from "lucide-react";
import { useApi } from "../hooks/useApi";

const inr = (v) => `${Number(v || 0) >= 0 ? "" : "-"}₹${Math.abs(Number(v || 0)).toLocaleString("en-IN", { maximumFractionDigits: 2 })}`;

export default function TradeJournal() {
  const { data: trades, loading } = useApi("/api/trades?limit=300");

  // Journal covers CLOSED trades only (exit recorded). Open fills stay in history.
  const closed = useMemo(
    () => (trades || []).filter((t) => t.exit_reason || (t.exit_price != null && t.pnl != null)),
    [trades]
  );

  const stats = useMemo(() => {
    const wins = closed.filter((t) => (t.pnl || 0) > 0);
    const losses = closed.filter((t) => (t.pnl || 0) < 0);
    const sumW = wins.reduce((a, t) => a + t.pnl, 0);
    const sumL = Math.abs(losses.reduce((a, t) => a + t.pnl, 0));
    const byReason = {};
    for (const t of closed) {
      const r = t.exit_reason || "unspecified";
      byReason[r] = byReason[r] || { n: 0, pnl: 0 };
      byReason[r].n += 1;
      byReason[r].pnl += t.pnl || 0;
    }
    return {
      n: closed.length,
      winRate: closed.length ? Math.round((wins.length / closed.length) * 100) : 0,
      pf: sumL > 0 ? +(sumW / sumL).toFixed(2) : null,
      avgWin: wins.length ? sumW / wins.length : 0,
      avgLoss: losses.length ? -sumL / losses.length : 0,
      bestReason: Object.entries(byReason).sort((a, b) => b[1].pnl - a[1].pnl)[0],
      worstReason: Object.entries(byReason).sort((a, b) => a[1].pnl - b[1].pnl)[0],
    };
  }, [closed]);

  return (
    <div className="space-y-5">
      <div className="flex items-center gap-2">
        <NotebookPen size={20} className="text-brand-electric" />
        <h1 className="font-display text-xl font-bold text-white">AI Trade Journal</h1>
      </div>
      <p className="text-xs text-slate-500 max-w-2xl">
        Evidence-based analysis computed strictly from your recorded closed trades. No invented observations — every claim below links to the aggregate shown.
      </p>

      {/* Stats strip */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <Stat label="Closed Trades" value={stats.n} />
        <Stat label="Win Rate" value={`${stats.winRate}%`} />
        <Stat label="Profit Factor" value={stats.pf ?? "—"} tone={stats.pf ? (stats.pf >= 1.5 ? "text-profit-400" : stats.pf < 1 ? "text-loss-400" : "") : "text-slate-500"} />
        <Stat label="Avg Win / Loss" value={`${inr(stats.avgWin)} / ${inr(stats.avgLoss)}`} small />
      </div>

      {/* Evidence-based insights */}
      {stats.bestReason && (
        <div className="glass-panel rounded-xl p-4 border border-profit-500/20 text-xs space-y-1">
          <p className="font-bold text-white flex items-center gap-2"><NotebookPen size={12} className="text-brand-electric" /> PATTERN DETECTED</p>
          <p className="text-slate-300">
            Across your last {stats.n} closed trades, exits tagged <strong className="text-white">“{stats.bestReason[0]}”</strong> are your most profitable
            ({stats.bestReason[1].n} trades, {inr(stats.bestReason[1].pnl)}), while <strong className="text-white">“{stats.worstReason[0]}”</strong> exits lost
            ({inr(stats.worstReason[1].pnl)} over {stats.worstReason[1].n} trades).
          </p>
          <p className="text-[10px] text-slate-600">Historical behaviour only — not predictive of future results.</p>
        </div>
      )}

      {/* Journal table */}
      <div className="glass-panel rounded-2xl border border-edge overflow-hidden">
        {loading ? <p className="py-10 text-center text-xs text-slate-500">Loading journal…</p> : closed.length === 0 ? (
          <p className="py-10 text-center text-xs text-slate-500">No closed trades yet — journal entries appear after strategy or DMA positions close.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-[11px] font-mono tabular-nums min-w-[720px]">
              <thead>
                <tr className="bg-surface-900/80 text-slate-500 border-b border-slate-800">
                  {["Timestamp", "Strategy", "Symbol", "Side", "Qty", "Entry", "Exit", "P&L", "Exit Reason"].map((h) => (
                    <th key={h} className={`py-2 px-3 font-medium ${["Qty", "Entry", "Exit", "P&L"].includes(h) ? "text-right" : "text-left"}`}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {closed.map((t) => (
                  <tr key={t.id} className="border-b border-slate-800/40 hover:bg-white/[0.02]">
                    <td className="py-1.5 px-3 text-slate-400">{new Date(t.executed_at).toLocaleString("en-IN")}</td>
                    <td className="px-3 text-slate-300">{t.strategy_name || "—"}</td>
                    <td className="px-3 text-white font-bold">{t.symbol}</td>
                    <td className={`px-3 ${t.side === "BUY" ? "text-emerald-400" : "text-rose-400"}`}>{t.side}</td>
                    <td className="px-3 text-right text-slate-200">{t.quantity}</td>
                    <td className="px-3 text-right text-slate-400">{fmtN(t.entry_price)}</td>
                    <td className="px-3 text-right text-slate-400">{fmtN(t.exit_price)}</td>
                    <td className={`px-3 text-right font-bold ${(t.pnl || 0) >= 0 ? "text-profit-400" : "text-loss-400"}`}>{inr(t.pnl)}</td>
                    <td className="px-3 text-slate-500">{t.exit_reason || "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}

function Stat({ label, value, tone = "text-white", small }) {
  return (
    <div className="glass-panel rounded-xl p-3.5 border border-edge">
      <p className="text-[10px] uppercase tracking-wider text-slate-500 font-semibold">{label}</p>
      <p className={`font-mono text-lg font-bold mt-1 tabular-nums ${tone}`}>{value}</p>
      {!small && <p className="text-[10px] text-slate-600 mt-0.5">computed from closed ledger</p>}
    </div>
  );
}

function fmtN(v) { return v == null ? "—" : Number(v).toFixed(2); }
