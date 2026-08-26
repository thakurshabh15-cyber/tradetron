import { useEffect, useState } from "react";
import { AlertTriangle, Power, CheckCircle2, X } from "lucide-react";
import { authFetch } from "../services/apiClient";
import { useToast } from "./Toast";

export default function KillSwitchModal({ isOpen, onClose, onKillSuccess }) {
  const [loading, setLoading] = useState(false);
  const [reason, setReason] = useState("Manual operator panic halt");
  const [actionType, setActionType] = useState("PAUSE_ALL");
  const [error, setError] = useState(null);
  const [result, setResult] = useState(null);
  const toast = useToast();

  // ESC to dismiss + body scroll lock while the panic modal is open
  useEffect(() => {
    if (!isOpen) return undefined;
    const onKeyDown = (e) => {
      if (e.key === "Escape" && !loading) onClose();
    };
    document.addEventListener("keydown", onKeyDown);
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = "";
    };
  }, [isOpen, loading, onClose]);

  if (!isOpen) return null;

  const handleTriggerKillSwitch = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await authFetch("/api/strategies/kill-switch", {
        method: "POST",
        body: JSON.stringify({ action: actionType, reason }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || "Failed to trigger kill switch");

      setResult(data);
      toast.warning(
        actionType === "SQUARE_OFF_ALL" ? "Panic stop engaged — positions squared off" : "Panic stop engaged",
        { description: data.message || `${data.paused_strategies_count ?? 0} strategies paused.` }
      );
      if (onKillSuccess) onKillSuccess(data);
    } catch (err) {
      setError(err.message);
      toast.error("Kill-switch failed", { description: err.message });
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-3.5 sm:p-4 bg-black/85 backdrop-blur-md animate-fade-in">
      <div className="relative w-full max-w-md bg-slate-900 border-2 border-red-500/40 rounded-2xl shadow-2xl p-4 sm:p-6 overflow-y-auto max-h-[90vh]">
        {/* Pulsing Red Accent */}
        <div className="absolute top-0 left-0 right-0 h-1.5 bg-gradient-to-r from-red-600 via-rose-500 to-amber-600 animate-pulse" />

        <button
          onClick={onClose}
          className="absolute top-4 right-4 text-slate-400 hover:text-white p-1 rounded-lg hover:bg-slate-800 transition-colors"
        >
          <X size={18} />
        </button>

        <div className="text-center mb-5">
          <div className="inline-flex items-center justify-center w-12 h-12 rounded-full bg-red-500/20 text-red-400 border border-red-500/30 mb-3">
            <AlertTriangle size={24} />
          </div>
          <h2 className="text-xl font-bold text-white tracking-tight">
            Emergency Strategy Kill-Switch
          </h2>
          <p className="text-xs text-slate-400 mt-1">
            Instantly halt all automated order routing, pause active strategy deployments, and lock execution engine.
          </p>
        </div>

        {error && (
          <div className="mb-4 p-3 rounded-lg bg-red-500/10 border border-red-500/30 text-red-400 text-xs">
            {error}
          </div>
        )}

        {result ? (
          <div className="space-y-4">
            <div className="p-4 rounded-xl bg-red-950/40 border border-red-500/30 text-red-300 text-xs space-y-2">
              <div className="flex items-center gap-2 font-bold text-sm text-red-400">
                <CheckCircle2 size={16} />
                <span>KILL-SWITCH ENGAGED</span>
              </div>
              <p>{result.message}</p>
              <div className="pt-2 border-t border-red-500/20 flex justify-between font-mono text-[11px]">
                <span>Paused Strategies:</span>
                <span className="font-bold text-white">{result.paused_strategies_count ?? 0}</span>
              </div>
            </div>

            <button
              onClick={onClose}
              className="w-full py-2.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-white font-bold text-xs transition-all"
            >
              Close & Monitor Dashboard
            </button>
          </div>
        ) : (
          <div className="space-y-4">
            <div>
              <label className="text-[11px] font-semibold text-slate-300">Action Scope</label>
              <div className="grid grid-cols-2 gap-2 mt-1.5">
                <button
                  type="button"
                  onClick={() => setActionType("PAUSE_ALL")}
                  className={`py-2 px-3 rounded-lg text-xs font-semibold border transition-all ${
                    actionType === "PAUSE_ALL"
                      ? "bg-red-500/20 text-red-300 border-red-500/40 shadow-sm"
                      : "bg-slate-800 text-slate-400 border-slate-700 hover:text-white"
                  }`}
                >
                  Pause All Strategies
                </button>
                <button
                  type="button"
                  onClick={() => setActionType("SQUARE_OFF_ALL")}
                  className={`py-2 px-3 rounded-lg text-xs font-semibold border transition-all ${
                    actionType === "SQUARE_OFF_ALL"
                      ? "bg-red-500/20 text-red-300 border-red-500/40 shadow-sm"
                      : "bg-slate-800 text-slate-400 border-slate-700 hover:text-white"
                  }`}
                >
                  Pause & Square-Off
                </button>
              </div>
            </div>

            <div>
              <label className="text-[11px] font-semibold text-slate-300">Audit Reason</label>
              <input
                type="text"
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                placeholder="Market extreme volatility / unexpected drawdown"
                className="w-full mt-1 bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-xs text-white placeholder-slate-500 focus:outline-none focus:border-red-500"
              />
            </div>

            <div className="flex gap-2 pt-2">
              <button
                type="button"
                onClick={onClose}
                className="flex-1 py-2.5 rounded-lg bg-slate-800 hover:bg-slate-750 border border-slate-700 text-slate-300 text-xs font-semibold transition-all"
              >
                Cancel
              </button>
              <button
                type="button"
                disabled={loading}
                onClick={handleTriggerKillSwitch}
                className="flex-1 py-2.5 rounded-lg bg-gradient-to-r from-red-600 to-rose-600 hover:from-red-500 hover:to-rose-500 text-white font-bold text-xs transition-all shadow-lg shadow-red-600/30 flex items-center justify-center gap-1.5 disabled:opacity-50"
              >
                <Power size={14} />
                {loading ? "Halting..." : "EXECUTE PANIC STOP"}
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
