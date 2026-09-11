import { useState } from "react";
import { Sliders, Shield, CheckCircle2, AlertCircle, Info } from "lucide-react";
import { useToast } from "./Toast";

export default function StrategyConfiguratorScreen({ strategy, onSave, onCancel }) {
  const toast = useToast();
  const [multiplier, setMultiplier] = useState(1.0);
  const [maxPositions, setMaxPositions] = useState(5);
  const [stopLossPct, setStopLossPct] = useState(2.0);
  const [takeProfitPct, setTakeProfitPct] = useState(5.0);
  const [trailingStop, setTrailingStop] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const [saveError, setSaveError] = useState(null);

  const handleSave = async (e) => {
    e.preventDefault();
    if (!strategy?.id) {
      setSaveError("This strategy is not persisted yet — save it via the builder first.");
      return;
    }
    setIsSaving(true);
    setSaveError(null);
    try {
      // onSave is an async backend-backed callback: it PATCHes the strategy
      // record and only resolves after the server confirms the update.
      await onSave({
        multiplier: Number(multiplier) || 1,
        maxPositions: Number(maxPositions) || 5,
        stopLossPct: Number(stopLossPct) || 2,
        takeProfitPct: Number(takeProfitPct) || 5,
        trailingStop: Boolean(trailingStop),
      });
      toast.success(`Configuration saved for "${strategy.name}"`, {
        description: "Risk-gating parameters staged for the deployment engine.",
      });
    } catch (err) {
      const msg = err?.message || "Failed to save configuration";
      setSaveError(msg);
      toast.error("Configuration save failed", { description: msg });
    } finally {
      setIsSaving(false);
    }
  };

  return (
    <form onSubmit={handleSave} className="glass-card space-y-5 animate-fade-in">
      <div className="flex items-center justify-between border-b border-white/[0.06] pb-3">
        <div className="flex items-center gap-2">
          <Sliders size={18} className="text-accent-400" />
          <div>
            <h2 className="text-base font-semibold text-white">
              Strategy Configurator & Risk Gating
            </h2>
            <p className="text-xs text-slate-400">
              Tune parameters for {strategy?.name || "Active Strategy"}
            </p>
          </div>
        </div>
      </div>

      <div className="grid gap-4 md:grid-cols-2">
        <div>
          <label className="mb-1 block text-xs font-medium text-slate-400">
            Order Capital Multiplier
          </label>
          <input
            type="number"
            step="0.1"
            min="0.1"
            max="10.0"
            value={multiplier}
            onChange={(e) => setMultiplier(Number(e.target.value))}
            className="input-field text-xs"
          />
        </div>

        <div>
          <label className="mb-1 block text-xs font-medium text-slate-400">
            Max Concurrent Positions
          </label>
          <input
            type="number"
            min="1"
            max="20"
            value={maxPositions}
            onChange={(e) => setMaxPositions(Number(e.target.value))}
            className="input-field text-xs"
          />
        </div>
      </div>

      <div className="grid gap-4 md:grid-cols-2">
        <div>
          <label className="mb-1 block text-xs font-medium text-slate-400">
            Dynamic Stop Loss (%)
          </label>
          <input
            type="number"
            step="0.1"
            min="0.5"
            max="15.0"
            value={stopLossPct}
            onChange={(e) => setStopLossPct(Number(e.target.value))}
            className="input-field text-xs"
          />
        </div>

        <div>
          <label className="mb-1 block text-xs font-medium text-slate-400">
            Target Take Profit (%)
          </label>
          <input
            type="number"
            step="0.1"
            min="1.0"
            max="40.0"
            value={takeProfitPct}
            onChange={(e) => setTakeProfitPct(Number(e.target.value))}
            className="input-field text-xs"
          />
        </div>
      </div>

      <div className="p-3 rounded-lg bg-surface-800/80 border border-white/[0.04] flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Shield size={16} className="text-cyan-400" />
          <div>
            <p className="text-xs font-semibold text-white">Trailing Stop Activation</p>
            <p className="text-[10px] text-slate-400">Auto-lock profits as market moves in favor</p>
          </div>
        </div>
        <input
          type="checkbox"
          checked={trailingStop}
          onChange={(e) => setTrailingStop(e.target.checked)}
          className="h-4 w-4 rounded border-slate-700 bg-surface-900 text-accent-500"
        />
      </div>

      {saveError && (
        <div className="flex items-start gap-2 p-3 rounded-lg bg-rose-500/10 border border-rose-500/30 text-rose-300 text-xs">
          <AlertCircle size={14} className="shrink-0 mt-0.5" />
          <span>{saveError}</span>
        </div>
      )}

      <div className="p-3 rounded-lg bg-surface-800/80 border border-white/[0.04] text-[10px] text-slate-400 leading-relaxed flex items-start gap-2">
        <Info size={13} className="shrink-0 mt-0.5 text-cyan-400" />
        <span>
          The order <strong className="text-slate-200">multiplier</strong> scales this strategy's allocated capital
          (₹{Number(strategy?.capital_allocated ?? 0).toLocaleString("en-IN")} → updated value) and is persisted to the
          backend. Stop-loss / take-profit / position-cap rules are staged here and enforced by the deployment engine
          on the strategy's next execution cycle.
        </span>
      </div>

      <div className="flex items-center justify-end gap-3 pt-3 border-t border-white/[0.06]">
        <button type="button" onClick={onCancel} className="btn-ghost text-xs">
          Cancel
        </button>
        <button type="submit" disabled={isSaving} className="btn-primary text-xs">
          <CheckCircle2 size={14} />
          {isSaving ? "Applying..." : "Save Configuration"}
        </button>
      </div>
    </form>
  );
}
