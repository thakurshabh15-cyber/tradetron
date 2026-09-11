import { describe, it, expect, beforeAll, afterAll, vi } from "vitest";
import { clearTokens } from "./apiClient";
import { apiErrorMessage } from "../utils/apiErrors";

function jsonResponse(status, body) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/**
 * alertService is a module singleton whose constructor kicks off an initial
 * fetchAlerts() — so the global fetch stub must be installed BEFORE the module
 * is imported. Dynamic import after stubbing keeps the tests hermetic.
 */
describe("alertService.createAlert truthful failure semantics", () => {
  let alertService;
  let fetchMock;

  beforeAll(async () => {
    clearTokens();
    fetchMock = vi.fn(async (url) => {
      if (String(url).includes("/api/watchlist/alerts/list")) return jsonResponse(200, []);
      return jsonResponse(201, { id: "a1", symbol: "NIFTY", condition: "ABOVE", target_price: 100 });
    });
    vi.stubGlobal("fetch", fetchMock);
    ({ alertService } = await import("./alertService"));
  });

  afterAll(() => {
    vi.unstubAllGlobals();
    clearTokens();
  });

  it("resolves and registers the alert when the backend accepts it", async () => {
    const created = await alertService.createAlert("nifty", "above", 100);
    expect(created.id).toBe("a1");
    expect(alertService.alerts.some((a) => a.id === "a1")).toBe(true);
  });

  it("throws a human-readable Error (string detail) when the backend rejects with HTTP 400", async () => {
    fetchMock.mockImplementation(async (url) => {
      if (String(url).includes("/api/watchlist/alerts/list")) return jsonResponse(200, []);
      return jsonResponse(400, { detail: "Condition must be 'ABOVE' or 'BELOW'" });
    });
    await expect(alertService.createAlert("NIFTY", "UDNER", 100)).rejects.toThrow(
      "Condition must be 'ABOVE' or 'BELOW'"
    );
  });

  it("throws a human-readable Error (Pydantic detail array) on 422 validation failures", async () => {
    fetchMock.mockImplementation(async (url) => {
      if (String(url).includes("/api/watchlist/alerts/list")) return jsonResponse(200, []);
      return jsonResponse(422, {
        detail: [
          {
            loc: ["body", "target_price"],
            msg: "Input should be a valid number",
            type: "float_type",
          },
        ],
      });
    });
    await expect(alertService.createAlert("NIFTY", "ABOVE", "not-a-number")).rejects.toThrow(
      "Input should be a valid number"
    );
  });

  it("never leaks '[object Object]' for an unparseable error body", async () => {
    fetchMock.mockImplementation(async (url) => {
      if (String(url).includes("/api/watchlist/alerts/list")) return jsonResponse(200, []);
      return new Response("<html>CDN fallback</html>", { status: 502 });
    });
    await expect(alertService.createAlert("NIFTY", "ABOVE", 100)).rejects.toThrow(
      /Could not create alert \(HTTP 502\)/
    );
  });

  it("does not register a locally-mutated alert when the backend rejects", async () => {
    const before = alertService.alerts.length;
    fetchMock.mockImplementation(async (url) => {
      if (String(url).includes("/api/watchlist/alerts/list")) return jsonResponse(200, []);
      return jsonResponse(409, { detail: "An alert for NIFTY ABOVE 100 already exists" });
    });
    await expect(alertService.createAlert("NIFTY", "ABOVE", 100)).rejects.toThrow(
      "already exists"
    );
    expect(alertService.alerts.length).toBe(before);
  });
});

describe("apiErrorMessage shared error normalization", () => {

  it("extracts a plain string detail", () => {
    expect(apiErrorMessage({ detail: "boom" }, "fb")).toBe("boom");
  });

  it("joins Pydantic validation arrays into readable text", () => {
    expect(
      apiErrorMessage({
        detail: [
          { loc: ["body", "x"], msg: "field required", type: "missing" },
          { loc: ["body", "y"], msg: "Input should be >= 0", type: "greater_than" },
        ],
      })
    ).toBe("field required; Input should be >= 0");
  });

  it("honors {message} and {error} shapes", () => {
    expect(apiErrorMessage({ message: "nope" })).toBe("nope");
    expect(apiErrorMessage({ error: "denied" })).toBe("denied");
  });

  it("falls back instead of returning null, undefined or [object Object]", () => {
    expect(apiErrorMessage(undefined, "fb")).toBe("fb");
    expect(apiErrorMessage(null, "fb")).toBe("fb");
    expect(apiErrorMessage({ detail: { nested: true } }, "fb")).toBe("fb");
    expect(apiErrorMessage("", "fb")).toBe("fb");
  });
});