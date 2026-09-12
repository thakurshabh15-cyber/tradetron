import { describe, it, expect } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import OpenPositionsPanel from "./OpenPositionsPanel";
import { protectionBadge } from "../utils/protection";
import { MarketProvider } from "../context/MarketContext";
import { ToastProvider } from "./Toast";

/**
 * OpenPositionsPanel protection-field rendering tests.
 *
 * Covers the exact Phase 15C honesty contract:
 *  - PROTECTED badge renders only from backend `protection_state`;
 *  - SL/TP values alone never produce a PROTECTED claim;
 *  - PROTECTION_FAILED renders as ERROR with the real `protection_error`
 *    surfaced inline;
 *  - null SL/TP renders an honest unset ("—") state with no fabricated price.
 */

function basePosition(overrides = {}) {
  return {
    id: "pos-1",
    symbol: "RELIANCE",
    side: "BUY",
    quantity: 10,
    entry_price: 100,
    current_price: 101,
    unrealized_pnl: 10,
    unrealized_pnl_pct: 10,
    mode: "LIVE",
    status: "OPEN",
    stop_loss_price: null,
    take_profit_price: null,
    protection_state: "UNPROTECTED",
    protection_error: null,
    protected_at: null,
    ...overrides,
  };
}

function renderPanel(position) {
  return renderToStaticMarkup(
    <MarketProvider>
      <ToastProvider>
        <OpenPositionsPanel positions={[position]} loading={false} error={null} />
      </ToastProvider>
    </MarketProvider>
  );
}

const standaloneProtectedClaim = (html) => /\bPROTECTED\b/.test(html);

describe("OpenPositionsPanel protection columns", () => {
  it("renders Stop Loss / Take Profit / Protection State column headers", () => {
    const html = renderPanel(basePosition());
    expect(html).toContain(">Stop Loss<");
    expect(html).toContain(">Take Profit<");
    expect(html).toContain(">Protection State<");
  });

  it("renders PROTECTED exclusively from backend protection_state", () => {
    const html = renderPanel(
      basePosition({
        protection_state: "PROTECTED",
        stop_loss_price: 95,
        take_profit_price: 110,
      })
    );
    expect(html).toContain(">PROTECTED<");
    expect(html).toMatch(/data-protection-state="PROTECTED"/);
    expect(html).toContain("₹95.00");
    expect(html).toContain("₹110.00");
  });

  it("never claims PROTECTED when SL/TP exist but backend state is UNPROTECTED", () => {
    const html = renderPanel(
      basePosition({
        protection_state: "UNPROTECTED",
        stop_loss_price: 95,
        take_profit_price: 110,
      })
    );
    expect(html).toContain(">UNPROTECTED<");
    expect(html).toMatch(/data-protection-state="UNPROTECTED"/);
    expect(html).toContain("₹95.00");
    expect(html).toContain("₹110.00");
    expect(standaloneProtectedClaim(html)).toBe(false);
  });

  it("renders ERROR for PROTECTION_FAILED and surfaces the real protection_error", () => {
    const html = renderPanel(
      basePosition({
        protection_state: "PROTECTION_FAILED",
        protection_error: "Upstox rejected stop-loss: margin insufficient",
      })
    );
    expect(html).toContain(">ERROR<");
    expect(html).toMatch(/data-protection-state="PROTECTION_FAILED"/);
    expect(html).toContain("Upstox rejected stop-loss: margin insufficient");
    expect(standaloneProtectedClaim(html)).toBe(false);
  });

  it("renders PENDING for the genuine backend PROTECTION_PENDING state", () => {
    const html = renderPanel(basePosition({ protection_state: "PROTECTION_PENDING" }));
    expect(html).toContain(">PENDING<");
    expect(html).toMatch(/data-protection-state="PROTECTION_PENDING"/);
    expect(standaloneProtectedClaim(html)).toBe(false);
  });

  it("renders PAPER (simulated) not PROTECTED when backend reports PAPER", () => {
    const html = renderPanel(
      basePosition({
        mode: "PAPER",
        protection_state: "PAPER",
        stop_loss_price: 95,
        take_profit_price: 110,
      })
    );
    expect(html).toMatch(/data-protection-state="PAPER"/);
    expect(html).toContain("Engine-simulated SL/TP");
    expect(standaloneProtectedClaim(html)).toBe(false);
  });

  it("renders an honest unset state when SL/TP are null (no fabricated price)", () => {
    const html = renderPanel(
      basePosition({ stop_loss_price: null, take_profit_price: null })
    );
    const unsetCount = (html.match(/—/g) || []).length;
    expect(unsetCount).toBeGreaterThanOrEqual(2);
    expect(html).not.toContain("₹0.00");
  });

  it("passes through any other reported backend state verbatim", () => {
    const html = renderPanel(basePosition({ protection_state: "STOP_TRIGGERED" }));
    expect(html).toContain(">STOP TRIGGERED<");
    expect(html).toMatch(/data-protection-state="STOP_TRIGGERED"/);
  });
});

describe("protectionBadge util", () => {
  it("maps the known backend states honestly", () => {
    expect(protectionBadge("PROTECTED", null).label).toBe("PROTECTED");
    expect(protectionBadge("UNPROTECTED", null).label).toBe("UNPROTECTED");
    expect(protectionBadge("PROTECTION_PENDING", null).label).toBe("PENDING");
    expect(protectionBadge("PROTECTION_FAILED", "boom").label).toBe("ERROR");
    expect(protectionBadge("PROTECTION_FAILED", "boom").errorText).toBe("boom");
    expect(protectionBadge("PAPER", null).label).toBe("PAPER");
    expect(protectionBadge("STOP_TRIGGERED", null).label).toBe("STOP TRIGGERED");
    expect(protectionBadge("TARGET_TRIGGERED", null).label).toBe("TARGET TRIGGERED");
  });

  it("never invents a frontend-only status for unknown or missing states", () => {
    expect(protectionBadge("SOME_FUTURE_BACKEND_STATE", "detail").label).toBe(
      "SOME_FUTURE_BACKEND_STATE"
    );
    expect(protectionBadge(null, null)).toBeNull();
    expect(protectionBadge(undefined, null)).toBeNull();
    expect(protectionBadge("", null)).toBeNull();
  });
});