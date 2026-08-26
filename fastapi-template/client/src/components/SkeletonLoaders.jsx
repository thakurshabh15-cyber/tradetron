import { AlertCircle, Inbox, RefreshCw } from "lucide-react";

export function SkeletonCard() {
  return (
    <div className="glass-card p-5 space-y-3">
      <div className="flex justify-between items-center">
        <div className="h-3 w-24 skeleton-box rounded" />
        <div className="h-6 w-6 skeleton-box rounded-lg" />
      </div>
      <div className="h-7 w-32 skeleton-box rounded-md" />
      <div className="h-3 w-20 skeleton-box rounded" />
    </div>
  );
}

export function SkeletonChart() {
  return (
    <div className="glass-card p-5 space-y-4">
      <div className="flex justify-between items-center">
        <div className="h-4 w-48 skeleton-box rounded" />
        <div className="h-8 w-36 skeleton-box rounded-xl" />
      </div>
      <div className="h-60 w-full skeleton-box rounded-2xl" />
    </div>
  );
}

export function SkeletonTable({ rows = 5 }) {
  return (
    <div className="overflow-hidden rounded-2xl border border-slate-800 bg-surface-900/80 p-4 space-y-3">
      <div className="h-4 w-40 skeleton-box rounded mb-3" />
      <div className="space-y-2">
        {Array.from({ length: rows }).map((_, i) => (
          <div key={i} className="h-10 w-full skeleton-box rounded-xl" />
        ))}
      </div>
    </div>
  );
}

export function EmptyState({
  icon: Icon = Inbox,
  title = "No Data Available",
  description = "There are no records matching your current filter criteria.",
  actionLabel,
  onAction,
}) {
  return (
    <div className="flex flex-col items-center justify-center p-8 text-center glass-panel rounded-2xl border border-slate-800/80 space-y-3 my-4">
      <div className="p-3.5 rounded-2xl bg-brand-purple/10 text-brand-purple border border-brand-purple/20 shadow-inner">
        <Icon size={24} />
      </div>
      <div className="max-w-xs space-y-1">
        <h4 className="font-display font-bold text-sm text-white">{title}</h4>
        <p className="text-xs text-slate-400">{description}</p>
      </div>
      {actionLabel && onAction && (
        <button
          onClick={onAction}
          className="mt-2 btn-secondary text-xs"
        >
          <RefreshCw size={13} />
          {actionLabel}
        </button>
      )}
    </div>
  );
}

export function ErrorState({
  title = "Failed to Load Trading Data",
  error = "An unexpected network or engine error occurred.",
  onRetry,
}) {
  return (
    <div className="flex flex-col items-center justify-center p-8 text-center glass-panel rounded-2xl border border-rose-500/30 bg-rose-950/20 space-y-3 my-4">
      <div className="p-3.5 rounded-2xl bg-rose-500/15 text-rose-400 border border-rose-500/30">
        <AlertCircle size={24} />
      </div>
      <div className="max-w-sm space-y-1">
        <h4 className="font-display font-bold text-sm text-white">{title}</h4>
        <p className="text-xs text-rose-300/80">{error}</p>
      </div>
      {onRetry && (
        <button
          onClick={onRetry}
          className="mt-2 btn-danger text-xs"
        >
          <RefreshCw size={13} />
          Retry Connection
        </button>
      )}
    </div>
  );
}
