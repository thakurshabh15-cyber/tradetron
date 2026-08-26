import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { CornerDownLeft, Search } from "lucide-react";
import { API_BASE } from "../config";

const GROUPS = [
  {
    section: "COMMAND",
    items: [{ label: "Open Command Center", to: "/" }],
  },
  {
    section: "BUILD",
    items: [
      { label: "Strategies", to: "/strategies" },
      { label: "AI Quant Lab", to: "/quant-lab" },
      { label: "Visual Options Builder", to: "/visual-builder" },
    ],
  },
  {
    section: "VALIDATE",
    items: [{ label: "Run Truthful Backtest", to: "/backtest" }],
  },
  {
    section: "DISCOVER",
    items: [
      { label: "Strategy Marketplace", to: "/marketplace" },
      { label: "Copy Trading", to: "/copy-trading" },
    ],
  },
  {
    section: "ANALYZE",
    items: [{ label: "Trade History", to: "/history" }],
  },
  {
    section: "CONTROL",
    items: [
      { label: "Watchlist & Alerts", to: "/watchlist" },
      { label: "Broker Sessions", to: "/broker-sessions" },
    ],
  },
  {
    section: "SYSTEM",
    items: [
      { label: "Pricing & Plans", to: "/pricing" },
      { label: "Profile & Settings", to: "/settings" },
      { label: "Admin Sentinel", to: "/admin" },
    ],
  },
];

export default function CommandPalette() {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [cursor, setCursor] = useState(0);
  const [instruments, setInstruments] = useState([]);
  const navigate = useNavigate();
  const inputRef = useRef(null);

  // Global hotkey: Ctrl/Cmd + K toggles, Esc closes
  useEffect(() => {
    const onKey = (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setOpen((v) => !v);
      }
      if (e.key === "Escape") setOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  useEffect(() => {
    if (open) {
      setQuery("");
      setCursor(0);
      setTimeout(() => inputRef.current?.focus(), 40);
    }
  }, [open]);

  // Debounced instrument search against the real instrument master
  useEffect(() => {
    if (!open || query.trim().length < 2) {
      setInstruments([]);
      return undefined;
    }
    const t = setTimeout(async () => {
      try {
        const res = await fetch(
          `${API_BASE}/api/market-data/instruments/search?q=${encodeURIComponent(query.trim())}&limit=6`
        );
        if (res.ok) setInstruments((await res.json()).instruments || []);
      } catch {
        /* offline — command list still usable */
      }
    }, 220);
    return () => clearTimeout(t);
  }, [query, open]);

  const q = query.trim().toLowerCase();
  const commandHits = useMemo(() => {
    const hits = [];
    for (const g of GROUPS) {
      for (const it of g.items) {
        if (!q || it.label.toLowerCase().includes(q)) hits.push({ ...it, section: g.section });
      }
    }
    return hits;
  }, [q]);

  const instrumentHits = useMemo(
    () =>
      instruments.map((i) => ({
        label: `${i.symbol}${i.exchange ? " · " + i.exchange : ""}`,
        sub: i.instrument_type || i.asset_class || "instrument",
        symbol: i.symbol,
        section: "INSTRUMENTS",
      })),
    [instruments]
  );

  const flat = useMemo(() => [...commandHits, ...instrumentHits], [commandHits, instrumentHits]);

  const go = useCallback(
    (item) => {
      setOpen(false);
      if (item.symbol) {
        navigate("/", { state: { focusSymbol: item.symbol } });
      } else {
        navigate(item.to);
      }
    },
    [navigate]
  );

  useEffect(() => {
    setCursor((c) => Math.min(c, Math.max(0, flat.length - 1)));
  }, [flat.length]);

  // Precompute grouped entries so rendering never mutates render-scope vars
  const sections = useMemo(() => {
    const map = new Map();
    for (const it of flat) {
      if (!map.has(it.section)) map.set(it.section, []);
      map.get(it.section).push(it);
    }
    return [...map.entries()];
  }, [flat]);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-[200] flex items-start justify-center pt-[12vh] px-4">
      <button aria-label="Close command palette" onClick={() => setOpen(false)} className="absolute inset-0 cursor-default bg-black/70 backdrop-blur-sm animate-fade-in" />
      <div className="relative w-full max-w-xl overflow-hidden rounded-2xl border border-slate-700 bg-surface-900 shadow-glass-lg animate-slide-up">
        <div className="flex items-center gap-2.5 border-b border-slate-800 px-4 py-3">
          <Search size={15} className="text-slate-500" />
          <input
            ref={inputRef}
            value={query}
            onChange={(e) => { setQuery(e.target.value); setCursor(0); }}
            onKeyDown={(e) => {
              if (e.key === "ArrowDown") { e.preventDefault(); setCursor((c) => Math.min(c + 1, flat.length - 1)); }
              else if (e.key === "ArrowUp") { e.preventDefault(); setCursor((c) => Math.max(c - 1, 0)); }
              else if (e.key === "Enter" && flat[cursor]) { go(flat[cursor]); }
            }}
            placeholder="Search instruments, strategies or jump to…"
            className="w-full bg-transparent text-sm text-white placeholder-slate-500 outline-none"
          />
          <kbd className="rounded border border-slate-700 px-1.5 py-0.5 text-[9px] font-mono text-slate-500">ESC</kbd>
        </div>

        <div className="max-h-[46vh] overflow-y-auto py-1.5">
          {flat.length === 0 && (
            <p className="px-4 py-6 text-center text-xs text-slate-500">No matches for “{query}”</p>
          )}
                    {sections.map(([section, items]) => (
            <div key={section} className="mb-1">
              <p className="px-4 pb-1 pt-2 text-[9px] font-bold uppercase tracking-[0.18em] text-slate-600">{section}</p>
              {items.map((it) => {
                const active = flat.indexOf(it) === cursor;
                return (
                  <button
                    key={it.symbol || it.to}
                    onMouseEnter={() => setCursor(flat.indexOf(it))}
                    onClick={() => go(it)}
                    className={`flex w-full items-center justify-between px-4 py-2 text-left text-xs ${active ? (it.symbol ? "bg-cyan-500/20 text-white" : "bg-brand-purple/20 text-white") : "text-slate-300 hover:bg-surface-800"}`}
                  >
                    <span>{it.label}{it.sub && <span className="ml-1.5 text-[10px] text-slate-500">{it.sub}</span>}</span>
                    {active && <CornerDownLeft size={11} className={it.symbol ? "text-cyan-400" : "text-brand-purple"} />}
                  </button>
                );
              })}
            </div>
          ))}
        </div>

        <div className="flex items-center justify-between border-t border-slate-800 px-4 py-2 text-[10px] text-slate-500">
          <span>↑↓ navigate · ↵ open</span>
          <span className="font-mono">CTRL/CMD + K</span>
        </div>
      </div>
    </div>
  );
}
