import { useState } from "react";
import { ArrowRight, ArrowLeft, CheckCircle2, Plus, Trash2, Zap } from "lucide-react";

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

export default function StrategyWizardScreen({ onSubmit, onCancel, isSubmitting }) {
  const [step, setStep] = useState(1);
  const [name, setName] = useState("");
  const [category, setCategory] = useState("Trend Following");
  const [symbols, setSymbols] = useState("AAPL, NVDA");
  const [timeframe, setTimeframe] = useState("1m");
  const [side, setSide] = useState("BUY");
  const [quantity, setQuantity] = useState(10);
  const [stopLossPct, setStopLossPct] = useState(2.0);
  const [takeProfitPct, setTakeProfitPct] = useState(5.0);
  const [enabled, setEnabled] = useState(true);

  const [conditions, setConditions] = useState([
    { indicator: "SMA", operator: "cross_above", value: 200, period: 50 },
  ]);

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

  const handleFinish = (e) => {
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
    };

    onSubmit(payload);
  };

  return (
    <div className="glass-card space-y-6 animate-fade-in">
      {/* Wizard Header & Step Indicator */}
      <div className="flex items-center justify-between border-b border-white/[0.06] pb-4">
        <div className="flex items-center gap-2.5">
          <div className="p-2 rounded-lg bg-accent-500/10 text-accent-400 border border-accent-500/20">
            <Zap size={18} />
          </div>
          <div>
            <h2 className="text-base font-semibold text-white">Strategy Creation Wizard</h2>
            <p className="text-xs text-slate-400">Step {step} of 3: {step === 1 ? "Basics & Asset Universe" : step === 2 ? "Signal Condition Logic" : "Order Execution & Risk Limits"}</p>
          </div>
        </div>

        {/* Step dots */}
        <div className="flex items-center gap-2">
          {[1, 2, 3].map((s) => (
            <div
              key={s}
              className={`w-7 h-7 rounded-full flex items-center justify-center text-xs font-bold transition-all ${
                step === s
                  ? "bg-accent-500 text-slate-950 shadow-md shadow-accent-500/20"
                  : step > s
                  ? "bg-profit-500/20 text-profit-400 border border-profit-500/30"
                  : "bg-surface-800 text-slate-500"
              }`}
            >
              {step > s ? "✓" : s}
            </div>
          ))}
        </div>
      </div>

      {/* STEP 1: BASICS & ASSETS */}
      {step === 1 && (
        <div className="space-y-4 animate-fade-in">
          <div className="grid gap-4 md:grid-cols-2">
            <div>
              <label className="mb-1 block text-xs font-medium text-slate-400">
                Strategy Name
              </label>
              <input
                type="text"
                required
                placeholder="e.g. Multi-MA Trend Hunter"
                value={name}
                onChange={(e) => setName(e.target.value)}
                className="input-field"
              />
            </div>

            <div>
              <label className="mb-1 block text-xs font-medium text-slate-400">
                Strategy Category
              </label>
              <select
                value={category}
                onChange={(e) => setCategory(e.target.value)}
                className="select-field"
              >
                <option value="Trend Following">Trend Following</option>
                <option value="Mean Reversion">Mean Reversion</option>
                <option value="Momentum">Momentum Breakout</option>
                <option value="Options">Options Multi-Leg</option>
              </select>
            </div>
          </div>

          <div className="grid gap-4 md:grid-cols-2">
            <div>
              <label className="mb-1 block text-xs font-medium text-slate-400">
                Target Assets (comma separated)
              </label>
              <input
                type="text"
                required
                placeholder="AAPL, MSFT, NVDA, GOOGL"
                value={symbols}
                onChange={(e) => setSymbols(e.target.value)}
                className="input-field"
              />
            </div>

            <div>
              <label className="mb-1 block text-xs font-medium text-slate-400">
                Execution Timeframe
              </label>
              <select
                value={timeframe}
                onChange={(e) => setTimeframe(e.target.value)}
                className="select-field"
              >
                <option value="1m">1 Minute (Ultra High-Frequency)</option>
                <option value="5m">5 Minutes (Intraday)</option>
                <option value="15m">15 Minutes (Swing)</option>
                <option value="1h">1 Hour (Macro Trend)</option>
              </select>
            </div>
          </div>
        </div>
      )}

      {/* STEP 2: SIGNAL LOGIC */}
      {step === 2 && (
        <div className="space-y-4 animate-fade-in">
          <div className="flex items-center justify-between">
            <span className="text-xs font-semibold uppercase tracking-wider text-slate-400">
              Entry Conditions (All must be TRUE)
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
                  <option key={i.value} value={i.value}>{i.label}</option>
                ))}
              </select>

              <select
                value={cond.operator}
                onChange={(e) => updateCondition(idx, "operator", e.target.value)}
                className="select-field text-xs"
              >
                {OPERATORS.map((o) => (
                  <option key={o.value} value={o.value}>{o.label}</option>
                ))}
              </select>

              <input
                type="number"
                step="any"
                required
                value={cond.value}
                onChange={(e) => updateCondition(idx, "value", e.target.value)}
                placeholder="Value"
                className="input-field text-xs"
              />

              <div className="flex items-center gap-2">
                <input
                  type="number"
                  min="1"
                  max="500"
                  value={cond.period}
                  onChange={(e) => updateCondition(idx, "period", e.target.value)}
                  placeholder="Period"
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
      )}

      {/* STEP 3: ORDER EXECUTION & RISK LIMITS */}
      {step === 3 && (
        <div className="space-y-4 animate-fade-in">
          <div className="grid gap-4 md:grid-cols-3">
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
                  className="h-4 w-4 rounded border-slate-700 bg-surface-800 text-accent-500"
                />
                <span>Enable immediately on engine</span>
              </label>
            </div>
          </div>

          <div className="grid gap-4 md:grid-cols-2 pt-2 border-t border-white/[0.06]">
            <div>
              <label className="mb-1 block text-xs font-medium text-slate-400">
                Stop-Loss Trigger (%)
              </label>
              <input
                type="number"
                step="0.5"
                min="0.5"
                max="20"
                value={stopLossPct}
                onChange={(e) => setStopLossPct(e.target.value)}
                className="input-field"
              />
            </div>

            <div>
              <label className="mb-1 block text-xs font-medium text-slate-400">
                Take-Profit Target (%)
              </label>
              <input
                type="number"
                step="0.5"
                min="1.0"
                max="50"
                value={takeProfitPct}
                onChange={(e) => setTakeProfitPct(e.target.value)}
                className="input-field"
              />
            </div>
          </div>
        </div>
      )}

      {/* Navigation Buttons */}
      <div className="flex items-center justify-between border-t border-white/[0.06] pt-4">
        {step > 1 ? (
          <button
            type="button"
            onClick={() => setStep((s) => s - 1)}
            className="btn-ghost text-xs"
          >
            <ArrowLeft size={14} /> Back
          </button>
        ) : (
          <button
            type="button"
            onClick={onCancel}
            className="btn-ghost text-xs"
          >
            Cancel
          </button>
        )}

        {step < 3 ? (
          <button
            type="button"
            onClick={() => {
              if (step === 1 && !name.trim()) return;
              setStep((s) => s + 1);
            }}
            className="btn-primary text-xs"
          >
            Next Step <ArrowRight size={14} />
          </button>
        ) : (
          <button
            type="button"
            onClick={handleFinish}
            disabled={isSubmitting}
            className="btn-primary text-xs"
          >
            <CheckCircle2 size={14} />
            {isSubmitting ? "Deploying..." : "Finalize & Launch Strategy"}
          </button>
        )}
      </div>
    </div>
  );
}
