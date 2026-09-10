import { useEffect, useState } from "react";
import { AlertCircle, X, Play, Zap, CheckCircle2 } from "lucide-react";
import { authFetch } from "../services/apiClient";
import { useToast } from "./Toast";
import { deployBrokerOptions, resolveDeployTarget } from "../utils/deployBroker";

export default function DeploymentModal({ isOpen, onClose, strategy, onDeployed }) {
  const [executionMode, setExecutionMode] = useState("PAPER");
  const [brokerName, setBrokerName] = useState("Simulated");
  const [brokerAccountId, setBrokerAccountId] = useState(null);
  const [multiplier, setMultiplier] = useState(1.0);
  const [capital, setCapital] = useState(strategy?.min_capital || 5000);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [deployedSuccess, setDeployedSuccess] = useState(false);
  const [deployError, setDeployError] = useState(null);
  const [connectedAccounts, setConnectedAccounts] = useState([]);
  const toast = useToast();

  // Fetch connected broker accounts when modal opens
  useEffect(() => {
    if (!isOpen) return;
    let cancelled = false;
    (async () => {
      try {
        const res = await authFetch("/api/brokers/accounts");
        if (!res.ok) throw new Error("Failed to load broker accounts");
        const data = await res.json();
        if (!cancelled) setConnectedAccounts(Array.isArray(data) ? data : data.accounts || []);
      } catch {
        if (!cancelled) setConnectedAccounts([]);
      }
    })();
    return () => { cancelled = true; };
  }, [isOpen]);

  const brokerOpts = deployBrokerOptions(connectedAccounts);

  // Reset transient state whenever a different strategy is deployed
  useEffect(() => {
    if (isOpen) {
      setDeployError(null);
      setDeployedSuccess(false);
      setExecutionMode("PAPER");
      setBrokerName("Simulated");
      setBrokerAccountId(null);
      setCapital(strategy?.min_capital || 5000);
    }
  }, [isOpen, strategy]);

  if (!isOpen || !strategy) return null;

  const handleDeploy = async (e) => {
    e.preventDefault();
    setIsSubmitting(true);
    setDeployError(null);
    try {
      const res = await authFetch(`/api/strategies/${strategy.id}/deploy`, {
        method: "POST",
        body: JSON.stringify({
          execution_mode: executionMode,
          broker_name: brokerName,
          broker_account_id: brokerAccountId,
          multiplier: Number(multiplier),
          capital_allocated: Number(capital),
        }),
      });

      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        // FastAPI validation errors return `detail` as an ARRAY of {loc,msg};
        // HTTPException returns a plain string. Normalize both so the UI shows
        // a real message instead of "[object Object]".
        const detail = Array.isArray(data.detail)
          ? data.detail.map((d) => d.msg || JSON.stringify(d)).join("; ")
          : data.detail;
        throw new Error(String(detail) || `Deployment rejected (HTTP ${res.status})`);
      }

      setDeployedSuccess(true);
      toast.success(`"${strategy.name}" is live`, {
        description: `${executionMode} execution via ${brokerName} · ₹${Number(capital).toLocaleString("en-IN")} allocated.`,
      });
      setTimeout(() => {
        if (onDeployed) onDeployed();
        onClose();
        setDeployedSuccess(false);
      }, 900);
    } catch (err) {
      setDeployError(err.message);
      toast.error("Deployment failed", { description: err.message });
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
                    const target = resolveDeployTarget("LIVE", connectedAccounts);
                    if (target.unavailable) {
                      setBrokerName("Simulated");
                      setBrokerAccountId(null);
                    } else {
                      setBrokerName(target.broker_name);
                      setBrokerAccountId(target.broker_id);
                    }
                  }}
                  className={`py-2 px-3 rounded-lg border text-xs font-semibold transition-all ${
                    executionMode === "LIVE"
                      ? "bg-emerald-500/20 text-emerald-300 border-emerald-500/40 shadow-sm"
                      : "bg-slate-800 text-slate-400 border-slate-700 hover:text-white"
                  }`}
                >
                  Live Broker{brokerOpts.length > 0 ? ` (${brokerOpts[0].broker_name})` : ""}
                </button>
              </div>
            </div>

            {/* Broker selection */}
            <div>
              <label className="text-xs font-medium text-slate-300">
                Target Broker Account
              </label>
              <select
                value={brokerAccountId || "Simulated"}
                onChange={(e) => {
                  if (e.target.value === "Simulated") {
                    setBrokerName("Simulated");
                    setBrokerAccountId(null);
                    setExecutionMode("PAPER");
                  } else {
                    const opt = brokerOpts.find((o) => o.broker_id === e.target.value);
                    setBrokerName(opt?.broker_name || e.target.value);
                    setBrokerAccountId(e.target.value);
                    setExecutionMode("LIVE");
                  }
                }}
                className="select-field w-full mt-1.5 text-xs"
              >
                <option value="Simulated">Simulated Mock Broker</option>
                {brokerOpts.map((o) => (
                  <option key={o.broker_id} value={o.broker_id}>{o.label}</option>
                ))}
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
                  Allocated Capital (₹)
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

            {deployError && (
          <div className="mb-3 flex items-start gap-2 p-2.5 rounded-lg bg-rose-500/10 border border-rose-500/30 text-rose-300 text-xs">
            <AlertCircle size={14} className="shrink-0 mt-0.5" />
            <span>{deployError}</span>
          </div>
        )}

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
