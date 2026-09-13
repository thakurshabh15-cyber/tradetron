import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Bot,
  Play,
  Pause,
  RotateCcw,
  Square,
  ShieldCheck,
  Save,
  Plus,
  AlertTriangle,
  CheckCircle2,
  Activity,
  ClipboardList,
  Target,
  FileText,
  Boxes,
  Layers,
  Lock,
} from "lucide-react";
import { useApi } from "../hooks/useApi";
import { authFetch } from "../services/apiClient";
import { useToast } from "../components/Toast";
import StatusBadge from "../components/StatusBadge";
import { useWebSocket } from "../hooks/useWebSocket";
import { createLiveRefresher, isConsoleRelevantEvent } from "./consoleLiveSync";

/**
 * Agent Console — the CONTROL PLANE for the autonomous trading agent.
 *
 * Every button here funnels through the backend `/api/agent` router ONLY, so
 * the ownership/LIVE/cas gates can never be bypassed by this page. The page is
 * a faithful rendering of the server snapshot — nothing is fabricated; if no
 * agent config exists yet the page shows the "create config" control.
 *
 * REAL-TIME SYNC: a single tenant-scoped `/ws/trades` subscription drives
 * reconciliation. Every console-relevant event (fills, closes, rejections,
 * trading halts, config/lifecycle/approval/decision broadcasts) already lands
 * on that channel; events are NEVER rendered as UI state — they only debounce
 * into a refetch of the authoritative `/api/agent/console` snapshot. While the
 * socket is live the page never polls; the 10s watchdog is retained ONLY as an
 * offline fallback while RUNNING with the socket disconnected/reconnecting.
 */

const AUTONOMY_LABELS = {
  0: "OBSERVE",
  1: "APPROVAL",
  2: "PAPER",
  3: "LIVE",
};

const STATUS_COLORS = {
  IDLE: "bg-slate-500/20 text-slate-300",
  RUNNING: "bg-emerald-500/20 text-emerald-300",
  PAUSED: "bg-amber-500/20 text-amber-300",
  STOPPED: "bg-slate-500/20 text-slate-400",
  FAILED: "bg-red-500/20 text-red-300",
};

const TERMINAL_STATUSES = new Set(["COMPLETED", "FAILED", "CANCELLED"]);

async function readDetail(res) {
  try {
    const data = await res.json();
    if (data && typeof data.detail === "object" && data.detail?.message) {
      return { code: data.detail.code, message: data.detail.message };
    }
    if (data && typeof data.detail === "string") return { message: data.detail };
  } catch {
    /* non-JSON body — fall through */
  }
  return { message: `HTTP ${res.status}` };
}

function fmtTs(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

function SectionCard({ icon: Icon, title, subtitle, children }) {
  return (
    <section className="glass-card p-5 space-y-4">
      <div className="flex items-start justify-between gap-3 border-b border-white/[0.06] pb-3">
        <div className="flex items-center gap-2">
          <Icon size={16} className="text-cyan-400" />
          <div>
            <h3 className="text-sm font-semibold text-white">{title}</h3>
            {subtitle && <p className="text-[11px] text-slate-500 mt-0.5">{subtitle}</p>}
          </div>
        </div>
      </div>
      {children}
    </section>
  );
}

export default function AgentConsole() {
  const toast = useToast();
  const { data, loading, error, refetch } = useApi("/api/agent/console");

  // ── live sync (single tenant-scoped /ws/trades subscription) ─────────────
  // Every console-relevant event (order_executed, trade_closed, order_rejected,
  // USER_TRADING_HALT, agent_configured, agent_state_changed, agent_approval,
  // agent_decision) already broadcasts to the "trades" channel — no new feed.
  // Events NEVER become UI state: they only debounce into a refetch of the
  // authoritative snapshot below, so the page always renders server truth.
  const liveRefresher = useRef(null);
  // Create the refresher in an effect — refs are never read or written during
  // render; the effect re-creates only if a future refetch identity ever
  // changes, and its cleanup cancels a pending debounce timer (no leak on
  // unmount or pre-emption).
  useEffect(() => {
    const refresher = createLiveRefresher({
      delayMs: 500, // a burst of events collapses into exactly one refetch
      onRefresh: () => refetch(),
    });
    liveRefresher.current = refresher;
    return () => refresher.cancel();
  }, [refetch]);
  // Stable handler (module-level predicate + stable ref) — never re-opens the
  // socket and costs nothing per render. Ref is read only when a message
  // arrives (outside render).
  const handleLiveEvent = useCallback((payload) => {
    // Fail-open: allowlisted and unknown object events reconcile; only
    // malformed/non-object payloads are ignored.
    if (isConsoleRelevantEvent(payload)) liveRefresher.current?.notify();
  }, []);
  const { isConnected: wsConnected } = useWebSocket("/ws/trades", {
    onMessage: handleLiveEvent,
  });

  const [busyAction, setBusyAction] = useState(null);
  const [formDirty, setFormDirty] = useState(false);
  const [createMode, setCreateMode] = useState(false);
  const [lastError, setLastError] = useState(null);

  // ── form state (synced from the authoritative server snapshot) ───────────
  const config = data?.config || null;
  const gate = data?.gate || {};
  // Initial values derive from the snapshot so a deterministic render (SSR /
  // tests) is truthful from the first paint; the effect below re-syncs after
  // the live fetch resolves and after every refetch().
  const [name, setName] = useState(config?.name || "");
  const [symbolsText, setSymbolsText] = useState((config?.symbols || []).join(", "));
  const [executionMode, setExecutionMode] = useState(config?.execution_mode || "PAPER");

  const [autonomyLevel, setAutonomyLevel] = useState(config?.autonomy_level ?? 0);
  const [approvalJson, setApprovalJson] = useState(
    JSON.stringify(config?.approval_policy || {}, null, 2),
  );
  const [riskJson, setRiskJson] = useState(
    JSON.stringify(config?.risk_policy || {}, null, 2),
  );

  // live-sync guard: a snapshot arrival (initial fetch, manual action refetch
  // OR WebSocket-driven refetch) must NEVER overwrite an operator's unsaved
  // edits — formDirty is set on every keystroke/select change.
  useEffect(() => {
    if (!config || formDirty) return;
    setName(config.name || "");
    setSymbolsText((config.symbols || []).join(", "));
    setExecutionMode(config.execution_mode || "PAPER");
    setAutonomyLevel(config.autonomy_level ?? 0);
    setApprovalJson(JSON.stringify(config.approval_policy || {}, null, 2));
    setRiskJson(JSON.stringify(config.risk_policy || {}, null, 2));
  }, [config, formDirty]);

  const ceiling = gate.effective_autonomy_ceiling ?? 0;
  const liveOk = gate.broker_mode === "live";
  const overCeiling = ceiling < autonomyLevel;

  const canStart = ["IDLE", "STOPPED", "FAILED", "PAUSED"].includes(config?.status);
  const canPause = config?.status === "RUNNING";
  const canResume = config?.status === "PAUSED";
  const canStop = ["RUNNING", "PAUSED", "IDLE", "FAILED"].includes(config?.status);
  const isRunning = config?.status === "RUNNING";

  // Reconnect reconciliation: the instant the socket (re)opens, re-read the
  // server snapshot once so any broadcast missed during the outage is never
  // stuck on screen. wsConnected starts false on mount, so this fires ONLY on
  // a real disconnected→connected transition — never on the initial fetch.
  useEffect(() => {
    if (wsConnected) refetch();
  }, [wsConnected, refetch]);

  // Offline watchdog ONLY: while RUNNING with the WebSocket disconnected or
  // reconnecting, keep the 10s poll as the fallback. While the socket is live
  // the console is event-driven — no blind polling at all.
  useEffect(() => {
    if (!isRunning || wsConnected) return undefined;
    const t = setInterval(() => refetch(), 10_000);
    return () => clearInterval(t);
  }, [isRunning, wsConnected, refetch]);

  // Timers must never survive unmount (the refresher's cleanup effect above
  // cancels the debounce timer; the watchdog clears its interval on re-run).
  // No separate unmount effect needed — both are covered by their own cleanups.

const runLifecycle = async (action) => {
    setBusyAction(action);
    setLastError(null);
    try {
      const res = await authFetch(`/api/agent/config/${action}`, { method: "POST" });
      if (!res.ok) {
        const d = await readDetail(res);
        setLastError(d.message);
        toast.error(`${action} refused`, { description: d.message });
        return;
      }
      await refetch();
      toast.success(`Agent ${action} acknowledged`);
    } catch (err) {
      const msg = err.message || "request failed";
      setLastError(msg);
      toast.error(`${action} failed`, { description: msg });
    } finally {
      setBusyAction(null);
    }
  };

  const saveConfig = async (create) => {
    setBusyAction(create ? "create" : "patch");
    setLastError(null);
    let jsonOk = true;
    let parsedApproval = {};
    let parsedRisk = {};
    try {
      parsedApproval = JSON.parse(approvalJson || "{}");
    } catch {
      jsonOk = false;
      toast.error("Approval policy is not valid JSON", { description: approvalJson });
    }
    try {
      parsedRisk = JSON.parse(riskJson || "{}");
    } catch {
      jsonOk = false;
      toast.error("Risk policy is not valid JSON", { description: riskJson });
    }
    if (!jsonOk) {
      setBusyAction(null);
      return;
    }
    const symbols = symbolsText
      .split(",")
      .map((s) => s.trim().toUpperCase())
      .filter(Boolean);
    if (!symbols.length) {
      setLastError("market universe must be a non-empty comma-separated list");
      setBusyAction(null);
      return;
    }

const body = {
      name: name.trim() || (create ? "Autonomous Agent" : config.name),
      strategy_id: config?.strategy_id ?? null,
      symbols,
      execution_mode: liveOk ? executionMode : "PAPER",
      autonomy_level: autonomyLevel,
      approval_policy: parsedApproval,
      risk_policy: parsedRisk,
    };
    try {
      const res = await authFetch("/api/agent/config", {
        method: create ? "POST" : "PATCH",
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        const d = await readDetail(res);
        setLastError(d.message);
        toast.error(create ? "Create refused" : "Update refused", { description: d.message });
        return;
      }
      await refetch();
      setFormDirty(false);
      setCreateMode(false);
      toast.success(create ? "Agent config created" : "Agent config updated");
    } catch (err) {
      const msg = err.message || "request failed";
      setLastError(msg);
      toast.error("Save failed", { description: msg });
    } finally {
      setBusyAction(null);
    }
  };

  const approveTask = async (taskId) => {
    setBusyAction(`approve:${taskId}`);
    try {
      const res = await authFetch(`/api/agent/tasks/${taskId}/approve`, { method: "POST" });
      if (!res.ok) {
        const d = await readDetail(res);
        toast.error("Approval refused", { description: d.message });
        return;
      }
      await refetch();
      toast.success("Task approved — the runtime gates still apply");
    } catch (err) {
      toast.error("Approval failed", { description: err.message });
    } finally {
      setBusyAction(null);
    }
  };

  const activity = data?.activity || {};
  const pendingApprovals = useMemo(
    () =>
      (activity.tasks || []).filter(
        (t) => t.requires_approval && !TERMINAL_STATUSES.has(t.status),
      ),
    [activity.tasks],
  );

  if (loading && !data) {
    return (
      <div className="space-y-8">
        <div className="flex items-center gap-3">
          <span className="h-10 w-10 rounded-xl border-2 border-brand-purple/30 border-t-brand-purple animate-spin" />
          <p className="font-mono text-[11px] uppercase tracking-widest text-slate-500">
            Loading agent console…
          </p>
        </div>
      </div>
    );
  }

  if (error && !data) {
    return (
      <div className="glass-card p-8 text-center space-y-3">
        <AlertTriangle size={28} className="mx-auto text-amber-400" />
        <p className="text-sm text-slate-300">Agent console unavailable.</p>
        <p className="text-xs text-slate-500">{error}</p>
        <button onClick={refetch} className="btn-ghost text-xs">Retry</button>
      </div>
    );
  }

return (
    <div className="space-y-6">
      {/* ── Page header ─────────────────────────────────────────────────── */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-gradient-to-br from-violet-500 via-brand-purple to-cyan-400">
            <Bot size={18} className="text-white" />
          </div>
          <div>
            <h1 className="font-display text-lg font-bold text-white tracking-tight">
              Agent Console
            </h1>
            <p className="text-[11px] text-slate-400">
              Control plane for the autonomous trading agent — every action is
              re-verified server-side.
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <span
            className={`inline-flex items-center gap-1.5 px-3 py-1 text-[11px] font-bold rounded-full border ${
              wsConnected
                ? "border-cyan-500/20 bg-cyan-500/10 text-cyan-300"
                : "border-amber-500/20 bg-amber-500/10 text-amber-300"
            }`}
          >
            <span className={`h-1.5 w-1.5 rounded-full ${wsConnected ? "bg-cyan-400 animate-pulse" : "bg-amber-400"}`} />
            {wsConnected ? "LIVE · WS CONNECTED" : "RECONNECTING…"}
          </span>
          {config && (
            <span className={`px-3 py-1 text-[11px] font-bold rounded-full ${STATUS_COLORS[config.status] || "bg-slate-500/20 text-slate-300"}`}>
              {config.status}
            </span>
          )}
        </div>
      </div>

      {/* ── Autonomy gate strip (read-only truth) ───────────────────────── */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <div className="glass-card p-4">
          <p className="text-[10px] uppercase tracking-widest text-slate-500">Autonomous mode</p>
          <p className={`text-sm font-bold mt-1 ${gate.autonomous_mode_enabled ? "text-emerald-300" : "text-amber-300"}`}>
            {gate.autonomous_mode_enabled ? "ENABLED" : "DISABLED"}
          </p>
        </div>
        <div className="glass-card p-4">
          <p className="text-[10px] uppercase tracking-widest text-slate-500">Effective ceiling</p>
          <p className="text-sm font-bold mt-1 text-white">
            {AUTONOMY_LABELS[ceiling] ?? ceiling}
            <span className="text-[10px] font-normal text-slate-500 ml-1">
              (min glob {gate.global_autonomy_level ?? 0} · agent {gate.trading_agent_max_autonomy ?? 0})
            </span>
          </p>
        </div>
        <div className="glass-card p-4">
          <p className="text-[10px] uppercase tracking-widest text-slate-500">Broker mode</p>
          <p className={`text-sm font-bold mt-1 ${liveOk ? "text-emerald-300" : "text-slate-300"}`}>
            {gate.broker_mode || "paper"} {liveOk ? "" : "(paper)"}
          </p>
        </div>
        <div className="glass-card p-4">
          <p className="text-[10px] uppercase tracking-widest text-slate-500">Trading agent</p>
          <p className={`text-sm font-bold mt-1 ${gate.trading_agent_enabled ? "text-emerald-300" : "text-amber-300"}`}>
            {gate.trading_agent_enabled ? "REGISTERED · ENABLED" : "REGISTERED · DISABLED"}
          </p>
        </div>
      </div>

{/* ── Operator warnings (honest, actionable) ──────────────────────── */}
      {gate.autonomous_mode_enabled === false && (
        <div className="flex items-start gap-2 rounded-xl border border-amber-500/20 bg-amber-500/10 px-4 py-3 text-xs text-amber-200">
          <Lock size={14} className="mt-0.5 shrink-0" />
          <span>
            <strong>Autonomous mode is disabled at the platform level.</strong> The agent
            cannot transition to RUNNING. An admin must enable autonomous mode and set
            the global autonomy level in the Admin Sentinel (runtime gates) first.
          </span>
        </div>
      )}
      {overCeiling && !createMode && (
        <div className="flex items-start gap-2 rounded-xl border border-red-500/20 bg-red-500/10 px-4 py-3 text-xs text-red-200">
          <AlertTriangle size={14} className="mt-0.5 shrink-0" />
          <span>
            Configured autonomy <strong>{AUTONOMY_LABELS[autonomyLevel]}</strong> exceeds the
            effective ceiling (<strong>{AUTONOMY_LABELS[ceiling]}</strong>). Start will be
            refused until the level is lowered or an admin raises the ceiling.
          </span>
        </div>
      )}
      {config?.last_error && !createMode && (
        <div className="flex items-start gap-2 rounded-xl border border-red-500/20 bg-red-500/10 px-4 py-3 text-xs text-red-200">
          <AlertTriangle size={14} className="mt-0.5 shrink-0" />
          <span><strong>Last error:</strong> {config.last_error}</span>
        </div>
      )}
      {lastError && (
        <div className="flex items-start gap-2 rounded-xl border border-red-500/20 bg-red-500/10 px-4 py-3 text-xs text-red-200">
          <AlertTriangle size={14} className="mt-0.5 shrink-0" />
          <span>{lastError}</span>
        </div>
      )}

      {/* ── Config + lifecycle ──────────────────────────────────────────── */}
      <div className="grid lg:grid-cols-2 gap-6">
        <SectionCard
          icon={ClipboardList}
          title={config ? "Agent configuration" : "Create agent configuration"}
          subtitle="Single per-tenant config — the browser edits, the server authorizes."
        >
          {!config && !createMode ? (
            <div className="text-center py-8 space-y-4">
              <p className="text-xs text-slate-500">
                No agent config exists yet. Create the tenant config to bind a
                strategy, universe and autonomy level, then start the loop.
              </p>
              <button
                onClick={() => setCreateMode(true)}
                className="btn-primary text-xs inline-flex items-center gap-2"
              >
                <Plus size={14} /> Create agent config
              </button>
            </div>
          ) : (
            <div className="space-y-4">
              <div>
                <label className="block text-[11px] font-medium text-slate-400 mb-1">Name</label>
                <input
                  value={name}
                  onChange={(e) => {
                    setName(e.target.value);
                    setFormDirty(true);
                  }}
                  maxLength={120}
                  className="w-full rounded-lg border border-white/10 bg-white/[0.03] px-3 py-2 text-sm text-white outline-none focus:border-brand-purple/60"
                  placeholder="Autonomous Agent"
                />
              </div>
              <div>
                <label className="block text-[11px] font-medium text-slate-400 mb-1">
                  Market universe <span className="text-slate-600">(comma-separated symbols, ≤40)</span>
                </label>
                <input
                  value={symbolsText}
                  onChange={(e) => {
                    setSymbolsText(e.target.value);
                    setFormDirty(true);
                  }}
                  className="w-full rounded-lg border border-white/10 bg-white/[0.03] px-3 py-2 text-sm text-white outline-none focus:border-brand-purple/60"
                  placeholder="NIFTY, BANKNIFTY"
                />
              </div>

<div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="block text-[11px] font-medium text-slate-400 mb-1">Execution mode</label>
                  <select
                    value={liveOk ? executionMode : "PAPER"}
                    onChange={(e) => {
                      setExecutionMode(e.target.value);
                      setFormDirty(true);
                    }}
                    disabled={!liveOk}
                    className="w-full rounded-lg border border-white/10 bg-surface-900 px-3 py-2 text-sm text-white outline-none focus:border-brand-purple/60 disabled:opacity-50"
                  >
                    <option value="PAPER">PAPER</option>
                    <option value="LIVE" disabled={!liveOk}>LIVE {liveOk ? "" : "(locked — broker not live)"}</option>
                  </select>
                </div>
                <div>
                  <label className="block text-[11px] font-medium text-slate-400 mb-1">Autonomy level</label>
                  <select
                    value={autonomyLevel}
                    onChange={(e) => {
                      setAutonomyLevel(Number(e.target.value));
                      setFormDirty(true);
                    }}
                    className="w-full rounded-lg border border-white/10 bg-surface-900 px-3 py-2 text-sm text-white outline-none focus:border-brand-purple/60"
                  >
                    {[0, 1, 2, 3].map((lvl) => (
                      <option key={lvl} value={lvl}>
                        {lvl} — {AUTONOMY_LABELS[lvl]}
                      </option>
                    ))}
                  </select>
                </div>
              </div>
              <div className="grid md:grid-cols-2 gap-4">
                <div>
                  <label className="block text-[11px] font-medium text-slate-400 mb-1">Approval policy (JSON)</label>
                  <textarea
                    value={approvalJson}
                    onChange={(e) => {
                      setApprovalJson(e.target.value);
                      setFormDirty(true);
                    }}
                    rows={4}
                    className="w-full rounded-lg border border-white/10 bg-white/[0.03] px-3 py-2 font-mono text-[11px] text-cyan-200 outline-none focus:border-brand-purple/60"
                  />
                </div>
                <div>
                  <label className="block text-[11px] font-medium text-slate-400 mb-1">Risk policy (JSON)</label>
                  <textarea
                    value={riskJson}
                    onChange={(e) => {
                      setRiskJson(e.target.value);
                      setFormDirty(true);
                    }}
                    rows={4}
                    className="w-full rounded-lg border border-white/10 bg-white/[0.03] px-3 py-2 font-mono text-[11px] text-cyan-200 outline-none focus:border-brand-purple/60"
                  />
                </div>
              </div>
              <div className="flex items-center justify-between gap-3">
                <p className="text-[10px] text-slate-600">
                  Updates are refused while the agent is RUNNING — pause it first.
                </p>
                <button
                  onClick={() => saveConfig(Boolean(config) === false)}
                  disabled={Boolean(busyAction)}
                  className="btn-primary text-xs inline-flex items-center gap-2 disabled:opacity-50"
                >
                  <Save size={14} /> {config ? "Update config" : "Create config"}
                </button>
              </div>
            </div>
          )}
        </SectionCard>

<SectionCard
          icon={ShieldCheck}
          title="Lifecycle control"
          subtitle="CAS-protected transitions — pause/stop fail closed on double action; start/resume are idempotent."
        >
          <div className="flex items-center gap-3">
            <span className={`px-3 py-1 text-[11px] font-bold rounded-full ${STATUS_COLORS[config?.status] || "bg-slate-500/20 text-slate-300"}`}>
              {config?.status || "NO CONFIG"}
            </span>
            {config?.strategy_id && (
              <span className="text-[11px] font-mono text-slate-500">strategy {config.strategy_id}</span>
            )}
          </div>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3 pt-2">
            <button
              onClick={() => runLifecycle("start")}
              disabled={!canStart || !gate.autonomous_mode_enabled || Boolean(busyAction)}
              className="btn-ghost text-xs inline-flex items-center justify-center gap-2 py-2.5 disabled:opacity-40"
            >
              <Play size={14} className="text-emerald-400" /> Start
            </button>
            <button
              onClick={() => runLifecycle("pause")}
              disabled={!canPause || Boolean(busyAction)}
              className="btn-ghost text-xs inline-flex items-center justify-center gap-2 py-2.5 disabled:opacity-40"
            >
              <Pause size={14} className="text-amber-400" /> Pause
            </button>
            <button
              onClick={() => runLifecycle("resume")}
              disabled={!canResume || Boolean(busyAction)}
              className="btn-ghost text-xs inline-flex items-center justify-center gap-2 py-2.5 disabled:opacity-40"
            >
              <RotateCcw size={14} className="text-cyan-400" /> Resume
            </button>
            <button
              onClick={() => runLifecycle("stop")}
              disabled={!canStop || Boolean(busyAction)}
              className="btn-ghost text-xs inline-flex items-center justify-center gap-2 py-2.5 disabled:opacity-40"
            >
              <Square size={14} className="text-red-400" /> Stop
            </button>
          </div>
          <div className="rounded-xl border border-white/[0.06] bg-white/[0.02] p-4 text-[11px] text-slate-400 space-y-2">
            <p className="flex items-center gap-2">
              {isRunning ? (
                <CheckCircle2 size={13} className="text-emerald-400" />
              ) : (
                <Activity size={13} className="text-slate-500" />
              )}
              {isRunning
                ? "The evaluation loop is live — every symbol is evaluated once per slot; each evaluation is recorded as a durable decision."
                : "The evaluation loop is not running. Start turns RUNNING configs into deterministic decisions."}
            </p>
            <p className="text-slate-600">
              Decisions never touch a broker directly — TRADE/NEEDS_APPROVAL decisions enqueue
              governed execute_trade tasks that pass through the platform risk and approval gates.
            </p>
          </div>
        </SectionCard>
      </div>

{/* ── Activity ledger ────────────────────────────────────────────── */}
      <SectionCard
        icon={Layers}
        title="Activity ledger"
        subtitle="Every decision, task, intent, order and open position — server truth, newest first."
      >
        {/* Decisions */}
        <div>
          <p className="flex items-center gap-2 text-[11px] font-semibold text-slate-300 mb-2">
            <Target size={13} className="text-cyan-400" /> Decisions
          </p>
          {(activity.decisions || []).length === 0 ? (
            <p className="text-[11px] text-slate-600 py-2">No decisions yet — the evaluation loop records every symbol/slot here.</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-left text-xs">
                <thead>
                  <tr className="border-b border-white/[0.06] text-slate-500">
                    <th className="pb-2 font-medium">When</th>
                    <th className="pb-2 font-medium">Symbol</th>
                    <th className="pb-2 font-medium">Decision</th>
                    <th className="pb-2 font-medium">Side</th>
                    <th className="pb-2 font-medium">Risk</th>
                    <th className="pb-2 font-medium">Approval</th>
                    <th className="pb-2 font-medium">Execution</th>
                    <th className="pb-2 font-medium">Reason</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-white/[0.04]">
                  {(activity.decisions || []).map((d) => (
                    <tr key={d.decision_id} className="hover:bg-white/[0.02]">
                      <td className="py-2.5 text-slate-500 whitespace-nowrap">{fmtTs(d.created_at)}</td>
                      <td className="py-2.5 font-mono text-white">{d.symbol || "—"}</td>
                      <td className="py-2.5"><StatusBadge status={d.decision} /></td>
                      <td className="py-2.5">{d.side || "—"}</td>
                      <td className="py-2.5"><StatusBadge status={d.risk_result} /></td>
                      <td className="py-2.5"><StatusBadge status={d.approval_result} /></td>
                      <td className="py-2.5"><StatusBadge status={d.execution_result} /></td>
                      <td className="py-2.5 text-slate-400 max-w-[260px] truncate" title={d.reason}>{d.reason}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>

        {/* Tasks */}
        <div className="mt-6">
          <p className="flex items-center gap-2 text-[11px] font-semibold text-slate-300 mb-2">
            <ClipboardList size={13} className="text-cyan-400" /> Tasks
            {pendingApprovals.length > 0 && (
              <span className="px-1.5 py-0.5 text-[9px] font-bold rounded-full bg-amber-500/20 text-amber-300">
                {pendingApprovals.length} awaiting approval
              </span>
            )}
          </p>

{(activity.tasks || []).length === 0 ? (
            <p className="text-[11px] text-slate-600 py-2">No tasks yet — the runtime ledger will list every evaluation and execute_trade task here.</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-left text-xs">
                <thead>
                  <tr className="border-b border-white/[0.06] text-slate-500">
                    <th className="pb-2 font-medium">When</th>
                    <th className="pb-2 font-medium">Kind</th>
                    <th className="pb-2 font-medium">Status</th>
                    <th className="pb-2 font-medium">Attempts</th>
                    <th className="pb-2 font-medium">Approval</th>
                    <th className="pb-2 font-medium text-right">Action</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-white/[0.04]">
                  {(activity.tasks || []).map((t) => (
                    <tr key={t.id} className="hover:bg-white/[0.02]">
                      <td className="py-2.5 text-slate-500 whitespace-nowrap">{fmtTs(t.created_at)}</td>
                      <td className="py-2.5 font-mono text-white">{t.task_kind}</td>
                      <td className="py-2.5"><StatusBadge status={t.status} /></td>
                      <td className="py-2.5 text-slate-400">{t.attempts}/{t.max_attempts}</td>
                      <td className="py-2.5">
                        {t.requires_approval ? (
                          t.approved_at ? (
                            <span className="text-[10px] text-emerald-400 font-bold">APPROVED</span>
                          ) : (
                            <span className="text-[10px] text-amber-400 font-bold">REQUIRED</span>
                          )
                        ) : (
                          <span className="text-[10px] text-slate-500">N/A</span>
                        )}
                      </td>
                      <td className="py-2.5 text-right">
                        {t.requires_approval && !t.approved_at && !TERMINAL_STATUSES.has(t.status) && (
                          <button
                            onClick={() => approveTask(t.id)}
                            disabled={Boolean(busyAction)}
                            className="btn-ghost text-[10px] py-1 px-2.5 inline-flex items-center gap-1 disabled:opacity-50"
                          >
                            <CheckCircle2 size={11} /> Approve
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>

{/* Intents / Orders / Positions */}
        <div className="grid md:grid-cols-3 gap-6 mt-6">
          <div>
            <p className="flex items-center gap-2 text-[11px] font-semibold text-slate-300 mb-2">
              <FileText size={13} className="text-cyan-400" /> Intents
            </p>
            {(activity.intents || []).length === 0 ? (
              <p className="text-[11px] text-slate-600 py-2">No intents yet.</p>
            ) : (
              <ul className="space-y-2 text-[11px]">
                {(activity.intents || []).map((it) => (
                  <li key={it.intent_id} className="rounded-lg border border-white/[0.06] bg-white/[0.02] px-3 py-2 flex justify-between gap-2">
                    <span>
                      <span className="font-mono text-white">{it.symbol}</span> {it.side} {it.quantity}
                    </span>
                    <StatusBadge status={it.status} />
                  </li>
                ))}
              </ul>
            )}
          </div>
          <div>
            <p className="flex items-center gap-2 text-[11px] font-semibold text-slate-300 mb-2">
              <Boxes size={13} className="text-cyan-400" /> Orders
            </p>
            {(activity.orders || []).length === 0 ? (
              <p className="text-[11px] text-slate-600 py-2">No orders yet.</p>
            ) : (
              <ul className="space-y-2 text-[11px]">
                {(activity.orders || []).map((o) => (
                  <li key={o.order_id} className="rounded-lg border border-white/[0.06] bg-white/[0.02] px-3 py-2 flex justify-between gap-2">
                    <span>
                      <span className="font-mono text-white">{o.symbol}</span> {o.side} {o.quantity}
                      {o.filled_price ? ` @ ${o.filled_price}` : ""}
                    </span>
                    <StatusBadge status={o.status} />
                  </li>
                ))}
              </ul>
            )}
          </div>
          <div>
            <p className="flex items-center gap-2 text-[11px] font-semibold text-slate-300 mb-2">
              <Layers size={13} className="text-cyan-400" /> Open positions
            </p>
            {(activity.positions || []).length === 0 ? (
              <p className="text-[11px] text-slate-600 py-2">No open positions.</p>
            ) : (
              <ul className="space-y-2 text-[11px]">
                {(activity.positions || []).map((p) => (
                  <li key={p.position_id} className="rounded-lg border border-white/[0.06] bg-white/[0.02] px-3 py-2 flex justify-between gap-2">
                    <span>
                      <span className="font-mono text-white">{p.symbol}</span> {p.side} {p.quantity}
                      {p.entry_price ? ` @ ${p.entry_price}` : ""}
                    </span>
                    <span className="text-[10px] text-slate-500">{p.protection_state || p.status}</span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      </SectionCard>
    </div>
  );
}