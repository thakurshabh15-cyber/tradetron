import { useState } from "react";
import { useApi } from "../hooks/useApi";
import {
  CheckCircle2,
  Clock,
  Store,
  Building2,
  Crown,
  Sparkles,
  ArrowRight,
} from "lucide-react";
import StatusBadge from "./StatusBadge";
import { API_BASE } from "../config";

const TASK_ICONS = {
  marketplace_setup: Store,
  broker_setup: Building2,
  subscription_setup: Crown,
};

export default function SetupChecklist() {
  const { data: setupData, loading, error, refetch, patch } = useApi("/api/user/setup-status");
  const [updatingTaskId, setUpdatingTaskId] = useState(null);

  const handleToggle = async (taskId, currentStatus) => {
    setUpdatingTaskId(taskId);
    const nextStatus = currentStatus === "Complete" ? "Pending" : "Complete";
    try {
      await fetch(`${API_BASE}/api/user/setup-status`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ task_id: taskId, status: nextStatus }),
      });
      await refetch();
    } catch (err) {
      console.error("Failed to toggle status:", err);
    } finally {
      setUpdatingTaskId(null);
    }
  };

  if (loading && !setupData) {
    return (
      <div className="glass-card p-6 animate-pulse">
        <div className="h-5 w-48 bg-surface-700 rounded mb-4"></div>
        <div className="grid gap-4 md:grid-cols-3">
          <div className="h-24 bg-surface-800 rounded-lg"></div>
          <div className="h-24 bg-surface-800 rounded-lg"></div>
          <div className="h-24 bg-surface-800 rounded-lg"></div>
        </div>
      </div>
    );
  }

  const tasks = setupData?.tasks || [];
  const progressPct = setupData?.overall_progress_pct ?? 0;
  const completedCount = setupData?.completed_count ?? 0;
  const totalCount = setupData?.total_count ?? 3;

  return (
    <div className="glass-card relative overflow-hidden space-y-4">
      {/* Background ambient glow */}
      <div className="absolute -top-12 -right-12 h-40 w-40 rounded-full bg-accent-500/10 blur-3xl pointer-events-none" />

      {/* Header with Progress Bar */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 border-b border-white/[0.06] pb-4">
        <div>
          <div className="flex items-center gap-2">
            <Sparkles size={16} className="text-accent-400" />
            <h2 className="text-sm font-semibold uppercase tracking-wider text-white">
              Account Onboarding & Setup Status
            </h2>
          </div>
          <p className="text-xs text-slate-400 mt-0.5">
            Complete the 3 foundation steps to unlock high-frequency live automated trading.
          </p>
        </div>

        <div className="flex items-center gap-3">
          <div className="text-right">
            <span className="text-xs font-mono font-bold text-white">
              {completedCount}/{totalCount} Completed
            </span>
            <span className="text-[11px] text-slate-400 block font-mono">
              ({progressPct}%)
            </span>
          </div>

          <div className="w-28 sm:w-36 h-2.5 rounded-full bg-surface-800 overflow-hidden border border-white/[0.04]">
            <div
              className={`h-full transition-all duration-500 rounded-full ${
                progressPct === 100
                  ? "bg-profit-500 shadow-lg shadow-profit-500/30"
                  : "bg-accent-500"
              }`}
              style={{ width: `${progressPct}%` }}
            />
          </div>
        </div>
      </div>

      {/* Tasks Grid */}
      <div className="grid gap-3 md:grid-cols-3">
        {tasks.map((task) => {
          const Icon = TASK_ICONS[task.id] || Store;
          const isComplete = task.status === "Complete";
          const isUpdating = updatingTaskId === task.id;

          return (
            <div
              key={task.id}
              className={`relative flex flex-col justify-between rounded-xl p-4 transition-all duration-200 border ${
                isComplete
                  ? "bg-surface-900/60 border-profit-500/20 hover:border-profit-500/40"
                  : "bg-surface-900/40 border-amber-500/20 hover:border-amber-500/40"
              }`}
            >
              <div>
                <div className="flex items-start justify-between gap-2 mb-2">
                  <div className="flex items-center gap-2.5">
                    <div
                      className={`flex h-8 w-8 items-center justify-center rounded-lg ${
                        isComplete
                          ? "bg-profit-500/10 text-profit-400"
                          : "bg-amber-500/10 text-amber-400"
                      }`}
                    >
                      <Icon size={16} />
                    </div>
                    <h3 className="text-sm font-semibold text-white tracking-tight">
                      {task.title}
                    </h3>
                  </div>

                  <StatusBadge status={task.status} />
                </div>

                <p className="text-xs text-slate-400 leading-relaxed mb-4">
                  {task.description}
                </p>
              </div>

              <div className="flex items-center justify-between pt-3 border-t border-white/[0.04] text-[11px]">
                <span className="text-slate-500 flex items-center gap-1">
                  {isComplete ? (
                    <>
                      <CheckCircle2 size={13} className="text-profit-400" />
                      Active
                    </>
                  ) : (
                    <>
                      <Clock size={13} className="text-amber-400" />
                      Action Required
                    </>
                  )}
                </span>

                <button
                  type="button"
                  disabled={isUpdating}
                  onClick={() => handleToggle(task.id, task.status)}
                  className={`inline-flex items-center gap-1 font-medium transition-colors cursor-pointer ${
                    isComplete
                      ? "text-slate-400 hover:text-slate-200"
                      : "text-accent-400 hover:text-accent-300 font-semibold"
                  }`}
                >
                  {isUpdating ? (
                    "Updating..."
                  ) : isComplete ? (
                    "Mark Pending"
                  ) : (
                    <>
                      Mark Complete <ArrowRight size={12} />
                    </>
                  )}
                </button>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
