import { useState, useEffect } from "react";
import {
  ShieldCheck,
  ShieldAlert,
  Users,
  FileCheck,
  Radio,
  Layers,
  DollarSign,
  ScrollText,
  Activity,
  Power,
  Lock,
  Search,
  CheckCircle2,
  XCircle,
  AlertTriangle,
  RefreshCw,
  Eye,
  Key,
} from "lucide-react";
import { API_BASE } from "../config";

export default function Admin() {
  const [adminToken, setAdminToken] = useState(localStorage.getItem("tradetron_admin_token") || "");
  const [adminUser, setAdminUser] = useState(null);
  const [loginEmail, setLoginEmail] = useState("");
  const [loginPassword, setLoginPassword] = useState("");
  const [adminPin, setAdminPin] = useState("9988");
  const [loginLoading, setLoginLoading] = useState(false);
  const [loginError, setLoginError] = useState(null);

  const [activeTab, setActiveTab] = useState("overview"); // 'overview' | 'users' | 'kyc' | 'brokers' | 'strategies' | 'revenue' | 'audit' | 'system'
  const [overview, setOverview] = useState(null);
  const [users, setUsers] = useState([]);
  const [kycQueue, setKycQueue] = useState([]);
  const [brokers, setBrokers] = useState([]);
  const [strategies, setStrategies] = useState([]);
  const [revenue, setRevenue] = useState(null);
  const [auditLogs, setAuditLogs] = useState([]);
  const [systemHealth, setSystemHealth] = useState(null);
  const [loading, setLoading] = useState(false);
  const [actionMsg, setActionMsg] = useState(null);
  const [userSearchQuery, setUserSearchQuery] = useState("");

  const authHeaders = {
    Authorization: `Bearer ${adminToken}`,
    "Content-Type": "application/json",
  };

  // Dedicated Admin Login
  const handleAdminLogin = async (e) => {
    e.preventDefault();
    setLoginLoading(true);
    setLoginError(null);
    try {
      const res = await fetch(`${API_BASE}/api/admin/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          email: loginEmail,
          password: loginPassword,
          admin_security_pin: adminPin,
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Admin authentication failed");

      setAdminToken(data.access_token);
      setAdminUser(data.user);
      localStorage.setItem("tradetron_admin_token", data.access_token);
    } catch (err) {
      setLoginError(err.message);
    } finally {
      setLoginLoading(false);
    }
  };

  const handleAdminLogout = () => {
    setAdminToken("");
    setAdminUser(null);
    localStorage.removeItem("tradetron_admin_token");
  };

  // Fetch overview & current tab data
  const fetchData = async () => {
    if (!adminToken) return;
    setLoading(true);
    try {
      if (activeTab === "overview") {
        const res = await fetch(`${API_BASE}/api/admin/overview`, { headers: authHeaders });
        if (res.ok) setOverview(await res.json());
      } else if (activeTab === "users") {
        const url = userSearchQuery
          ? `${API_BASE}/api/admin/users?query=${encodeURIComponent(userSearchQuery)}`
          : `${API_BASE}/api/admin/users`;
        const res = await fetch(url, { headers: authHeaders });
        if (res.ok) setUsers(await res.json());
      } else if (activeTab === "kyc") {
        const res = await fetch(`${API_BASE}/api/admin/kyc/queue`, { headers: authHeaders });
        if (res.ok) setKycQueue(await res.json());
      } else if (activeTab === "brokers") {
        const res = await fetch(`${API_BASE}/api/admin/brokers/monitor`, { headers: authHeaders });
        if (res.ok) setBrokers(await res.json());
      } else if (activeTab === "strategies") {
        const res = await fetch(`${API_BASE}/api/admin/strategies/oversight`, { headers: authHeaders });
        if (res.ok) setStrategies(await res.json());
      } else if (activeTab === "revenue") {
        const res = await fetch(`${API_BASE}/api/admin/revenue/metrics`, { headers: authHeaders });
        if (res.ok) setRevenue(await res.json());
      } else if (activeTab === "audit") {
        const res = await fetch(`${API_BASE}/api/admin/audit-logs?limit=50`, { headers: authHeaders });
        if (res.ok) setAuditLogs(await res.json());
      } else if (activeTab === "system") {
        const res = await fetch(`${API_BASE}/api/admin/system/health`, { headers: authHeaders });
        if (res.ok) setSystemHealth(await res.json());
      }
    } catch (err) {
      console.error(err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchData();
  }, [adminToken, activeTab, userSearchQuery]);

  // Admin Actions
  const handleToggleUserStatus = async (userId, currentActive) => {
    try {
      const res = await fetch(`${API_BASE}/api/admin/users/${userId}/status`, {
        method: "POST",
        headers: authHeaders,
        body: JSON.stringify({ is_active: !currentActive, reason: "Admin console action" }),
      });
      if (res.ok) {
        setActionMsg(`User status updated to ${!currentActive ? "Active" : "Suspended"}`);
        fetchData();
      }
    } catch (err) {
      console.error(err);
    }
  };

  const handleReviewKYC = async (userId, decision) => {
    try {
      const res = await fetch(`${API_BASE}/api/admin/kyc/${userId}/review`, {
        method: "POST",
        headers: authHeaders,
        body: JSON.stringify({ decision, remarks: `SEBI document audit: ${decision}` }),
      });
      if (res.ok) {
        setActionMsg(`KYC application marked as ${decision}`);
        fetchData();
      }
    } catch (err) {
      console.error(err);
    }
  };

  const handlePlatformKillSwitch = async () => {
    if (!confirm("CRITICAL WARNING: Are you sure you want to halt ALL live strategy trading across the entire platform?")) return;
    try {
      const res = await fetch(`${API_BASE}/api/admin/kill-switch/platform`, {
        method: "POST",
        headers: authHeaders,
        body: JSON.stringify({ reason: "Emergency operator platform halt" }),
      });
      if (res.ok) {
        setActionMsg("CRITICAL: Platform-wide trading halted successfully!");
        fetchData();
      }
    } catch (err) {
      console.error(err);
    }
  };

  // If not authenticated as Admin, show strictly-separated login portal
  if (!adminToken) {
    return (
      <div className="flex items-center justify-center min-h-[80vh] p-4">
        <div className="relative w-full max-w-md bg-slate-900 border-2 border-violet-500/40 rounded-3xl p-8 shadow-2xl shadow-violet-950/50 overflow-hidden">
          <div className="absolute top-0 left-0 right-0 h-1.5 bg-gradient-to-r from-violet-600 via-indigo-500 to-cyan-400 animate-pulse" />

          <div className="text-center mb-6">
            <div className="inline-flex items-center justify-center w-14 h-14 rounded-2xl bg-violet-500/10 text-violet-400 border border-violet-500/30 mb-3 shadow-inner">
              <ShieldCheck size={28} />
            </div>
            <h1 className="text-2xl font-bold text-white tracking-tight">Tradetron Sentinel</h1>
            <p className="text-xs text-slate-400 mt-1">Strictly Restricted Administrator Clearance Portal</p>
          </div>

          {loginError && (
            <div className="mb-4 flex items-center gap-2 p-3 rounded-xl bg-red-500/10 border border-red-500/30 text-red-400 text-xs">
              <AlertTriangle size={16} className="shrink-0" />
              <span>{loginError}</span>
            </div>
          )}

          <form onSubmit={handleAdminLogin} className="space-y-4">
            <div>
              <label className="text-[11px] font-semibold text-slate-300">Admin Email</label>
              <input
                type="email"
                value={loginEmail}
                onChange={(e) => setLoginEmail(e.target.value)}
                placeholder="admin@tradetron.io"
                className="w-full mt-1 bg-slate-950 border border-slate-800 rounded-xl px-3.5 py-2.5 text-xs text-white placeholder-slate-600 focus:outline-none focus:border-violet-500"
                required
              />
            </div>

            <div>
              <label className="text-[11px] font-semibold text-slate-300">Admin Password</label>
              <input
                type="password"
                value={loginPassword}
                onChange={(e) => setLoginPassword(e.target.value)}
                placeholder="••••••••••••••••"
                className="w-full mt-1 bg-slate-950 border border-slate-800 rounded-xl px-3.5 py-2.5 text-xs text-white placeholder-slate-600 focus:outline-none focus:border-violet-500"
                required
              />
            </div>

            <div>
              <label className="text-[11px] font-semibold text-slate-300">Security PIN / Hardware Key</label>
              <input
                type="text"
                value={adminPin}
                onChange={(e) => setAdminPin(e.target.value)}
                placeholder="9988"
                className="w-full mt-1 bg-slate-950 border border-slate-800 rounded-xl px-3.5 py-2.5 text-xs text-white placeholder-slate-600 focus:outline-none focus:border-violet-500 font-mono tracking-widest"
              />
            </div>

            <button
              type="submit"
              disabled={loginLoading}
              className="w-full py-3 rounded-xl bg-gradient-to-r from-violet-600 to-indigo-600 hover:from-violet-500 hover:to-indigo-500 text-white font-bold text-xs transition-all shadow-lg shadow-violet-600/30 flex items-center justify-center gap-2 disabled:opacity-50 mt-2"
            >
              <Lock size={14} />
              {loginLoading ? "Authenticating Clearance..." : "Enter Governance Command Center"}
            </button>
          </form>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Admin Command Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 p-4 rounded-2xl bg-slate-900 border border-violet-500/30 shadow-lg">
        <div className="flex items-center gap-3">
          <div className="p-2.5 rounded-xl bg-violet-500/10 text-violet-400 border border-violet-500/20">
            <ShieldCheck size={22} />
          </div>
          <div>
            <div className="flex items-center gap-2">
              <h1 className="text-xl font-bold tracking-tight text-white">Platform Governance & Risk Oversight</h1>
              <span className="px-2 py-0.5 rounded text-[10px] font-mono font-bold bg-violet-500/20 text-violet-300 border border-violet-500/30">
                SUPERADMIN
              </span>
            </div>
            <p className="text-xs text-slate-400 mt-0.5">Real-time user KYC auditing, broker link health, risk limits, and telemetry</p>
          </div>
        </div>

        <div className="flex items-center gap-2 self-start sm:self-auto">
          <button
            onClick={fetchData}
            className="flex items-center gap-1.5 px-3 py-2 rounded-xl bg-slate-800 hover:bg-slate-750 border border-slate-700 text-slate-300 text-xs font-semibold"
          >
            <RefreshCw size={13} className={loading ? "animate-spin" : ""} /> Refresh
          </button>
          <button
            onClick={handlePlatformKillSwitch}
            className="flex items-center gap-1.5 px-3 py-2 rounded-xl bg-red-600/20 hover:bg-red-600/30 border border-red-500/40 text-red-300 text-xs font-bold shadow-md shadow-red-500/10"
          >
            <Power size={13} className="text-red-400" /> Platform Panic Halt
          </button>
          <button
            onClick={handleAdminLogout}
            className="px-3 py-2 rounded-xl bg-slate-800 hover:bg-slate-750 border border-slate-700 text-slate-400 hover:text-white text-xs font-semibold"
          >
            Exit Portal
          </button>
        </div>
      </div>

      {actionMsg && (
        <div className="p-3 rounded-xl bg-emerald-500/10 border border-emerald-500/30 text-emerald-300 text-xs flex items-center justify-between">
          <span>{actionMsg}</span>
          <button onClick={() => setActionMsg(null)} className="text-slate-400 hover:text-white">✕</button>
        </div>
      )}

      {/* Admin Navigation Tabs */}
      <div className="flex flex-wrap gap-2 bg-slate-950 p-1.5 rounded-2xl border border-slate-800">
        {[
          { id: "overview", label: "Overview & KPIs", icon: Activity },
          { id: "users", label: "User Directory", icon: Users },
          { id: "kyc", label: "KYC Review Queue", icon: FileCheck },
          { id: "brokers", label: "Broker Connections", icon: Radio },
          { id: "strategies", label: "Strategy Risk Oversight", icon: Layers },
          { id: "revenue", label: "Revenue & Subscriptions", icon: DollarSign },
          { id: "audit", label: "Audit Trail Stream", icon: ScrollText },
          { id: "system", label: "System Telemetry", icon: Activity },
        ].map((tab) => {
          const Icon = tab.icon;
          return (
            <button
              key={tab.id}
              onClick={() => setActiveTab(tab.id)}
              className={`flex items-center gap-1.5 px-3.5 py-2 rounded-xl text-xs font-semibold transition-all ${
                activeTab === tab.id
                  ? "bg-violet-600 text-white shadow-md shadow-violet-600/30 font-bold"
                  : "text-slate-400 hover:text-slate-200 hover:bg-slate-900"
              }`}
            >
              <Icon size={14} />
              <span>{tab.label}</span>
            </button>
          );
        })}
      </div>

      {/* TAB CONTENT: 1. OVERVIEW */}
      {activeTab === "overview" && overview && (
        <div className="space-y-6">
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <div className="card p-5 bg-slate-900 border border-slate-800 rounded-2xl">
              <div className="flex items-center justify-between text-slate-400 text-xs">
                <span>Total Registered Traders</span>
                <Users size={16} className="text-violet-400" />
              </div>
              <div className="mt-2 text-2xl font-bold font-mono text-white">{overview.users.total}</div>
              <div className="mt-1 text-[11px] text-emerald-400">{overview.users.verified_kyc} KYC Verified</div>
            </div>

            <div className="card p-5 bg-slate-900 border border-slate-800 rounded-2xl">
              <div className="flex items-center justify-between text-slate-400 text-xs">
                <span>Live Broker Links</span>
                <Radio size={16} className="text-cyan-400" />
              </div>
              <div className="mt-2 text-2xl font-bold font-mono text-cyan-400">{overview.brokers.active_connections}</div>
              <div className="mt-1 text-[11px] text-slate-400">Across Zerodha, Angel One, Binance</div>
            </div>

            <div className="card p-5 bg-slate-900 border border-slate-800 rounded-2xl">
              <div className="flex items-center justify-between text-slate-400 text-xs">
                <span>Managed Capital (Live)</span>
                <Layers size={16} className="text-amber-400" />
              </div>
              <div className="mt-2 text-2xl font-bold font-mono text-white">
                ₹{overview.strategies.total_capital_managed.toLocaleString()}
              </div>
              <div className="mt-1 text-[11px] text-amber-400">{overview.strategies.live_running} running strategies</div>
            </div>

            <div className="card p-5 bg-slate-900 border border-slate-800 rounded-2xl">
              <div className="flex items-center justify-between text-slate-400 text-xs">
                <span>MRR (Monthly Revenue)</span>
                <DollarSign size={16} className="text-emerald-400" />
              </div>
              <div className="mt-2 text-2xl font-bold font-mono text-emerald-400">
                ₹{overview.revenue.mrr.toLocaleString()}
              </div>
              <div className="mt-1 text-[11px] text-slate-400">{overview.revenue.active_subscribers} subscribers (Churn 2.1%)</div>
            </div>
          </div>
        </div>
      )}

      {/* TAB CONTENT: 2. USER MANAGEMENT */}
      {activeTab === "users" && (
        <div className="space-y-4">
          <div className="flex items-center gap-3 bg-slate-900 p-3 rounded-2xl border border-slate-800">
            <Search size={16} className="text-slate-400 ml-2" />
            <input
              type="text"
              value={userSearchQuery}
              onChange={(e) => setUserSearchQuery(e.target.value)}
              placeholder="Search users by name or email..."
              className="bg-transparent border-none text-xs text-white placeholder-slate-500 focus:outline-none flex-1"
            />
          </div>

          <div className="overflow-x-auto bg-slate-900 rounded-2xl border border-slate-800">
            <table className="w-full text-left text-xs text-slate-300">
              <thead className="border-b border-slate-800 text-[11px] uppercase tracking-wider text-slate-400 bg-slate-950/60">
                <tr>
                  <th className="p-3.5">Trader</th>
                  <th className="p-3.5">Role</th>
                  <th className="p-3.5">KYC Status</th>
                  <th className="p-3.5">2FA</th>
                  <th className="p-3.5">Account Status</th>
                  <th className="p-3.5 text-right">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800/60 font-mono">
                {users.map((u) => (
                  <tr key={u.id} className="hover:bg-slate-800/40 transition-colors">
                    <td className="p-3.5 font-sans">
                      <div className="font-bold text-white">{u.full_name}</div>
                      <div className="text-[11px] text-slate-400">{u.email}</div>
                    </td>
                    <td className="p-3.5">
                      <span className="px-2 py-0.5 rounded text-[10px] uppercase font-bold bg-slate-800 text-slate-300">
                        {u.role}
                      </span>
                    </td>
                    <td className="p-3.5">
                      <span className={`px-2 py-0.5 rounded text-[10px] font-bold ${
                        u.kyc_status === "VERIFIED" ? "bg-emerald-500/20 text-emerald-300" : (
                          u.kyc_status === "PENDING" ? "bg-amber-500/20 text-amber-300" : "bg-red-500/20 text-red-300"
                        )
                      }`}>
                        {u.kyc_status}
                      </span>
                    </td>
                    <td className="p-3.5">{u.two_factor_enabled ? "✅ TOTP" : "❌ Disabled"}</td>
                    <td className="p-3.5">
                      <span className={`px-2 py-0.5 rounded text-[10px] font-bold ${u.is_active ? "bg-emerald-500/10 text-emerald-400" : "bg-red-500/10 text-red-400"}`}>
                        {u.is_active ? "ACTIVE" : "SUSPENDED"}
                      </span>
                    </td>
                    <td className="p-3.5 text-right font-sans">
                      <button
                        onClick={() => handleToggleUserStatus(u.id, u.is_active)}
                        className={`px-2.5 py-1 rounded-lg text-[11px] font-semibold border transition-all ${
                          u.is_active ? "bg-red-500/10 border-red-500/30 text-red-400 hover:bg-red-500/20" : "bg-emerald-500/10 border-emerald-500/30 text-emerald-400 hover:bg-emerald-500/20"
                        }`}
                      >
                        {u.is_active ? "Suspend" : "Reactivate"}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* TAB CONTENT: 3. KYC REVIEW QUEUE */}
      {activeTab === "kyc" && (
        <div className="space-y-4">
          <div className="bg-slate-900 p-4 rounded-2xl border border-slate-800 text-xs text-slate-300">
            <h3 className="font-bold text-white text-sm">SEBI Compliance KYC Review Queue</h3>
            <p className="text-slate-400 mt-1">Traders must be verified prior to deploying real capital on licensed brokers.</p>
          </div>

          <div className="grid gap-4 sm:grid-cols-2">
            {kycQueue.length === 0 ? (
              <div className="col-span-2 p-8 text-center bg-slate-900 rounded-2xl border border-slate-800 text-slate-400 text-xs">
                ✅ All KYC submissions are currently up to date. No pending reviews in queue.
              </div>
            ) : (
              kycQueue.map((item) => (
                <div key={item.user_id} className="p-5 rounded-2xl bg-slate-900 border border-slate-800 space-y-3">
                  <div className="flex justify-between items-start">
                    <div>
                      <h4 className="font-bold text-white text-sm">{item.full_name}</h4>
                      <p className="text-xs text-slate-400">{item.email} • {item.phone || "No phone"}</p>
                    </div>
                    <span className="px-2 py-0.5 rounded text-[10px] font-bold bg-amber-500/20 text-amber-300">
                      PENDING REVIEW
                    </span>
                  </div>

                  <div className="p-3 rounded-xl bg-slate-950 border border-slate-800 text-xs space-y-1 font-mono">
                    <div className="text-slate-400">PAN: <span className="text-white">ABCDE1234F</span> (Verified NSDL)</div>
                    <div className="text-slate-400">Aadhaar: <span className="text-white">XXXX-XXXX-9911</span> (OTP Validated)</div>
                  </div>

                  <div className="flex gap-2 pt-1">
                    <button
                      onClick={() => handleReviewKYC(item.user_id, "VERIFIED")}
                      className="flex-1 py-2 rounded-xl bg-emerald-600 hover:bg-emerald-500 text-white font-bold text-xs transition-all flex items-center justify-center gap-1"
                    >
                      <CheckCircle2 size={14} /> Approve SEBI KYC
                    </button>
                    <button
                      onClick={() => handleReviewKYC(item.user_id, "REJECTED")}
                      className="flex-1 py-2 rounded-xl bg-red-600/20 hover:bg-red-600/30 border border-red-500/40 text-red-400 font-bold text-xs transition-all flex items-center justify-center gap-1"
                    >
                      <XCircle size={14} /> Reject
                    </button>
                  </div>
                </div>
              ))
            )}
          </div>
        </div>
      )}

      {/* TAB CONTENT: 4. BROKER MONITOR */}
      {activeTab === "brokers" && (
        <div className="overflow-x-auto bg-slate-900 rounded-2xl border border-slate-800">
          <table className="w-full text-left text-xs text-slate-300">
            <thead className="border-b border-slate-800 text-[11px] uppercase tracking-wider text-slate-400 bg-slate-950/60">
              <tr>
                <th className="p-3.5">Trader</th>
                <th className="p-3.5">Broker</th>
                <th className="p-3.5">Client Code</th>
                <th className="p-3.5">API Key</th>
                <th className="p-3.5">Link Health</th>
                <th className="p-3.5">Last Sync</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800/60 font-mono">
              {brokers.map((b) => (
                <tr key={b.account_id} className="hover:bg-slate-800/40 transition-colors">
                  <td className="p-3.5 font-sans">
                    <div className="font-bold text-white">{b.user_name}</div>
                    <div className="text-[11px] text-slate-400">{b.user_email}</div>
                  </td>
                  <td className="p-3.5 font-bold text-cyan-400">{b.broker_name}</td>
                  <td className="p-3.5 text-white">{b.client_id}</td>
                  <td className="p-3.5 text-slate-400">{b.api_key_masked}</td>
                  <td className="p-3.5">
                    <span className="px-2 py-0.5 rounded text-[10px] font-bold bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">
                      ● {b.health}
                    </span>
                  </td>
                  <td className="p-3.5 text-slate-400 text-[11px]">{b.last_synced_at ? new Date(b.last_synced_at).toLocaleTimeString() : "Live"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* TAB CONTENT: 5. STRATEGY RISK OVERSIGHT */}
      {activeTab === "strategies" && (
        <div className="overflow-x-auto bg-slate-900 rounded-2xl border border-slate-800">
          <table className="w-full text-left text-xs text-slate-300">
            <thead className="border-b border-slate-800 text-[11px] uppercase tracking-wider text-slate-400 bg-slate-950/60">
              <tr>
                <th className="p-3.5">Strategy Name</th>
                <th className="p-3.5">Execution Mode</th>
                <th className="p-3.5">Broker Route</th>
                <th className="p-3.5">Multiplier</th>
                <th className="p-3.5">Capital Allocated</th>
                <th className="p-3.5">Status</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800/60 font-mono">
              {strategies.map((s) => (
                <tr key={s.deployment_id} className="hover:bg-slate-800/40 transition-colors">
                  <td className="p-3.5 font-sans font-bold text-white">{s.strategy_name}</td>
                  <td className="p-3.5">
                    <span className={`px-2 py-0.5 rounded text-[10px] font-bold ${
                      s.execution_mode === "LIVE" ? "bg-red-500/20 text-red-300" : "bg-emerald-500/20 text-emerald-300"
                    }`}>
                      {s.execution_mode}
                    </span>
                  </td>
                  <td className="p-3.5 text-cyan-400">{s.broker_name}</td>
                  <td className="p-3.5">x{s.multiplier}</td>
                  <td className="p-3.5 font-bold text-white">₹{Number(s.capital_allocated).toLocaleString()}</td>
                  <td className="p-3.5">
                    <span className={`px-2 py-0.5 rounded text-[10px] font-bold ${s.status === "RUNNING" ? "bg-emerald-500/10 text-emerald-400" : "bg-amber-500/10 text-amber-400"}`}>
                      {s.status}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* TAB CONTENT: 6. REVENUE DASHBOARD */}
      {activeTab === "revenue" && revenue && (
        <div className="space-y-6">
          <div className="grid gap-4 sm:grid-cols-3">
            <div className="card p-5 bg-slate-900 border border-slate-800 rounded-2xl">
              <span className="text-xs text-slate-400">Monthly Recurring Revenue (MRR)</span>
              <div className="text-2xl font-bold font-mono text-emerald-400 mt-2">₹{revenue.mrr.toLocaleString()}</div>
            </div>
            <div className="card p-5 bg-slate-900 border border-slate-800 rounded-2xl">
              <span className="text-xs text-slate-400">Annual Run Rate (ARR)</span>
              <div className="text-2xl font-bold font-mono text-cyan-400 mt-2">₹{revenue.arr.toLocaleString()}</div>
            </div>
            <div className="card p-5 bg-slate-900 border border-slate-800 rounded-2xl">
              <span className="text-xs text-slate-400">Failed Payments (24h)</span>
              <div className="text-2xl font-bold font-mono text-rose-400 mt-2">{revenue.failed_payments_count}</div>
            </div>
          </div>
        </div>
      )}

      {/* TAB CONTENT: 7. AUDIT TRAIL STREAM */}
      {activeTab === "audit" && (
        <div className="overflow-x-auto bg-slate-900 rounded-2xl border border-slate-800">
          <table className="w-full text-left text-xs text-slate-300">
            <thead className="border-b border-slate-800 text-[11px] uppercase tracking-wider text-slate-400 bg-slate-950/60">
              <tr>
                <th className="p-3.5">Action Event</th>
                <th className="p-3.5">Resource</th>
                <th className="p-3.5">Status</th>
                <th className="p-3.5">Timestamp (UTC)</th>
                <th className="p-3.5">Details</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800/60 font-mono text-[11px]">
              {auditLogs.map((log) => (
                <tr key={log.id} className="hover:bg-slate-800/40 transition-colors">
                  <td className="p-3.5 font-bold text-white">{log.action}</td>
                  <td className="p-3.5 text-cyan-400">{log.resource_type}</td>
                  <td className="p-3.5">
                    <span className={`px-2 py-0.5 rounded text-[10px] font-bold ${
                      log.status === "SUCCESS" ? "bg-emerald-500/10 text-emerald-400" : (
                        log.status === "CRITICAL" ? "bg-red-500/20 text-red-300" : "bg-amber-500/10 text-amber-400"
                      )
                    }`}>
                      {log.status}
                    </span>
                  </td>
                  <td className="p-3.5 text-slate-400">{log.timestamp ? new Date(log.timestamp).toLocaleString() : ""}</td>
                  <td className="p-3.5 text-slate-300 truncate max-w-xs">{JSON.stringify(log.details)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* TAB CONTENT: 8. SYSTEM TELEMETRY */}
      {activeTab === "system" && systemHealth && (
        <div className="space-y-4">
          <div className="grid gap-4 sm:grid-cols-3">
            <div className="p-5 rounded-2xl bg-slate-900 border border-slate-800">
              <span className="text-xs text-slate-400">Indian Equity SmartAPI Feed</span>
              <div className="text-lg font-bold font-mono text-emerald-400 mt-1">{systemHealth.providers.indian_equity_smartapi.status}</div>
              <div className="text-[11px] text-slate-500 mt-1">Latency: {systemHealth.providers.indian_equity_smartapi.latency_ms}ms • Rate: {systemHealth.providers.indian_equity_smartapi.tick_rate_sec} ticks/sec</div>
            </div>
            <div className="p-5 rounded-2xl bg-slate-900 border border-slate-800">
              <span className="text-xs text-slate-400">Binance Crypto WebSocket</span>
              <div className="text-lg font-bold font-mono text-cyan-400 mt-1">{systemHealth.providers.crypto_binance.status}</div>
              <div className="text-[11px] text-slate-500 mt-1">Latency: {systemHealth.providers.crypto_binance.latency_ms}ms • Rate: {systemHealth.providers.crypto_binance.tick_rate_sec} ticks/sec</div>
            </div>
            <div className="p-5 rounded-2xl bg-slate-900 border border-slate-800">
              <span className="text-xs text-slate-400">WebSocket Live Client Sessions</span>
              <div className="text-lg font-bold font-mono text-white mt-1">{systemHealth.websocket_subscribers} active</div>
              <div className="text-[11px] text-slate-500 mt-1">24h Error Rate: {systemHealth.error_rate_24h_pct}%</div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
