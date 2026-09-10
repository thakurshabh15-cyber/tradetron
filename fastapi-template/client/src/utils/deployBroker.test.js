import { describe, it, expect } from "vitest";
import { deployBrokerOptions, resolveDeployTarget } from "./deployBroker";

const connectedZerodha = { id: "b1", broker_name: "ZERODHA", account_name: "Primary", status: "CONNECTED", is_token_expired: false };
const connectedAngel = { id: "b2", broker_name: "ANGEL_ONE", account_name: "AO", status: "CONNECTED", is_token_expired: false };
const expired = { id: "b3", broker_name: "UPSTOX", account_name: "UP", status: "CONNECTED", is_token_expired: true };
const disconnected = { id: "b4", broker_name: "BINANCE", account_name: "BN", status: "DISCONNECTED", is_token_expired: false };

describe("deployBrokerOptions — only live, non-expired, connected broker accounts", () => {
  it("filters to connected + non-expired accounts", () => {
    const opts = deployBrokerOptions([connectedZerodha, connectedAngel, expired, disconnected]);
    expect(opts.map((o) => o.broker_id)).toEqual(["b1", "b2"]);
    expect(opts[0].broker_name).toBe("ZERODHA");
  });

  it("returns [] when nothing is connected", () => {
    expect(deployBrokerOptions([])).toEqual([]);
    expect(deployBrokerOptions([expired, disconnected])).toEqual([]);
    expect(deployBrokerOptions(null)).toEqual([]);
    expect(deployBrokerOptions(undefined)).toEqual([]);
  });
});

describe("resolveDeployTarget", () => {
  it("PAPER always targets the simulated engine", () => {
    expect(resolveDeployTarget("PAPER", [connectedZerodha])).toEqual({
      mode: "PAPER",
      broker_id: null,
      broker_name: "Simulated",
      unavailable: false,
    });
  });

  it("LIVE targets the first connected broker account (never a hardcoded name)", () => {
    const t = resolveDeployTarget("LIVE", [connectedZerodha, connectedAngel]);
    expect(t.unavailable).toBe(false);
    expect(t.broker_name).toBe("ZERODHA");
    expect(t.broker_id).toBe("b1");
  });

  it("LIVE is unavailable (never silently falls back to Simulated/Angel One) with no connected account", () => {
    const t = resolveDeployTarget("LIVE", []);
    expect(t.unavailable).toBe(true);
    expect(t.broker_id).toBeNull();
    expect(t.broker_name).toBeNull();
    expect(resolveDeployTarget("LIVE", [expired]).broker_name).toBeNull();
  });
});