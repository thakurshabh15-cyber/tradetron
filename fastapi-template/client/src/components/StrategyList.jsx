import React, { useState } from "react";
import { Power, Trash2, Cpu, Zap, ShieldCheck, AlertOctagon } from "lucide-react";
import { authFetch } from "../services/apiClient";
import ConfirmDialog from "./ConfirmDialog";
import { useToast } from "./Toast";

function StrategyListComponent({
  strategies = [],
  onToggle,
  onDelete,
}) {
  const [filterMode, setFilterMode] = useState("ALL"); // 'ALL' | 'LIVE' | 'PAPER'
  const [killSwitchLoading, setKillSwitchLoading] = useState(false);
  const [killConfirmOpen, setKillConfirmOpen] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState(null);
  const toast = useToast();

  const filteredStrategies = strategies.filter((s) => {
    if (filterMode === "ALL") return true;
    const mode = (s.execution_mode || "PAPER").toUpperCase();
    return mode === filterMode;
  });

  const liveCount = strategies.filter(
    (s) => (s.execution_mode || "PAPER").toUpperCase() === "LIVE" && s.enabled
  ).length;

  const handleKillSwitch = async () => {
    setKillSwitchLoading(true);
    try {
      const res = await authFetch("/api/strategies/kill-switch", {
        method: "POST",
        body: JSON.stringify({
          action: "PAUSE_ALL",
          reason: "Manual Emergency Kill-Switch Activated by Operator",
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || "Failed to trigger kill switch");
      toast.warning("Kill-switch engaged", {
        description: data.message || `${data.paused_strategies_count ?? 0} strategies paused.`,
      });
    } catch (err) {
      toast.error("Kill-switch failed", { description: err.message });
    } finally {
      setKillSwitchLoading(false);
      setKillConfirmOpen(false);
    }
  };

  const handleDeleteClick = (strat) => {
    setDeleteTarget(strat);
  };

  const handleDeleteConfirm = async () => {
    if (!deleteTarget) return;
    const ok = await onDelete(deleteTarget.id);
    if (ok !== false) {
      toast.success(`"${deleteTarget.name}" deleted`);
    }
    setDeleteTarget(null);
  };

  return (
    <div className="glass-card space-y-4">
      {/* Top Header & Mode Filters */}
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between border-b border-white/[0.06] pb-3">
        <div className="flex items-center gap-2">
          <Cpu size={18} className="text-accent-400" />
          <h2 className="text-base font-semibold text-white">Strategy Portfolio</h2>
          {liveCount > 0 && (
            <span className="flex items-center gap-1.5 px-2 py-0.5 rounded-full text-[10px] font-bold bg-emerald-500/20 text-emerald-400 border border-emerald-500/30 animate-pulse">
              <span className="h-1.5 w-1.5 rounded-full bg-emerald-400"></span>
              {liveCount} LIVE TRADING ACTIVE
            </span>
          )}
        </div>

        <div className="flex items-center gap-2">
          {/* Mode Filter Toggle */}
          <div className="flex items-center gap-1 p-0.5 rounded-lg bg-surface-900 border border-white/[0.06] text-xs">
            <button
              onClick={() => setFilterMode("ALL")}
              className={`px-2.5 py-1 rounded font-medium transition-all ${
                filterMode === "ALL"
                  ? "bg-surface-700 text-white font-semibold"
                  : "text-slate-400 hover:text-slate-200"
              }`}
            >
              All ({strategies.length})
            </button>
            <button
              onClick={() => setFilterMode("LIVE")}
              className={`flex items-center gap-1 px-2.5 py-1 rounded font-medium transition-all ${
                filterMode === "LIVE"
                  ? "bg-emerald-500 text-slate-950 font-bold shadow"
                  : "text-emerald-400 hover:text-emerald-300"
              }`}
            >
              <Zap size={11} /> Live
            </button>
            <button
              onClick={() => setFilterMode("PAPER")}
              className={`flex items-center gap-1 px-2.5 py-1 rounded font-medium transition-all ${
                filterMode === "PAPER"
                  ? "bg-slate-700 text-white font-semibold"
                  : "text-slate-400 hover:text-slate-200"
              }`}
            >
              <ShieldCheck size={11} className="text-cyan-400" /> Paper
            </button>
          </div>

          {/* Emergency Kill Switch Button */}
          <button
            onClick={() => setKillConfirmOpen(true)}
            disabled={killSwitchLoading}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-bold bg-loss-500/20 text-loss-400 border border-loss-500/40 hover:bg-loss-500/30 transition-all shadow-sm active:scale-95"
            title="Emergency Panic Button: Immediately pause all running strategies"
          >
            <AlertOctagon size={13} className="text-loss-400" />
            {killSwitchLoading ? "Halting..." : "Kill-Switch"}
          </button>
        </div>
      </div>

      {filteredStrategies.length === 0 ? (
        <div className="text-center py-8 text-slate-500 text-xs">
          No strategies found matching mode filter "{filterMode}".
        </div>
      ) : (
        <div className="grid gap-3">
          {filteredStrategies.map((strat) => {
            const isLive = (strat.execution_mode || "PAPER").toUpperCase() === "LIVE";

            return (
              <div
                key={strat.id}
                className={`flex flex-col sm:flex-row sm:items-center justify-between gap-3 rounded-lg p-4 transition-all ${
                  isLive
                    ? "bg-emerald-950/20 border border-emerald-500/30 hover:border-emerald-500/50 shadow-md shadow-emerald-950/40"
                    : "bg-surface-800/60 border border-white/[0.04] hover:bg-surface-800"
                }`}
              >
                <div className="space-y-1.5">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="font-semibold text-white text-sm">
                      {strat.name}
                    </span>

                    {/* Hard Mode Badge */}
                    {isLive ? (
                      <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-[10px] font-bold bg-emerald-500/20 text-emerald-300 border border-emerald-500/40">
                        <Zap size={10} className="text-emerald-400" /> LIVE TRADING
                      </span>
                    ) : (
                      <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-[10px] font-semibold bg-slate-800 text-slate-400 border border-white/[0.08]">
                        <ShieldCheck size={10} className="text-cyan-400" /> PAPER SIMULATION
                      </span>
                    )}

                    {/* Status Badge */}
                    <span
                      className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-medium ${
                        strat.enabled
                          ? "bg-profit-500/10 text-profit-400 border border-profit-500/20"
                          : "bg-surface-700 text-slate-400"
                      }`}
                    >
                      <span
                        className={`h-1.5 w-1.5 rounded-full ${
                          strat.enabled ? "bg-profit-400 animate-pulse" : "bg-slate-500"
                        }`}
                      ></span>
                      {strat.enabled ? "RUNNING" : "PAUSED"}
                    </span>
                  </div>

                  <div className="flex items-center gap-3 text-xs text-slate-400 flex-wrap">
                    <span>
                      Symbols:{" "}
                      <strong className="text-slate-200">
                        {strat.symbols.join(", ")}
                      </strong>
                    </span>
                    <span>•</span>
                    <span>
                      Action:{" "}
                      <strong className="text-slate-200">
                        {strat.action.side} {strat.action.quantity}
                      </strong>
                    </span>
                    <span>•</span>
                    <span>
                      Capital:{" "}
                      <strong className={isLive ? "text-emerald-300 font-bold" : "text-slate-200"}>
                        ₹{Number(strat.capital_allocated || 10000).toLocaleString("en-IN")}
                      </strong>
                    </span>
                    <span>•</span>
                    <span>
                      Rules:{" "}
                      <strong className="text-slate-200">
                        {strat.conditions.length} condition(s)
                      </strong>
                    </span>
                  </div>
                </div>

                <div className="flex items-center gap-2 self-end sm:self-center">
                  <button
                    onClick={() => onToggle(strat.id, !strat.enabled)}
                    title={strat.enabled ? "Pause Strategy" : "Activate Strategy"}
                    className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold transition-all ${
                      strat.enabled
                        ? "bg-profit-500/20 text-profit-400 border border-profit-500/30 hover:bg-profit-500/30"
                        : "bg-surface-700 text-slate-300 hover:bg-surface-600"
                    }`}
                  >
                    <Power size={13} />
                    {strat.enabled ? "Active" : "Paused"}
                  </button>

                  <button
                    onClick={() => handleDeleteClick(strat)}
                    title="Delete Strategy"
                    className="p-1.5 rounded-lg text-slate-400 hover:bg-loss-500/10 hover:text-loss-400 transition-colors"
                  >
                    <Trash2 size={15} />
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      )}

      {/* Kill-Switch Confirmation */}
      <ConfirmDialog
        isOpen={killConfirmOpen}
        onClose={() => setKillConfirmOpen(false)}
        onConfirm={handleKillSwitch}
        title="Engage Emergency Kill-Switch?"
        message="This immediately halts all live trading order dispatch and pauses every running strategy. This cannot be undone from here."
        confirmLabel={killSwitchLoading ? "Halting…" : "Confirm Kill-Switch"}
        variant="critical"
        loading={killSwitchLoading}
      />

      {/* Strategy Deletion Confirmation */}
      <ConfirmDialog
        isOpen={Boolean(deleteTarget)}
        onClose={() => setDeleteTarget(null)}
        onConfirm={handleDeleteConfirm}
        title="Delete this strategy?"
        message={`"${deleteTarget?.name || "This strategy"}" and its full trading history will be permanently removed. This cannot be undone.`}
        confirmLabel="Permanently Delete"
        variant="danger"
      />
    </div>
  );
}

export const StrategyList = React.memo(StrategyListComponent);
export default StrategyList;
