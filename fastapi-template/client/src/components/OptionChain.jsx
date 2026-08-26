import React, { useCallback, useEffect, useRef, useState } from "react";
import { Activity, Layers3, Loader2, Radio } from "lucide-react";
import { API_BASE, getWsUrl } from "../config";

const INDEX_TABS = [
  { id: "NIFTY50", label: "NIFTY", step: 50 },
  { id: "BANKNIFTY", label: "BANKNIFTY", step: 100 },
  { id: "FINNIFTY", label: "FINNIFTY", step: 50 },
  { id: "SENSEX", label: "SENSEX", step: 100 },
];

const fmt = (v, d = 2) =>
  v == null || isNaN(v) ? "–" : Number(v).toLocaleString("en-IN", { maximumFractionDigits: d });

function OptionChain({ symbol = "NIFTY50" }) {
  const [activeSymbol, setActiveSymbol] = useState(symbol);
  const [chain, setChain] = useState(null);
  const [expiry, setExpiry] = useState("");
  const [loading, setLoading] = useState(true);
  const [wsState, setWsState] = useState("connecting"); // connecting | live | polling | error
  const prevLtpRef = useRef({});
  const wsRef = useRef(null);
  const pollRef = useRef(null);

  const loadRest = useCallback(async () => {
    try {
      const q = new URLSearchParams({ symbol: activeSymbol, levels: "7" });
      if (expiry) q.set("expiry", expiry);
      const res = await fetch(`${API_BASE}/api/optionchain?${q}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setChain(data);
      if (!expiry && data.expiry) setExpiry(data.expiry);
    } finally {
      setLoading(false);
    }
  }, [activeSymbol, expiry]);

  // First paint + expiry/symbol switches (REST snapshot)
  useEffect(() => {
    setLoading(true);
    loadRest();
  }, [loadRest]);

  // Live stream: WebSocket w/ auto-reconnect, falls back to 2s polling
  useEffect(() => {
    let disposed = false;
    let retry = 0;
    let wsTimer;

    const startPolling = () => {
      if (disposed || pollRef.current) return;
      setWsState("polling");
      pollRef.current = setInterval(() => loadRest().catch(() => {}), 2000);
    };
    const stopPolling = () => {
      if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
    };

    const connect = () => {
      if (disposed) return;
      const qp = expiry ? `?expiry=${expiry}` : "";
      const ws = new WebSocket(getWsUrl(`/ws/optionchain/${activeSymbol}${qp}`));
      wsRef.current = ws;
      ws.onopen = () => { retry = 0; stopPolling(); setWsState("live"); };
      ws.onmessage = (e) => {
        try {
          const d = JSON.parse(e.data);
          setChain(d);
        } catch { /* partial frame */ }
      };
      ws.onclose = () => {
        if (disposed) return;
        retry += 1;
        if (retry >= 3) startPolling();
        wsTimer = setTimeout(connect, Math.min(8000, 800 * retry));
      };
      ws.onerror = () => ws.close();
    };

    connect();
    return () => {
      disposed = true;
      clearTimeout(wsTimer);
      stopPolling();
      if (wsRef.current) { wsRef.current.onclose = null; wsRef.current.close(); }
    };
  }, [activeSymbol, expiry]);

  const rows = chain?.rows || [];
  const maxOi = Math.max(1, ...rows.map((r) => Math.max(r.CE.oi, r.PE.oi)));
  const spot = chain?.underlying_spot;
  const pcr = chain?.pcr?.oi ?? 0;
  const pcrTone = pcr >= 1.05
    ? "text-emerald-400 bg-emerald-500/10 border-emerald-500/30"
    : pcr <= 0.75 ? "text-rose-400 bg-rose-500/10 border-rose-500/30"
    : "text-amber-400 bg-amber-500/10 border-amber-500/30";

  return (
    <div className="glass-panel rounded-2xl border border-slate-800/80 shadow-glass-md overflow-hidden">
      <div className="flex flex-wrap items-center justify-between gap-2 px-4 py-3 border-b border-slate-800/70">
        <div className="flex items-center gap-2">
          <div className="p-1.5 rounded-lg bg-fuchsia-500/10 text-fuchsia-400 border border-fuchsia-500/25"><Layers3 size={16} /></div>
          <div>
            <h3 className="font-display font-bold text-sm text-white">Live Option Chain</h3>
            <span className="text-[10px] text-slate-400 font-mono">
              {chain ? `${chain.symbol} · ${chain.expiry_label} (${chain.days_to_expiry}d)` : activeSymbol}
            </span>
          </div>
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-bold border ${
            wsState === "live" ? "text-emerald-400 bg-emerald-500/10 border-emerald-500/30"
            : wsState === "polling" ? "text-amber-400 bg-amber-500/10 border-amber-500/30"
            : "text-slate-400 bg-slate-800 border-slate-700"}`}>
            {wsState === "live" ? <Radio size={9} className="animate-pulse" /> : <Activity size={9} />}
            {wsState.toUpperCase()}
          </span>
          {chain && (
            <span className={`px-2 py-0.5 rounded-full text-[10px] font-bold border ${pcrTone}`}>
              PCR {fmt(pcr, 3)}
            </span>
          )}
          {chain && (
            <span className="px-2 py-0.5 rounded-full text-[10px] font-bold bg-violet-500/10 border border-violet-500/30 text-violet-300">
              MaxPain {fmt(chain.max_pain, 0)}
            </span>
          )}
        </div>
      </div>


      {/* Index tabs + expiry selector */}
      <div className="flex flex-wrap items-center gap-2 px-4 py-2 border-b border-slate-800/60">
        <div className="flex bg-surface-950 p-1 rounded-xl border border-slate-800 text-[11px] font-bold">
          {INDEX_TABS.map((t) => (
            <button key={t.id}
              onClick={() => { setActiveSymbol(t.id); setExpiry(""); }}
              className={`px-2.5 py-1 rounded-lg transition-all ${
                activeSymbol === t.id ? "bg-brand-violet text-white" : "text-slate-400 hover:text-white"
              }`}
            >
              {t.label}
            </button>
          ))}
        </div>
        {chain?.expiries && (
          <select
            value={expiry}
            onChange={(e) => setExpiry(e.target.value)}
            className="select-field text-xs py-1.5 ml-auto max-w-[240px]"
          >
            {chain.expiries.map((e) => (
              <option key={e.date} value={e.date}>
                {e.label} ? {e.type} ? {e.days_to_expiry}d
              </option>
            ))}
          </select>
        )}
      </div>

      {/* Chain table */}
      {loading && !chain ? (
        <div className="flex items-center justify-center py-14 text-cyan-400 text-xs font-mono gap-2">
          <Loader2 size={14} className="animate-spin" /> Loading {activeSymbol} chain?
        </div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-[11px] font-mono tabular-nums min-w-[820px]">
            <thead>
              <tr className="bg-surface-900/80 text-sky-400 border-b border-slate-800">
                <th colSpan={5} className="py-1.5 text-center border-r border-slate-800">CALLS (CE)</th>
                <th className="py-1.5 text-center text-slate-300">STRIKE</th>
                <th colSpan={5} className="py-1.5 text-center text-amber-400 border-l border-slate-800">PUTS (PE)</th>
              </tr>
              <tr className="bg-surface-950/70 text-slate-500">
                {["OI", "Chg.OI", "Vol", "IV%", "LTP"].map((h) => (
                  <th key={`c${h}`} className="py-1 pr-3 text-right font-medium">{h}</th>
                ))}
                <th className="py-1 text-center">?</th>
                {["LTP", "IV%", "Vol", "Chg.OI", "OI"].map((h) => (
                  <th key={`p${h}`} className="py-1 pl-3 text-left font-medium">{h}</th>
                ))}
              </tr>
            </thead>

            <tbody>
              {rows.map((r) => {
                const ceItm = r.strike < spot;
                const peItm = r.strike > spot;
                const shade = (isItm) => (isItm ? " bg-sky-500/[0.045]" : "");
                return (
                  <tr key={r.strike}
                    className={`border-b border-slate-800/40 transition-colors ${
                      r.is_atm ? "bg-cyan-500/[0.07] ring-1 ring-inset ring-cyan-500/40" : "hover:bg-white/[0.02]"
                    }`}
                  >
                    <td className={`py-1.5 pr-2 text-right relative${shade(ceItm)}`}>
                      <span
                        className="absolute left-1 top-1/2 -translate-y-1/2 h-[5px] rounded bg-sky-500/35"
                        style={{ width: `${Math.max(4, (r.CE.oi / maxOi) * 52)}%` }}
                      />
                      <span className="relative">{fmt(r.CE.oi)}</span>
                    </td>
                    <td className={`pr-3 text-right${shade(ceItm)} ${r.CE.chg_oi >= 0 ? "text-emerald-400" : "text-rose-400"}`}>
                      {r.CE.chg_oi >= 0 ? "+" : ""}{fmt(r.CE.chg_oi)}
                    </td>
                    <td className={`pr-3 text-right text-slate-300${shade(ceItm)}`}>{fmt(r.CE.volume)}</td>
                    <td className={`pr-3 text-right text-fuchsia-300${shade(ceItm)}`}>{fmt(r.CE.iv_pct)}</td>
                    <td className={`pr-3 text-right font-bold${shade(ceItm)} ${flashCls("CE", r.strike, r.CE.ltp)}`}>
                      {fmt(r.CE.ltp)}
                      <span className="ml-1 text-[9px] text-slate-500 font-normal">?{fmt(r.CE.delta)}</span>
                    </td>

                    <td className={`py-1.5 text-center font-bold ${r.is_atm ? "text-cyan-300" : "text-white"}`}>
                      {fmt(r.strike, 0)}
                      {r.is_atm && <div className="text-[8px] text-cyan-500 tracking-widest">ATM</div>}
                    </td>

                    <td className={`py-1.5 pl-3 text-left font-bold${shade(peItm)} ${flashCls("PE", r.strike, r.PE.ltp)}`}>
                      {fmt(r.PE.ltp)}
                      <span className="ml-1 text-[9px] text-slate-500 font-normal">?{fmt(r.PE.delta)}</span>
                    </td>
                    <td className={`pl-3 text-left text-fuchsia-300${shade(peItm)}`}>{fmt(r.PE.iv_pct)}</td>
                    <td className={`pl-3 text-left text-slate-300${shade(peItm)}`}>{fmt(r.PE.volume)}</td>
                    <td className={`pl-3 text-left${shade(peItm)} ${r.PE.chg_oi >= 0 ? "text-emerald-400" : "text-rose-400"}`}>
                      {r.PE.chg_oi >= 0 ? "+" : ""}{fmt(r.PE.chg_oi)}
                    </td>
                    <td className="py-1.5 pl-2 text-left relative${shade(peItm)}">
                      <span
                        className="absolute right-1 top-1/2 -translate-y-1/2 h-[5px] rounded bg-amber-500/35"
                        style={{ width: `${Math.max(4, (r.PE.oi / maxOi) * 52)}%` }}
                      />
                      <span className="relative">{fmt(r.PE.oi)}</span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {chain && (
        <div className="px-4 py-2 border-t border-slate-800/60 flex flex-wrap gap-x-5 gap-y-1 text-[10px] font-mono text-slate-500">
          <span>Spot <strong className="text-white">{fmt(spot)}</strong></span>
          <span>CE OI <strong className="text-sky-400">{fmt(chain.totals.ce_oi)}</strong></span>
          <span>PE OI <strong className="text-amber-400">{fmt(chain.totals.pe_oi)}</strong></span>
          <span>CE IVavg <strong>{chain.totals.ce_iv_avg}%</strong></span>
          <span>PE IVavg <strong>{chain.totals.pe_iv_avg}%</strong></span>
          <span>Lot <strong>{chain.contract_lot}</strong></span>
          <span className="ml-auto opacity-70">{chain.data_source}</span>
        </div>
      )}
    </div>
  );
}

export default OptionChain;
