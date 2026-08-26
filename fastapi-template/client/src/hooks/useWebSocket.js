import { useCallback, useEffect, useRef, useState } from "react";
import { getWsUrl } from "../config";

const RECONNECT_DELAYS = [1000, 2000, 4000, 8000, 15000];

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

  // Keep the latest handler without re-opening the socket on every render
  useEffect(() => {
    onMessageRef.current = onMessage;
  }, [onMessage]);
  const wsOutRef = useRef(null);

  useEffect(() => {
    if (!enabled) return undefined;

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
        ws = new WebSocket(getWsUrl(path));
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

      ws.onclose = () => {
        setIsConnected(false);
        if (wsOutRef.current === ws) wsOutRef.current = null;
        ws = null;
        if (!cancelled) scheduleReconnect();
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
  }, [path, enabled]);

  const send = useCallback((data) => {
    const socket = wsOutRef.current;
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(typeof data === "string" ? data : JSON.stringify(data));
    }
  }, []);

  return { lastMessage, isConnected, send };
}
