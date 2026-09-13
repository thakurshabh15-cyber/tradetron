import { describe, it, expect, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import AgentConsole from "./AgentConsole";
import { ToastProvider } from "../components/Toast";

/**
 * AgentConsole rendering tests — the page must be a FAITHFUL, deterministic
 * rendering of the `/api/agent/console` snapshot:
 *  - the autonomy gate strip shows the server's authoritative truth;
 *  - the config/lifecycle cards are driven purely by the snapshot (no
 *    fabricated states);
 *  - the pending-approval badge and Approve buttons only surface tasks the
 *    backend flagged `requires_approval`;
 *  - ceiling/autonomy and autonomous-mode warnings render exactly when the
 *    server data implies them.
 *
 * The `useApi` hook is mocked; the page is rendered with renderToStaticMarkup
 * so effects do not run — assertions therefore exercise the deterministic
 * first-paint path.
 */

const { mocks } = vi.hoisted(() => ({ mocks: { data: null, wsConnected: false } }));

vi.mock("../hooks/useApi", () => ({
  useApi: () => ({
    data: mocks.data,
    loading: false,
    error: null,
    refetch: vi.fn(),
  }),
}));

// The live-sync subscription is mocked so the SSR tests stay deterministic:
// effects never run under renderToStaticMarkup, but the header connection
// indicator derives from useWebSocket's `isConnected` and stays assertable.
vi.mock("../hooks/useWebSocket", () => ({
  useWebSocket: () => ({
    isConnected: mocks.wsConnected,
    lastMessage: null,
    send: vi.fn(),
  }),
}));

function gate(overrides = {}) {
  return {
    autonomous_mode_enabled: true,
    effective_autonomy_ceiling: 2,
    global_autonomy_level: 2,
    trading_agent_max_autonomy: 2,
    trading_agent_enabled: true,
    broker_mode: "paper",
    ...overrides,
  };
}

function baseBundle(overrides = {}) {
  return {
    gate: gate(),
    config: {
      config_id: "cfg-1",
      name: "Autonomous Agent",
      strategy_id: "strat-1",
      symbols: ["NIFTY", "BANKNIFTY"],
      execution_mode: "PAPER",
      autonomy_level: 1,
      approval_policy: {},
      risk_policy: {},
      status: "RUNNING",
      last_error: null,
    },
    activity: {
      decisions: [
        {
          decision_id: "d-1",
          created_at: "2026-09-13T08:00:00Z",
          symbol: "NIFTY",
          decision: "NEEDS_APPROVAL",
          side: "BUY",
          risk_result: "APPROVED",
          approval_result: "PENDING",
          execution_result: "NOT_ATTEMPTED",
          reason: "Momentum signal in hour slot",
        },
      ],
      tasks: [
        {
          id: "t-1",
          task_kind: "execute_trade",
          status: "PENDING",
          attempts: 1,
          max_attempts: 3,
          requires_approval: true,
          approved_at: null,
          created_at: "2026-09-13T08:00:01Z",
        },
        {
          id: "t-2",
          task_kind: "evaluate_symbol",
          status: "COMPLETED",
          attempts: 1,
          max_attempts: 3,
          requires_approval: false,
          approved_at: null,
          created_at: "2026-09-13T07:55:01Z",
        },
      ],
      intents: [
        { intent_id: "i-1", symbol: "NIFTY", side: "BUY", quantity: 15, status: "PENDING" },
      ],
      orders: [
        { order_id: "o-1", symbol: "NIFTY", side: "BUY", quantity: 15, filled_price: "22500.0", status: "FILLED" },
      ],
      positions: [
        {
          position_id: "p-1",
          symbol: "NIFTY",
          side: "BUY",
          quantity: 15,
          entry_price: "22300.0",
          status: "OPEN",
          protection_state: "PROTECTED",
        },
      ],
    },
    ...overrides,
  };
}

function renderConsole(bundle) {
  mocks.data = bundle;
  return renderToStaticMarkup(
    <ToastProvider>
      <AgentConsole />
    </ToastProvider>
  );
}

describe("AgentConsole", () => {
  it("renders the header and authoritative autonomy gate strip", () => {
    const html = renderConsole(baseBundle());
    expect(html).toContain(">Agent Console<");
    expect(html).toContain(">ENABLED<"); // autonomous mode
    expect(html).toContain("REGISTERED · ENABLED");
    expect(html).toContain(">PAPER<span"); // effective ceiling 2 → PAPER label
    expect(html).toContain("min glob 2 · agent 2");
  });

  it("renders decisions, pending-approval badge and Approve button ONLY for requires_approval tasks", () => {
    const html = renderConsole(baseBundle());
    expect(html).toContain("NEEDS_APPROVAL");
    expect(html).toContain("Momentum signal in hour slot");
    expect(html).toContain("1 awaiting approval");
    expect(html).toContain("REQUIRED");
    // The completed evaluate_symbol task (no approval) must NOT get an Approve button.
    expect((html.match(/Approve<\/button>/g) || []).length).toBe(1);
  });

  it("renders the RUNNING lifecycle truthfully: Start and Resume disabled, live copy shown", () => {
    // broker_mode "live" keeps the execution-mode select enabled so only the
    // lifecycle buttons contribute `disabled` attributes.
    const html = renderConsole(
      baseBundle({
        gate: gate({ broker_mode: "live" }),
      })
    );
    expect(html).toContain("RUNNING");
    expect(html).toContain("The evaluation loop is live");
    // Start and Resume are disabled while RUNNING; Pause and Stop remain active.
    const disabledCount = (html.match(/disabled=""/g) || []).length;
    expect(disabledCount).toBe(2);
  });

  it("warns when configured autonomy exceeds the effective ceiling", () => {
    const html = renderConsole(
      baseBundle({
        config: {
          ...baseBundle().config,
          autonomy_level: 3,
        },
      })
    );
    expect(html).toContain("Configured autonomy");
    expect(html).toContain("exceeds the effective ceiling");
    expect(html).toContain("LIVE");
  });

  it("warns when autonomous mode is disabled platform-wide", () => {
    const html = renderConsole(
      baseBundle({
        gate: gate({ autonomous_mode_enabled: false }),
      })
    );
    expect(html).toContain("Autonomous mode is disabled at the platform level");
  });

  it("shows the deterministic create-config control when no config exists", () => {
    const html = renderConsole(
      baseBundle({
        config: null,
        activity: {},
      })
    );
    expect(html).toContain(">Create agent configuration<");
    expect(html).toContain("Create agent config</button>");
    expect(html).toContain("NO CONFIG");
  });

  it("shows honest empty states instead of fabricating activity", () => {
    const html = renderConsole(
      baseBundle({
        activity: {},
      })
    );
    expect(html).toContain("No decisions yet");
    expect(html).toContain("No tasks yet");
    expect(html).toContain("No intents yet.");
    expect(html).toContain("No open positions.");
  });

  it("shows a compact honest WebSocket connection indicator", () => {
    mocks.wsConnected = false;
    const offline = renderConsole(baseBundle());
    expect(offline).toContain("RECONNECTING…");

    mocks.wsConnected = true;
    const live = renderConsole(baseBundle());
    expect(live).toContain("LIVE · WS CONNECTED");
    expect(live).not.toContain("RECONNECTING…");

    mocks.wsConnected = false;
  });
});
