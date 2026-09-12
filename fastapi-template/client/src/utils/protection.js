/**
 * Honest mapping of the backend Phase 15C protection lifecycle state to a
 * renderable badge.
 *
 * Single source of truth is the backend contract:
 *   app/models/trading.py (PositionRecord §Phase 15C comment):
 *     LIVE:  UNPROTECTED | PROTECTION_PENDING | PROTECTED | PROTECTION_FAILED
 *            | STOP_TRIGGERED | TARGET_TRIGGERED
 *     PAPER: PAPER (engine-simulated SL/TP) | UNPROTECTED
 *
 * Rules enforced by the UI through this mapping:
 *  - PROTECTED renders ONLY when the backend reports PROTECTED.  The presence
 *    of stop_loss_price / take_profit_price NEVER implies protection.
 *  - PROTECTION_FAILED renders as the user-facing ERROR badge and the real
 *    protection_error string is surfaced inline (never hover-only).
 *  - PROTECTION_PENDING renders as PENDING (an actual backend state).
 *  - Any other non-empty backend value passes through verbatim — the UI never
 *    invents or renames backend truth into a fake frontend-only state.
 *
 * Returns { label, cls, title?, errorText? } or null when the backend reported
 * no state (rendered as an honest "—" unset marker).
 */

export const PROTECTION_TONE = {
  emerald: "bg-emerald-500/20 text-emerald-300 border-emerald-500/40",
  slate: "bg-slate-500/15 text-slate-300 border-slate-500/30",
  amber: "bg-amber-500/15 text-amber-300 border-amber-500/40",
  rose: "bg-rose-500/20 text-rose-300 border-rose-500/40",
  cyan: "bg-cyan-500/15 text-cyan-300 border-cyan-500/40",
};

export function protectionBadge(state, protectionError) {
  const s = (state || "").toUpperCase();

  if (s === "PROTECTED") {
    return {
      label: "PROTECTED",
      cls: PROTECTION_TONE.emerald,
      title: "Protective stop-loss + take-profit placed with the broker",
    };
  }

  // Backend PROTECTION_FAILED surfaces as ERROR and the honest protection_error
  // string is rendered inline so the failure is visible without hover.
  if (s === "PROTECTION_FAILED") {
    return {
      label: "ERROR",
      cls: PROTECTION_TONE.rose,
      title: "Protective order placement failed",
      errorText: protectionError || "",
    };
  }

  // PROTECTION_PENDING is a genuine backend state (committed before dispatch).
  if (s === "PROTECTION_PENDING") {
    return {
      label: "PENDING",
      cls: PROTECTION_TONE.amber,
      title: "Protective orders being armed",
    };
  }

  if (s === "STOP_TRIGGERED") {
    return {
      label: "STOP TRIGGERED",
      cls: PROTECTION_TONE.rose,
      title: protectionError || "Broker-reported stop-loss leg triggered",
    };
  }

  if (s === "TARGET_TRIGGERED") {
    return {
      label: "TARGET TRIGGERED",
      cls: PROTECTION_TONE.amber,
      title: protectionError || "Broker-reported take-profit leg triggered",
    };
  }

  // PAPER: engine-simulated SL/TP — never a broker protection claim.
  if (s === "PAPER") {
    return {
      label: "PAPER",
      cls: PROTECTION_TONE.cyan,
      title: "Engine-simulated SL/TP in paper mode",
    };
  }

  if (s === "UNPROTECTED") {
    return {
      label: "UNPROTECTED",
      cls: PROTECTION_TONE.slate,
      title: "No protective orders active",
    };
  }

  // Unknown-but-reported backend value: pass through verbatim, never renamed.
  if (s) {
    return {
      label: s,
      cls: PROTECTION_TONE.slate,
      title: protectionError || "",
    };
  }

  // No backend state reported — the UI shows an honest "—" unset marker.
  return null;
}

export default protectionBadge;