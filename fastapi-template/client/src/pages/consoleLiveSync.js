/**
 * consoleLiveSync — event-driven Agent Console reconciliation helpers.
 *
 * The tenant-scoped /ws/trades feed is the SINGLE source of "something changed"
 * notifications for the Agent Console; every console-relevant event
 * (order_executed, trade_closed, order_rejected, USER_TRADING_HALT,
 * agent_configured, agent_state_changed, agent_approval, agent_decision)
 * already lands on that channel. These helpers are pure and dependency-free:
 *
 *   - isConsoleRelevantEvent(payload) decides whether a feed message should
 *     trigger reconciliation. Events are NEVER rendered as UI state — they only
 *     schedule a refetch of the authoritative /api/agent/console snapshot, so
 *     the page always displays server truth.
 *   - createLiveRefresher({ delayMs, onRefresh }) collapses bursty event
 *     notifications into exactly one debounced snapshot refetch.
 */

/** Verified console-relevant event names (mirrors the backend broadcast sites:
 * app/engine/trading_engine.py, app/engine/agent_control.py, app/api/admin.py). */
export const CONSOLE_EVENTS = Object.freeze([
  "order_executed",
  "trade_closed",
  "order_rejected",
  "USER_TRADING_HALT",
  "agent_configured",
  "agent_state_changed",
  "agent_approval",
  "agent_decision",
]);

const KNOWN_EVENTS = new Set(CONSOLE_EVENTS);

/**
 * Should a WebSocket message trigger Agent Console reconciliation?
 *
 * - Allowlisted events are recognized and reconcile.
 * - UNKNOWN object events FAIL OPEN and reconcile too: the debounced refetch of
 *   the server snapshot is idempotent and correctness-neutral, so a future
 *   event name must never starve the console.
 * - Malformed / non-object payloads (null, strings, arrays, primitives) are
 *   safely ignored — they are never tenant events on this channel.
 */
export function isConsoleRelevantEvent(payload) {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    return false;
  }
  const { event } = payload;
  if (typeof event === "string" && KNOWN_EVENTS.has(event)) return true;
  // Fail-open: unknown/shape-changed object events still reconcile.
  return true;
}

/**
 * Trailing debounce that collapses a burst of notifications into exactly ONE
 * onRefresh call. The window is measured from the FIRST notification of a
 * burst: notifications that arrive mid-window do not extend it, so a sustained
 * stream of events can never starve the console (refresh still fires once per
 * window).
 *
 * @param {object} options
 * @param {number} options.delayMs Debounce window in ms (default 500).
 * @param {function} options.onRefresh Called at most once per burst window.
 * @returns {{ notify: function, cancel: function, flush: function }}
 */
export function createLiveRefresher({ delayMs = 500, onRefresh }) {
  if (typeof onRefresh !== "function") {
    throw new TypeError("createLiveRefresher: onRefresh must be a function");
  }

  let timer = null;
  let pending = false;

  /** Schedule a refresh; mid-window notifications simply collapse. */
  const notify = () => {
    if (timer !== null) return; // one timer per burst — no duplicate timers
    pending = true;
    timer = setTimeout(() => {
      // Reset state BEFORE invoking so a re-entrant notify() during onRefresh
      // arms a fresh window instead of being swallowed (no stale state).
      timer = null;
      pending = false;
      onRefresh();
    }, delayMs);
  };

  /** Prevent any pending refresh (unmount / disposal cleanup). */
  const cancel = () => {
    if (timer !== null) clearTimeout(timer);
    timer = null;
    pending = false;
  };

  /** Immediately execute a pending refresh and clear its timer. */
  const flush = () => {
    if (timer !== null) clearTimeout(timer);
    timer = null;
    if (pending) {
      pending = false;
      onRefresh();
    }
  };

  return { notify, cancel, flush };
}