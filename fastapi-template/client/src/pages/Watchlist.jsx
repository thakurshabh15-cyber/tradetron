import { useState, useEffect, useRef } from "react";
import {
  Plus,
  Trash2,
  Bell,
  BellRing,
  BellOff,
  ArrowUpRight,
  ArrowDownRight,
  RefreshCw,
  Eye,
  ShieldAlert,
  Search,
  CheckCircle2,
  TrendingUp,
  Layers,
  Sparkles,
} from "lucide-react";
import { useApi } from "../hooks/useApi";
import { useDebounce } from "../hooks/useDebounce";
import { useMarket } from "../context/MarketContext";
import MarketTicker from "../components/MarketTicker";
import FastOrderPanel from "../components/FastOrderPanel";
import { alertService } from "../services/alertService";
import { API_BASE } from "../config";

const SEGMENT_TABS = [
  { id: "ALL", label: "All Markets" },
  { id: "EQUITY", label: "NSE/BSE Equities" },
  { id: "FNO", label: "F&O Derivatives" },
  { id: "COMMODITY", label: "MCX Commodities" },
  { id: "CRYPTO", label: "Crypto" },
  { id: "FOREX", label: "Forex" },
];

export default function Watchlist() {
  const { quotes, getQuote, isConnected: isMarketConnected } = useMarket();
  const { data: watchlistData, refetch: refetchWatchlist } = useApi("/api/watchlist");
  const { data: marketData, refetch: refetchMarket } = useApi("/api/market-data");

  // Universal Search State
  const [searchQuery, setSearchQuery] = useState("");
  const [selectedSegment, setSelectedSegment] = useState("ALL");
  const [searchResults, setSearchResults] = useState([]);
  const [isSearching, setIsSearching] = useState(false);
  const [showDropdown, setShowDropdown] = useState(false);
  const [isAddingSymbol, setIsAddingSymbol] = useState(null);
  const [addSuccessMsg, setAddSuccessMsg] = useState(null);
  const [error, setError] = useState(null);

  // Fast Order Modal/Inline State
  const [activeOrderSymbol, setActiveOrderSymbol] = useState(null);

  // Price Alert Form State
  const [alertSymbol, setAlertSymbol] = useState("NIFTY50");
  const [alertCondition, setAlertCondition] = useState("ABOVE");
  const [alertPrice, setAlertPrice] = useState("");
  const [alerts, setAlerts] = useState([]);
  const [isCreatingAlert, setIsCreatingAlert] = useState(false);

  const searchContainerRef = useRef(null);

  useEffect(() => {
    const unsub = alertService.subscribe((updatedAlerts) => {
      setAlerts(updatedAlerts);
    });
    return () => unsub();
  }, []);

  // Close search dropdown on click outside
  useEffect(() => {
    const handleClickOutside = (e) => {
      if (searchContainerRef.current && !searchContainerRef.current.contains(e.target)) {
        setShowDropdown(false);
      }
    };
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, []);

  // Pipe incoming market data to alertService
  useEffect(() => {
    if (marketData?.market) {
      for (const item of marketData.market) {
        alertService.processPriceTick(item.symbol, item.price);
      }
    }
  }, [marketData]);

  const debouncedSearchQuery = useDebounce(searchQuery, 350);

  // Debounced search query against backend instrument master
  useEffect(() => {
    let active = true;
    const fetchInstruments = async () => {
      setIsSearching(true);
      try {
        const params = new URLSearchParams();
        if (debouncedSearchQuery.trim()) params.append("q", debouncedSearchQuery.trim());
        if (selectedSegment !== "ALL") params.append("segment", selectedSegment);
        params.append("limit", "15");

        const res = await fetch(`${API_BASE}/api/market-data/instruments/search?${params.toString()}`);
        if (res.ok && active) {
          const data = await res.json();
          setSearchResults(data.instruments || []);
        }
      } catch (err) {
        console.error("Instrument search error:", err);
      } finally {
        if (active) setIsSearching(false);
      }
    };

    fetchInstruments();

    return () => {
      active = false;
    };
  }, [debouncedSearchQuery, selectedSegment]);

  const handleAddSymbol = async (symbol, notes = "") => {
    if (!symbol) return;
    setIsAddingSymbol(symbol);
    setError(null);
    setAddSuccessMsg(null);
    try {
      const res = await fetch(`${API_BASE}/api/watchlist`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbol: symbol.trim().toUpperCase(), notes: notes || "Added from Universal Search" }),
      });
      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || "Failed to add symbol");
      }
      setAddSuccessMsg(`Added ${symbol} to your active watchlist!`);
      setSearchQuery("");
      setShowDropdown(false);
      refetchWatchlist();
      refetchMarket();
      setTimeout(() => setAddSuccessMsg(null), 3000);
    } catch (err) {
      setError(err.message);
    } finally {
      setIsAddingSymbol(null);
    }
  };

  const handleDeleteSymbol = async (symbol) => {
    try {
      const res = await fetch(`${API_BASE}/api/watchlist/${symbol}`, {
        method: "DELETE",
      });
      if (res.ok) {
        refetchWatchlist();
      }
    } catch (err) {
      console.error("Failed to delete symbol:", err);
    }
  };

  const handleCreateAlert = async (e) => {
    e.preventDefault();
    if (!alertPrice || isNaN(alertPrice)) return;
    setIsCreatingAlert(true);
    try {
      await alertService.createAlert(alertSymbol, alertCondition, alertPrice);
      setAlertPrice("");
    } catch (err) {
      console.error("Failed to create alert:", err);
    } finally {
      setIsCreatingAlert(false);
    }
  };

  const marketMap = {
    ...(marketData?.market || []).reduce((acc, curr) => {
      acc[curr.symbol] = curr;
      return acc;
    }, {}),
    ...quotes,
  };

  const symbols = (watchlistData || []).map((w) => w.symbol);

  const getExchangeColor = (exchange) => {
    switch (exchange) {
      case "NSE":
        return "bg-blue-500/15 text-blue-400 border-blue-500/30";
      case "BSE":
        return "bg-slate-500/15 text-slate-300 border-slate-500/30";
      case "NFO":
        return "bg-purple-500/15 text-purple-400 border-purple-500/30";
      case "MCX":
        return "bg-amber-500/15 text-amber-400 border-amber-500/30";
      case "BINANCE":
        return "bg-yellow-500/15 text-yellow-400 border-yellow-500/30";
      default:
        return "bg-emerald-500/15 text-emerald-400 border-emerald-500/30";
    }
  };

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-white flex items-center gap-2">
            <Layers className="text-accent-400" size={24} />
            Universal Market Watchlist & Price Alerts
          </h1>
          <p className="text-xs text-slate-400">
            Search & track ANY real NSE/BSE Equity, F&O Option/Future, MCX Commodity, or Crypto ticker in real-time
          </p>
        </div>
        <button
          onClick={() => {
            refetchWatchlist();
            refetchMarket();
            alertService.fetchAlerts();
          }}
          className="btn-ghost text-xs flex items-center gap-1.5"
        >
          <RefreshCw size={14} /> Refresh Feeds
        </button>
      </div>

      {error && (
        <div className="p-3.5 rounded-xl bg-red-500/10 border border-red-500/25 text-red-400 text-xs flex items-center gap-2 animate-fade-in">
          <ShieldAlert size={16} />
          <span>{error}</span>
        </div>
      )}

      {addSuccessMsg && (
        <div className="p-3.5 rounded-xl bg-emerald-500/10 border border-emerald-500/25 text-emerald-400 text-xs flex items-center gap-2 animate-fade-in">
          <CheckCircle2 size={16} />
          <span>{addSuccessMsg}</span>
        </div>
      )}

      {/* Universal Search & Alert Creation Row */}
      <div className="grid gap-6 lg:grid-cols-3">
        {/* Universal Instrument Search Box (Spans 2 cols on lg) */}
        <div className="lg:col-span-2 glass-card p-3.5 sm:p-5 space-y-4 relative">
          <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-2 border-b border-white/[0.06] pb-3">
            <div className="flex items-center gap-2">
              <Search size={16} className="text-cyan-400" />
              <h2 className="text-sm font-semibold text-white">Search & Add Market Instruments</h2>
            </div>
            <span className="text-[10px] sm:text-[11px] font-mono text-cyan-400/80 bg-cyan-500/10 px-2 py-0.5 rounded border border-cyan-500/20 self-start sm:self-auto">
              10,000+ Real Scrips Active
            </span>
          </div>

          {/* Segment Filter Chips */}
          <div className="flex flex-wrap gap-1.5">
            {SEGMENT_TABS.map((tab) => (
              <button
                key={tab.id}
                onClick={() => {
                  setSelectedSegment(tab.id);
                  setShowDropdown(true);
                }}
                className={`px-2.5 py-1 rounded-lg text-xs font-semibold transition-all ${
                  selectedSegment === tab.id
                    ? "bg-accent-500/20 text-accent-300 border border-accent-500/40"
                    : "bg-surface-800/60 hover:bg-surface-750 text-slate-400 border border-white/[0.04]"
                }`}
              >
                {tab.label}
              </button>
            ))}
          </div>

          {/* Live Search Input with Dropdown */}
          <div ref={searchContainerRef} className="relative">
            <div className="relative">
              <Search size={15} className="absolute left-3.5 top-1/2 -translate-y-1/2 text-slate-400" />
              <input
                type="text"
                value={searchQuery}
                onFocus={() => setShowDropdown(true)}
                onChange={(e) => {
                  setSearchQuery(e.target.value);
                  setShowDropdown(true);
                }}
                placeholder="Search any symbol, company name, or strike (e.g. RELIANCE, NIFTY 24800 CE, CRUDEOIL, GOLD, TRENT)..."
                className="w-full bg-slate-950 border border-slate-800 rounded-xl pl-10 pr-4 py-2.5 text-xs text-white placeholder-slate-500 focus:outline-none focus:border-cyan-500 transition-all font-mono"
              />
              {isSearching && (
                <RefreshCw size={14} className="absolute right-3.5 top-1/2 -translate-y-1/2 text-slate-400 animate-spin" />
              )}
            </div>

            {/* Auto-suggest Search Results Dropdown */}
            {showDropdown && (
              <div className="absolute top-full left-0 right-0 mt-2 bg-surface-900 border border-slate-700/80 rounded-xl shadow-2xl z-30 max-h-72 overflow-y-auto divide-y divide-slate-800 animate-fade-in">
                {searchResults.length > 0 ? (
                  searchResults.map((item) => {
                    const isAlreadyInWatchlist = symbols.includes(item.symbol);
                    return (
                      <div
                        key={`${item.exchange}-${item.symbol}`}
                        className="p-3 hover:bg-slate-800/60 flex items-center justify-between gap-3 transition-colors"
                      >
                        <div className="flex items-center gap-2.5 overflow-hidden">
                          <span
                            className={`px-1.5 py-0.5 rounded text-[10px] font-bold border ${getExchangeColor(
                              item.exchange
                            )}`}
                          >
                            {item.exchange}
                          </span>
                          <div className="overflow-hidden">
                            <div className="flex items-center gap-2">
                              <span className="text-xs font-bold text-white font-mono">{item.symbol}</span>
                              <span className="text-[10px] text-slate-400 font-mono bg-slate-800 px-1.5 py-0.2 rounded">
                                {item.instrument_type}
                              </span>
                              {item.lot_size > 1 && (
                                <span className="text-[10px] text-cyan-400 font-mono">Lot: {item.lot_size}</span>
                              )}
                            </div>
                            <p className="text-[11px] text-slate-400 truncate">{item.name}</p>
                          </div>
                        </div>

                        <div className="flex items-center gap-3 shrink-0">
                          <div className="text-right">
                            <span className="text-xs font-bold text-white font-mono">
                              {item.exchange === "BINANCE" || item.segment === "CRYPTO" ? "$" : "₹"}
                              {item.base_price.toLocaleString("en-IN", { minimumFractionDigits: 2 })}
                            </span>
                            <span className="block text-[9px] text-slate-500">Ref Anchor</span>
                          </div>

                          <button
                            onClick={() => handleAddSymbol(item.symbol, item.name)}
                            disabled={isAddingSymbol === item.symbol || isAlreadyInWatchlist}
                            className={`px-3 py-1.5 rounded-lg text-xs font-semibold transition-all flex items-center gap-1 ${
                              isAlreadyInWatchlist
                                ? "bg-slate-800 text-slate-500 cursor-not-allowed"
                                : "bg-cyan-500/20 hover:bg-cyan-500/30 text-cyan-300 border border-cyan-500/30"
                            }`}
                          >
                            {isAddingSymbol === item.symbol ? (
                              <RefreshCw size={12} className="animate-spin" />
                            ) : isAlreadyInWatchlist ? (
                              <CheckCircle2 size={12} className="text-emerald-400" />
                            ) : (
                              <Plus size={12} />
                            )}
                            <span>{isAlreadyInWatchlist ? "Added" : "Watch"}</span>
                          </button>
                        </div>
                      </div>
                    );
                  })
                ) : (
                  <div className="p-6 text-center text-xs text-slate-400">
                    {searchQuery ? `No instruments matching "${searchQuery}"` : "Type any symbol name to search..."}
                  </div>
                )}
              </div>
            )}
          </div>

          <div className="flex items-center gap-2 pt-1 text-[11px] text-slate-400">
            <Sparkles size={13} className="text-accent-400" />
            <span>
              Search supports all NSE cash shares, Nifty/BankNifty index options, stock futures, MCX metals/energy, and Binance spot.
            </span>
          </div>
        </div>

        {/* Set Automated Price Alert Form */}
        <div className="glass-card p-5 space-y-4">
          <div className="flex items-center gap-2 border-b border-white/[0.06] pb-3">
            <BellRing size={16} className="text-amber-400" />
            <h2 className="text-sm font-semibold text-white">Create Price Alert</h2>
          </div>

          <form onSubmit={handleCreateAlert} className="space-y-3">
            <div>
              <label className="text-xs font-medium text-slate-400 mb-1 block">Ticker Symbol</label>
              <select
                value={alertSymbol}
                onChange={(e) => setAlertSymbol(e.target.value)}
                className="select-field text-xs w-full font-mono"
              >
                {symbols.length > 0 ? (
                  symbols.map((s) => (
                    <option key={s} value={s}>
                      {s}
                    </option>
                  ))
                ) : (
                  <option value="NIFTY50">NIFTY50</option>
                )}
              </select>
            </div>

            <div className="grid grid-cols-2 gap-2">
              <div>
                <label className="text-xs font-medium text-slate-400 mb-1 block">Trigger When</label>
                <select
                  value={alertCondition}
                  onChange={(e) => setAlertCondition(e.target.value)}
                  className="select-field text-xs w-full"
                >
                  <option value="ABOVE">Price ≥ Target</option>
                  <option value="BELOW">Price ≤ Target</option>
                </select>
              </div>

              <div>
                <label className="text-xs font-medium text-slate-400 mb-1 block">Target Price</label>
                <input
                  type="number"
                  step="any"
                  required
                  placeholder="e.g. 25000"
                  value={alertPrice}
                  onChange={(e) => setAlertPrice(e.target.value)}
                  className="input-field text-xs font-mono"
                />
              </div>
            </div>

            <button
              type="submit"
              disabled={isCreatingAlert || !alertPrice}
              className="btn-accent text-xs py-2 px-4 w-full justify-center disabled:opacity-50"
            >
              <Bell size={14} />
              {isCreatingAlert ? "Saving Alert..." : "Set Price Alert"}
            </button>
          </form>
        </div>
      </div>

      {/* Fast Order Execution Modal if clicked from Watchlist */}
      {activeOrderSymbol && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/80 backdrop-blur-sm animate-fade-in">
          <div className="w-full max-w-md">
            <div className="flex justify-end mb-2">
              <button
                onClick={() => setActiveOrderSymbol(null)}
                className="text-xs text-slate-400 hover:text-white px-2 py-1 bg-surface-800 rounded-lg border border-slate-700"
              >
                ✕ Close DMA Panel
              </button>
            </div>
            <FastOrderPanel
              symbol={activeOrderSymbol}
              currentPrice={marketMap[activeOrderSymbol]?.price || 24850.0}
              onOrderPlaced={() => setActiveOrderSymbol(null)}
            />
          </div>
        </div>
      )}

      {/* Active Watchlist Grid / Table */}
      <div className="glass-card overflow-hidden">
        <div className="p-4 border-b border-white/[0.06] flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Eye size={16} className="text-accent-400" />
            <h2 className="text-sm font-semibold text-white">Active Watched Instruments ({symbols.length})</h2>
          </div>
        </div>

        {symbols.length === 0 ? (
          <div className="p-8 text-center text-slate-400 text-xs">
            Your watchlist is empty. Use the search box above to add equities, options, or commodities!
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs min-w-[640px]">
              <thead className="bg-surface-800/40 text-slate-400 font-medium border-b border-white/[0.04]">
                <tr>
                  <th className="py-3 px-4">Instrument</th>
                  <th className="py-3 px-4 text-right">LTP (Price)</th>
                  <th className="py-3 px-4 text-right">24h Change</th>
                  <th className="py-3 px-4 text-right">Day Range</th>
                  <th className="py-3 px-4">Notes / Thesis</th>
                  <th className="py-3 px-4 text-center">Quick Trade</th>
                  <th className="py-3 px-4 text-right">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-white/[0.04]">
                {watchlistData?.map((item) => {
                  const quote = marketMap[item.symbol];
                  const isUp = (quote?.change ?? 0) >= 0;
                  const isINR =
                    item.symbol.includes("NIFTY") ||
                    item.symbol.includes("RELIANCE") ||
                    item.symbol.includes("INR") ||
                    item.symbol.includes("TCS") ||
                    item.symbol.includes("GOLD") ||
                    item.symbol.includes("CRUDE");
                  const currSign = isINR ? "₹" : "$";

                  return (
                    <tr key={item.id} className="hover:bg-white/[0.02] transition-colors">
                      <td className="py-3 px-4 font-mono font-bold text-white flex items-center gap-2">
                        <span>{item.symbol}</span>
                      </td>

                      <td className="py-3 px-4 font-mono text-right font-semibold text-white">
                        {quote?.price !== undefined
                          ? `${currSign}${quote.price.toLocaleString("en-IN", { minimumFractionDigits: 2 })}`
                          : "Connecting feed..."}
                      </td>

                      <td className={`py-3 px-4 font-mono text-right font-medium ${isUp ? "text-emerald-400" : "text-rose-400"}`}>
                        {quote ? (
                          <div className="flex items-center justify-end gap-1">
                            {isUp ? <ArrowUpRight size={13} /> : <ArrowDownRight size={13} />}
                            <span>
                              {isUp ? "+" : ""}
                              {quote.change_pct?.toFixed(2)}%
                            </span>
                          </div>
                        ) : (
                          "-"
                        )}
                      </td>

                      <td className="py-3 px-4 font-mono text-right text-slate-400">
                        {quote?.low && quote?.high
                          ? `${currSign}${quote.low.toFixed(1)} - ${currSign}${quote.high.toFixed(1)}`
                          : "-"}
                      </td>

                      <td className="py-3 px-4 text-slate-400 italic max-w-xs truncate">
                        {item.notes || "-"}
                      </td>

                      <td className="py-3 px-4 text-center">
                        <button
                          onClick={() => setActiveOrderSymbol(item.symbol)}
                          className="px-2.5 py-1 rounded bg-accent-500/15 hover:bg-accent-500/25 border border-accent-500/30 text-accent-300 text-[11px] font-bold transition-all"
                        >
                          Fast Order
                        </button>
                      </td>

                      <td className="py-3 px-4 text-right">
                        <button
                          onClick={() => handleDeleteSymbol(item.symbol)}
                          className="p-1.5 text-slate-500 hover:text-rose-400 hover:bg-rose-500/10 rounded transition-colors"
                          title="Remove from Watchlist"
                        >
                          <Trash2 size={14} />
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
