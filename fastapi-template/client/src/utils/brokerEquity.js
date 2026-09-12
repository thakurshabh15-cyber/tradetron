/**
 * Phase 15B — broker-truth portfolio display helper (pure, unit-testable).
 *
 * Classifies a dashboard-summary `equity` block (the persisted, freshness-
 * labelled broker_state snapshot) into an honest display model:
 *
 *   LIVE        → broker-truth net equity / available cash, LIVE badge.
 *   STALE       → broker values still shown but amber STALE badge + capture.
 *   PAPER       → paper ledger (cash + open P&L), labelled "(Paper)".
 *   ERROR       → no fabricated/substituted number, error message surfaced.
 *   UNAVAILABLE → no fabricated/substituted number, "no snapshot yet" surfaced.
 *
 * Rules: a broker-truth state NEVER silently falls back to paper values, and
 * broker fields the snapshot does not provide render as `null` (UI shows "—").
 */

const inr = (v) => `₹${Number(v || 0).toLocaleString("en-IN", { maximumFractionDigits: 2 })}`;

export function deriveEquityDisplay({ equity, paperBalance, upnl, rowsLength = 0 }) {
  const eq = equity || null;
  const eqStatus = eq?.status || null;
  const isBrokerTruth = eqStatus === "LIVE" || eqStatus === "STALE";
  const capturedTime = eq?.captured_at ? new Date(eq.captured_at).toLocaleString("en-IN") : null;
  const formatMoney = (v) => (v == null ? null : Number(v));
  const brokerUpnl = Number(eq?.unrealized_pnl);
  const displayUpnl = isBrokerTruth && Number.isFinite(brokerUpnl) ? brokerUpnl : upnl;

  let badge = null;
  let headTitle = "Net Worth (Paper)";
  let headValue = formatMoney(paperBalance + upnl);
  let headSub = `cash ${inr(paperBalance)} + open P&L ${inr(upnl)}`;
  let headTone = "text-white";
  let openPnlSub = `across ${rowsLength} positions`;

  if (isBrokerTruth) {
    headTitle = `Net Worth (Live${eqStatus === "STALE" ? " · STALE" : ""}${eq?.broker_name ? ` · ${eq.broker_name}` : ""})`;
    headValue = eq.total_equity != null ? formatMoney(eq.total_equity) : null;
    headTone = eqStatus === "STALE" ? "text-amber-300" : "text-white";
    if (headValue != null) {
      headSub = eq.available_cash != null
        ? `available cash ${inr(eq.available_cash)} · captured ${capturedTime || "unknown"}`
        : `broker reported ${capturedTime || "unknown"}`;
    } else {
      headSub = `broker did not report total equity · available cash ${eq.available_cash != null ? inr(eq.available_cash) : "—"} · captured ${capturedTime || "unknown"}`;
    }
    badge = eqStatus === "LIVE" ? "LIVE" : "STALE";
    openPnlSub = `across ${rowsLength} positions${Number.isFinite(brokerUpnl) ? " · broker mark-to-market" : " · internal mark-to-market"}`;
  } else if (eqStatus === "PAPER") {
    headValue = eq.total_equity != null ? formatMoney(eq.total_equity) : formatMoney(paperBalance + upnl);
    headSub = eq.message || `cash ${inr(paperBalance)} + open P&L ${inr(upnl)}`;
    badge = "PAPER";
  } else if (eqStatus === "ERROR" || eqStatus === "UNAVAILABLE") {
    headTitle = "Account Equity";
    headValue = null;
    headTone = "text-slate-500";
    headSub = eq?.message || (
      eqStatus === "ERROR"
        ? "Last broker-state sync failed — broker truth unavailable."
        : "No synchronized broker snapshot yet — sync the broker account first."
    );
    badge = eqStatus === "ERROR" ? "ERROR" : "UNAVAILABLE";
  }

  return {
    headTitle,
    headValue,
    headSub,
    headTone,
    badge,
    displayUpnl,
    openPnlSub,
    isBrokerTruth,
    brokerUtilizedMargin: isBrokerTruth && eq?.utilized_margin != null ? Number(eq.utilized_margin) : null,
    brokerName: eq?.broker_name || null,
  };
}

export const formatMoney = (v) => (v == null ? "—" : inr(v));