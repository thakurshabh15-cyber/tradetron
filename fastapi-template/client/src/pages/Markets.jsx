import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Globe2, Search } from "lucide-react";
import MarketTicker from "../components/MarketTicker";
import { API_BASE } from "../config";

const UNIVERSE = {
  ALL: ["NIFTY50", "BANKNIFTY", "RELIANCE", "TCS", "CRUDEOIL", "GOLD", "BTCUSDT", "ETHUSDT", "EURUSD"],
  EQUITIES: ["RELIANCE", "TCS", "INFY", "HDFCBANK"],
  FNO: ["NIFTY50", "BANKNIFTY", "FINNIFTY"],
  MCX: ["CRUDEOIL", "GOLD", "SILVER"],
  CRYPTO: ["BTCUSDT", "ETHUSDT", "SOLUSDT"],
  "FOREX/CDS": ["EURUSD", "USDINR"],
};
const TABS = ["ALL", "EQUITIES", "FNO", "MCX", "CRYPTO", "FOREX/CDS"];

export default function Markets() {
  const navigate = useNavigate();
  const [tab, setTab] = useState("ALL");
  const [q, setQ] = useState("");
  const [results, setResults] = useState([]);

  const symbols = useMemo(() => {
    const base = UNIVERSE[tab] || UNIVERSE.ALL;
    if (!q.trim()) return base;
    return base.filter((s) => s.includes(q.toUpperCase()));
  }, [tab, q]);

  // Debounced global instrument search beyond the curated universe
  useEffect(() => {
    if (q.trim().length < 2) { setResults([]); return; }
    const t = setTimeout(async () => {
      try {
        const res = await fetch(`${API_BASE}/api/market-data/instruments/search?q=${encodeURIComponent(q)}&limit=8`);
        if (res.ok) setResults((await res.json()).instruments || []);
      } catch { /* master offline ? curated universe still renders */ }
    }, 250);
    return () => clearTimeout(t);
  }, [q]);

  return (
    <div className="space-y-5">
      <div className="flex items-center gap-2">
        <Globe2 size={20} className="text-brand-electric" />
        <h1 className="font-display text-xl font-bold text-white">Markets</h1>
      </div>

      <div className="relative">
        <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-500" />
        <input value={q} onChange={(e) => setQ(e.target.value)}
          placeholder="Search NIFTY · RELIANCE · BTCUSDT · any master instrument…"
          className="input-field pl-9" />
        {results.length > 0 && (
          <div className="absolute z-20 mt-1 w-full rounded-xl border border-slate-700 bg-surface-900 shadow-glass-lg overflow-hidden">
            {results.map((i) => (
              <button key={i.symbol} onClick={() => navigate(`/markets/${i.symbol}`)}
                className="flex w-full items-center justify-between px-3 py-2 text-xs hover:bg-surface-800">
                <span className="font-bold text-white">{i.symbol}</span>
                <span className="text-slate-500">{i.exchange || i.instrument_type || "instrument"}</span>
              </button>
            ))}
          </div>
        )}
      </div>

      <div className="flex flex-wrap gap-1.5">
        {TABS.map((t) => (
          <button key={t} onClick={() => setTab(t)}
            className={`px-3 py-1.5 rounded-xl text-xs font-bold transition-all border ${
              tab === t ? "bg-brand-purple/15 text-brand-purple border-brand-purple/30" : "bg-surface-900 border-slate-800 text-slate-400 hover:text-white"
            }`}>
            {t}
          </button>
        ))}
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-3">
        {symbols.map((sym) => (
          <MarketTicker key={sym} symbol={sym} isSelected={false}
            onSelect={(s) => navigate(`/markets/${s}`)} />
        ))}
      </div>
      <p className="text-[10px] text-slate-600 font-mono">Click any card to open the instrument workspace · live unified tape</p>
    </div>
  );
}
