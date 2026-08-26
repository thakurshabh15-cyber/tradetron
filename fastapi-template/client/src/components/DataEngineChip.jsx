import { useEffect, useState } from "react";
import { Database } from "lucide-react";
import { API_BASE } from "../config";

// Truthful data-engine indicator — reflects ACTUAL backend feed mode.
// Never claims LIVE unless the engine itself reports live broker routing.
export default function DataEngineChip() {
  const [state, setState] = useState({ mode: null, engine: null, err: false });

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const res = await fetch(`${API_BASE}/api/health`, { cache: "no-store" });
        if (!res.ok) throw new Error(String(res.status));
        const d = await res.json();
        if (alive) setState({ mode: d.broker_mode, engine: d.engine_running, err: false });
      } catch {
        if (alive) setState((s) => ({ ...s, err: true }));
      }
    };
    load();
    const t = setInterval(load, 30000);
    return () => { alive = false; clearInterval(t); };
  }, []);

  const isLive = state.mode === "live";
  const tone = state.err
    ? "border-slate-700 bg-surface-900/80 text-slate-500"
    : isLive
    ? "border-rose-500/40 bg-rose-500/10 text-rose-300"
    : "border-amber-500/30 bg-amber-500/[0.07] text-amber-300";
  const dot = state.err ? "bg-slate-500" : isLive ? "bg-rose-400 animate-pulse" : "bg-amber-400";
  const label = state.err ? "DATA · OFFLINE" : isLive ? "DATA · LIVE" : "DATA · PAPER";

  return (
    <div
      className={`pointer-events-none fixed bottom-[76px] left-3 z-30 flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[9px] font-bold tracking-widest backdrop-blur-md md:bottom-4 md:left-56 ${tone}`}
      title={
        state.err
          ? "Data engine unreachable"
          : `Broker mode: ${state.mode ?? "unknown"} · Engine: ${state.engine ? "RUNNING" : "STOPPED"} — labels reflect actual backend state`
      }
    >
      <Database size={9} />
      <span className={`h-1.5 w-1.5 rounded-full ${dot}`} />
      {label}
    </div>
  );
}
