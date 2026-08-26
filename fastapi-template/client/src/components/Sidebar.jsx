import { useState, useEffect } from "react";
import { NavLink, useNavigate } from "react-router-dom";
import {
  LayoutDashboard,
  Store,
  Eye,
  Brain,
  History,
  Settings as SettingsIcon,
  Activity,
  User,
  LogOut,
  ShieldCheck,
  Menu,
  X,
  Users,
  Server,
  CreditCard,
  Lock,
  Workflow,
  FlaskConical,
  LineChart,
  Wallet,
  Radar,
  FileText,
  BadgeCheck,
  Globe2,
  Crown,
} from "lucide-react";
import AuthModal from "./AuthModal";
import { useAuthStore } from "../stores/useAuthStore";

// Lifecycle information architecture (BUILD > VALIDATE > SIMULATE > DEPLOY > CONTROL > LEARN)
const NAV_GROUPS = [
  { header: "COMMAND", items: [
    { to: "/", icon: LayoutDashboard, label: "Command Center" },
    { to: "/markets", icon: Globe2, label: "Markets" },
    { to: "/portfolio", icon: Wallet, label: "Portfolio" },
    { to: "/execution", icon: Radar, label: "Live Execution" },
  ] },
  { header: "BUILD", items: [
    { to: "/strategies", icon: Brain, label: "Strategies" },
    { to: "/quant-lab", icon: FlaskConical, label: "AI Strategy Builder" },
    { to: "/visual-builder", icon: Workflow, label: "Visual Options Builder" },
  ] },
  { header: "VALIDATE", items: [
    { to: "/backtest", icon: LineChart, label: "Truthful Backtest" },
    { to: "/reality-mode", icon: Activity, label: "Reality Mode" },
  ] },
  { header: "DISCOVER", items: [
    { to: "/marketplace", icon: Store, label: "Marketplace" },
    { to: "/copy-trading", icon: Users, label: "Copy Trading" },
  ] },
  { header: "CONTROL", items: [
    { to: "/execution#risk", icon: ShieldCheck, label: "Risk Center" },
    { to: "/broker-sessions", icon: Server, label: "Broker Sessions" },
    { to: "/watchlist", icon: Eye, label: "Watchlist & Alerts" },
  ] },
  { header: "ANALYZE", items: [
    { to: "/history", icon: History, label: "Trade History" },
    { to: "/trade-journal", icon: FileText, label: "AI Trade Journal" },
  ] },
  { header: "SYSTEM", items: [
    { to: "/pricing", icon: CreditCard, label: "Pricing & Plans" },
    { to: "/kyc", icon: BadgeCheck, label: "KYC Center" },
    { to: "/settings", icon: SettingsIcon, label: "Profile & Settings" },
    { to: "/admin", icon: Lock, label: "Admin Sentinel" },
  ] },
];

// Flat render list with section-header markers interleaved
const links = NAV_GROUPS.flatMap((g) => [
  { header: g.header },
  ...g.items,
]);

export default function Sidebar({ onOpenKYC, kycStatus = "NOT_SUBMITTED" }) {
  const navigate = useNavigate();
  const [isAuthOpen, setIsAuthOpen] = useState(false);
  const [mobileDrawerOpen, setMobileDrawerOpen] = useState(false);
  const authUser = useAuthStore((state) => state.user);
  const currentUser = authUser || { full_name: "Trader", email: "" };
  const isAuthenticated = useAuthStore((state) => state.isAuthenticated);
  const initializeAuth = useAuthStore((state) => state.initialize);
  const logout = useAuthStore((state) => state.logout);

  useEffect(() => {
    initializeAuth();
  }, [initializeAuth]);

  const handleLogout = async () => {
    await logout();
    setMobileDrawerOpen(false);
    navigate("/login", { state: { notice: "You have been logged out." } });
  };

  return (
    <>
      {/* ── MOBILE TOP NAVIGATION BAR (Visible < lg) ────────────────── */}
      <header className="md:hidden fixed top-0 left-0 right-0 z-40 h-14 bg-surface-950/90 border-b border-slate-800/80 backdrop-blur-md px-4 flex items-center justify-between">
        <div className="flex items-center gap-2.5">
                    <div className="flex h-8 w-8 items-center justify-center rounded-xl bg-gradient-to-br from-violet-500 via-brand-purple to-cyan-400 p-0.5 shadow-none">
            <div className="flex h-full w-full items-center justify-center rounded-lg bg-surface-900">
              <Crown size={16} className="text-white" />
            </div>
          </div>
          <span className="font-display font-bold text-sm text-white tracking-tight">TradeThrone</span>
        </div>

        <div className="flex items-center gap-2">
          {isAuthenticated ? (
            <div className="flex items-center gap-1.5">
              <button
                onClick={() => setMobileDrawerOpen(true)}
                aria-label="Open user profile and navigation"
                className="flex min-h-[44px] min-w-[44px] items-center justify-center rounded-xl border border-emerald-500/30 bg-emerald-500/10 text-emerald-300"
              >
                <span className="flex h-6 w-6 items-center justify-center rounded-full bg-emerald-400 text-xs font-bold text-slate-950">
                  {currentUser.full_name ? currentUser.full_name[0].toUpperCase() : "T"}
                </span>
              </button>
              <button
                onClick={handleLogout}
                aria-label="Log out of TradeThrone"
                className="flex min-h-[44px] items-center gap-1.5 rounded-xl border border-rose-500/50 bg-rose-500/15 px-2.5 text-xs font-bold text-rose-300 transition-colors hover:bg-rose-500/25"
              >
                <LogOut size={16} />
                <span>Logout</span>
              </button>
            </div>
          ) : (
            <button
              onClick={() => setIsAuthOpen(true)}
              className="min-h-[44px] rounded-xl border border-emerald-500/40 bg-emerald-500 px-3 text-xs font-bold text-slate-950"
            >
              Sign In / Register
            </button>
          )}
          <button
            onClick={() => setMobileDrawerOpen(!mobileDrawerOpen)}
            aria-label="Toggle navigation menu"
            className="min-h-[44px] min-w-[44px] flex items-center justify-center rounded-xl bg-surface-800 border border-slate-700 text-slate-300 hover:text-white"
          >
            {mobileDrawerOpen ? <X size={20} /> : <Menu size={20} />}
          </button>
        </div>
      </header>

      {/* ── MOBILE SLIDE-OVER DRAWER (Visible when opened on mobile) ── */}
      {mobileDrawerOpen && (
        <div className="md:hidden fixed inset-0 z-[60] flex">
          {/* Backdrop */}
          <div
            onClick={() => setMobileDrawerOpen(false)}
            className="fixed inset-0 z-[50] bg-black/80 backdrop-blur-sm animate-fade-in"
          />

          {/* Drawer Content */}
          <div className="relative z-[60] flex h-screen w-72 max-w-full flex-col gap-4 overflow-y-auto bg-surface-900 border-r border-slate-800 p-5 shadow-2xl animate-slide-in-right">
            <div className="flex items-center justify-between gap-3 border-b border-slate-800 pb-4">
              <div className="flex min-w-0 items-center gap-2.5">
                <div className="relative flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-gradient-to-br from-violet-500 via-brand-purple to-cyan-400 p-0.5 shadow-none">
                  <div className="flex h-full w-full items-center justify-center rounded-lg bg-surface-900">
                    <Crown size={16} className="text-white" />
                  </div>
                </div>
                <h2 className="truncate font-display text-sm font-bold text-white">TradeThrone Mobile</h2>
              </div>
              <button
                onClick={() => setMobileDrawerOpen(false)}
                aria-label="Close navigation drawer"
                className="flex h-11 w-11 shrink-0 items-center justify-center rounded-xl border border-slate-700/60 bg-surface-800 text-slate-400 transition-colors hover:bg-surface-700 hover:text-white active:scale-95"
              >
                <X size={18} />
              </button>
            </div>

            {/* Links */}
            <nav className="flex-1 space-y-1 overflow-y-auto pt-1">
              {links.map((item) =>
                item.header ? (
                  <p
                    key={item.header}
                    className="px-3.5 pb-1 pt-3 text-[9px] font-bold uppercase tracking-[0.18em] text-slate-600"
                  >
                    {item.header}
                  </p>
                ) : (
                  <NavLink
                    key={item.to}
                    to={item.to}
                    onClick={() => setMobileDrawerOpen(false)}
                    className={({ isActive }) =>
                      `flex items-center gap-3 rounded-xl px-3.5 py-3 text-xs font-semibold transition-all min-h-[44px] ${
                        isActive
                          ? "bg-brand-purple/15 text-brand-purple border border-brand-purple/25 shadow-sm"
                          : "text-slate-400 hover:bg-surface-800 hover:text-white"
                      }`
                    }
                  >
                    <item.icon size={18} />
                    <span>{item.label}</span>
                  </NavLink>
                )
              )}
            </nav>

            {/* User Auth Section */}
            <div className="pt-3 border-t border-slate-800">
              {isAuthenticated ? (
                <div className="space-y-2">
                  <div className="flex items-center gap-2 rounded-xl border border-slate-700/60 bg-surface-800 p-2.5">
                    <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-brand-purple/20 text-xs font-bold text-brand-purple">
                      {currentUser.full_name ? currentUser.full_name[0].toUpperCase() : "T"}
                    </div>
                    <div className="min-w-0 truncate text-xs">
                      <p className="font-bold text-white truncate">{currentUser.full_name || "Trader"}</p>
                      <p className="text-[10px] text-slate-400 truncate">{currentUser.email}</p>
                    </div>
                  </div>
                  <button
                    onClick={handleLogout}
                    aria-label="Log out of TradeThrone"
                    className="flex min-h-11 w-full items-center justify-center gap-2 rounded-xl border border-rose-500/40 bg-rose-500/10 px-3 text-xs font-bold text-rose-300 transition-colors hover:bg-rose-500/20 hover:text-rose-200"
                  >
                    <LogOut size={16} />
                    Logout
                  </button>
                </div>
              ) : (
                <button
                  onClick={() => {
                    setMobileDrawerOpen(false);
                    setIsAuthOpen(true);
                  }}
                  className="w-full btn-primary text-xs"
                >
                  <User size={14} />
                  Sign In / Register
                </button>
              )}
            </div>
          </div>
        </div>
      )}

      {/* ── DESKTOP FIXED SIDEBAR (Visible >= lg) ────────────────────── */}
      <aside className="hidden md:flex fixed left-0 top-0 z-30 h-screen w-64 flex-col border-r border-slate-800/80 bg-surface-900/60 backdrop-blur-md">
                {/* Brand Header — TradeThrone Crown + PRO TRADING DESK */}
        <div className="flex items-center gap-3 border-b border-slate-800/80 px-5 py-4.5">
          <div className="relative flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-gradient-to-br from-violet-500 via-brand-purple to-cyan-400 p-0.5 shadow-none">
            <div className="flex h-full w-full items-center justify-center rounded-lg bg-surface-900">
              <Crown size={20} className="text-white" />
            </div>
            <span className="absolute -bottom-0.5 left-1/2 -translate-x-1/2 block h-0.5 w-5 rounded-full bg-gradient-to-r from-violet-500 via-brand-purple to-cyan-400 shadow-[0_0_10px_rgba(132,204,255,0.6)]"></span>
          </div>
          <div className="flex flex-col">
            <h1 className="text-base font-display font-bold text-white tracking-tight">
              TradeThrone
            </h1>
            <p className="text-[9px] font-mono text-slate-500 tracking-[0.25em] uppercase">
              PRO TRADING DESK
            </p>
          </div>
        </div>

        {/* Navigation */}
        <nav className="flex-1 space-y-1.5 px-3 py-4 overflow-y-auto">
          {links.map((item) =>
            item.header ? (
              <p
                key={item.header}
                className="px-3.5 pb-1 pt-4 text-[9px] font-bold uppercase tracking-[0.18em] text-slate-600 first:pt-0"
              >
                {item.header}
              </p>
            ) : (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.to === "/"}
                className={({ isActive }) =>
                  `flex items-center gap-3 rounded-xl px-3.5 py-2.5 text-xs font-semibold transition-all duration-150 min-h-[40px] ${
                    isActive
                      ? "bg-brand-purple/15 text-brand-purple border border-brand-purple/30 shadow-sm shadow-brand-purple/10 font-bold"
                      : "text-slate-400 hover:bg-surface-800/80 hover:text-white"
                  }`
                }
              >
                <item.icon size={17} />
                <span>{item.label}</span>
              </NavLink>
            )
          )}
        </nav>

        {/* User Account / Auth Section */}
        <div className="border-t border-slate-800/80 p-3 space-y-2">
          {isAuthenticated ? (
            <>
              <div className="flex items-center justify-between p-2.5 rounded-xl bg-surface-800/80 border border-slate-700/60">
                <div className="flex items-center gap-2.5 overflow-hidden">
                  <div className="w-8 h-8 rounded-full bg-brand-purple/20 text-brand-purple border border-brand-purple/30 flex items-center justify-center text-xs font-bold shrink-0">
                    {currentUser.full_name ? currentUser.full_name[0].toUpperCase() : "T"}
                  </div>
                  <div className="overflow-hidden">
                    <p className="text-xs font-bold text-white truncate">
                      {currentUser.full_name || "Trader"}
                    </p>
                    <p className="text-[10px] text-slate-400 truncate">{currentUser.email}</p>
                  </div>
                </div>
                <button
                  onClick={handleLogout}
                  title="Log Out"
                  className="p-1.5 text-slate-400 hover:text-rose-400 transition-colors rounded-lg hover:bg-surface-700"
                >
                  <LogOut size={15} />
                </button>
              </div>

              {onOpenKYC && (
                <button
                  onClick={onOpenKYC}
                  className={`w-full py-1.5 px-2.5 rounded-lg border text-[11px] font-semibold flex items-center justify-between transition-all ${
                    kycStatus === "VERIFIED"
                      ? "bg-emerald-500/10 border-emerald-500/30 text-emerald-400"
                      : kycStatus === "PENDING"
                      ? "bg-amber-500/10 border-amber-500/30 text-amber-400"
                      : "bg-violet-600/15 border-violet-500/30 text-violet-300 hover:bg-violet-600/25"
                  }`}
                >
                  <span className="flex items-center gap-1.5">
                    <ShieldCheck size={13} />
                    <span>SEBI KYC Compliance</span>
                  </span>
                  <span className="font-bold">
                    {kycStatus === "VERIFIED" ? "✓ Verified" : kycStatus === "PENDING" ? "⏳ Pending" : "Submit →"}
                  </span>
                </button>
              )}
            </>
          ) : (
            <button
              onClick={() => setIsAuthOpen(true)}
              className="w-full btn-primary text-xs min-h-[40px]"
            >
              <User size={14} />
              Sign In / OTP
            </button>
          )}
        </div>

        {/* Status footer */}
        <div className="border-t border-slate-800/80 px-4 py-3">
          <div className="flex items-center gap-2">
            <Activity size={14} className="animate-pulse text-emerald-400" />
            <span className="text-[11px] font-mono text-slate-400">DMA Engine: <strong className="text-emerald-400">ACTIVE (0ms)</strong></span>
          </div>
        </div>
      </aside>

      {/* Authentication Modal */}
      <AuthModal
        isOpen={isAuthOpen}
        onClose={() => setIsAuthOpen(false)}
        onAuthSuccess={() => initializeAuth()}
      />
    </>
  );
}
