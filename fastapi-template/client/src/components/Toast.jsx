/* eslint-disable react-refresh/only-export-components -- Provider + useToast hook pairing is the intended context-module pattern */
import { createContext, useCallback, useContext, useMemo, useRef, useState } from "react";
import { CheckCircle2, XCircle, AlertTriangle, Info, X } from "lucide-react";

/**
 * Institutional Toast Notification System.
 *
 * Usage:
 *   const toast = useToast();
 *   toast.success("Strategy deployed");
 *   toast.error("Order rejected", { description: "Insufficient margin" });
 *
 * Variants auto-dismiss (success 4s / info 4s / warning 5s / error 6.5s),
 * stack top-right below the execution banner, and are announced to screen
 * readers via aria-live.
 */

const ToastContext = createContext(null);

const VARIANT_CONFIG = {
  success: {
    icon: CheckCircle2,
    iconClass: "text-emerald-400",
    ring: "border-emerald-500/30 hover:border-emerald-500/50",
    glow: "shadow-[0_8px_30px_rgba(16,185,129,0.15)]",
    bar: "bg-gradient-to-r from-emerald-500 to-teal-400",
  },
  error: {
    icon: XCircle,
    iconClass: "text-rose-400",
    ring: "border-rose-500/30 hover:border-rose-500/50",
    glow: "shadow-[0_8px_30px_rgba(244,63,94,0.18)]",
    bar: "bg-gradient-to-r from-rose-500 to-red-400",
  },
  warning: {
    icon: AlertTriangle,
    iconClass: "text-amber-400",
    ring: "border-amber-500/30 hover:border-amber-500/50",
    glow: "shadow-[0_8px_30px_rgba(245,158,11,0.15)]",
    bar: "bg-gradient-to-r from-amber-500 to-orange-400",
  },
  info: {
    icon: Info,
    iconClass: "text-cyan-400",
    ring: "border-cyan-500/30 hover:border-cyan-500/50",
    glow: "shadow-[0_8px_30px_rgba(6,182,212,0.15)]",
    bar: "bg-gradient-to-r from-cyan-500 to-sky-400",
  },
};

const AUTO_DISMISS_MS = { success: 4000, info: 4000, warning: 5000, error: 6500 };

let _toastSeq = 0;

export function ToastProvider({ children }) {
  const [toasts, setToasts] = useState([]);
  const timersRef = useRef(new Map());

  const dismiss = useCallback((id) => {
    setToasts((current) => current.filter((t) => t.id !== id));
    const timer = timersRef.current.get(id);
    if (timer) {
      clearTimeout(timer);
      timersRef.current.delete(id);
    }
  }, []);

  const push = useCallback(
    (variant, title, options = {}) => {
      const id = ++_toastSeq;
      const duration = options.duration ?? AUTO_DISMISS_MS[variant] ?? 4500;
      const toast = { id, variant, title, description: options.description || null };
      setToasts((current) => [...current.slice(-4), toast]); // cap stack at 5
      if (duration > 0) {
        timersRef.current.set(
          id,
          setTimeout(() => dismiss(id), duration)
        );
      }
      return id;
    },
    [dismiss]
  );

  const api = useMemo(
    () => ({
      success: (title, opts) => push("success", title, opts),
      error: (title, opts) => push("error", title, opts),
      warning: (title, opts) => push("warning", title, opts),
      info: (title, opts) => push("info", title, opts),
      dismiss,
    }),
    [push, dismiss]
  );

  return (
    <ToastContext.Provider value={api}>
      {children}
      {/* Toast viewport */}
      <div
        aria-live="polite"
        role="region"
        aria-label="Notifications"
        className="pointer-events-none fixed right-3 top-[76px] z-[120] flex w-[calc(100vw-1.5rem)] max-w-sm flex-col gap-2 sm:right-5 sm:top-20"
      >
        {toasts.map(({ id, variant, title, description }) => {
          const cfg = VARIANT_CONFIG[variant] || VARIANT_CONFIG.info;
          const Icon = cfg.icon;
          return (
            <div
              key={id}
              role="status"
              className={`pointer-events-auto relative overflow-hidden rounded-xl border bg-slate-900/90 backdrop-blur-md ${cfg.ring} ${cfg.glow} shadow-glass-md animate-slide-in-right`}
            >
              <div className="flex items-start gap-2.5 p-3 pr-9">
                <Icon size={17} className={`mt-0.5 shrink-0 ${cfg.iconClass}`} />
                <div className="min-w-0">
                  <p className="text-xs font-semibold text-white">{title}</p>
                  {description && (
                    <p className="mt-0.5 text-[11px] leading-relaxed text-slate-400 break-words">
                      {description}
                    </p>
                  )}
                </div>
                <button
                  onClick={() => dismiss(id)}
                  aria-label="Dismiss notification"
                  className="absolute right-2 top-2 rounded-md p-1 text-slate-500 transition-colors hover:bg-white/5 hover:text-white"
                >
                  <X size={13} />
                </button>
              </div>
              {/* Auto-dismiss progress hairline */}
              <div className="absolute inset-x-0 bottom-0 h-0.5 bg-white/[0.04]">
                <div
                  className={`h-full ${cfg.bar}`}
                  style={{
                    animation: `toastProgress ${AUTO_DISMISS_MS[variant] ?? 4500}ms linear forwards`,
                  }}
                />
              </div>
            </div>
          );
        })}
      </div>
      <style>{`@keyframes toastProgress { from { width: 100%; } to { width: 0%; } }`}</style>
    </ToastContext.Provider>
  );
}

export function useToast() {
  const ctx = useContext(ToastContext);
  if (!ctx) {
    // Fail-safe no-op so a stray call can never crash production UI.
    return {
      success: () => {},
      error: () => {},
      warning: () => {},
      info: () => {},
      dismiss: () => {},
    };
  }
  return ctx;
}

export default ToastProvider;
