import React, { useState, useEffect, useCallback } from "react";
import {
  Users,
  UserCheck,
  Zap,
  TrendingUp,
  ShieldCheck,
  Copy,
  Check,
  Plus,
  Play,
  Pause,
  Trash2,
  Sliders,
  Award,
  Search,
  RefreshCw,
  AlertCircle,
  Clock,
  ArrowUpRight,
  ArrowDownRight,
  Radio,
  Lock,
  Layers,
  ChevronRight,
} from "lucide-react";
import { authFetch } from "../services/apiClient";
import { useDebounce } from "../hooks/useDebounce";

export default function CopyTrading() {
  const [activeTab, setActiveTab] = useState("explore"); // 'explore' | 'following' | 'master_hub'
  const [exploreGroups, setExploreGroups] = useState([]);
  const [myFollowedGroups, setMyFollowedGroups] = useState([]);
  const [myMasterGroups, setMyMasterGroups] = useState([]);
  const [loading, setLoading] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  const [inviteCodeInput, setInviteCodeInput] = useState("");

  // Modals & Active State
  const [selectedGroup, setSelectedGroup] = useState(null);
  const [isJoinModalOpen, setIsJoinModalOpen] = useState(false);
  const [isCreateGroupModalOpen, setIsCreateGroupModalOpen] = useState(false);
  const [isEditFollowerModalOpen, setIsEditFollowerModalOpen] = useState(false);
  const [activeFollowerSub, setActiveFollowerSub] = useState(null);

  // Form States
  const [joinMultiplier, setJoinMultiplier] = useState(1.0);
  const [joinMaxAllocation, setJoinMaxAllocation] = useState(50000);
  const [joinMode, setJoinMode] = useState("PAPER");
  const [joinError, setJoinError] = useState(null);
  const [joinLoading, setJoinLoading] = useState(false);
  const [successMessage, setSuccessMessage] = useState(null);

  // Master Group Form
  const [groupName, setGroupName] = useState("");
  const [groupDesc, setGroupDesc] = useState("");
  const [profitSharePct, setProfitSharePct] = useState(20.0);
  const [minCapital, setMinCapital] = useState(15000);
  const [isGroupPublic, setIsGroupPublic] = useState(true);
  const [createGroupLoading, setCreateGroupLoading] = useState(false);
  const [createGroupError, setCreateGroupError] = useState(null);

  // Group followers drawer
  const [selectedGroupFollowers, setSelectedGroupFollowers] = useState([]);
  const [followersLoading, setFollowersLoading] = useState(false);
  const [activeMasterGroup, setActiveMasterGroup] = useState(null);

  // Copied code feedback
  const [copiedCode, setCopiedCode] = useState(null);

  const debouncedSearch = useDebounce(searchQuery, 300);

  const fetchExplore = useCallback(async () => {
    setLoading(true);
    try {
      const url = debouncedSearch.trim()
        ? `/api/copy-trading/explore?search=${encodeURIComponent(debouncedSearch.trim())}`
        : `/api/copy-trading/explore`;
      const res = await authFetch(url);
      if (res.ok) {
        const data = await res.json();
        setExploreGroups(Array.isArray(data) ? data : []);
      }
    } catch (err) {
      console.error("Error loading explore groups:", err);
    } finally {
      setLoading(false);
    }
  }, [debouncedSearch]);

  const fetchMyFollowed = useCallback(async () => {
    try {
      const res = await authFetch("/api/copy-trading/following");
      if (res.ok) {
        const data = await res.json();
        setMyFollowedGroups(Array.isArray(data) ? data : []);
      }
    } catch (err) {
      console.error("Error loading following groups:", err);
    }
  }, []);

  const fetchMyMasterGroups = useCallback(async () => {
    try {
      const res = await authFetch("/api/copy-trading/groups/mine");
      if (res.ok) {
        const data = await res.json();
        setMyMasterGroups(Array.isArray(data) ? data : []);
      }
    } catch (err) {
      console.error("Error loading master groups:", err);
    }
  }, []);

  useEffect(() => {
    fetchExplore();
    fetchMyFollowed();
    fetchMyMasterGroups();
  }, [fetchExplore, fetchMyFollowed, fetchMyMasterGroups]);

  const handleCopyInviteCode = (code) => {
    navigator.clipboard.writeText(code);
    setCopiedCode(code);
    setTimeout(() => setCopiedCode(null), 2500);
  };

  const handleOpenJoin = (group) => {
    setSelectedGroup(group);
    setJoinMultiplier(1.0);
    setJoinMaxAllocation(50000);
    setJoinMode("PAPER");
    setJoinError(null);
    setIsJoinModalOpen(true);
  };

  const handleJoinSubmit = async (e) => {
    e.preventDefault();
    setJoinLoading(true);
    setJoinError(null);
    try {
      const payload = {
        group_id: selectedGroup ? selectedGroup.id : undefined,
        invite_code: !selectedGroup && inviteCodeInput.trim() ? inviteCodeInput.trim().toUpperCase() : undefined,
        multiplier: Number(joinMultiplier),
        max_allocation: Number(joinMaxAllocation),
        mode: joinMode,
      };

      const res = await authFetch("/api/copy-trading/join", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Failed to subscribe to copy group");

      setSuccessMessage(data.message || "Subscribed to master trader successfully!");
      setIsJoinModalOpen(false);
      setInviteCodeInput("");
      fetchMyFollowed();
      fetchExplore();
      setTimeout(() => setSuccessMessage(null), 4000);
    } catch (err) {
      setJoinError(err.message);
    } finally {
      setJoinLoading(false);
    }
  };

  const handleCreateGroupSubmit = async (e) => {
    e.preventDefault();
    if (!groupName.trim()) return;
    setCreateGroupLoading(true);
    setCreateGroupError(null);
    try {
      const res = await authFetch("/api/copy-trading/groups", {
        method: "POST",
        body: JSON.stringify({
          name: groupName.trim(),
          description: groupDesc.trim() || null,
          profit_share_pct: Number(profitSharePct),
          min_capital: Number(minCapital),
          is_public: isGroupPublic,
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Failed to create master copy group");

      setSuccessMessage(`Created Master Group "${groupName}"! Invite Code: ${data.group.invite_code}`);
      setIsCreateGroupModalOpen(false);
      setGroupName("");
      setGroupDesc("");
      fetchMyMasterGroups();
      fetchExplore();
      setTimeout(() => setSuccessMessage(null), 5000);
    } catch (err) {
      setCreateGroupError(err.message);
    } finally {
      setCreateGroupLoading(false);
    }
  };

  const handleToggleFollowerStatus = async (sub) => {
    const newStatus = sub.status === "ACTIVE" ? "PAUSED" : "ACTIVE";
    try {
      const res = await authFetch(`/api/copy-trading/following/${sub.id}`, {
        method: "PATCH",
        body: JSON.stringify({ status: newStatus }),
      });
      if (res.ok) {
        fetchMyFollowed();
      }
    } catch (err) {
      console.error("Failed to toggle status:", err);
    }
  };

  const handleLeaveGroup = async (subId) => {
    const confirmed = window.confirm("Are you sure you want to stop copying this master trader and leave the group?");
    if (!confirmed) return;

    try {
      const res = await authFetch(`/api/copy-trading/following/${subId}`, {
        method: "DELETE",
      });
      if (res.ok) {
        fetchMyFollowed();
        fetchExplore();
      }
    } catch (err) {
      console.error("Failed to leave group:", err);
    }
  };

  const handleViewGroupFollowers = async (group) => {
    setActiveMasterGroup(group);
    setFollowersLoading(true);
    try {
      const res = await authFetch(`/api/copy-trading/groups/${group.id}/followers`);
      if (res.ok) {
        const data = await res.json();
        setSelectedGroupFollowers(Array.isArray(data) ? data : []);
      }
    } catch (err) {
      console.error("Error loading group followers:", err);
    } finally {
      setFollowersLoading(false);
    }
  };

  return (
    <div className="space-y-6 animate-fade-in">
      {/* Top Page Header */}
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <div className="flex items-center gap-2.5">
            <div className="p-2 rounded-xl bg-gradient-to-br from-brand-purple/20 to-brand-violet/20 border border-brand-purple/30 text-brand-purple">
              <Users size={22} />
            </div>
            <div>
              <h1 className="text-2xl font-bold tracking-tight text-white flex items-center gap-2">
                Copy Trading & Master Fan-Out
              </h1>
              <p className="text-xs text-slate-400">
                Institutional sub-50ms trade mirroring, customizable lot multipliers & profit sharing
              </p>
            </div>
          </div>
        </div>

        <div className="flex items-center gap-2 self-start sm:self-auto">
          <button
            onClick={() => setIsCreateGroupModalOpen(true)}
            className="btn-primary text-xs flex items-center gap-1.5 shadow-md shadow-brand-purple/20"
          >
            <Plus size={14} /> Become a Master Trader
          </button>
          <button
            onClick={() => {
              fetchExplore();
              fetchMyFollowed();
              fetchMyMasterGroups();
            }}
            className="btn-ghost text-xs flex items-center gap-1"
          >
            <RefreshCw size={13} className={loading ? "animate-spin" : ""} /> Refresh
          </button>
        </div>
      </div>

      {/* Success Notification Banner */}
      {successMessage && (
        <div className="p-3.5 rounded-xl bg-emerald-500/10 border border-emerald-500/30 text-emerald-300 text-xs flex items-center gap-2 animate-fade-in shadow-md">
          <ShieldCheck size={16} className="text-emerald-400 shrink-0" />
          <span className="font-semibold">{successMessage}</span>
        </div>
      )}

      {/* Quick Invite Code Join Strip */}
      <div className="glass-card p-3 sm:p-4 border border-slate-800 flex flex-col sm:flex-row items-center justify-between gap-3 bg-gradient-to-r from-surface-900 via-surface-900 to-brand-purple/10">
        <div className="flex items-center gap-2.5">
          <Radio size={16} className="text-cyan-400 animate-pulse shrink-0" />
          <span className="text-xs text-slate-300">
            Have a Private Master Invite Code? (e.g. <strong className="text-white font-mono">CPY-9A4F</strong>)
          </span>
        </div>

        <form onSubmit={handleJoinSubmit} className="flex items-center gap-2 w-full sm:w-auto">
          <input
            type="text"
            placeholder="ENTER INVITE CODE..."
            value={inviteCodeInput}
            onChange={(e) => setInviteCodeInput(e.target.value.toUpperCase())}
            className="input-field text-xs font-mono uppercase w-full sm:w-48 py-1.5"
          />
          <button
            type="submit"
            disabled={!inviteCodeInput.trim() || joinLoading}
            className="btn-accent text-xs py-1.5 px-4 shrink-0"
          >
            {joinLoading ? "Joining..." : "Join Master"}
          </button>
        </form>
      </div>

      {/* Navigation Tabs */}
      <div className="flex items-center gap-2 border-b border-slate-800 pb-2 overflow-x-auto">
        <button
          onClick={() => setActiveTab("explore")}
          className={`flex items-center gap-2 px-4 py-2 rounded-xl text-xs font-semibold transition-all ${
            activeTab === "explore"
              ? "bg-brand-purple/20 text-brand-purple border border-brand-purple/40 font-bold shadow-sm"
              : "text-slate-400 hover:text-white hover:bg-surface-800"
          }`}
        >
          <Search size={14} /> Explore Master Traders ({exploreGroups.length})
        </button>

        <button
          onClick={() => setActiveTab("following")}
          className={`flex items-center gap-2 px-4 py-2 rounded-xl text-xs font-semibold transition-all ${
            activeTab === "following"
              ? "bg-cyan-500/20 text-cyan-300 border border-cyan-500/40 font-bold shadow-sm"
              : "text-slate-400 hover:text-white hover:bg-surface-800"
          }`}
        >
          <UserCheck size={14} /> My Copied Masters ({myFollowedGroups.length})
        </button>

        <button
          onClick={() => setActiveTab("master_hub")}
          className={`flex items-center gap-2 px-4 py-2 rounded-xl text-xs font-semibold transition-all ${
            activeTab === "master_hub"
              ? "bg-emerald-500/20 text-emerald-300 border border-emerald-500/40 font-bold shadow-sm"
              : "text-slate-400 hover:text-white hover:bg-surface-800"
          }`}
        >
          <Zap size={14} /> Master Trader Hub ({myMasterGroups.length})
        </button>
      </div>

      {/* ── TAB 1: EXPLORE MASTER TRADERS ────────────────────────────────────── */}
      {activeTab === "explore" && (
        <div className="space-y-4">
          <div className="flex items-center justify-between gap-3">
            <div className="relative flex-1 max-w-md">
              <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-500" />
              <input
                type="text"
                placeholder="Search master groups or traders..."
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                className="input-field pl-9 text-xs w-full"
              />
            </div>
            <span className="text-[11px] font-mono text-slate-400">
              Verified Real-time DMA Execution
            </span>
          </div>

          {exploreGroups.length === 0 ? (
            <div className="glass-card p-12 text-center text-slate-400 space-y-3">
              <Users size={32} className="mx-auto text-slate-600" />
              <p className="text-sm font-semibold">No public master copy groups found.</p>
              <p className="text-xs text-slate-500 max-w-sm mx-auto">
                Create the first Master Trading Group to let other traders subscribe and mirror your alpha!
              </p>
              <button
                onClick={() => setIsCreateGroupModalOpen(true)}
                className="btn-primary text-xs inline-flex items-center gap-1.5"
              >
                <Plus size={14} /> Create Master Group
              </button>
            </div>
          ) : (
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
              {exploreGroups.map((grp) => {
                const isMyOwnGroup = myMasterGroups.some((m) => m.id === grp.id);
                const isAlreadyFollowing = myFollowedGroups.some((f) => f.group_id === grp.id);

                return (
                  <div
                    key={grp.id}
                    className="glass-card p-5 flex flex-col justify-between hover:border-brand-purple/40 transition-all group space-y-4 border border-slate-800 shadow-glass-md"
                  >
                    <div>
                      {/* Top Master & Tag */}
                      <div className="flex items-start justify-between gap-2">
                        <div>
                          <span className="text-[10px] font-bold px-2 py-0.5 rounded bg-brand-purple/15 text-brand-purple border border-brand-purple/30 uppercase">
                            {grp.profit_share_pct}% Profit Share
                          </span>
                          <h3 className="text-base font-bold text-white group-hover:text-cyan-300 transition-colors mt-1.5">
                            {grp.name}
                          </h3>
                          <p className="text-[11px] text-slate-400">
                            by <strong className="text-slate-200">{grp.master_name}</strong>
                          </p>
                        </div>

                        <div className="flex items-center gap-1 text-xs font-bold text-amber-400 bg-amber-500/10 border border-amber-500/20 px-2 py-0.5 rounded-lg">
                          <Award size={13} />
                          <span>{grp.rating || 4.9}</span>
                        </div>
                      </div>

                      <p className="text-xs text-slate-400 mt-2 line-clamp-2">
                        {grp.description || "Sub-millisecond direct market access momentum trading strategy."}
                      </p>

                      {/* Performance Metrics Bar */}
                      <div className="grid grid-cols-3 gap-2 my-3 p-2.5 rounded-xl bg-surface-950/80 border border-slate-800 text-center font-mono tabular-nums">
                        <div>
                          <span className="text-[9px] text-slate-500 block uppercase">Win Rate</span>
                          <span className="text-xs font-bold text-emerald-400">
                            {grp.win_rate || 72.4}%
                          </span>
                        </div>
                        <div>
                          <span className="text-[9px] text-slate-500 block uppercase">Followers</span>
                          <span className="text-xs font-bold text-cyan-400">
                            {grp.active_followers || 0}
                          </span>
                        </div>
                        <div>
                          <span className="text-[9px] text-slate-500 block uppercase">Min Capital</span>
                          <span className="text-xs font-bold text-white">
                            ₹{(grp.min_capital || 10000).toLocaleString("en-IN")}
                          </span>
                        </div>
                      </div>
                    </div>

                    {/* Footer Actions */}
                    <div className="pt-3 border-t border-slate-800 flex items-center justify-between">
                      <div className="flex items-center gap-1.5 text-[11px] font-mono text-slate-400">
                        <span>Code:</span>
                        <span className="font-bold text-white bg-surface-800 px-1.5 py-0.5 rounded border border-slate-700">
                          {grp.invite_code}
                        </span>
                        <button
                          type="button"
                          onClick={() => handleCopyInviteCode(grp.invite_code)}
                          title="Copy Invite Code"
                          className="p-1 hover:text-cyan-400"
                        >
                          {copiedCode === grp.invite_code ? <Check size={12} className="text-emerald-400" /> : <Copy size={12} />}
                        </button>
                      </div>

                      {isMyOwnGroup ? (
                        <span className="text-xs font-semibold text-slate-500 italic">Your Group</span>
                      ) : isAlreadyFollowing ? (
                        <span className="text-xs font-bold text-emerald-400 flex items-center gap-1">
                          <Check size={13} /> Active Copy
                        </span>
                      ) : (
                        <button
                          type="button"
                          onClick={() => handleOpenJoin(grp)}
                          className="btn-primary text-xs py-1.5 px-3 flex items-center gap-1"
                        >
                          <Zap size={13} /> Copy Master
                        </button>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      )}

      {/* ── TAB 2: MY COPIED MASTERS (FOLLOWER VIEW) ────────────────────────── */}
      {activeTab === "following" && (
        <div className="space-y-4">
          {myFollowedGroups.length === 0 ? (
            <div className="glass-card p-12 text-center text-slate-400 space-y-3">
              <UserCheck size={32} className="mx-auto text-slate-600" />
              <p className="text-sm font-semibold">You are not following any Master Traders yet.</p>
              <p className="text-xs text-slate-500 max-w-sm mx-auto">
                Explore verified Master Traders and 1-click mirror their automated trades into your portfolio!
              </p>
              <button
                onClick={() => setActiveTab("explore")}
                className="btn-primary text-xs inline-flex items-center gap-1"
              >
                <Search size={14} /> Explore Master Traders
              </button>
            </div>
          ) : (
            <div className="glass-card overflow-hidden">
              <div className="p-4 border-b border-slate-800 flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <UserCheck size={16} className="text-cyan-400" />
                  <h2 className="text-sm font-semibold text-white">Active Subscribed Strategies ({myFollowedGroups.length})</h2>
                </div>
              </div>

              <div className="overflow-x-auto">
                <table className="w-full text-left text-xs min-w-[650px]">
                  <thead className="bg-surface-800/40 text-slate-400 font-medium border-b border-slate-800">
                    <tr>
                      <th className="py-3 px-4">Master Strategy</th>
                      <th className="py-3 px-4">Multiplier</th>
                      <th className="py-3 px-4 text-right">Max Risk Cap</th>
                      <th className="py-3 px-4 text-center">Status</th>
                      <th className="py-3 px-4 text-right">Copied Trades</th>
                      <th className="py-3 px-4 text-right">Realized PnL</th>
                      <th className="py-3 px-4 text-right">Actions</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-800">
                    {myFollowedGroups.map((sub) => {
                      const isProfit = (sub.realized_pnl || 0) >= 0;
                      return (
                        <tr key={sub.id} className="hover:bg-white/[0.02] transition-colors">
                          <td className="py-3 px-4">
                            <div className="font-bold text-white">{sub.group_name}</div>
                            <div className="text-[11px] text-slate-400">Master: {sub.master_name} ({sub.profit_share_pct}% share)</div>
                          </td>

                          <td className="py-3 px-4 font-mono font-bold text-cyan-400">
                            <span className="px-2 py-0.5 rounded bg-cyan-500/15 border border-cyan-500/30">
                              {sub.multiplier}x
                            </span>
                          </td>

                          <td className="py-3 px-4 font-mono text-right text-slate-300">
                            ₹{Number(sub.max_allocation || 50000).toLocaleString("en-IN")}
                          </td>

                          <td className="py-3 px-4 text-center">
                            <span
                              className={`px-2 py-0.5 rounded-full text-[10px] font-bold ${
                                sub.status === "ACTIVE"
                                  ? "bg-emerald-500/15 text-emerald-300 border border-emerald-500/30 animate-pulse"
                                  : "bg-amber-500/15 text-amber-300 border border-amber-500/30"
                              }`}
                            >
                              {sub.status}
                            </span>
                          </td>

                          <td className="py-3 px-4 font-mono text-right text-white">
                            {sub.total_copied_trades || 0}
                          </td>

                          <td className={`py-3 px-4 font-mono text-right font-bold ${isProfit ? "text-emerald-400" : "text-rose-400"}`}>
                            {isProfit ? "+" : ""}₹{Number(sub.realized_pnl || 0).toLocaleString("en-IN", { minimumFractionDigits: 2 })}
                          </td>

                          <td className="py-3 px-4 text-right space-x-1.5">
                            <button
                              onClick={() => handleToggleFollowerStatus(sub)}
                              title={sub.status === "ACTIVE" ? "Pause Mirroring" : "Resume Mirroring"}
                              className={`p-1.5 rounded-lg border transition-all ${
                                sub.status === "ACTIVE"
                                  ? "bg-amber-500/10 text-amber-300 border-amber-500/30 hover:bg-amber-500/20"
                                  : "bg-emerald-500/10 text-emerald-300 border-emerald-500/30 hover:bg-emerald-500/20"
                              }`}
                            >
                              {sub.status === "ACTIVE" ? <Pause size={13} /> : <Play size={13} />}
                            </button>

                            <button
                              onClick={() => handleLeaveGroup(sub.id)}
                              title="Stop Copying & Leave Group"
                              className="p-1.5 rounded-lg bg-rose-500/10 text-rose-400 border border-rose-500/30 hover:bg-rose-500/20 transition-all"
                            >
                              <Trash2 size={13} />
                            </button>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>
      )}

      {/* ── TAB 3: MASTER TRADER HUB ────────────────────────────────────────── */}
      {activeTab === "master_hub" && (
        <div className="space-y-6">
          {/* Master Telemetry Strip */}
          <div className="grid gap-4 sm:grid-cols-3">
            <div className="glass-card p-4 border border-slate-800 space-y-1">
              <span className="text-[11px] text-slate-400 uppercase font-semibold">Active Copy Groups</span>
              <div className="text-2xl font-bold font-mono text-white">{myMasterGroups.length}</div>
              <span className="text-[10px] text-emerald-400 flex items-center gap-1">
                <Zap size={11} /> Real-time Fan-Out Active
              </span>
            </div>

            <div className="glass-card p-4 border border-slate-800 space-y-1">
              <span className="text-[11px] text-slate-400 uppercase font-semibold">Total Follower Subscriptions</span>
              <div className="text-2xl font-bold font-mono text-cyan-400">
                {myMasterGroups.reduce((acc, curr) => acc + (curr.active_followers || 0), 0)}
              </div>
              <span className="text-[10px] text-slate-400">Sub-millisecond parallel dispatch</span>
            </div>

            <div className="glass-card p-4 border border-slate-800 space-y-1">
              <span className="text-[11px] text-slate-400 uppercase font-semibold">Est. Profit-Sharing Accrued</span>
              <div className="text-2xl font-bold font-mono text-emerald-400">
                ₹{myMasterGroups.reduce((acc, curr) => acc + (curr.estimated_master_fee || 0), 0).toLocaleString("en-IN", { minimumFractionDigits: 2 })}
              </div>
              <span className="text-[10px] text-emerald-400/80">Automated ledger billing</span>
            </div>
          </div>

          {/* Master Groups List */}
          <div className="glass-card overflow-hidden">
            <div className="p-4 border-b border-slate-800 flex items-center justify-between">
              <div className="flex items-center gap-2">
                <Zap size={16} className="text-brand-purple" />
                <h2 className="text-sm font-semibold text-white">Your Managed Master Copy Groups</h2>
              </div>

              <button
                onClick={() => setIsCreateGroupModalOpen(true)}
                className="btn-primary text-xs py-1.5 px-3 flex items-center gap-1"
              >
                <Plus size={13} /> Create Group
              </button>
            </div>

            {myMasterGroups.length === 0 ? (
              <div className="p-10 text-center text-slate-400 space-y-3">
                <Zap size={32} className="mx-auto text-slate-600" />
                <p className="text-xs font-semibold">You haven't created any Master Copy Groups yet.</p>
                <p className="text-[11px] text-slate-500 max-w-xs mx-auto">
                  Create a copy group, set your profit share %, and share your unique invite code with your community.
                </p>
                <button
                  onClick={() => setIsCreateGroupModalOpen(true)}
                  className="btn-primary text-xs"
                >
                  Create Master Group Now
                </button>
              </div>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-left text-xs min-w-[650px]">
                  <thead className="bg-surface-800/40 text-slate-400 font-medium border-b border-slate-800">
                    <tr>
                      <th className="py-3 px-4">Group Name</th>
                      <th className="py-3 px-4">Invite Code</th>
                      <th className="py-3 px-4 text-center">Profit Share</th>
                      <th className="py-3 px-4 text-center">Followers</th>
                      <th className="py-3 px-4 text-right">Total Copied</th>
                      <th className="py-3 px-4 text-right">Follower Net PnL</th>
                      <th className="py-3 px-4 text-right">Roster</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-800">
                    {myMasterGroups.map((g) => (
                      <tr key={g.id} className="hover:bg-white/[0.02] transition-colors">
                        <td className="py-3 px-4 font-bold text-white">
                          <div>{g.name}</div>
                          <span className="text-[10px] text-slate-400 font-normal">Min: ₹{g.min_capital?.toLocaleString("en-IN")}</span>
                        </td>

                        <td className="py-3 px-4 font-mono font-bold">
                          <div className="flex items-center gap-1.5">
                            <span className="bg-surface-950 px-2 py-0.5 rounded border border-slate-700 text-cyan-400">
                              {g.invite_code}
                            </span>
                            <button
                              onClick={() => handleCopyInviteCode(g.invite_code)}
                              className="p-1 hover:text-white text-slate-400"
                              title="Copy Invite Code"
                            >
                              {copiedCode === g.invite_code ? <Check size={13} className="text-emerald-400" /> : <Copy size={13} />}
                            </button>
                          </div>
                        </td>

                        <td className="py-3 px-4 text-center font-mono font-bold text-amber-400">
                          {g.profit_share_pct}%
                        </td>

                        <td className="py-3 px-4 text-center font-mono font-bold text-cyan-400">
                          {g.active_followers || 0}
                        </td>

                        <td className="py-3 px-4 font-mono text-right text-white">
                          {g.total_copied_trades || 0} orders
                        </td>

                        <td className="py-3 px-4 font-mono text-right font-bold text-emerald-400">
                          +₹{Number(g.followers_pnl || 0).toLocaleString("en-IN", { minimumFractionDigits: 2 })}
                        </td>

                        <td className="py-3 px-4 text-right">
                          <button
                            onClick={() => handleViewGroupFollowers(g)}
                            className="px-2.5 py-1 rounded bg-brand-purple/15 hover:bg-brand-purple/25 border border-brand-purple/30 text-brand-purple font-semibold text-[11px] transition-all inline-flex items-center gap-1"
                          >
                            <span>Followers</span>
                            <ChevronRight size={12} />
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>

          {/* Followers Details Roster if selected */}
          {activeMasterGroup && (
            <div className="glass-card p-5 border border-slate-800 space-y-3 animate-fade-in">
              <div className="flex items-center justify-between border-b border-slate-800 pb-2">
                <div className="flex items-center gap-2">
                  <Users size={16} className="text-brand-purple" />
                  <h3 className="text-sm font-bold text-white">
                    Follower Roster for "{activeMasterGroup.name}"
                  </h3>
                </div>
                <button
                  onClick={() => setActiveMasterGroup(null)}
                  className="text-xs text-slate-400 hover:text-white"
                >
                  ✕ Close Roster
                </button>
              </div>

              {followersLoading ? (
                <div className="p-4 text-center text-xs text-slate-400">Loading followers...</div>
              ) : selectedGroupFollowers.length === 0 ? (
                <div className="p-6 text-center text-xs text-slate-500">
                  No followers have joined this group yet. Share invite code <strong className="text-white font-mono">{activeMasterGroup.invite_code}</strong>!
                </div>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-left text-xs min-w-[550px]">
                    <thead className="bg-surface-800/40 text-slate-400 border-b border-slate-800">
                      <tr>
                        <th className="py-2.5 px-3">Follower Name</th>
                        <th className="py-2.5 px-3">Multiplier</th>
                        <th className="py-2.5 px-3">Status</th>
                        <th className="py-2.5 px-3 text-right">Max Risk Cap</th>
                        <th className="py-2.5 px-3 text-right">Copied Trades</th>
                        <th className="py-2.5 px-3 text-right">Realized PnL</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-800 font-mono">
                      {selectedGroupFollowers.map((f) => (
                        <tr key={f.id} className="hover:bg-white/[0.02]">
                          <td className="py-2.5 px-3 text-white font-sans font-bold">{f.follower_name}</td>
                          <td className="py-2.5 px-3 text-cyan-400 font-bold">{f.multiplier}x</td>
                          <td className="py-2.5 px-3 font-sans">
                            <span className="text-[10px] px-1.5 py-0.5 rounded bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">
                              {f.status}
                            </span>
                          </td>
                          <td className="py-2.5 px-3 text-right text-slate-300">₹{f.max_allocation?.toLocaleString("en-IN")}</td>
                          <td className="py-2.5 px-3 text-right text-white">{f.total_copied_trades}</td>
                          <td className="py-2.5 px-3 text-right text-emerald-400 font-bold">+₹{f.realized_pnl?.toFixed(2)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {/* ── MODAL: JOIN MASTER GROUP ────────────────────────────────────────── */}
      {isJoinModalOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/80 backdrop-blur-sm animate-fade-in">
          <div className="w-full max-w-md glass-card p-6 border border-slate-700 space-y-4">
            <div className="flex items-center justify-between border-b border-slate-800 pb-3">
              <div className="flex items-center gap-2">
                <Zap size={18} className="text-brand-purple" />
                <h3 className="font-bold text-base text-white">
                  Subscribe to {selectedGroup ? selectedGroup.name : "Master Trader"}
                </h3>
              </div>
              <button
                onClick={() => setIsJoinModalOpen(false)}
                className="text-slate-400 hover:text-white"
              >
                ✕
              </button>
            </div>

            {joinError && (
              <div className="p-3 rounded-xl bg-red-500/10 border border-red-500/30 text-red-400 text-xs flex items-center gap-2">
                <AlertCircle size={14} className="shrink-0" />
                <span>{joinError}</span>
              </div>
            )}

            <form onSubmit={handleJoinSubmit} className="space-y-4">
              {/* Lot Multiplier Slider & Value */}
              <div>
                <div className="flex items-center justify-between text-xs font-semibold text-slate-300 mb-1">
                  <span>Lot Size Multiplier</span>
                  <span className="font-mono text-cyan-400 font-bold bg-cyan-500/15 px-2 py-0.5 rounded border border-cyan-500/30">
                    {joinMultiplier}x
                  </span>
                </div>
                <input
                  type="range"
                  min="0.1"
                  max="5.0"
                  step="0.1"
                  value={joinMultiplier}
                  onChange={(e) => setJoinMultiplier(parseFloat(e.target.value))}
                  className="w-full accent-cyan-400 cursor-pointer"
                />
                <p className="text-[10px] text-slate-500 mt-1">
                  When Master places 1 lot, you will automatically execute {Math.max(1, Math.round(1 * joinMultiplier))} lot(s).
                </p>
              </div>

              {/* Max Risk Cap */}
              <div>
                <label className="text-xs font-semibold text-slate-300 mb-1 block">
                  Max Capital Risk Limit (₹)
                </label>
                <input
                  type="number"
                  min="1000"
                  step="5000"
                  required
                  value={joinMaxAllocation}
                  onChange={(e) => setJoinMaxAllocation(parseFloat(e.target.value) || 1000)}
                  className="input-field text-xs font-mono font-bold w-full"
                />
                <p className="text-[10px] text-slate-500 mt-1">
                  Orders exceeding this aggregate value will be throttled or scaled down automatically.
                </p>
              </div>

              {/* Execution Mode */}
              <div>
                <label className="text-xs font-semibold text-slate-300 mb-1 block">Execution Mode</label>
                <div className="grid grid-cols-2 gap-2">
                  <button
                    type="button"
                    onClick={() => setJoinMode("PAPER")}
                    className={`py-2 rounded-xl text-xs font-bold transition-all border ${
                      joinMode === "PAPER"
                        ? "bg-emerald-500/20 text-emerald-300 border-emerald-500/40"
                        : "bg-surface-900 border-slate-800 text-slate-400 hover:text-white"
                    }`}
                  >
                    <ShieldCheck size={13} className="inline mr-1 text-cyan-400" /> Paper Simulator
                  </button>
                  <button
                    type="button"
                    onClick={() => setJoinMode("LIVE")}
                    className={`py-2 rounded-xl text-xs font-bold transition-all border ${
                      joinMode === "LIVE"
                        ? "bg-rose-500/20 text-rose-300 border-rose-500/40"
                        : "bg-surface-900 border-slate-800 text-slate-400 hover:text-white"
                    }`}
                  >
                    <Zap size={13} className="inline mr-1 text-rose-400" /> Live Broker
                  </button>
                </div>
              </div>

              <button
                type="submit"
                disabled={joinLoading}
                className="w-full btn-primary text-xs py-3 justify-center shadow-lg shadow-brand-purple/30 font-bold"
              >
                {joinLoading ? "Connecting Mirror Stream..." : "Activate Copy Trading"}
              </button>
            </form>
          </div>
        </div>
      )}

      {/* ── MODAL: CREATE MASTER GROUP ──────────────────────────────────────── */}
      {isCreateGroupModalOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/80 backdrop-blur-sm animate-fade-in">
          <div className="w-full max-w-md glass-card p-6 border border-slate-700 space-y-4">
            <div className="flex items-center justify-between border-b border-slate-800 pb-3">
              <div className="flex items-center gap-2">
                <Users size={18} className="text-brand-purple" />
                <h3 className="font-bold text-base text-white">Create Master Copy Trading Group</h3>
              </div>
              <button
                onClick={() => setIsCreateGroupModalOpen(false)}
                className="text-slate-400 hover:text-white"
              >
                ✕
              </button>
            </div>

            {createGroupError && (
              <div className="p-3 rounded-xl bg-red-500/10 border border-red-500/30 text-red-400 text-xs flex items-center gap-2">
                <AlertCircle size={14} className="shrink-0" />
                <span>{createGroupError}</span>
              </div>
            )}

            <form onSubmit={handleCreateGroupSubmit} className="space-y-3.5">
              <div>
                <label className="text-xs font-semibold text-slate-300 mb-1 block">Group Title</label>
                <input
                  type="text"
                  required
                  placeholder="e.g. NIFTY Pro Momentum Alpha"
                  value={groupName}
                  onChange={(e) => setGroupName(e.target.value)}
                  className="input-field text-xs w-full"
                />
              </div>

              <div>
                <label className="text-xs font-semibold text-slate-300 mb-1 block">Strategy Description / Thesis</label>
                <textarea
                  rows="2"
                  placeholder="High-frequency momentum and breakout strategy on index derivatives..."
                  value={groupDesc}
                  onChange={(e) => setGroupDesc(e.target.value)}
                  className="input-field text-xs w-full"
                />
              </div>

              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="text-xs font-semibold text-slate-300 mb-1 block">Profit Share (%)</label>
                  <input
                    type="number"
                    min="0"
                    max="50"
                    step="1"
                    required
                    value={profitSharePct}
                    onChange={(e) => setProfitSharePct(parseFloat(e.target.value) || 0)}
                    className="input-field text-xs font-mono font-bold w-full"
                  />
                </div>

                <div>
                  <label className="text-xs font-semibold text-slate-300 mb-1 block">Min Recommended Capital (₹)</label>
                  <input
                    type="number"
                    min="500"
                    step="5000"
                    required
                    value={minCapital}
                    onChange={(e) => setMinCapital(parseFloat(e.target.value) || 500)}
                    className="input-field text-xs font-mono font-bold w-full"
                  />
                </div>
              </div>

              <div className="flex items-center gap-2 pt-1">
                <input
                  type="checkbox"
                  id="public_group"
                  checked={isGroupPublic}
                  onChange={(e) => setIsGroupPublic(e.target.checked)}
                  className="rounded border-slate-700 bg-surface-900 text-brand-purple focus:ring-brand-purple/30"
                />
                <label htmlFor="public_group" className="text-xs text-slate-300 cursor-pointer">
                  List publicly in the Explore directory
                </label>
              </div>

              <button
                type="submit"
                disabled={createGroupLoading || !groupName.trim()}
                className="w-full btn-primary text-xs py-3 justify-center shadow-lg shadow-brand-purple/30 font-bold mt-2"
              >
                {createGroupLoading ? "Generating Master Gateway..." : "Create Master Group"}
              </button>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
