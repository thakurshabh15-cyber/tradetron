import { useState, useEffect } from "react";
import { Plus, Trash2, Bell, BellRing, BellOff, ArrowUpRight, ArrowDownRight, RefreshCw, Eye, ShieldAlert } from "lucide-react";
import { useApi } from "../hooks/useApi";
import MarketTicker from "../components/MarketTicker";
import { alertService } from "../services/alertService";
import { API_BASE } from "../config";

export default function Watchlist() {
  const { data: watchlistData, refetch: refetchWatchlist } = useApi("/api/watchlist");
  const { data: marketData, refetch: refetchMarket } = useApi("/api/market-data");

  const [newSymbol, setNewSymbol] = useState("");
  const [newNotes, setNewNotes] = useState("");
  const [isAdding, setIsAdding] = useState(false);
  const [error, setError] = useState(null);

  // Price Alert Form State
  const [alertSymbol, setAlertSymbol] = useState("AAPL");
  const [alertCondition, setAlertCondition] = useState("ABOVE");
  const [alertPrice, setAlertPrice] = useState("");
  const [alerts, setAlerts] = useState([]);
  const [isCreatingAlert, setIsCreatingAlert] = useState(false);

  useEffect(() => {
    const unsub = alertService.subscribe((updatedAlerts) => {
      setAlerts(updatedAlerts);
    });
    return () => unsub();
  }, []);

  // Pipe incoming market data to alertService
  useEffect(() => {
    if (marketData?.market) {
      for (const item of marketData.market) {
        alertService.processPriceTick(item.symbol, item.price);
      }
    }
  }, [marketData]);

  const handleAddSymbol = async (e) => {
    e.preventDefault();
    if (!newSymbol.trim()) return;
    setIsAdding(true);
    setError(null);
    try {
      const res = await fetch(`${API_BASE}/api/watchlist`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbol: newSymbol.trim(), notes: newNotes }),
      });
      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || "Failed to add symbol");
      }
      setNewSymbol("");
      setNewNotes("");
      refetchWatchlist();
    } catch (err) {
      setError(err.message);
    } finally {
      setIsAdding(false);
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

  const marketMap = (marketData?.market || []).reduce((acc, curr) => {
    acc[curr.symbol] = curr;
    return acc;
  }, {});

  const symbols = (watchlistData || []).map((w) => w.symbol);

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-white">
            Custom Watchlist & Price Alerts
          </h1>
          <p className="text-xs text-slate-400">
            Track key market tickers and configure automated threshold alerts
          </p>
        </div>
        <button
          onClick={() => {
            refetchWatchlist();
            refetchMarket();
            alertService.fetchAlerts();
          }}
          className="btn-ghost text-xs"
        >
          <RefreshCw size={14} /> Refresh Data
        </button>
      </div>

      {error && (
        <div className="p-3 rounded-lg bg-red-500/10 border border-red-500/20 text-red-400 text-xs">
          {error}
        </div>
      )}

      {/* Add Symbol & Create Alert Row */}
      <div className="grid gap-6 lg:grid-cols-2">
        {/* Add Symbol Form */}
        <div className="glass-card p-5 space-y-4">
          <div className="flex items-center gap-2 border-b border-white/[0.06] pb-3">
            <Eye size={16} className="text-cyan-400" />
            <h2 className="text-sm font-semibold text-white">Add Watchlist Symbol</h2>
          </div>

          <form onSubmit={handleAddSymbol} className="space-y-3">
            <div className="grid gap-3 sm:grid-cols-2">
              <div>
                <label className="text-xs font-medium text-slate-400 mb-1 block">
                  Ticker Symbol
                </label>
                <input
                  type="text"
                  required
                  placeholder="e.g. TSLA, AMD, QQQ"
                  value={newSymbol}
                  onChange={(e) => setNewSymbol(e.target.value.toUpperCase())}
                  className="input-field text-xs font-mono"
                />
              </div>

              <div>
                <label className="text-xs font-medium text-slate-400 mb-1 block">
                  Notes / Thesis (Optional)
                </label>
                <input
                  type="text"
                  placeholder="e.g. Breakout watchlist"
                  value={newNotes}
                  onChange={(e) => setNewNotes(e.target.value)}
                  className="input-field text-xs"
                />
              </div>
            </div>

            <button
              type="submit"
              disabled={isAdding}
              className="btn-primary text-xs py-2 px-4 w-full justify-center"
            >
              <Plus size={14} />
              {isAdding ? "Adding..." : "Add to Watchlist"}
            </button>
          </form>
        </div>

        {/* Create Price Alert Form */}
        <div className="glass-card p-5 space-y-4">
          <div className="flex items-center gap-2 border-b border-white/[0.06] pb-3">
            <BellRing size={16} className="text-amber-400" />
            <h2 className="text-sm font-semibold text-white">Set Price Alert</h2>
          </div>

          <form onSubmit={handleCreateAlert} className="space-y-3">
            <div className="grid grid-cols-3 gap-2">
              <div>
                <label className="text-xs font-medium text-slate-400 mb-1 block">
                  Symbol
                </label>
                <select
                  value={alertSymbol}
                  onChange={(e) => setAlertSymbol(e.target.value)}
                  className="select-field text-xs w-full"
                >
                  {symbols.length > 0 ? (
                    symbols.map((s) => (
                      <option key={s} value={s}>
                        {s}
                      </option>
                    ))
                  ) : (
                    <option value="AAPL">AAPL</option>
                  )}
                </select>
              </div>

              <div>
                <label className="text-xs font-medium text-slate-400 mb-1 block">
                  Condition
                </label>
                <select
                  value={alertCondition}
                  onChange={(e) => setAlertCondition(e.target.value)}
                  className="select-field text-xs w-full"
                >
                  <option value="ABOVE">Crosses Above (≥)</option>
                  <option value="BELOW">Crosses Below (≤)</option>
                </select>
              </div>

              <div>
                <label className="text-xs font-medium text-slate-400 mb-1 block">
                  Target Price ($)
                </label>
                <input
                  type="number"
                  step="0.1"
                  required
                  placeholder="230.00"
                  value={alertPrice}
                  onChange={(e) => setAlertPrice(e.target.value)}
                  className="input-field text-xs font-mono"
                />
              </div>
            </div>

            <button
              type="submit"
              disabled={isCreatingAlert}
              className="btn-primary text-xs py-2 px-4 w-full justify-center bg-amber-500 hover:bg-amber-400 text-slate-950 shadow-amber-500/20"
            >
              <Bell size={14} />
              {isCreatingAlert ? "Setting Alert..." : "Enable Price Alert"}
            </button>
          </form>
        </div>
      </div>

      {/* Active Watchlist Grid */}
      <div className="space-y-3">
        <h2 className="text-sm font-bold text-white tracking-tight">
          Active Watchlist ({symbols.length})
        </h2>

        {symbols.length === 0 ? (
          <div className="glass-card p-8 text-center text-slate-500 text-xs">
            No symbols currently in watchlist. Add one above!
          </div>
        ) : (
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-5">
            {symbols.map((sym) => (
              <div key={sym} className="relative group">
                <MarketTicker symbol={sym} initialData={marketMap[sym]} />
                <button
                  onClick={() => handleDeleteSymbol(sym)}
                  title="Remove from watchlist"
                  className="absolute top-3 right-3 p-1.5 rounded-md bg-slate-900/80 hover:bg-red-500/20 text-slate-400 hover:text-red-400 border border-slate-700/60 opacity-0 group-hover:opacity-100 transition-all"
                >
                  <Trash2 size={13} />
                </button>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Configured Price Alerts List */}
      <div className="glass-card p-5 space-y-4">
        <div className="flex items-center justify-between border-b border-white/[0.06] pb-3">
          <div className="flex items-center gap-2">
            <Bell size={16} className="text-amber-400" />
            <h2 className="text-sm font-semibold text-white">
              Registered Alerts ({alerts.length})
            </h2>
          </div>
          <span className="text-[10px] text-slate-400 font-mono">
            Evaluated on real-time price ticks
          </span>
        </div>

        {alerts.length === 0 ? (
          <div className="p-6 text-center text-slate-500 text-xs">
            No active price alerts configured.
          </div>
        ) : (
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {alerts.map((a) => (
              <div
                key={a.id}
                className={`p-3.5 rounded-lg border transition-all flex items-center justify-between ${
                  a.is_triggered
                    ? "bg-amber-500/10 border-amber-500/30 text-amber-300"
                    : a.is_active
                    ? "bg-surface-800/80 border-white/[0.06] text-white"
                    : "bg-surface-900 border-slate-800 text-slate-500 opacity-60"
                }`}
              >
                <div>
                  <div className="flex items-center gap-2">
                    <span className="text-xs font-bold font-mono">{a.symbol}</span>
                    <span
                      className={`text-[9px] px-1.5 py-0.2 rounded font-bold ${
                        a.condition === "ABOVE"
                          ? "bg-emerald-500/20 text-emerald-400"
                          : "bg-loss-500/20 text-loss-400"
                      }`}
                    >
                      {a.condition === "ABOVE" ? "≥" : "≤"} ${Number(a.target_price).toFixed(2)}
                    </span>
                  </div>
                  <p className="text-[10px] text-slate-400 mt-1">
                    {a.is_triggered ? "Triggered Alert" : a.is_active ? "Monitoring..." : "Paused"}
                  </p>
                </div>

                <div className="flex items-center gap-1.5">
                  <button
                    onClick={() => alertService.toggleAlert(a.id)}
                    title={a.is_active ? "Disable Alert" : "Enable Alert"}
                    className={`p-1.5 rounded-md border text-xs transition-colors ${
                      a.is_active
                        ? "bg-emerald-500/20 text-emerald-400 border-emerald-500/30 hover:bg-emerald-500/30"
                        : "bg-slate-800 text-slate-500 border-slate-700 hover:text-white"
                    }`}
                  >
                    {a.is_active ? <BellRing size={13} /> : <BellOff size={13} />}
                  </button>

                  <button
                    onClick={() => alertService.deleteAlert(a.id)}
                    title="Delete Alert"
                    className="p-1.5 rounded-md bg-surface-700 hover:bg-red-500/20 text-slate-400 hover:text-red-400 transition-colors"
                  >
                    <Trash2 size={13} />
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
