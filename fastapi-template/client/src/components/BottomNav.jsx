import { NavLink } from "react-router-dom";
import { CreditCard, Settings, Users, Terminal, Workflow } from "lucide-react";

const items = [
  { to: "/dashboard", label: "Terminal", icon: Terminal },
  { to: "/visual-builder", label: "Builder", icon: Workflow },
  { to: "/copy-trading", label: "Copy Trade", icon: Users },
  { to: "/pricing", label: "Pricing", icon: CreditCard },
  { to: "/settings", label: "Settings", icon: Settings },
];

export default function BottomNav() {
  return (
    <nav className="md:hidden fixed inset-x-0 bottom-0 z-50 border-t border-slate-700/80 bg-[#0B0F17]/95 px-2 pb-[env(safe-area-inset-bottom)] shadow-[0_-8px_24px_rgba(0,0,0,0.35)] backdrop-blur-xl">
      <div className="mx-auto flex h-16 max-w-lg items-stretch justify-around">
        {items.map(({ to, label, icon: Icon }) => (
          <NavLink
            key={to}
            to={to}
            onClick={() => {
              if (navigator.vibrate) navigator.vibrate(8);
            }}
            className={({ isActive }) =>
              `relative flex min-w-0 flex-1 touch-manipulation flex-col items-center justify-center gap-1 text-[10px] font-semibold transition-colors active:scale-95 ${isActive ? "text-emerald-400" : "text-slate-500 hover:text-slate-200"}`
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
        ))}
      </div>
    </nav>
  );
}