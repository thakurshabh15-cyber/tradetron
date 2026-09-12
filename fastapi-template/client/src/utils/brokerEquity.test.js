import { describe, it, expect } from "vitest";
import { deriveEquityDisplay, formatMoney } from "./brokerEquity";

const LIVE_EQUITY = {
  source: "BROKER",
  status: "LIVE",
  broker_account_id: "b1",
  broker_name: "ZERODHA",
  total_equity: 98000.5,
  available_cash: 75000.0,
  utilized_margin: 23000.0,
  unrealized_pnl: 3200.0,
  currency: "INR",
  captured_at: "2026-09-11T06:30:00Z",
};

describe("deriveEquityDisplay — Phase 15B broker-truth hero classification", () => {
  it("PAPER keeps the paper ledger (no broker equity masquerading)", () => {
    const view = deriveEquityDisplay({
      equity: { status: "PAPER", total_equity: 1030000, message: "Paper account — no live broker connected" },
      paperBalance: 1000000,
      upnl: 30000,
      rowsLength: 2,
    });
    expect(view.badge).toBe("PAPER");
    expect(view.headTitle).toBe("Net Worth (Paper)");
    expect(view.headValue).toBe(1030000);
    expect(view.displayUpnl).toBe(30000); // internal paper P&L
  });

  it("LIVE shows broker-truth total equity + LIVE badge and never paperBalance", () => {
    const view = deriveEquityDisplay({
      equity: LIVE_EQUITY,
      paperBalance: 1000000, // delicious lure: must NOT leak through
      upnl: -9999,
      rowsLength: 2,
    });
    expect(view.badge).toBe("LIVE");
    expect(view.headTitle).toContain("ZERODHA");
    expect(view.headValue).toBe(98000.5);
    expect(view.headSub).toContain("available cash");
    expect(view.displayUpnl).toBe(3200); // broker mark-to-market wins
  });

  it("STALE still shows broker values with a STALE badge (never a silent paper fallback)", () => {
    const stale = { ...LIVE_EQUITY, status: "STALE", captured_at: "2026-09-10T06:30:00Z" };
    const view = deriveEquityDisplay({ equity: stale, paperBalance: 1000000, upnl: 123, rowsLength: 1 });
    expect(view.badge).toBe("STALE");
    expect(view.headTitle).toContain("STALE");
    expect(view.headTone).toBe("text-amber-300");
    expect(view.headValue).toBe(98000.5);
  });

  it("UNAVAILABLE shows no fabricated number even when paperBalance exists", () => {
    const view = deriveEquityDisplay({
      equity: { status: "UNAVAILABLE", message: "No synchronized broker-truth snapshot yet — sync the account first" },
      paperBalance: 1000000,
      upnl: 5000,
      rowsLength: 0,
    });
    expect(view.badge).toBe("UNAVAILABLE");
    expect(view.headValue).toBeNull();
    expect(view.headSub).toContain("No synchronized");
  });

  it("ERROR surfaces the failure instead of stale/paper numbers", () => {
    const view = deriveEquityDisplay({
      equity: { status: "ERROR", sync_message: "Broker API failure" },
      paperBalance: 1000000,
      upnl: 0,
    });
    expect(view.badge).toBe("ERROR");
    expect(view.headValue).toBeNull();
  });

  it("null equity (summary not loaded) degrades to the historical paper view", () => {
    const view = deriveEquityDisplay({ equity: null, paperBalance: 1000000, upnl: 25000, rowsLength: 3 });
    expect(view.badge).toBeNull();
    expect(view.headTitle).toBe("Net Worth (Paper)");
    expect(view.headValue).toBe(1025000);
  });

  it("LIVE with no broker-provided total equity returns null headValue (UI shows '—')", () => {
    const noEquity = { ...LIVE_EQUITY, total_equity: null, unrealized_pnl: null };
    const view = deriveEquityDisplay({ equity: noEquity, paperBalance: 1000000, upnl: 0 });
    expect(view.badge).toBe("LIVE");
    expect(view.headValue).toBeNull();
    expect(view.headSub).toContain("broker did not report total equity");
    expect(view.headSub).toContain("available cash");
  });
});

describe("formatMoney", () => {
  it("renders '—' for null/undefined and INR formatting otherwise", () => {
    expect(formatMoney(null)).toBe("—");
    expect(formatMoney(undefined)).toBe("—");
    expect(formatMoney(1234.5)).toBe("₹1,234.5");
  });
});