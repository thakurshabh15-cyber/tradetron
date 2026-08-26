/**
 * Global Application & API Configuration
 * Supports dynamic configuration via Vite environment variables with production fallback:
 * - VITE_API_URL: Target Backend REST URL
 * - VITE_WS_URL: Target Backend WebSocket URL
 */

const rawApiUrl = import.meta.env.VITE_API_URL;
const rawWsUrl = import.meta.env.VITE_WS_URL;

const PROD_API_URL = "https://tradethrone.onrender.com";
const PROD_WS_URL = "wss://tradethrone.onrender.com";

export const API_BASE = (() => {
  if (rawApiUrl !== undefined && rawApiUrl !== "") {
    return rawApiUrl.replace(/\/$/, "");
  }
  if (typeof window !== "undefined") {
    // Local development fallback
    if (window.location.hostname === "localhost" || window.location.hostname === "127.0.0.1") {
      if (window.location.port === "5173" || window.location.port === "3000") {
        return "http://127.0.0.1:8080";
      }
    }
    // Production fallback for Vercel / Cloud deployments
    return PROD_API_URL;
  }
  return PROD_API_URL;
})();

export function getWsUrl(path = "") {
  const cleanPath = path.startsWith("/") ? path : `/${path}`;

  if (rawWsUrl !== undefined && rawWsUrl !== "") {
    const base = rawWsUrl.replace(/\/$/, "");
    return `${base}${cleanPath}`;
  }

  // Derive WS URL from API_BASE if it's an absolute URL
  if (API_BASE && API_BASE.startsWith("http")) {
    const wsBase = API_BASE.replace(/^http/, "ws").replace(/\/$/, "");
    return `${wsBase}${cleanPath}`;
  }

  if (typeof window !== "undefined") {
    if (window.location.hostname === "localhost" || window.location.hostname === "127.0.0.1") {
      return `ws://127.0.0.1:8080${cleanPath}`;
    }
    return `${PROD_WS_URL}${cleanPath}`;
  }

  return `${PROD_WS_URL}${cleanPath}`;
}

export const WS_BASE = getWsUrl("");
