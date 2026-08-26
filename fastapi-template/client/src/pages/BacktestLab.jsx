import { useState } from "react";
import {
  LineChart, Play, Loader2, AlertTriangle,
} from "lucide-react";
import EquityCurve from "../components/EquityCurve";
import { authFetch } from "../services/apiClient";
import { useToast } from "../components/Toast";

const SYMBOLS = ["NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX", "RELIANCE", "TCS", "INFY", "HDFCBANK"];
const TIMEFRAMES = ["1m", "5m", "15m", "30m", "1h", "1d"];

export default function BacktestLab() {
  const toast = useToast();
  const [form, setForm] = useState({
    symbol: "NIFTY", timeframe: "15m", days: 30,
    side: "BUY", quantity: 65, capital: 100000,
    stop_loss_pct: 0.5, take_profit_pct: 1.0, slippage_pct: "", seed: "",
  });
  const [report, setReport] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const set = (k) => (e) =>
    setForm((f) => ({ ...f, [k]: e.target.value }));

  const run = async () => {
    setBusy(true); setError("");
    try {
      const res = await authFetch("/api/backtest/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          symbol: form.symbol,
          conditions: [{ indicator: "RSI", operator: "lt", value: 45, period: 14 }],
          side: form.side,
          quantity: Number(form.quantity),
          timeframe: form.timeframe,
          days: Number(form.days),
          capital: Number(form.capital),
          stop_loss_pct: form.stop_loss_pct ? Number(form.stop_loss_pct) : null,
          take_profit_pct: form.take_profit_pct ? Number(form.take_profit_pct) : null,
          slippage_pct: form.slippage_pct ? Number(form.slippage_pct) : null,
          seed: form.seed === "" ? null : Number(form.seed),
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || `${res.status} failed`);
      setReport(data);
      toast.success("Backtest complete", {
        description: `${data.metrics?.total_trades ?? 0} trades · net ${data.metrics?.total_pnl ? "₹" + Number(data.metrics.total_pnl).toLocaleString("en-IN") : "—"}`,
      });
    } catch (e) {
      setError(e.message);
      toast.error("Backtest failed", { description: e.message });
    } finally {
      setBusy(false);
    }
  };

  const m = report?.metrics || {};

  return (
    <div className="mx-auto max-w-6xl space-y-6 p-4 md:p-6">
      <div className="flex items-center gap-3">
        <div className="flex h-11 w-11 items-center justify-center rounded-xl bg-gradient-to-br from-cyan-600 to-emerald-600 shadow-lg shadow-emerald-600/25">
          <LineChart size={22} className="text-white" />
        </div>
        <div>
          <h1 className="font-display text-xl font-bold text-white">Truthful Backtesting Lab</h1>
          <p className="text-xs text-slate-400">Exact brokerage · STT · GST · stamp duty · SEBI fee · slippage — every number is NET.</p>
        </div>
      </div>

      {/* ── Controls ── */}
      <section className="rounded-2xl border border-slate-800 bg-surface-900/70 p-4">
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
          <Field label="Symbol">
            <select value={form.symbol} onChange={set("symbol")} className="input-dark">{SYMBOLS.map((s) => <option key={s}>{s}</option>)}</select>
          </Field>
          <Field label="Timeframe">
            <select value={form.timeframe} onChange={set("timeframe")} className="input-dark">{TIMEFRAMES.map((t) => <option key={t}>{t}</option>)}</select>
          </Field>
          <Field label="Side">
            <select value={form.side} onChange={set("side")} className="input-dark"><option>BUY</option><option>SELL</option></select>
          </Field>
          <Field label="Quantity"><input type="number" min="1" value={form.quantity} onChange={set("quantity")} className="input-dark" /></Field>
          <Field label="Days"><input type="number" min="1" max="365" value={form.days} onChange={set("days")} className="input-dark" /></Field>
          <Field label="Capital ₹"><input type="number" min="1000" step="1000" value={form.capital} onChange={set("capital")} className="input-dark" /></Field>
          <Field label="Stop-Loss %"><input type="number" step="0.1" min="0.05" value={form.stop_loss_pct} onChange={set("stop_loss_pct")} className="input-dark" /></Field>
          <Field label="Target %"><input type="number" step="0.1" min="0.05" value={form.take_profit_pct} onChange={set("take_profit_pct")} className="input-dark" /></Field>
          <Field label="Slippage %"><input type="number" step="0.01" placeholder="0.1" value={form.slippage_pct} onChange={set("slippage_pct")} className="input-dark" /></Field>
          <Field label="Seed (replay)"><input type="number" placeholder="random" value={form.seed} onChange={set("seed")} className="input-dark" /></Field>
          <div className="col-span-2 flex items-end sm:col-span-3 lg:col-span-2">
            <button onClick={run} disabled={busy}
              className="btn-primary inline-flex w-full items-center justify-center gap-2 text-xs disabled:opacity-40">
              {busy ? <Loader2 size={14} className="animate-spin" /> : <Play size={14} />}
              {busy ? "Simulating…" : "Run Truthful Backtest"}
            </button>
          </div>
        </div>
        {error && (
          <p className="mt-3 flex items-center gap-1.5 rounded-lg border border-rose-500/40 bg-rose-500/10 px-3 py-2 text-xs text-rose-300">
            <AlertTriangle size={13} /> {error}
          </p>
        )}
      </section>

      {/* ── Results ── */}
      {report && !report.error && m.total_trades !== undefined && (
        <>
          <section className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
            <Metric label="Net P&L" value={`₹${Number(m.net_pnl).toLocaleString("en-IN")}`} tone={m.net_pnl >= 0 ? "good" : "bad"} />
            <Metric label="Win Rate" value={`${m.win_rate_pct}%`} sub={`${m.wins}W / ${m.losses}L`} />
            <Metric label="Profit Factor" value={m.profit_factor} />
            <Metric label="Max Drawdown" value={`${m.max_drawdown_pct}%`} tone={m.max_drawdown_pct > 20 ? "bad" : undefined} />
            <Metric label="Total Charges" value={`₹${Number(m.total_charges).toLocaleString("en-IN")}`} sub={`${m.charges_as_pct_of_gross}% of gross`} />
            <Metric label="Sharpe (ann.)" value={m.sharpe_annualised} sub={`${m.total_trades} trades`} />
          </section>

          <section className="rounded-2xl border border-slate-800 bg-surface-900/70 p-4">
            <div className="mb-1 flex flex-wrap items-center justify-between gap-2 text-[11px] text-slate-500">
              <span className="font-semibold uppercase tracking-wider">Equity Curve — net of all statutory charges</span>
              <span className="font-mono">{report.symbol} · {report.timeframe} · {report.bars} bars · qty {report.quantity} ({report.lot_size}/lot){report.seed != null ? ` · seed ${report.seed}` : ""}</span>
            </div>
            {report.lot_warning && (
              <p className="mb-2 rounded-lg border border-amber-500/30 bg-amber-500/10 px-2.5 py-1.5 text-[11px] text-amber-300">⚠ {report.lot_warning}</p>
            )}
            <EquityCurve data={report.equity_curve} baseline={report.capital} />
          </section>

          <section className="grid gap-4 lg:grid-cols-2">
            {/* Statutory breakdown */}
            <div className="rounded-2xl border border-slate-800 bg-surface-900/70 p-4">
              <p className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-slate-500">Statutory Charges Breakdown</p>
              <table className="w-full text-xs">
                <tbody>
                  {Object.entries(report.charges_breakdown || {}).map(([k, v]) => (
                    <tr key={k} className="border-b border-slate-800/60 last:border-0">
                      <td className="py-1.5 capitalize text-slate-400">{k.replace(/_/g, " ")}</td>
                      <td className="py-1.5 text-right font-mono text-slate-200">₹{Number(v).toLocaleString("en-IN", { maximumFractionDigits: 2 })}</td>
                    </tr>
                  ))}
                  <tr>
                    <td className="pt-2 font-bold text-slate-300">Final Equity</td>
                    <td className="pt-2 text-right font-mono font-bold text-cyan-300">₹{Number(m.final_equity).toLocaleString("en-IN")}</td>
                  </tr>
                </tbody>
              </table>
            </div>

            {/* Recent trades */}
            <div className="rounded-2xl border border-slate-800 bg-surface-900/70 p-4">
              <p className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-slate-500">Trades ({m.total_trades}) — latest 12</p>
              <div className="max-h-64 overflow-y-auto">
                <table className="w-full text-xs">
                  <thead className="sticky top-0 bg-surface-900 text-[10px] uppercase tracking-wider text-slate-500">
                    <tr><th className="py-1 text-left">#</th><th className="text-left">Side</th><th className="text-right">Entry</th><th className="text-right">Exit</th><th className="text-left pl-2">Exit Why</th><th className="text-right">Net P&L</th></tr>
                  </thead>
                  <tbody>
                    {[...report.trades].reverse().slice(0, 12).map((t, i) => (
                      <tr key={t.entry_ts} className="border-t border-slate-800/60">
                        <td className="py-1 text-slate-500">{m.total_trades - i}</td>
                        <td className={t.side === "BUY" ? "text-emerald-400" : "text-rose-400"}>{t.side}</td>
                        <td className="text-right font-mono text-slate-400">{t.entry_price}</td>
                        <td className="text-right font-mono text-slate-400">{t.exit_price}</td>
                        <td className="pl-2 text-[10px] text-slate-500">{t.exit_reason}</td>
                        <td className={`text-right font-mono font-semibold ${t.net_pnl >= 0 ? "text-emerald-400" : "text-rose-400"}`}>
                          {t.net_pnl >= 0 ? "+" : ""}₹{Number(t.net_pnl).toLocaleString("en-IN")}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          </section>
        </>
      )}

      {report?.error && (
        <p className="flex items-center gap-2 rounded-xl border border-rose-500/40 bg-rose-500/10 px-3 py-2 text-sm text-rose-300">
          <AlertTriangle size={15} /> {report.error}
        </p>
      )}
    </div>
  );
}

function Field({ label, children }) {
  return (
    <label className="block">
      <span className="mb-1 block text-[10px] font-semibold uppercase tracking-wider text-slate-500">{label}</span>
      {children}
    </label>
  );
}

function Metric({ label, value, sub, tone }) {
  const color = tone === "good" ? "text-emerald-400" : tone === "bad" ? "text-rose-400" : "text-white";
  return (
    <div className="rounded-xl border border-slate-800 bg-surface-950/70 p-3">
      <p className="text-[10px] uppercase tracking-wider text-slate-500">{label}</p>
      <p className={`mt-0.5 font-mono text-base font-bold ${color}`}>{value}</p>
      {sub && <p className="text-[10px] text-slate-500">{sub}</p>}
    </div>
  );
}