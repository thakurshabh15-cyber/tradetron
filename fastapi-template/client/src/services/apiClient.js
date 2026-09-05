/**
 * Production API Client with Automatic Token Refresh & Session Persistence.
 * 
 * - Injects Authorization: Bearer <access_token> on authenticated requests
 * - Intercepts 401 errors, rotates tokens via POST /api/auth/refresh, and retries request
 * - Invalidates refresh token server-side via POST /api/auth/logout on logout
 */

import { API_BASE } from "../config";
export { API_BASE };

// ── P2-9: resilience helpers ─────────────────────────────────────────────
// Hard timeout for every request so a hung backend can never freeze the UI.
const DEFAULT_TIMEOUT_MS = 15_000;
// Bounded retries for idempotent GETs only (never re-issue non-GET requests).
const MAX_RETRIES = 2;

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function shouldRetry(status) {
  return status === 408 || status === 429 || status >= 500;
}

/**
 * fetch with a hard timeout plus bounded exponential-backoff retries.
 *
 * Retries happen ONLY for idempotent GET requests and only on network
 * failures, timeouts, or transient server statuses (408/429/5xx); Jitter is
 * added so concurrent refreshes don't stampede the backend.
 */
async function fetchWithResilience(url, options = {}) {
  const method = (options.method || "GET").toUpperCase();
  const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const maxRetries = method === "GET" ? (options.maxRetries ?? MAX_RETRIES) : 0;

  for (let attempt = 0; attempt <= maxRetries; attempt++) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    let res;
    try {
      res = await fetch(url, { ...options, signal: controller.signal });
    } catch (err) {
      clearTimeout(timer);
      if (err.name === "AbortError") throw err; // timeout is final
      if (attempt < maxRetries) {
        await sleep(250 * 2 ** attempt + Math.random() * 150);
        continue;
      }
      throw err;
    }
    clearTimeout(timer);
    if (shouldRetry(res.status) && attempt < maxRetries) {
      await sleep(250 * 2 ** attempt + Math.random() * 150);
      continue;
    }
    return res;
  }
  throw new Error("unreachable");
}

let isRefreshing = false;
let refreshSubscribers = [];

function onRefreshed(newToken) {
  refreshSubscribers.forEach((cb) => cb(newToken));
  refreshSubscribers = [];
}

function addRefreshSubscriber(cb) {
  refreshSubscribers.push(cb);
}

export function getStoredUser() {
  try {
    const raw = localStorage.getItem("tradetron_user");
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

export function getAccessToken() {
  return localStorage.getItem("tradetron_access_token") || "";
}

export function getRefreshToken() {
  return localStorage.getItem("tradetron_refresh_token") || "";
}

export function setTokens(data) {
  if (data.access_token) {
    localStorage.setItem("tradetron_access_token", data.access_token);
  }
  if (data.refresh_token) {
    localStorage.setItem("tradetron_refresh_token", data.refresh_token);
  }
  if (data.user) {
    localStorage.setItem("tradetron_user", JSON.stringify(data.user));
  }
  window.dispatchEvent(new CustomEvent("tradetron_auth_change", { detail: data.user }));
}

export function clearTokens() {
  localStorage.removeItem("tradetron_access_token");
  localStorage.removeItem("tradetron_refresh_token");
  localStorage.removeItem("tradetron_user");
  window.dispatchEvent(new CustomEvent("tradetron_auth_change", { detail: null }));
}

/**
 * Server-side logout invalidating refresh token in database
 */
export async function logoutUser() {
  const refreshToken = getRefreshToken();
  const accessToken = getAccessToken();
  try {
    await fetchWithResilience(`${API_BASE}/api/auth/logout`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}),
      },
      body: JSON.stringify({ refresh_token: refreshToken || null }),
    });
  } catch (err) {
    console.warn("[Auth] Logout API call failed, clearing local state:", err);
  } finally {
    clearTokens();
  }
}

/**
 * Perform token refresh using stored refresh token
 */
export async function refreshAccessToken() {
  const refreshToken = getRefreshToken();
  if (!refreshToken) {
    clearTokens();
    return null;
  }

  try {
    const res = await fetchWithResilience(`${API_BASE}/api/auth/refresh`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: refreshToken }),
    });

    if (!res.ok) {
      // Token is expired, invalid, or revoked in DB
      clearTokens();
      return null;
    }

    const data = await res.json();
    setTokens(data);
    return data.access_token;
  } catch (err) {
    console.error("[Auth] Token refresh error:", err);
    clearTokens();
    return null;
  }
}

/**
 * Public (unauthenticated) fetch for endpoints accessible to guests.
 * Uses no Authorization header and does NOT attempt token refresh on 401.
 * A 401/403 resolves to `null` so callers can fall back to skeleton UI
 * without getting stuck in an infinite loading state.
 */
export async function publicFetch(url, options = {}) {
  const fullUrl = url.startsWith("http") ? url : `${API_BASE}${url}`;
  const headers = { ...options.headers };
  if (!headers["Content-Type"] && !(options.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
  }
  const res = await fetchWithResilience(fullUrl, { ...options, headers });
  if (res.status === 401 || res.status === 403) return null;
  return res;
}

/**
 * Core authenticated fetch with automatic 401 interception & retry
 */
export async function authFetch(url, options = {}) {
  const fullUrl = url.startsWith("http") ? url : `${API_BASE}${url}`;
  const headers = { ...options.headers };

  let token = getAccessToken();
  if (token && !headers["Authorization"]) {
    headers["Authorization"] = `Bearer ${token}`;
  }
  if (!headers["Content-Type"] && !(options.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
  }

  let res = await fetchWithResilience(fullUrl, { ...options, headers });

  // If 401 Unauthorized, try refreshing access token
  if (res.status === 401 && getRefreshToken()) {
    if (!isRefreshing) {
      isRefreshing = true;
      const newToken = await refreshAccessToken();
      isRefreshing = false;
      if (newToken) {
        onRefreshed(newToken);
      }
    }

    return new Promise((resolve) => {
      addRefreshSubscriber(async (newToken) => {
        if (!newToken) {
          resolve(res);
          return;
        }
        const retryHeaders = {
          ...headers,
          Authorization: `Bearer ${newToken}`,
        };
        const retryRes = await fetchWithResilience(fullUrl, { ...options, headers: retryHeaders });
        resolve(retryRes);
      });
    });
  }

  return res;
}

/**
 * Verify current session on app startup / page reload
 */
export async function initializeSession() {
  const token = getAccessToken();
  if (!token) {
    if (getRefreshToken()) {
      const refreshedToken = await refreshAccessToken();
      return refreshedToken ? getStoredUser() : null;
    }
    return null;
  }

  try {
    const res = await authFetch("/api/auth/me");
    if (res.ok) {
      const user = await res.json();
      localStorage.setItem("tradetron_user", JSON.stringify(user));
      return user;
    }
  } catch (err) {
    console.warn("[Auth] Session initialization failed:", err);
  }
  clearTokens();
  return null;
}
