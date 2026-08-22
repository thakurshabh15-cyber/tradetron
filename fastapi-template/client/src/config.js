/**
 * Global Application & API Configuration
 * Supports dynamic configuration via Vite environment variables:
 * - VITE_API_URL: Target Backend REST URL (e.g. "https://tradetron-backend.onrender.com")
 * - VITE_WS_URL: Target Backend WebSocket URL (e.g. "wss://tradetron-backend.onrender.com")
 */

const rawApiUrl = import.meta.env.VITE_API_URL;
const rawWsUrl = import.meta.env.VITE_WS_URL;

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
    // In production without VITE_API_URL, log warning to assist quick configuration
    console.warn(
      "⚠️ [Tradetron Notice] VITE_API_URL is not set. Ensure VITE_API_URL is configured in your Vercel Project Settings (e.g. https://your-backend.onrender.com) and redeployed."
    );
    return "";
  }
  return "http://127.0.0.1:8080";
})();

export function getWsUrl(path) {
  const cleanPath = path.startsWith("/") ? path : `/${path}`;

  if (rawWsUrl !== undefined && rawWsUrl !== "") {
    const base = rawWsUrl.replace(/\/$/, "");
    return `${base}${cleanPath}`;
  }

  // Derive WS URL from API_BASE if it's an absolute URL
  if (rawApiUrl && rawApiUrl.startsWith("http")) {
    const wsBase = rawApiUrl.replace(/^http/, "ws").replace(/\/$/, "");
    return `${wsBase}${cleanPath}`;
  }

  if (typeof window !== "undefined") {
    if (window.location.hostname === "localhost" || window.location.hostname === "127.0.0.1") {
      if (window.location.port === "5173" || window.location.port === "3000") {
        return `ws://127.0.0.1:8080${cleanPath}`;
      }
    }
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    return `${protocol}//${window.location.host}${cleanPath}`;
  }

  return `ws://127.0.0.1:8080${cleanPath}`;
}
