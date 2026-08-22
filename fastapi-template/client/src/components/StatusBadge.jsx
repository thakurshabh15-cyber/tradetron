export default function StatusBadge({ status, className = "" }) {
  const styles = {
    FILLED: "badge-profit",
    BUY: "badge-profit",
    SELL: "badge-loss",
    PENDING: "badge-neutral",
    CANCELLED: "badge-neutral",
    REJECTED: "badge-loss",
    active: "badge-active",
    inactive: "badge-neutral",
    Complete: "badge-profit",
    Pending: "badge-neutral text-amber-400 bg-amber-500/15",
  };

  return (
    <span className={`${styles[status] || "badge-neutral"} ${className}`}>
      {status}
    </span>
  );
}
