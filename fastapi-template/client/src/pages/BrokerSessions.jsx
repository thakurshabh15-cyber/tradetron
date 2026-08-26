import { useState, useEffect, useCallback, memo } from "react";
import {
  RefreshCw,
  CheckCircle2,
  XCircle,
  AlertTriangle,
  Clock,
  Zap,
  Shield,
  Activity,
  Server,
  KeyRound,
  Calendar,
  BarChart3,
  ChevronDown,
  ChevronUp,
  Play,
  Loader2,
} from "lucide-react";
import { authFetch } from "../services/apiClient";

// ─── Status Badge ──────────────────────────────────────────────────────────────
const StatusBadge = memo(({ status }) => {
  const config = {
    ACTIVE: { label: "Active", color: "text-emerald-400 bg-emerald-500/15 border-emerald-500/30", icon: CheckCircle2 },
    EXPIRING_SOON: { label: "Expiring Soon", color: "text-amber-400 bg-amber-500/15 border-amber-500/30", icon: Clock },
    EXPIRED: { label: "Expired", color: "text-rose-400 bg-rose-500/15 border-rose-500/30", icon: XCircle },
    AUTH_FAILED: { label: "Auth Failed", color: "text-rose-400 bg-rose-500/15 border-rose-500/30", icon: XCircle },
    INACTIVE: { label: "Inactive", color: "text-slate-400 bg-slate-500/15 border-slate-500/30", icon: Activity },
  }[status] || { label: status, color: "text-slate-400 bg-slate-500/15 border-slate-500/30", icon: Activity };

  const Icon = config.icon;
  return (
    <span className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg border text-[11px] font-semibold ${config.color}`}>
      <Icon size={11} />
      {config.label}
    </span>
  );
});

// ─── Renewal Log Badge ─────────────────────────────────────────────────────────
const RenewalBadge = memo(({ status }) => {
  const config = {
    SUCCESS: "text-emerald-400 bg-emerald-500/10",
    FAILED: "text-rose-400 bg-rose-500/10",
    TOTP_INVALID: "text-amber-400 bg-amber-500/10",
    PENDING: "text-slate-400 bg-slate-500/10",
  }[status] || "text-slate-400 bg-slate-500/10";
  return (
    <span className={`px-2 py-0.5 rounded text-[10px] font-bold uppercase tracking-widest ${config}`}>{status}</span>
  );
});

// ─── Broker Logo ───────────────────────────────────────────────────────────────
const BrokerIcon = memo(({ name }) => {
  const n = (name || "").toUpperCase();
  const colors = {
    ANGEL_ONE: "from-orange-500 to-amber-500",
    ZERODHA: "from-blue-500 to-cyan-500",
    UPSTOX: "from-violet-500 to-purple-500",
    BINANCE: "from-yellow-400 to-amber-400",
    SIMULATED: "from-emerald-500 to-teal-500",
  };
  const labels = {
    ANGEL_ONE: "AO",
    ZERODHA: "ZR",
    UPSTOX: "UP",
    BINANCE: "BN",
    SIMULATED: "SIM",
  };
  return (
    <div className={`h-10 w-10 rounded-xl bg-gradient-to-br ${colors[n] || "from-slate-600 to-slate-700"} flex items-center justify-center text-white text-[11px] font-black shrink-0 shadow-lg`}>
      {labels[n] || n.slice(0, 2)}
    </div>
  );
});

// ─── Broker Account Card ───────────────────────────────────────────────────────
const BrokerCard = memo(({ account, onRenewSingle, renewingId }) => {
  const [expanded, setExpanded] = useState(false);
  const isRenewing = renewingId === account.account_id;
  const renewal = account.latest_renewal || {};

  const formatDate = (iso) => {
    if (!iso) return "—";
    return new Date(iso).toLocaleString("en-IN", { timeZone: "Asia/Kolkata", dateStyle: "medium", timeStyle: "short" });
  };

  return (
    <div className="rounded-2xl bg-surface-800/60 border border-slate-700/60 backdrop-blur overflow-hidden transition-all duration-200 hover:border-slate-600">
      {/* Header Row */}
      <div className="flex items-center gap-4 p-4">
        <BrokerIcon name={account.broker_name} />

        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <h3 className="text-sm font-bold text-white">{account.account_name || account.broker_name}</h3>
            <StatusBadge status={account.health_status} />
            {account.has_totp_configured && (
              <span className="flex items-center gap-1 px-2 py-0.5 rounded text-[10px] font-semibold text-violet-400 bg-violet-500/10 border border-violet-500/20">
                <KeyRound size={9} />
                TOTP
              </span>
            )}
          </div>
          <p className="text-[11px] text-slate-400 mt-0.5">
            {account.broker_name} · {account.client_id || "—"} · {account.api_key_masked || "—"}
          </p>
        </div>

        <div className="flex items-center gap-2 shrink-0">
          <button
            onClick={() => onRenewSingle(account.account_id)}
            disabled={isRenewing}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-xl bg-violet-600/20 border border-violet-500/30 text-violet-300 text-[11px] font-semibold hover:bg-violet-600/35 transition-all disabled:opacity-50"
          >
            {isRenewing ? <Loader2 size={12} className="animate-spin" /> : <Play size={12} />}
            {isRenewing ? "Renewing…" : "Renew"}
          </button>
          <button
            onClick={() => setExpanded(!expanded)}
            className="p-1.5 rounded-lg text-slate-400 hover:text-white hover:bg-surface-700 transition-all"
          >
            {expanded ? <ChevronUp size={16} /> : <ChevronDown size={16} />}
          </button>
        </div>
      </div>

      {/* Stats Row */}
      <div className="grid grid-cols-3 divide-x divide-slate-700/50 border-t border-slate-700/50">
        <div className="px-4 py-2.5 text-center">
          <p className="text-[10px] text-slate-500 uppercase tracking-wider mb-0.5">Last Renewal</p>
          <p className="text-[11px] font-semibold text-slate-200">{formatDate(renewal.renewed_at)}</p>
        </div>
        <div className="px-4 py-2.5 text-center">
          <p className="text-[10px] text-slate-500 uppercase tracking-wider mb-0.5">Token Expires</p>
          <p className="text-[11px] font-semibold text-slate-200">{formatDate(account.token_expires_at)}</p>
        </div>
        <div className="px-4 py-2.5 text-center">
          <p className="text-[10px] text-slate-500 uppercase tracking-wider mb-0.5">Last Latency</p>
          <p className="text-[11px] font-semibold text-emerald-400">
            {renewal.latency_ms != null ? `${renewal.latency_ms} ms` : "—"}
          </p>
        </div>
      </div>

      {/* Expanded Details */}
      {expanded && (
        <div className="border-t border-slate-700/50 p-4 space-y-3 bg-surface-900/40">
          <div className="flex items-start gap-3 p-3 rounded-xl bg-surface-800 border border-slate-700/50">
            <RenewalBadge status={renewal.status || "PENDING"} />
            <p className="text-[11px] text-slate-300 leading-relaxed">
              {renewal.message || "Awaiting first daily renewal at 8:45 AM IST"}
            </p>
          </div>
          <div className="grid grid-cols-2 gap-2 text-[11px]">
            <div className="p-2.5 rounded-lg bg-surface-800 border border-slate-700/50">
              <span className="text-slate-500">Last Synced:</span>
              <span className="ml-2 text-slate-300">{formatDate(account.last_synced_at)}</span>
            </div>
            <div className="p-2.5 rounded-lg bg-surface-800 border border-slate-700/50">
              <span className="text-slate-500">TOTP Secret:</span>
              <span className="ml-2 text-slate-300">{account.has_totp_configured ? "✓ Configured" : "Not set"}</span>
            </div>
          </div>
        </div>
      )}
    </div>
  );
});

// ─── Renewal Log Table ─────────────────────────────────────────────────────────
const RenewalLogsTable = memo(({ logs }) => {
  const formatDate = (iso) =>
    iso ? new Date(iso).toLocaleString("en-IN", { timeZone: "Asia/Kolkata", dateStyle: "short", timeStyle: "short" }) : "—";

  if (!logs.length) {
    return (
      <div className="flex flex-col items-center justify-center py-12 text-slate-500 gap-2">
        <Calendar size={32} className="opacity-30" />
        <p className="text-sm">No renewal logs yet. First run at 8:45 AM IST.</p>
      </div>
    );
  }

  return (
    <div className="overflow-x-auto rounded-xl border border-slate-700/60">
      <table className="w-full text-xs">
        <thead>
          <tr className="bg-surface-800/80 border-b border-slate-700/60">
            <th className="text-left px-4 py-3 text-slate-400 font-semibold uppercase tracking-wider text-[10px]">Broker</th>
            <th className="text-left px-4 py-3 text-slate-400 font-semibold uppercase tracking-wider text-[10px]">Status</th>
            <th className="text-left px-4 py-3 text-slate-400 font-semibold uppercase tracking-wider text-[10px]">Renewed At (IST)</th>
            <th className="text-right px-4 py-3 text-slate-400 font-semibold uppercase tracking-wider text-[10px]">Latency</th>
            <th className="text-left px-4 py-3 text-slate-400 font-semibold uppercase tracking-wider text-[10px]">Message</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-700/30">
          {logs.map((log, i) => (
            <tr key={log.id || i} className="hover:bg-surface-800/40 transition-colors">
              <td className="px-4 py-3 font-semibold text-slate-200">{log.broker_name}</td>
              <td className="px-4 py-3">
                <RenewalBadge status={log.status} />
              </td>
              <td className="px-4 py-3 text-slate-400 font-mono text-[11px]">{formatDate(log.renewed_at)}</td>
              <td className="px-4 py-3 text-right font-mono text-emerald-400">
                {log.latency_ms != null ? `${log.latency_ms} ms` : "—"}
              </td>
              <td className="px-4 py-3 text-slate-400 max-w-xs truncate" title={log.message}>
                {log.message || "—"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
});

// ─── Main Page ─────────────────────────────────────────────────────────────────
export default function BrokerSessions() {
  const [healthData, setHealthData] = useState(null);
  const [logs, setLogs] = useState([]);
  const [loading, setLoading] = useState(true);
  const [renewingAll, setRenewingAll] = useState(false);
  const [renewingId, setRenewingId] = useState(null);
  const [activeTab, setActiveTab] = useState("health");
  const [error, setError] = useState(null);

  const fetchHealthData = useCallback(async () => {
    try {
      const res = await authFetch("/api/brokers/health-status");
      if (res.ok) setHealthData(await res.json());
    } catch {
      setError("Failed to load broker health data.");
    }
  }, []);

  const fetchLogs = useCallback(async () => {
    try {
      const res = await authFetch("/api/brokers/renewal-logs?limit=50");
      if (res.ok) setLogs(await res.json());
    } catch {
      // Renewal logs are supplementary — surface empty table rather than an error banner
    }
  }, []);

  const loadAll = useCallback(async () => {
    setLoading(true);
    setError(null);
    await Promise.all([fetchHealthData(), fetchLogs()]);
    setLoading(false);
  }, [fetchHealthData, fetchLogs]);

  useEffect(() => { loadAll(); }, [loadAll]);

  const handleRenewAll = async () => {
    setRenewingAll(true);
    try {
      const res = await authFetch("/api/brokers/renew-all", { method: "POST" });
      if (res.ok) await loadAll();
    } finally {
      setRenewingAll(false);
    }
  };

  const handleRenewSingle = async (accountId) => {
    setRenewingId(accountId);
    try {
      await authFetch(`/api/brokers/${accountId}/renew`, { method: "POST" });
      await loadAll();
    } finally {
      setRenewingId(null);
    }
  };

  const accounts = healthData?.accounts || [];
  const stats = healthData || {};

  // Summary stats
  const summaryItems = [
    {
      label: "Total Connected",
      value: stats.total_connected ?? "—",
      icon: Server,
      color: "text-violet-400",
      bg: "bg-violet-500/10 border-violet-500/20",
    },
    {
      label: "Active & Healthy",
      value: stats.active_healthy ?? "—",
      icon: CheckCircle2,
      color: "text-emerald-400",
      bg: "bg-emerald-500/10 border-emerald-500/20",
    },
    {
      label: "Expiring Soon",
      value: stats.expiring_soon ?? "—",
      icon: Clock,
      color: "text-amber-400",
      bg: "bg-amber-500/10 border-amber-500/20",
    },
    {
      label: "Expired / Failed",
      value: stats.expired_or_failed ?? "—",
      icon: XCircle,
      color: "text-rose-400",
      bg: "bg-rose-500/10 border-rose-500/20",
    },
  ];

  return (
    <div className="min-h-screen bg-surface-950 lg:pl-64 px-4 pt-16 lg:pt-0">
      <div className="max-w-5xl mx-auto py-6 space-y-6">

        {/* ── Page Header ── */}
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
          <div>
            <div className="flex items-center gap-2 mb-1">
              <div className="h-8 w-8 rounded-xl bg-gradient-to-br from-violet-500 to-purple-600 flex items-center justify-center shadow-lg shadow-violet-500/25">
                <Shield size={16} className="text-white" />
              </div>
              <h1 className="text-xl font-display font-bold text-white">Broker Sessions</h1>
            </div>
            <p className="text-sm text-slate-400">
              Automated TOTP renewal engine · Next scheduled run at{" "}
              <span className="text-violet-400 font-semibold">{stats.next_scheduled_renewal_ist || "08:45 AM IST"}</span>
            </p>
          </div>

          <div className="flex items-center gap-2">
            <button
              onClick={loadAll}
              disabled={loading}
              className="flex items-center gap-1.5 px-3 py-2 rounded-xl bg-surface-800 border border-slate-700 text-slate-300 text-xs font-semibold hover:bg-surface-700 transition-all"
            >
              <RefreshCw size={13} className={loading ? "animate-spin" : ""} />
              Refresh
            </button>
            <button
              onClick={handleRenewAll}
              disabled={renewingAll || loading}
              className="flex items-center gap-1.5 px-4 py-2 rounded-xl bg-gradient-to-r from-violet-600 to-purple-600 text-white text-xs font-bold shadow-lg shadow-violet-500/20 hover:shadow-violet-500/30 hover:scale-[1.02] transition-all disabled:opacity-50"
            >
              {renewingAll ? <Loader2 size={13} className="animate-spin" /> : <Zap size={13} />}
              {renewingAll ? "Renewing All…" : "Renew All Now"}
            </button>
          </div>
        </div>

        {/* ── Stats Grid ── */}
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
          {summaryItems.map(({ label, value, icon: Icon, color, bg }) => (
            <div key={label} className={`rounded-2xl border p-4 ${bg} backdrop-blur`}>
              <div className="flex items-center justify-between mb-2">
                <span className="text-[11px] text-slate-400 font-semibold uppercase tracking-wider">{label}</span>
                <Icon size={15} className={color} />
              </div>
              <p className={`text-2xl font-display font-black ${color}`}>{value}</p>
            </div>
          ))}
        </div>

        {/* ── TOTP Cron Info Banner ── */}
        <div className="rounded-2xl border border-violet-500/20 bg-violet-500/5 p-4 flex items-start gap-3">
          <div className="h-8 w-8 rounded-xl bg-violet-500/20 flex items-center justify-center shrink-0 mt-0.5">
            <Clock size={16} className="text-violet-400" />
          </div>
          <div>
            <p className="text-sm font-semibold text-violet-300 mb-0.5">Automated Daily Renewal Cron</p>
            <p className="text-[12px] text-slate-400 leading-relaxed">
              The system automatically generates live TOTP codes via <code className="text-violet-400">pyotp</code> and
              re-authenticates all Angel One / SmartAPI accounts at <strong className="text-white">8:45 AM IST</strong> every
              weekday. Zerodha and Upstox sessions are refreshed via their token renewal APIs. All renewal statuses are
              persisted in the audit log below.
            </p>
          </div>
        </div>

        {/* ── Tabs ── */}
        <div className="flex gap-1 p-1 bg-surface-800/60 rounded-xl border border-slate-700/50 w-fit">
          {[
            { id: "health", label: "Session Health", icon: Activity },
            { id: "logs", label: "Renewal Logs", icon: BarChart3 },
          ].map(({ id, label, icon: Icon }) => (
            <button
              key={id}
              onClick={() => setActiveTab(id)}
              className={`flex items-center gap-2 px-4 py-2 rounded-lg text-xs font-semibold transition-all ${
                activeTab === id
                  ? "bg-violet-600 text-white shadow-md"
                  : "text-slate-400 hover:text-white hover:bg-surface-700"
              }`}
            >
              <Icon size={13} />
              {label}
            </button>
          ))}
        </div>

        {/* ── Tab Content ── */}
        {error && (
          <div className="flex items-center gap-3 p-4 rounded-xl bg-rose-500/10 border border-rose-500/20 text-rose-300 text-sm">
            <AlertTriangle size={16} />
            {error}
            <button onClick={loadAll} className="ml-auto text-xs underline">Retry</button>
          </div>
        )}

        {activeTab === "health" && (
          <div className="space-y-3">
            {loading ? (
              Array.from({ length: 3 }).map((_, i) => (
                <div key={i} className="h-24 rounded-2xl bg-surface-800/60 border border-slate-700/50 animate-pulse" />
              ))
            ) : accounts.length === 0 ? (
              <div className="flex flex-col items-center justify-center py-16 text-slate-500 gap-3">
                <Server size={40} className="opacity-25" />
                <p className="text-sm">No broker accounts linked yet.</p>
                <p className="text-[12px] text-slate-600">Connect a broker account in Profile &amp; Settings.</p>
              </div>
            ) : (
              accounts.map((acc) => (
                <BrokerCard
                  key={acc.account_id}
                  account={acc}
                  onRenewSingle={handleRenewSingle}
                  renewingId={renewingId}
                />
              ))
            )}
          </div>
        )}

        {activeTab === "logs" && (
          <div>
            {loading ? (
              <div className="h-40 rounded-2xl bg-surface-800/60 border border-slate-700/50 animate-pulse" />
            ) : (
              <RenewalLogsTable logs={logs} />
            )}
          </div>
        )}
      </div>
    </div>
  );
}
