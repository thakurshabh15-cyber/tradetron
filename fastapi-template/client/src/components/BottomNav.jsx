import { NavLink } from "react-router-dom";
import { CreditCard, Wallet, Workflow, Terminal, Menu, Globe2, Brain, FileText, ShieldCheck, Activity, LayoutDashboard, Settings, Users, X } from "lucide-react";
import { useState } from "react";

const items = [
  { to: "/command-center", label: "Home", icon: Terminal },
  { to: "/markets", label: "Markets", icon: Workflow },
  { to: "/portfolio", label: "Portfolio", icon: Wallet },
  { to: "/execution", label: "Orders", icon: CreditCard },
  { to: null, label: "More", icon: Menu },
];

const MORE_LINKS = [
  { to: "/strategies", icon: Brain, label: "Strategies" },
  { to: "/quant-lab", icon: Activity, label: "AI Builder" },
  { to: "/visual-builder", icon: LayoutDashboard, label: "Visual Builder" },
  { to: "/backtest", icon: Activity, label: "Backtest" },
  { to: "/execution", icon: ShieldCheck, label: "Execution" },
  { to: "/marketplace", icon: Globe2, label: "Marketplace" },
  { to: "/copy-trading", icon: Users, label: "Copy Trade" },
  { to: "/broker-sessions", icon: CreditCard, label: "Brokers" },
  { to: "/kyc", icon: FileText, label: "KYC" },
  { to: "/settings", icon: Settings, label: "Settings" },
];

export default function BottomNav() {
  const [moreOpen, setMoreOpen] = useState(false);

  const closeMore = () => setMoreOpen(false);

  return (
    <>
      {/* Mobile bottom tab bar - exactly 5 unique tabs: Home, Markets, Portfolio, Orders, More */}
      <nav className="md:hidden fixed inset-x-0 bottom-0 z-50 border-t border-slate-700/80 bg-[#0B0F17]/95 px-2 pb-[env(safe-area-inset-bottom)] shadow-[0_-8px_24px_rgba(0,0,0,0.35)] backdrop-blur-xl">
        <div className="mx-auto flex h-16 max-w-lg items-stretch justify-around">
          {items.map(({ to, label, icon: Icon }) =>
            to ? (
              <NavLink
                key={label}
                to={to}
                onClick={() => {
                  if (navigator.vibrate) navigator.vibrate(8);
                  closeMore();
                }}
                className={({ isActive }) =>
                  `relative flex min-w-0 flex-1 touch-manipulation flex-col items-center justify-center gap-1 text-[10px] font-semibold transition-colors active:scale-95 ${
                    isActive ? "text-emerald-400" : "text-slate-500 hover:text-slate-200"
                  }`
                }
              >
                {({ isActive }) => (
                  <>
                    {isActive && <span className="absolute top-0 h-0.5 w-8 rounded-full bg-emerald-400 shadow-[0_0_10px_rgba(52,211,153,0.8)]" />}
                    <Icon size={19} strokeWidth={isActive ? 2.5 : 2} />
                    <span className="truncate px-1">{label}</span>
                  </>
                )}
              </NavLink>
            ) : (
              <button
                key={label}
                type="button"
                aria-label="More navigation"
                aria-expanded={moreOpen}
                onClick={() => {
                  if (navigator.vibrate) navigator.vibrate(8);
                  setMoreOpen(!moreOpen);
                }}
                className={`relative flex min-w-0 flex-1 touch-manipulation flex-col items-center justify-center gap-1 text-[10px] font-semibold transition-colors active:scale-95 ${
                  moreOpen ? "text-emerald-400" : "text-slate-500 hover:text-slate-200"
                }`}
              >
                {moreOpen && <span className="absolute top-0 h-0.5 w-8 rounded-full bg-emerald-400 shadow-[0_0_10px_rgba(52,211,153,0.8)]" />}
                <Icon size={19} strokeWidth={moreOpen ? 2.5 : 2} />
                <span className="truncate px-1">{label}</span>
              </button>
            )
          )}
        </div>
      </nav>

      {moreOpen && (
        <div className="md:hidden fixed inset-0 z-[70]">
          <div
            onClick={closeMore}
            className="fixed inset-0 z-[65] bg-black/70 backdrop-blur-sm animate-fade-in"
            aria-hidden="true"
          />
          <aside
            role="dialog"
            aria-modal="true"
            aria-label="More navigation"
            className="absolute right-0 top-0 z-[70] flex h-full w-72 max-w-[85vw] flex-col border-l border-slate-700/80 bg-[#0B0F17] shadow-2xl animate-slide-in-right"
          >
            <div className="flex items-center justify-between gap-3 border-b border-slate-700/80 px-5 pb-4 pt-[calc(env(safe-area-inset-top)+16px)]">
              <div className="flex min-w-0 items-center gap-2.5">
                <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-gradient-to-br from-violet-500 via-brand-purple to-cyan-400 p-0.5 shadow-none">
                  <div className="flex h-full w-full items-center justify-center rounded-lg bg-[#0B0F17]">
                    <Menu size={16} className="text-white" />
                  </div>
                </div>
                <h2 className="truncate font-display text-sm font-bold text-white">More</h2>
              </div>
              <button
                onClick={closeMore}
                aria-label="Close More navigation"
                className="flex h-11 w-11 shrink-0 items-center justify-center rounded-xl border border-slate-700/60 bg-surface-800 text-slate-400 transition-colors hover:bg-surface-700 hover:text-white active:scale-95"
              >
                <X size={18} />
              </button>
            </div>

            <nav className="flex-1 space-y-1 overflow-y-auto px-3 py-4">
              {MORE_LINKS.map(({ to, icon: Icon, label }) => (
                <NavLink
                  key={to}
                  to={to}
                  onClick={closeMore}
                  className={({ isActive }) =>
                    `flex min-h-11 items-center gap-3 rounded-xl px-3.5 py-3 text-sm font-semibold transition-all ${
                      isActive ? "bg-emerald-500/15 text-emerald-300" : "text-slate-300 hover:bg-surface-800 hover:text-white"
                    }`
                  }
                >
                  <Icon size={18} />
                  <span>{label}</span>
                </NavLink>
              ))}
            </nav>
          </aside>
        </div>
      )}
    </>
  );
}
