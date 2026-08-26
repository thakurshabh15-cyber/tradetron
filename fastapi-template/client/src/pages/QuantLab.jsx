import { useState } from "react";
import {
  FlaskConical, Sparkles, Stethoscope, Loader2, AlertTriangle,
  CheckCircle2, Info, XCircle, ArrowRight,
} from "lucide-react";
import RobustnessGauge from "../components/RobustnessGauge";
import EquityCurve from "../components/EquityCurve";
import { authFetch } from "../services/apiClient";
import { useToast } from "../components/Toast";

const EXAMPLES = [
  "Buy NIFTY when RSI(14) drops below 35 on 15min, stop loss 0.5%, target 1%, 2 lots",
  "Sell BANKNIFTY when RSI goes above 70 hourly, sl 1%, target 2%, 500 qty",
  "buy RELIANCE when SMA(50) rises above price daily, 250 shares, target 3%",
];

const sevStyle = {
  critical: { icon: XCircle, cls: "border-rose-500/40 bg-rose-500/10 text-rose-300" },
  warning: { icon: AlertTriangle, cls: "border-amber-500/40 bg-amber-500/10 text-amber-300" },
  info: { icon: Info, cls: "border-sky-500/40 bg-sky-500/10 text-sky-300" },
  pass: { icon: CheckCircle2, cls: "border-emerald-500/30 bg-emerald-500/[0.07] text-emerald-300" },
};

export default function QuantLab() {
  const toast = useToast();
  const [text, setText] = useState(EXAMPLES[0]);
  const [parsed, setParsed] = useState(null);
  const [report, setReport] = useState(null);
  const [busy, setBusy] = useState("");        // "", "parse", "analyze"
  const [error, setError] = useState("");

  const post = async (path, body) => {
    const res = await authFetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `${res.status} request failed`);
    return data;
  };

  const runParse = async () => {
    setBusy("parse"); setError(""); setReport(null);
    try {
      const p = await post("/api/quant-lab/parse", { text });
      setParsed(p);
      toast.success("Strategy parsed", { description: "Structure understood. You can now analyze health." });
    } catch (e) { setError(e.message); toast.error("Parsing failed", { description: e.message }); }
    finally { setBusy(""); }
  };

  const runAnalyze = async () => {
    setBusy("analyze"); setError("");
    try {
      const { parsed: p, health_report: hr } = await post("/api/quant-lab/analyze", {
        text, days: 90,
      });
      setParsed(p); setReport(hr);
      toast.success("Health analysis ready", {
        description: hr?.verdict || "Robustness report generated from a truthful net-cost backtest.",
      });
    } catch (e) { setError(e.message); toast.error("Analysis failed", { description: e.message }); }
    finally { setBusy(""); }
  };

  const confidencePct = parsed ? Math.round((parsed.confidence || 0) * 100) : 0;

  return (
    <div className="mx-auto max-w-6xl space-y-6 p-4 md:p-6">
      {/* ── Header ── */}
      <div className="flex items-center gap-3">
        <div className="flex h-11 w-11 items-center justify-center rounded-xl bg-gradient-to-br from-violet-600 to-fuchsia-600 shadow-lg shadow-violet-600/25">
          <FlaskConical size={22} className="text-white" />
        </div>
        <div>
          <h1 className="font-display text-xl font-bold text-white">AI Quant Lab</h1>
          <p className="text-xs text-slate-400">
            Describe a strategy in plain English — TradeThrone parses it, backtests it truthfully and grades its robustness.
          </p>
        </div>
      </div>

      {/* ── NL Input ── */}
      <section className="rounded-2xl border border-slate-800 bg-surface-900/70 p-4 shadow-lg shadow-black/20">
        <label htmlFor="nl-strategy" className="mb-2 block text-[11px] font-semibold uppercase tracking-wider text-slate-400">
          Strategy Description
        </label>
        <textarea
          id="nl-strategy"
          value={text}
          onChange={(e) => setText(e.target.value)}
          rows={3}
          placeholder="e.g. Buy NIFTY when RSI(14) drops below 35 on 15min, stop loss 0.5%, target 1%, 2 lots"
          className="w-full resize-y rounded-xl border border-slate-700 bg-surface-950/80 px-3 py-2.5 font-mono text-sm text-slate-200 placeholder:text-slate-600 focus:border-violet-500 focus:outline-none focus:ring-1 focus:ring-violet-500/50"
        />
        <div className="mt-2 flex flex-wrap items-center gap-2">
          {EXAMPLES.map((ex, i) => (
            <button key={i} onClick={() => setText(ex)}
              className="rounded-full border border-slate-700 bg-surface-800/60 px-2.5 py-1 text-[11px] text-slate-400 transition-colors hover:border-violet-500/50 hover:text-violet-300">
              Example {i + 1}
            </button>
          ))}
        </div>
        <div className="mt-3 flex flex-wrap gap-2">
          <button onClick={runParse} disabled={!!busy || !text.trim()}
            className="inline-flex items-center gap-2 rounded-lg border border-slate-600 bg-surface-800 px-3.5 py-2 text-xs font-semibold text-slate-200 transition-all hover:bg-surface-700 disabled:opacity-40 active:scale-95">
            {busy === "parse" ? <Loader2 size={14} className="animate-spin" /> : <Sparkles size={14} />}
            Parse Only
          </button>
          <button onClick={runAnalyze} disabled={!!busy || !text.trim()}
            className="btn-primary inline-flex items-center gap-2 text-xs disabled:opacity-40">
            {busy === "analyze" ? <Loader2 size={14} className="animate-spin" /> : <Stethoscope size={14} />}
            Parse + Run AI Doctor
          </button>
        </div>
        {error && (
          <p className="mt-3 flex items-center gap-1.5 rounded-lg border border-rose-500/40 bg-rose-500/10 px-3 py-2 text-xs text-rose-300">
            <AlertTriangle size={13} /> {error}
          </p>
        )}
      </section>

      {/* ── Parsed Strategy Card ── */}
      {parsed && (
        <section className="rounded-2xl border border-slate-800 bg-surface-900/70 p-4">
          <div className="mb-3 flex items-center justify-between">
            <h2 className="flex items-center gap-2 text-sm font-bold text-white">
              <Sparkles size={15} className="text-violet-400" /> Parsed Strategy
            </h2>
            <span className={`rounded-full px-2 py-0.5 font-mono text-[11px] font-bold ${confidencePct >= 80 ? "bg-emerald-500/15 text-emerald-300" : confidencePct >= 50 ? "bg-amber-500/15 text-amber-300" : "bg-rose-500/15 text-rose-300"}`}>
              confidence {confidencePct}%
            </span>
          </div>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            {[
              ["Symbol", (parsed.symbols || []).join(", ") || "—"],
              ["Timeframe", parsed.timeframe || "—"],
              ["Direction", parsed.action?.side || "—"],
              ["Quantity", `${parsed.action?.quantity ?? "—"}${(parsed.symbols?.[0] && !parsed.unparsed.includes("missing:symbol")) ? "" : ""}`],
            ].map(([k, v]) => (
              <div key={k} className="rounded-xl border border-slate-800 bg-surface-950/60 p-2.5">
                <p className="text-[10px] uppercase tracking-wider text-slate-500">{k}</p>
                <p className="mt-0.5 truncate font-mono text-sm font-bold text-cyan-300">{v}</p>
              </div>
            ))}
          </div>
          {!!parsed.conditions?.length && (
            <div className="mt-3 space-y-1.5">
              {parsed.conditions.map((c, i) => (
                <p key={i} className="rounded-lg bg-surface-950/80 px-3 py-1.5 font-mono text-[12px] text-slate-300">
                  WHEN <span className="text-violet-300">{c.indicator}</span>({c.period}) {" "}
                  <span className="text-amber-300">{c.operator}</span> {" "}
                  <span className="text-emerald-300">{c.value}</span>
                </p>
              ))}
            </div>
          )}
        </section>
      )}

      {/* ── AI Doctor Health Report ── */}
      {report && (
        <section className="rounded-2xl border border-slate-800 bg-surface-900/70 p-4">
          {!report.error ? (
            <>
              <div className="flex flex-col items-center gap-5 sm:flex-row sm:items-stretch">
                <div className="flex flex-col items-center justify-center rounded-xl border border-slate-800 bg-surface-950/60 p-4">
                  <RobustnessGauge score={report.robustness_score} grade={report.grade} />
                  <p className="mt-2 max-w-[220px] text-center text-xs text-slate-400">{report.verdict}</p>
                </div>
                <div className="grid flex-1 content-start gap-2">
                  {Object.entries(report.components).map(([key, c]) => {
                    const pct = Math.round((c.score / c.weight) * 100);
                    const bar = pct >= 75 ? "bg-emerald-400" : pct >= 40 ? "bg-amber-400" : "bg-rose-400";
                    return (
                      <div key={key}>
                        <div className="mb-0.5 flex items-center justify-between text-[11px]">
                          <span className="font-semibold capitalize text-slate-300">{key.replace(/_/g, " ")}</span>
                          <span className="font-mono text-slate-500">{c.score}/{c.weight} · {c.detail}</span>
                        </div>
                        <div className="h-1.5 w-full overflow-hidden rounded-full bg-slate-800">
                          <div className={`h-full rounded-full ${bar} transition-all duration-500`} style={{ width: `${Math.max(pct, 2)}%` }} />
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>

              {/* Equity curve from truthful backtest */}
              <div className="mt-4 rounded-xl border border-slate-800 bg-surface-950/60 p-3">
                <p className="mb-1 text-[11px] font-semibold uppercase tracking-wider text-slate-500">Equity Curve (net of all statutory charges)</p>
                <EquityCurve data={report.base_report?.equity_curve || []} />
              </div>

              {/* Findings */}
              <div className="mt-4 space-y-2">
                <p className="text-[11px] font-semibold uppercase tracking-wider text-slate-500">Doctor's Findings</p>
                {(report.findings || []).map((f, i) => {
                  const s = sevStyle[f.severity] || sevStyle.pass;
                  const Icon = s.icon;
                  return (
                    <div key={i} className={`flex items-start gap-2 rounded-xl border px-3 py-2 text-xs ${s.cls}`}>
                      <Icon size={14} className="mt-0.5 shrink-0" />
                      <div>
                        <p className="font-semibold">{f.title}</p>
                        {f.recommendation && <p className="mt-0.5 opacity-80">→ {f.recommendation}</p>}
                      </div>
                    </div>
                  );
                })}
              </div>
            </>
          ) : (
            <p className="flex items-center gap-2 text-sm text-rose-300"><AlertTriangle size={15} /> {report.error}</p>
          )}
        </section>
      )}

      <footer className="flex items-center gap-1.5 pb-4 text-[11px] text-slate-600">
        <ArrowRight size={11} /> Scores use deterministic seeded data and exact Indian statutory charges — treat as relative health, not a profit promise.
      </footer>
    </div>
  );
}