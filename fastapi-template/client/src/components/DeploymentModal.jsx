import { useState } from "react";
import { X, Play, ShieldCheck, Zap, DollarSign, CheckCircle2 } from "lucide-react";
import { API_BASE } from "../config";

export default function DeploymentModal({ isOpen, onClose, strategy, onDeployed }) {
  const [executionMode, setExecutionMode] = useState("PAPER");
  const [brokerName, setBrokerName] = useState("Simulated");
  const [multiplier, setMultiplier] = useState(1.0);
  const [capital, setCapital] = useState(strategy?.min_capital || 5000);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [deployedSuccess, setDeployedSuccess] = useState(false);

  if (!isOpen || !strategy) return null;

  const handleDeploy = async (e) => {
    e.preventDefault();
    setIsSubmitting(true);
    try {
      const res = await fetch(`${API_BASE}/api/strategies/${strategy.id}/deploy`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          execution_mode: executionMode,
          broker_name: brokerName,
          multiplier: Number(multiplier),
          capital_allocated: Number(capital),
        }),
      });

      if (res.ok) {
        setDeployedSuccess(true);
        setTimeout(() => {
          if (onDeployed) onDeployed();
          onClose();
          setDeployedSuccess(false);
        }, 900);
      }
    } catch (err) {
      console.error("Failed to deploy strategy:", err);
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 backdrop-blur-sm p-3.5 sm:p-4 animate-fade-in">
      <div className="card w-full max-w-md bg-slate-900 border border-slate-700 shadow-2xl p-4 sm:p-6 relative overflow-y-auto max-h-[90vh]">
        <button
          onClick={onClose}
          className="absolute right-4 top-4 text-slate-400 hover:text-white transition-colors"
        >
          <X size={18} />
        </button>

        <div className="flex items-center gap-3 mb-4">
          <div className="p-2.5 rounded-xl bg-cyan-500/10 text-cyan-400 border border-cyan-500/20">
            <Zap size={22} />
          </div>
          <div>
            <h2 className="text-base font-bold text-white tracking-tight">
              Deploy Strategy
            </h2>
            <p className="text-xs text-cyan-400 font-semibold">{strategy.name}</p>
          </div>
        </div>

        {deployedSuccess ? (
          <div className="py-8 text-center space-y-2">
            <CheckCircle2 size={40} className="mx-auto text-emerald-400 animate-bounce" />
            <h3 className="text-sm font-bold text-white">Strategy Deployed!</h3>
            <p className="text-xs text-slate-400">
              Live orders are now managed by {brokerName} ({executionMode} mode).
            </p>
          </div>
        ) : (
          <form onSubmit={handleDeploy} className="space-y-4">
            {/* Mode selection */}
            <div>
              <label className="text-xs font-medium text-slate-300">
                Execution Mode
              </label>
              <div className="grid grid-cols-2 gap-2 mt-1.5">
                <button
                  type="button"
                  onClick={() => {
                    setExecutionMode("PAPER");
                    setBrokerName("Simulated");
                  }}
                  className={`py-2 px-3 rounded-lg border text-xs font-semibold transition-all ${
                    executionMode === "PAPER"
                      ? "bg-cyan-500/20 text-cyan-300 border-cyan-500/40 shadow-sm"
                      : "bg-slate-800 text-slate-400 border-slate-700 hover:text-white"
                  }`}
                >
                  Paper Trading (Simulated)
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setExecutionMode("LIVE");
                    setBrokerName("Angel One");
                  }}
                  className={`py-2 px-3 rounded-lg border text-xs font-semibold transition-all ${
                    executionMode === "LIVE"
                      ? "bg-emerald-500/20 text-emerald-300 border-emerald-500/40 shadow-sm"
                      : "bg-slate-800 text-slate-400 border-slate-700 hover:text-white"
                  }`}
                >
                  Live Broker (Angel One)
                </button>
              </div>
            </div>

            {/* Broker selection */}
            <div>
              <label className="text-xs font-medium text-slate-300">
                Target Broker Account
              </label>
              <select
                value={brokerName}
                onChange={(e) => setBrokerName(e.target.value)}
                className="select-field w-full mt-1.5 text-xs"
              >
                <option value="Simulated">Simulated Mock Broker</option>
                <option value="Angel One">Angel One SmartAPI (Live)</option>
              </select>
            </div>

            {/* Multiplier & Capital */}
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="text-xs font-medium text-slate-300">
                  Multiplier (x)
                </label>
                <input
                  type="number"
                  step="0.1"
                  min="0.1"
                  max="10.0"
                  value={multiplier}
                  onChange={(e) => setMultiplier(e.target.value)}
                  className="input-field w-full mt-1.5 text-xs"
                  required
                />
              </div>

              <div>
                <label className="text-xs font-medium text-slate-300">
                  Allocated Capital ($)
                </label>
                <input
                  type="number"
                  step="500"
                  min="500"
                  value={capital}
                  onChange={(e) => setCapital(e.target.value)}
                  className="input-field w-full mt-1.5 text-xs"
                  required
                />
              </div>
            </div>

            {/* Strategy Specs Summary */}
            <div className="p-3 rounded-lg bg-slate-800/60 border border-slate-700/60 text-xs space-y-1 text-slate-400">
              <div className="flex justify-between">
                <span>Expected Win Rate:</span>
                <span className="font-bold text-white font-mono">{strategy.win_rate}%</span>
              </div>
              <div className="flex justify-between">
                <span>Historical Max DD:</span>
                <span className="font-bold text-loss-400 font-mono">-{strategy.max_drawdown_pct}%</span>
              </div>
            </div>

            <button
              type="submit"
              disabled={isSubmitting}
              className="btn-primary w-full justify-center py-2.5 mt-2"
            >
              <Play size={14} />
              {isSubmitting ? "Deploying..." : "Confirm & Deploy to Engine"}
            </button>
          </form>
        )}
      </div>
    </div>
  );
}
