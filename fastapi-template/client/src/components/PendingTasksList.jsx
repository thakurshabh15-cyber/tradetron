import { useState } from "react";
import { CheckCircle2, Clock, ArrowRight, ShieldAlert } from "lucide-react";
import { API_BASE } from "../config";

export default function PendingTasksList({ initialTasks = [], onTaskToggled }) {
  const [tasks, setTasks] = useState(initialTasks);
  const [togglingId, setTogglingId] = useState(null);

  const handleToggle = async (taskId, currentStatus) => {
    setTogglingId(taskId);
    try {
      const res = await fetch(`${API_BASE}/api/dashboard/complete-task`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ task_id: taskId, completed: !currentStatus }),
      });
      if (res.ok) {
        setTasks((prev) =>
          prev.map((t) =>
            t.id === taskId ? { ...t, is_completed: !currentStatus } : t
          )
        );
        if (onTaskToggled) onTaskToggled();
      }
    } catch (err) {
      console.error("Failed to complete task:", err);
    } finally {
      setTogglingId(null);
    }
  };

  const pending = tasks.filter((t) => !t.is_completed);

  return (
    <div className="card p-5 bg-slate-900 border border-slate-800 rounded-xl shadow-lg">
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-2.5">
          <div className="p-2 rounded-lg bg-cyan-500/10 text-cyan-400 border border-cyan-500/20">
            <Clock size={18} />
          </div>
          <div>
            <h2 className="text-sm font-bold text-white tracking-tight">
              Actionable Tasks ({pending.length})
            </h2>
            <p className="text-[11px] text-slate-400">
              Pending items to maximize algorithm throughput
            </p>
          </div>
        </div>
      </div>

      {pending.length === 0 ? (
        <div className="flex items-center gap-3 p-4 rounded-lg bg-emerald-500/10 border border-emerald-500/20 text-emerald-400 text-xs">
          <CheckCircle2 size={18} className="shrink-0" />
          <span>All onboarding & setup tasks are completed! Desk fully operational.</span>
        </div>
      ) : (
        <div className="space-y-2.5">
          {pending.map((task) => (
            <div
              key={task.id}
              className="flex items-center justify-between p-3 rounded-lg bg-slate-850 hover:bg-slate-800/60 border border-slate-800 transition-all"
            >
              <div className="flex items-start gap-3 pr-2">
                <div className="p-1 rounded-md bg-amber-500/10 text-amber-400 border border-amber-500/20 mt-0.5">
                  <ShieldAlert size={14} />
                </div>
                <div>
                  <h3 className="text-xs font-semibold text-white">
                    {task.title}
                  </h3>
                  <p className="text-[11px] text-slate-400 mt-0.5 line-clamp-1">
                    {task.description}
                  </p>
                </div>
              </div>

              <button
                onClick={() => handleToggle(task.id, task.is_completed)}
                disabled={togglingId === task.id}
                className="shrink-0 flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-cyan-500/10 hover:bg-cyan-500/20 text-cyan-300 border border-cyan-500/30 text-xs font-medium transition-all hover:border-cyan-400/50 disabled:opacity-50"
              >
                {togglingId === task.id ? (
                  "Updating..."
                ) : (
                  <>
                    Mark Done <ArrowRight size={12} />
                  </>
                )}
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
