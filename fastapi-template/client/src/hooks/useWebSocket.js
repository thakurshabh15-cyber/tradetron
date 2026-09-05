import { useCallback, useEffect, useRef, useState } from "react";
import { getWsUrl } from "../config";
import { useAuthStore } from "../stores/useAuthStore";

const RECONNECT_DELAYS = [1000, 2000, 4000, 8000, 15000];

// Private WebSocket endpoints require an access token (?token=<jwt>).
// Public market/option-chain feeds stay unauthenticated.
const PRIVATE_WS_PATHS = ["/ws/trades", "/ws/events"];

// RFC 6455 application close codes emitted by the server — do not
// reconnect-loop on these (token will not improve until re-login):
//   4001 — missing/invalid authentication
//   4003 — token rejected (malformed/expired/wrong type/inactive user)
//   4408 — per-user connection cap exceeded (terminal until an older
//          socket is closed — reconnecting immediately cannot succeed)
const NO_RECONNECT_CODES = new Set([4001, 4003, 4408]);

/** Determine whether a WS path is a private (authenticated) endpoint. */
function isPrivateWsPath(path) {
  return PRIVATE_WS_PATHS.includes(path);
}

/**
 * Auto-reconnecting WebSocket hook.
 *
 * @param {string} path - WebSocket path (e.g., "/ws/market/AAPL" or "/ws/trades")
 * @param {object} options
 * @param {boolean} options.enabled - Whether to connect (default: true)
 * @param {function} options.onMessage - Callback for each parsed JSON message
 * @returns {{ lastMessage, isConnected, send }}
 */
export function useWebSocket(path, { enabled = true, onMessage } = {}) {
  const [lastMessage, setLastMessage] = useState(null);
  const [isConnected, setIsConnected] = useState(false);
  const onMessageRef = useRef(onMessage);
  // Reactive auth state: private feeds connect only while a token is present.
  const accessToken = useAuthStore((s) => s.accessToken);
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);

  const privatePath = isPrivateWsPath(path);

  // Keep the latest handler without re-opening the socket on every render
  useEffect(() => {
    onMessageRef.current = onMessage;
  }, [onMessage]);
  const wsOutRef = useRef(null);

  useEffect(() => {
    if (!enabled) return undefined;

    // Private feeds must never attempt an unauthenticated connection —
    // the server rejects it. Skip until the user is authenticated.
    if (privatePath && !isAuthenticated) {
      setIsConnected(false);
      return undefined;
    }

    let ws = null;
    let cancelled = false;
    let retries = 0;
    let timer = null;

    const scheduleReconnect = () => {
      const delay = RECONNECT_DELAYS[Math.min(retries, RECONNECT_DELAYS.length - 1)];
      retries += 1;
      timer = setTimeout(openSocket, delay);
    };

    const openSocket = () => {
      if (cancelled) return;
      try {
        const token = privatePath ? accessToken || null : null;
        ws = new WebSocket(getWsUrl(path, token));
      } catch {
        scheduleReconnect();
        return;
      }
      wsOutRef.current = ws;

      ws.onopen = () => {
        setIsConnected(true);
        retries = 0;
      };

      ws.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);
          setLastMessage(data);
          onMessageRef.current?.(data);
        } catch {
          // Non-JSON message — ignore
        }
      };

      ws.onclose = (event) => {
        setIsConnected(false);
        if (wsOutRef.current === ws) wsOutRef.current = null;
        ws = null;
        // Auth rejections (4001/4003) are terminal until re-login.
        if (!cancelled && !NO_RECONNECT_CODES.has(event?.code)) scheduleReconnect();
      };

      ws.onerror = () => {
        ws?.close();
      };
    };

    openSocket();

    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
      if (wsOutRef.current === ws) wsOutRef.current = null;
      ws?.close();
      ws = null;
      setIsConnected(false);
    };
  }, [path, enabled, privatePath, isAuthenticated, accessToken]);

  const send = useCallback((data) => {
    const socket = wsOutRef.current;
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(typeof data === "string" ? data : JSON.stringify(data));
    }
  }, []);

  return { lastMessage, isConnected, send };
}
