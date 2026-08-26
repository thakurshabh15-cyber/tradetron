import { useEffect, useRef } from "react";
import { AlertTriangle, ShieldAlert, Loader2 } from "lucide-react";

/**
 * Institutional Confirmation Dialog for critical/destructive actions.
 * Replaces native window.confirm() with an on-brand, accessible modal.
 *
 * Props:
 *  - isOpen, onClose, onConfirm(loading-safe), title, message
 *  - confirmLabel, cancelLabel
 *  - variant: "danger" (deletion) | "critical" (platform-wide halt) | "primary"
 */
export default function ConfirmDialog({
  isOpen,
  onClose,
  onConfirm,
  title = "Are you sure?",
  message = "This action cannot be undone.",
  confirmLabel = "Confirm",
  cancelLabel = "Cancel",
  variant = "danger",
  loading = false,
}) {
  const confirmBtnRef = useRef(null);

  useEffect(() => {
    if (!isOpen) return undefined;
    const onKeyDown = (e) => {
      if (e.key === "Escape" && !loading) onClose();
    };
    document.addEventListener("keydown", onKeyDown);
    // Focus the confirm button for fast keyboard flows (Enter confirms).
    const t = setTimeout(() => confirmBtnRef.current?.focus(), 60);
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      clearTimeout(t);
      document.body.style.overflow = "";
    };
  }, [isOpen, onClose, loading]);

  if (!isOpen) return null;

  const styles =
    variant === "critical"
      ? {
          iconBox:
            "bg-rose-500/15 border border-rose-500/40 text-rose-400 animate-pulse",
          btn: "btn-danger flex-1",
        }
      : variant === "primary"
      ? {
          iconBox: "bg-brand-purple/15 border border-brand-purple/40 text-brand-purple",
          btn: "btn-primary flex-1",
        }
      : {
          iconBox: "bg-amber-500/10 border border-amber-500/40 text-amber-400",
          btn: "btn-secondary flex-1 !border-loss-500/40 !text-loss-400 hover:!bg-loss-500/10",
        };

  const Icon = variant === "critical" ? ShieldAlert : AlertTriangle;

  return (
    <div
      className="fixed inset-0 z-[110] flex items-center justify-center p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="confirm-dialog-title"
    >
      {/* Backdrop */}
      <button
        aria-label="Close dialog"
        onClick={onClose}
        disabled={loading}
        className="absolute inset-0 cursor-default bg-black/70 backdrop-blur-sm animate-fade-in disabled:cursor-wait"
      />

      {/* Panel */}
      <div className="relative w-full max-w-sm overflow-hidden rounded-2xl border border-slate-800 bg-surface-900 shadow-glass-lg animate-slide-up">
        {/* Top accent */}
        <div
          className={`h-0.5 w-full ${
            variant === "critical"
              ? "bg-gradient-to-r from-rose-600 via-red-500 to-rose-600"
              : variant === "primary"
              ? "bg-gradient-to-r from-brand-violet to-brand-purple"
              : "bg-gradient-to-r from-amber-500 to-orange-400"
          }`}
        />
        <div className="space-y-4 p-5">
          <div className="flex items-start gap-3.5">
            <div className={`shrink-0 rounded-xl p-2.5 ${styles.iconBox}`}>
              <Icon size={20} />
            </div>
            <div className="min-w-0 space-y-1">
              <h3 id="confirm-dialog-title" className="font-display text-sm font-bold text-white">
                {title}
              </h3>
              <p className="text-xs leading-relaxed text-slate-400">{message}</p>
            </div>
          </div>

          <div className="flex gap-2 pt-1">
            <button onClick={onClose} disabled={loading} className="btn-ghost min-h-[44px] flex-1">
              {cancelLabel}
            </button>
            <button
              ref={confirmBtnRef}
              onClick={onConfirm}
              disabled={loading}
              className={`${styles.btn} justify-center`}
            >
              {loading && <Loader2 size={14} className="animate-spin" />}
              {loading ? "Working…" : confirmLabel}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
