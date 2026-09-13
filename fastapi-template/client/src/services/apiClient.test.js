import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import {
  authFetch,
  setTokens,
  clearTokens,
  __resetApiClientCoalescing,
} from "./apiClient";

function jsonResponse(status, body) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("authFetch 401 single-flight token refresh", () => {
  beforeEach(() => {
    clearTokens();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    clearTokens();
  });

  it("resolves a leader and a concurrent 401 request with the fresh token (no hang)", async () => {
    let refreshCount = 0;

    vi.stubGlobal(
      "fetch",
      vi.fn(async (url, opts = {}) => {
        if (String(url).includes("/api/auth/refresh")) {
          refreshCount += 1;
          setTokens({ access_token: "NEW_ACCESS", refresh_token: "NEW_REFRESH" });
          return jsonResponse(200, { access_token: "NEW_ACCESS", refresh_token: "NEW_REFRESH" });
        }
        const auth = opts?.headers?.Authorization || "";
        if (!auth.includes("NEW_ACCESS")) return jsonResponse(401, { detail: "expired" });
        return jsonResponse(200, { ok: true });
      })
    );

    setTokens({ access_token: "OLD_ACCESS", refresh_token: "OLD_REFRESH", user: { id: "u1" } });

    // Two requests in-flight at the same time, both carrying the expired token.
    const [a, b] = await Promise.all([
      authFetch("/api/foo"),
      authFetch("/api/bar"),
    ]);

    expect(a.status).toBe(200);
    expect(b.status).toBe(200);
    // Single-flight: exactly one refresh request for the 401 burst.
    expect(refreshCount).toBe(1);
  });

  it("resolves with the original 401 (no hang) when the refresh itself fails", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url) => {
        if (String(url).includes("/api/auth/refresh")) {
          return jsonResponse(401, { detail: "refresh token revoked" });
        }
        return jsonResponse(401, { detail: "unauthorized" });
      })
    );

    setTokens({ access_token: "OLD", refresh_token: "DEAD" });

    const res = await Promise.race([
      authFetch("/api/foo"),
      new Promise((r) => setTimeout(() => r("HANG"), 800)),
    ]);

    expect(res).not.toBe("HANG");
    expect(res.status).toBe(401);
  });

  it("attaches the current bearer token on the first attempt", async () => {
    const seen = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url, opts = {}) => {
        seen.push(opts?.headers?.Authorization || null);
        if (String(url).includes("/api/auth/refresh")) {
          setTokens({ access_token: "NEW_ACCESS", refresh_token: "NEW_REFRESH" });
          return jsonResponse(200, { access_token: "NEW_ACCESS", refresh_token: "NEW_REFRESH" });
        }
        const auth = opts?.headers?.Authorization || "";
        if (!auth.includes("NEW_ACCESS")) return jsonResponse(401, { detail: "expired" });
        return jsonResponse(200, { ok: true });
      })
    );

    setTokens({ access_token: "TOKEN_A", refresh_token: "RF" });
    const res = await authFetch("/api/foo");
    expect(res.status).toBe(200);
    // First attempt used the original token; retry used the fresh token.
    expect(seen).toContain("Bearer TOKEN_A");
    expect(seen).toContain("Bearer NEW_ACCESS");
  });
});

describe("apiClient P4 GET dedup + reference cache", () => {
  beforeEach(() => {
    clearTokens();
    __resetApiClientCoalescing();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    clearTokens();
    __resetApiClientCoalescing();
  });

  it("coalesces concurrent identical authenticated GETs into one network call", async () => {
    let network = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        network += 1;
        return jsonResponse(200, { n: network });
      })
    );
    setTokens({ access_token: "T1", refresh_token: "R1", user: { id: "u1" } });

    const [a, b] = await Promise.all([
      authFetch("/api/trades/positions"),
      authFetch("/api/trades/positions"),
    ]);
    expect(network).toBe(1);
    // Each caller reads its own Response clone exactly once.
    expect(await a.json()).toEqual({ n: 1 });
    expect(await b.json()).toEqual({ n: 1 });
  });

  it("never shares dedup across different Authorization tokens", async () => {
    let network = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        network += 1;
        return jsonResponse(200, { n: network });
      })
    );

    setTokens({ access_token: "T1", refresh_token: "R1", user: { id: "u1" } });
    const p1 = authFetch("/api/me");
    setTokens({ access_token: "T2", refresh_token: "R2", user: { id: "u2" } });
    const p2 = authFetch("/api/me");
    await Promise.all([p1, p2]);

    expect(network).toBe(2);
  });

  it("serves opt-in cached reference GETs within the TTL without a second fetch", async () => {
    let network = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        network += 1;
        return jsonResponse(200, { v: network });
      })
    );
    setTokens({ access_token: "T1", refresh_token: "R1", user: { id: "u1" } });

    const a = await authFetch("/api/billing/plans", { cacheTtlMs: 60_000 });
    const b = await authFetch("/api/billing/plans", { cacheTtlMs: 60_000 });
    expect(network).toBe(1);
    expect(await a.json()).toEqual({ v: 1 });
    expect(await b.json()).toEqual({ v: 1 });
  });

  it("re-fetches reference data after the TTL elapses", async () => {
    let network = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        network += 1;
        return jsonResponse(200, { v: network });
      })
    );
    setTokens({ access_token: "T1", refresh_token: "R1", user: { id: "u1" } });

    const a = await authFetch("/api/billing/plans", { cacheTtlMs: 20 });
    await new Promise((resolve) => setTimeout(resolve, 60));
    const b = await authFetch("/api/billing/plans", { cacheTtlMs: 20 });
    expect(network).toBe(2);
    expect(await a.json()).toEqual({ v: 1 });
    expect(await b.json()).toEqual({ v: 2 });
  });

  it("a non-opt-in trading-truth GET always hits the network on a fresh call", async () => {
    let network = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        network += 1;
        return jsonResponse(200, { n: network });
      })
    );
    setTokens({ access_token: "T1", refresh_token: "R1", user: { id: "u1" } });

    await authFetch("/api/trades/positions"); // no cacheTtlMs
    await authFetch("/api/trades/positions"); // follow-up must re-fetch
    expect(network).toBe(2);
  });
});
