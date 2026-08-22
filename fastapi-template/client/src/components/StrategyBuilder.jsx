import { useState, useEffect } from "react";
import { Plus, Trash2, Sliders, AlertTriangle, ShieldCheck, Zap } from "lucide-react";
import axios from "axios";

const INDICATORS = [
  { value: "PRICE", label: "Market Price" },
  { value: "RSI", label: "Relative Strength Index (RSI)" },
  { value: "SMA", label: "Simple Moving Average (SMA)" },
  { value: "EMA", label: "Exponential Moving Average (EMA)" },
];

const OPERATORS = [
  { value: "lt", label: "< (Less Than)" },
  { value: "lte", label: "<= (Less Than / Equal)" },
  { value: "gt", label: "> (Greater Than)" },
  { value: "gte", label: ">= (Greater Than / Equal)" },
  { value: "cross_above", label: "Crosses Above" },
  { value: "cross_below", label: "Crosses Below" },
];

export default function StrategyBuilder({ onSubmit, isSubmitting }) {
  const [name, setName] = useState("");
  const [symbols, setSymbols] = useState("RELIANCE, TCS");
  const [side, setSide] = useState("BUY");
  const [quantity, setQuantity] = useState(10);
  const [enabled, setEnabled] = useState(true);
  const [executionMode, setExecutionMode] = useState("PAPER"); // 'PAPER' | 'LIVE'
  const [capitalAllocated, setCapitalAllocated] = useState(25000);
  const [brokerAccounts, setBrokerAccounts] = useState([]);
  const [selectedBrokerId, setSelectedBrokerId] = useState("");

  const [conditions, setConditions] = useState([
    { indicator: "PRICE", operator: "gt", value: 2500, period: 14 },
  ]);

  useEffect(() => {
    // Fetch connected broker accounts for Live Mode selection
    axios.get("/api/brokers/accounts")
      .then((res) => {
        const accs = res.data?.accounts || [];
        setBrokerAccounts(accs);
        if (accs.length > 0) {
          setSelectedBrokerId(accs[0].id);
        }
      })
      .catch(() => {});
  }, []);

  const addCondition = () => {
    setConditions((prev) => [
      ...prev,
      { indicator: "RSI", operator: "lt", value: 30, period: 14 },
    ]);
  };

  const removeCondition = (idx) => {
    setConditions((prev) => prev.filter((_, i) => i !== idx));
  };

  const updateCondition = (idx, field, val) => {
    setConditions((prev) =>
      prev.map((c, i) => (i === idx ? { ...c, [field]: val } : c))
    );
  };

  const handleSubmit = (e) => {
    e.preventDefault();
    if (!name.trim()) return;

    const payload = {
      name: name.trim(),
      symbols: symbols.split(",").map((s) => s.trim().toUpperCase()).filter(Boolean),
      conditions: conditions.map((c) => ({
        indicator: c.indicator,
        operator: c.operator,
        value: Number(c.value),
        period: Number(c.period),
      })),
      action: {
        side,
        quantity: Number(quantity),
        order_type: "MARKET",
      },
      enabled,
      execution_mode: executionMode,
      broker_account_id: executionMode === "LIVE" ? selectedBrokerId : null,
      capital_allocated: Number(capitalAllocated),
    };

    onSubmit(payload);
    setName("");
  };

  return (
    <form onSubmit={handleSubmit} className="glass-card space-y-5">
      <div className="flex items-center justify-between border-b border-white/[0.06] pb-3">
        <div className="flex items-center gap-2">
          <Sliders size={18} className="text-accent-400" />
          <h2 className="text-base font-semibold text-white">Visual Strategy Builder</h2>
        </div>

        {/* Hard Mode Toggle */}
        <div className="flex items-center gap-1.5 p-1 rounded-lg bg-surface-900 border border-white/[0.08]">
          <button
            type="button"
            onClick={() => setExecutionMode("PAPER")}
            className={`flex items-center gap-1.5 px-3 py-1.5 rounded text-xs font-semibold transition-all ${
              executionMode === "PAPER"
                ? "bg-slate-700 text-white shadow-sm"
                : "text-slate-400 hover:text-slate-200"
            }`}
          >
            <ShieldCheck size={13} className="text-cyan-400" /> Paper Mode
          </button>
          <button
            type="button"
            onClick={() => setExecutionMode("LIVE")}
            className={`flex items-center gap-1.5 px-3 py-1.5 rounded text-xs font-semibold transition-all ${
              executionMode === "LIVE"
                ? "bg-emerald-500 text-slate-950 font-bold shadow-md shadow-emerald-500/20"
                : "text-slate-400 hover:text-emerald-400"
            }`}
          >
            <Zap size={13} className={executionMode === "LIVE" ? "text-slate-950" : "text-emerald-400"} /> Live Broker Mode
          </button>
        </div>
      </div>

      {/* Live Mode Warning & Broker Selector */}
      {executionMode === "LIVE" && (
        <div className="rounded-lg bg-emerald-500/10 border border-emerald-500/30 p-4 space-y-3">
          <div className="flex items-start gap-3">
            <AlertTriangle className="text-emerald-400 shrink-0 mt-0.5" size={18} />
            <div className="space-y-1">
              <div className="text-xs font-bold uppercase tracking-wider text-emerald-400">
                Live Broker Order Routing Activated
              </div>
              <p className="text-xs text-slate-300 leading-relaxed">
                Signals generated by this strategy will execute <strong>real market orders</strong> on your linked broker API with genuine order IDs and real margin deductions.
              </p>
            </div>
          </div>

          <div className="grid gap-3 pt-1 sm:grid-cols-2">
            <div>
              <label className="block text-xs font-medium text-slate-300 mb-1">
                Connected Broker Account
              </label>
              <select
                value={selectedBrokerId}
                onChange={(e) => setSelectedBrokerId(e.target.value)}
                className="select-field text-xs bg-surface-900 border-emerald-500/30 text-white"
              >
                {brokerAccounts.length === 0 ? (
                  <option value="">No connected broker (Simulated Active)</option>
                ) : (
                  brokerAccounts.map((acc) => (
                    <option key={acc.id} value={acc.id}>
                      {acc.broker_name} — {acc.account_id || acc.id.slice(0, 8)} ({acc.status})
                    </option>
                  ))
                )}
              </select>
            </div>
            <div>
              <label className="block text-xs font-medium text-slate-300 mb-1">
                Allocated Capital (₹)
              </label>
              <input
                type="number"
                min="500"
                step="500"
                value={capitalAllocated}
                onChange={(e) => setCapitalAllocated(e.target.value)}
                className="input-field text-xs bg-surface-900 border-emerald-500/30 text-white"
              />
            </div>
          </div>
        </div>
      )}

      <div className="grid gap-4 md:grid-cols-2">
        <div>
          <label className="mb-1 block text-xs font-medium text-slate-400">
            Strategy Name
          </label>
          <input
            type="text"
            required
            placeholder="e.g. NIFTY Supertrend Breakout"
            value={name}
            onChange={(e) => setName(e.target.value)}
            className="input-field"
          />
        </div>
        <div>
          <label className="mb-1 block text-xs font-medium text-slate-400">
            Watchlist Symbols (comma separated)
          </label>
          <input
            type="text"
            required
            placeholder="RELIANCE, TCS, INFY, NIFTY50"
            value={symbols}
            onChange={(e) => setSymbols(e.target.value)}
            className="input-field"
          />
        </div>
      </div>

      {/* Conditions list */}
      <div className="space-y-3">
        <div className="flex items-center justify-between">
          <span className="text-xs font-semibold uppercase tracking-wider text-slate-400">
            Execution Rules (ALL must match)
          </span>
          <button
            type="button"
            onClick={addCondition}
            className="inline-flex items-center gap-1 text-xs font-medium text-accent-400 hover:text-accent-300"
          >
            <Plus size={14} /> Add Condition
          </button>
        </div>

        {conditions.map((cond, idx) => (
          <div
            key={idx}
            className="grid items-center gap-3 rounded-lg bg-surface-800/80 p-3 md:grid-cols-4"
          >
            <select
              value={cond.indicator}
              onChange={(e) => updateCondition(idx, "indicator", e.target.value)}
              className="select-field text-xs"
            >
              {INDICATORS.map((i) => (
                <option key={i.value} value={i.value}>
                  {i.label}
                </option>
              ))}
            </select>

            <select
              value={cond.operator}
              onChange={(e) => updateCondition(idx, "operator", e.target.value)}
              className="select-field text-xs"
            >
              {OPERATORS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>

            <div className="flex items-center gap-2">
              <input
                type="number"
                step="any"
                required
                value={cond.value}
                onChange={(e) => updateCondition(idx, "value", e.target.value)}
                placeholder="Value"
                className="input-field text-xs"
              />
            </div>

            <div className="flex items-center gap-2">
              <input
                type="number"
                min="1"
                max="500"
                value={cond.period}
                onChange={(e) => updateCondition(idx, "period", e.target.value)}
                placeholder="Period"
                title="Lookback Period"
                className="input-field text-xs w-20"
              />
              {conditions.length > 1 && (
                <button
                  type="button"
                  onClick={() => removeCondition(idx)}
                  className="p-1.5 text-slate-500 hover:text-loss-400 transition-colors"
                >
                  <Trash2 size={16} />
                </button>
              )}
            </div>
          </div>
        ))}
      </div>

      {/* Action configuration */}
      <div className="grid gap-4 border-t border-white/[0.06] pt-4 md:grid-cols-3">
        <div>
          <label className="mb-1 block text-xs font-medium text-slate-400">
            Action Side
          </label>
          <select
            value={side}
            onChange={(e) => setSide(e.target.value)}
            className="select-field"
          >
            <option value="BUY">BUY</option>
            <option value="SELL">SELL</option>
          </select>
        </div>

        <div>
          <label className="mb-1 block text-xs font-medium text-slate-400">
            Order Quantity
          </label>
          <input
            type="number"
            min="1"
            required
            value={quantity}
            onChange={(e) => setQuantity(e.target.value)}
            className="input-field"
          />
        </div>

        <div className="flex items-end pb-1">
          <label className="flex items-center gap-2 cursor-pointer text-xs text-slate-300">
            <input
              type="checkbox"
              checked={enabled}
              onChange={(e) => setEnabled(e.target.checked)}
              className="h-4 w-4 rounded border-slate-700 bg-surface-800 text-accent-500 focus:ring-accent-500/30"
            />
            <span>Enable strategy immediately</span>
          </label>
        </div>
      </div>

      <button
        type="submit"
        disabled={isSubmitting}
        className={`w-full py-2.5 px-4 rounded-lg font-semibold text-xs transition-all flex items-center justify-center gap-2 ${
          executionMode === "LIVE"
            ? "bg-emerald-500 hover:bg-emerald-400 text-slate-950 font-bold shadow-lg shadow-emerald-500/20"
            : "btn-primary"
        }`}
      >
        {isSubmitting ? (
          "Deploying Strategy..."
        ) : executionMode === "LIVE" ? (
          <>
            <Zap size={14} /> Deploy to LIVE Broker Account
          </>
        ) : (
          <>
            <ShieldCheck size={14} /> Deploy to Paper Trading Engine
          </>
        )}
      </button>
    </form>
  );
}
