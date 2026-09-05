/**
 * Global Application & API Configuration
 * Supports dynamic configuration via Vite environment variables with production fallback:
 * - VITE_API_URL: Target Backend REST URL
 * - VITE_WS_URL: Target Backend WebSocket URL
 */

const rawApiUrl = import.meta.env.VITE_API_URL;
const rawApiBaseUrl = import.meta.env.VITE_API_BASE_URL;
const rawWsUrl = import.meta.env.VITE_WS_URL;

// ── Phase 5D URL TRUTH (verified live 2026-09-04) ────────────────────────────
// The legacy `tradethrone.onrender.com` host is DEAD (Render returns HTTP 404
// `x-render-routing: no-server` on every path). The actually-running backend
// is `tradetron-8jkz.onrender.com` (verified: /api/health, /healthz, /readyz,
// /api/market-data all HTTP 200). The production frontend MUST target the live
// host or it can never reach the backend. Keep these in sync with the VITE_*
// overrides (see config resolution below).
const PROD_API_URL = "https://tradetron-8jkz.onrender.com";
const PROD_WS_URL = "wss://tradetron-8jkz.onrender.com";

// Prefer explicit URL, then VITE_API_BASE_URL alias, then local/prod fallback.
const resolveApiBase = () => {
  const envUrl = (rawApiUrl !== undefined && rawApiUrl !== "" ? rawApiUrl : null)
    || (rawApiBaseUrl !== undefined && rawApiBaseUrl !== "" ? rawApiBaseUrl : null);
  if (envUrl) return envUrl.replace(/\/$/, "");
  if (typeof window !== "undefined") {
    if (window.location.hostname === "localhost" || window.location.hostname === "127.0.0.1") {
      if (window.location.port === "5173" || window.location.port === "3000") {
        return "http://127.0.0.1:8080";
      }
    }
  }
  return PROD_API_URL;
};

export const API_BASE = resolveApiBase();

export function getWsUrl(path = "", token = null) {
  const cleanPath = path.startsWith("/") ? path : `/${path}`;
  // Access tokens are appended ONLY for private WS endpoints by the caller
  // (useWebSocket). Public market feeds never receive a token.
  const query = token ? `?token=${encodeURIComponent(token)}` : "";

  if (rawWsUrl !== undefined && rawWsUrl !== "") {
    const base = rawWsUrl.replace(/\/$/, "");
    return `${base}${cleanPath}${query}`;
  }

  // Derive WS URL from API_BASE if it's an absolute URL
  if (API_BASE && API_BASE.startsWith("http")) {
    const wsBase = API_BASE.replace(/^http/, "ws").replace(/\/$/, "");
    return `${wsBase}${cleanPath}${query}`;
  }

  if (typeof window !== "undefined") {
    if (window.location.hostname === "localhost" || window.location.hostname === "127.0.0.1") {
      return `ws://127.0.0.1:8080${cleanPath}${query}`;
    }
    return `${PROD_WS_URL}${cleanPath}${query}`;
  }

  return `${PROD_WS_URL}${cleanPath}${query}`;
}

export const WS_BASE = getWsUrl("");
