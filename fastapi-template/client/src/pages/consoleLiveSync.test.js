import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  CONSOLE_EVENTS,
  createLiveRefresher,
  isConsoleRelevantEvent,
} from "./consoleLiveSync";

/**
 * consoleLiveSync helpers — pure, deterministic, fake-timer tests.
 *
 * isConsoleRelevantEvent: allowlisted events reconcile; unknown OBJECT events
 * fail open (snapshot refetch is idempotent); malformed/non-object payloads are
 * ignored safely.
 *
 * createLiveRefresher: N rapid notifications ⇒ exactly one refresh after the
 * debounce window; cancel() prevents it; flush() runs it immediately and
 * clears the pending timer; bursts separated by the window refresh separately.
 */

describe("isConsoleRelevantEvent", () => {
  it("recognizes every allowlisted console-relevant event", () => {
    for (const event of CONSOLE_EVENTS) {
      expect(isConsoleRelevantEvent({ event })).toBe(true);
    }
  });

  it("recognizes realistic full event payloads from the trades channel", () => {
    expect(
      isConsoleRelevantEvent({
        event: "order_executed",
        id: "x-1",
        order_id: "o-1",
        strategy_name: "autonomous",
        symbol: "NIFTY",
        mode: "PAPER",
      }),
    ).toBe(true);
    expect(
      isConsoleRelevantEvent({ event: "agent_state_changed", ref_id: "cfg-1", status: "RUNNING" }),
    ).toBe(true);
    expect(isConsoleRelevantEvent({ event: "USER_TRADING_HALT", reason: "operator kill" })).toBe(true);
  });

  it("fails open for unknown object events (reconciliation is idempotent)", () => {
    expect(isConsoleRelevantEvent({ event: "some_future_event" })).toBe(true);
    expect(isConsoleRelevantEvent({ unexpected: "shape" })).toBe(true);
    expect(isConsoleRelevantEvent({})).toBe(true);
  });

  it("safely ignores malformed and non-object payloads without crashing", () => {
    expect(isConsoleRelevantEvent(null)).toBe(false);
    expect(isConsoleRelevantEvent(undefined)).toBe(false);
    expect(isConsoleRelevantEvent("order_executed")).toBe(false);
    expect(isConsoleRelevantEvent(42)).toBe(false);
    expect(isConsoleRelevantEvent(true)).toBe(false);
    expect(isConsoleRelevantEvent(["order_executed"])).toBe(false);
    expect(isConsoleRelevantEvent([])).toBe(false);
  });
});

describe("createLiveRefresher", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("notify() does not refresh immediately", () => {
    const onRefresh = vi.fn();
    const refresher = createLiveRefresher({ delayMs: 500, onRefresh });
    refresher.notify();
    expect(onRefresh).not.toHaveBeenCalled();
  });

  it("N rapid notifications produce exactly ONE refresh after the window", () => {
    const onRefresh = vi.fn();
    const refresher = createLiveRefresher({ delayMs: 500, onRefresh });
    for (let i = 0; i < 25; i++) refresher.notify();
    vi.advanceTimersByTime(499);
    expect(onRefresh).toHaveBeenCalledTimes(0);
    vi.advanceTimersByTime(1);
    expect(onRefresh).toHaveBeenCalledTimes(1);
    // Nothing pending afterwards — the burst is fully collapsed.
    vi.advanceTimersByTime(2_000);
    expect(onRefresh).toHaveBeenCalledTimes(1);
  });

  it("fires the single refresh after the debounce delay", () => {
    const onRefresh = vi.fn();
    const refresher = createLiveRefresher({ delayMs: 500, onRefresh });
    refresher.notify();
    vi.advanceTimersByTime(499);
    expect(onRefresh).toHaveBeenCalledTimes(0);
    vi.advanceTimersByTime(1);
    expect(onRefresh).toHaveBeenCalledTimes(1);
  });

  it("cancel() prevents a pending refresh", () => {
    const onRefresh = vi.fn();
    const refresher = createLiveRefresher({ delayMs: 500, onRefresh });
    refresher.notify();
    refresher.cancel();
    vi.advanceTimersByTime(2_000);
    expect(onRefresh).not.toHaveBeenCalled();
  });

  it("flush() executes a pending refresh immediately", () => {
    const onRefresh = vi.fn();
    const refresher = createLiveRefresher({ delayMs: 500, onRefresh });
    refresher.notify();
    refresher.flush();
    expect(onRefresh).toHaveBeenCalledTimes(1);
  });

  it("flush() clears the pending timer so the refresher re-arms cleanly", () => {
    const onRefresh = vi.fn();
    const refresher = createLiveRefresher({ delayMs: 500, onRefresh });
    refresher.notify();
    refresher.flush();
    expect(onRefresh).toHaveBeenCalledTimes(1);
    // The original timer must be gone (no late duplicate).
    vi.advanceTimersByTime(2_000);
    expect(onRefresh).toHaveBeenCalledTimes(1);
    // A fresh burst after flush schedules a brand-new window.
    refresher.notify();
    vi.advanceTimersByTime(500);
    expect(onRefresh).toHaveBeenCalledTimes(2);
  });

  it("flush() with nothing pending is a harmless no-op", () => {
    const onRefresh = vi.fn();
    const refresher = createLiveRefresher({ delayMs: 500, onRefresh });
    refresher.flush();
    expect(onRefresh).not.toHaveBeenCalled();
  });

  it("bursts separated by the debounce window produce separate refreshes", () => {
    const onRefresh = vi.fn();
    const refresher = createLiveRefresher({ delayMs: 500, onRefresh });
    refresher.notify();
    refresher.notify();
    vi.advanceTimersByTime(500);
    expect(onRefresh).toHaveBeenCalledTimes(1);
    refresher.notify();
    vi.advanceTimersByTime(500);
    expect(onRefresh).toHaveBeenCalledTimes(2);
  });

  it("cancel() is safe to call when idle", () => {
    const onRefresh = vi.fn();
    const refresher = createLiveRefresher({ delayMs: 500, onRefresh });
    refresher.cancel();
    vi.advanceTimersByTime(1_000);
    expect(onRefresh).not.toHaveBeenCalled();
  });

  it("honors a custom delay window", () => {
    const onRefresh = vi.fn();
    const refresher = createLiveRefresher({ delayMs: 1_000, onRefresh });
    refresher.notify();
    vi.advanceTimersByTime(999);
    expect(onRefresh).toHaveBeenCalledTimes(0);
    vi.advanceTimersByTime(1);
    expect(onRefresh).toHaveBeenCalledTimes(1);
  });

  it("requires an onRefresh callback", () => {
    expect(() => createLiveRefresher({ delayMs: 500 })).toThrow(TypeError);
  });
});