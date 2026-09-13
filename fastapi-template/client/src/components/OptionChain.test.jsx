import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { createRoot } from "react-dom/client";
import { act } from "react";
import OptionChain from "./OptionChain";

/**
 * OptionChain WebSocket gating regression (P2, Session 2)
 *
 * Before the fix, OptionChain opened a socket on mount (pre-expiry) and then a
 * SECOND one as soon as the REST snapshot resolved the chain expiry
 * (`?expiry=...`), i.e. 2 option-chain WebSocket connections per dashboard
 * load. The socket is now gated on the REST snapshot (`loading`), so exactly
 * one connection is opened, with the resolved expiry.
 */

const CHAIN = {
  symbol: "NIFTY50",
  expiry: "2026-09-26",
  expiry_label: "26 Sep 2026",
  days_to_expiry: 13,
  underlying_spot: 25000,
  contract_lot: 50,
  data_source: "DEMO",
  totals: { ce_oi: 100000, pe_oi: 90000, ce_iv_avg: 12.5, pe_iv_avg: 13.2 },
  rows: [
    {
      strike: 25000,
      is_atm: true,
      CE: { ltp: 100, oi: 1000, chg_oi: 10, volume: 1200, iv_pct: 12.1, delta: 0.5 },
      PE: { ltp: 90, oi: 900, chg_oi: -5, volume: 800, iv_pct: 13.1, delta: -0.52 },
    },
  ],
};

class MockWebSocket {
  static instances = [];
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;

  constructor(url) {
    this.url = url;
    this.readyState = MockWebSocket.CONNECTING;
    this.onopen = null;
    this.onmessage = null;
    this.onclose = null;
    this.onerror = null;
    MockWebSocket.instances.push(this);
  }

  close() {
    this.readyState = MockWebSocket.CLOSED;
    if (this.onclose && this.readyState === MockWebSocket.CLOSED) {
      // do not auto-fire; the component closes sockets on cleanup by itself
    }
  }
}

describe("OptionChain WebSocket gating", () => {
  let container;
  let root;
  let fetchResolve;

  beforeEach(() => {
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    MockWebSocket.instances = [];
    globalThis.WebSocket = MockWebSocket;
    globalThis.fetch = vi.fn(
      () =>
        new Promise((resolve) => {
          fetchResolve = resolve;
        })
    );
  });

  afterEach(async () => {
    if (root) {
      await act(async () => root.unmount());
    }
    root = null;
    container.remove();
    delete globalThis.WebSocket;
    delete globalThis.fetch;
    fetchResolve = null;
    MockWebSocket.instances = [];
  });

  it("opens NO socket before the first REST snapshot resolves", async () => {
    await act(async () => {
      root.render(<OptionChain />);
    });
    // Flush the mount effect; the snapshot is still pending.
    await act(async () => {
      await new Promise((r) => setTimeout(r, 0));
    });
    expect(MockWebSocket.instances.length).toBe(0);
  });

  it("opens exactly ONE socket with the resolved expiry after the snapshot", async () => {
    await act(async () => {
      root.render(<OptionChain />);
    });

    await act(async () => {
      fetchResolve({ ok: true, json: async () => CHAIN });
      await new Promise((r) => setTimeout(r, 0));
    });
    await act(async () => {
      await new Promise((r) => setTimeout(r, 0));
    });

    expect(MockWebSocket.instances.length).toBe(1);
    const ws = MockWebSocket.instances[0];
    expect(ws.url).toContain("/ws/optionchain/NIFTY50");
    expect(ws.url).toContain("expiry=2026-09-26");
    // The snapshot-loading effect must NOT re-run when expiry is resolved by the
    // snapshot (no refetch loop, no socket churn).
    expect(globalThis.fetch).toHaveBeenCalledTimes(1);
  });

  it("does not open an additional socket when the chain ticks after the snapshot", async () => {
    await act(async () => {
      root.render(<OptionChain />);
    });
    await act(async () => {
      fetchResolve({ ok: true, json: async () => CHAIN });
      await new Promise((r) => setTimeout(r, 0));
    });
    await act(async () => {
      await new Promise((r) => setTimeout(r, 0));
    });
    const afterSnapshot = MockWebSocket.instances.length;
    expect(afterSnapshot).toBe(1);

    // Simulate a live tick coming back on the open socket (no re-render churn).
    await act(async () => {
      const ws = MockWebSocket.instances[0];
      ws.onmessage?.({ data: JSON.stringify(CHAIN) });
    });
    expect(MockWebSocket.instances.length).toBe(1);
  });
});