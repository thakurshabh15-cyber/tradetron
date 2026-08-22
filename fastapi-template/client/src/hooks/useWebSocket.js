import { useCallback, useEffect, useRef, useState } from "react";
import { getWsUrl } from "../config";

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
  const wsRef = useRef(null);
  const retriesRef = useRef(0);
  const enabledRef = useRef(enabled);
  enabledRef.current = enabled;

  const onMessageRef = useRef(onMessage);
  onMessageRef.current = onMessage;

  const connect = useCallback(() => {
    if (!enabledRef.current) return;

    const url = getWsUrl(path);
    const ws = new WebSocket(url);
    wsRef.current = ws;

    ws.onopen = () => {
      setIsConnected(true);
      retriesRef.current = 0;
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
      wsRef.current = null;

      if (!enabledRef.current) return;

      // Reconnect with exponential backoff
      const delay =
        RECONNECT_DELAYS[
          Math.min(retriesRef.current, RECONNECT_DELAYS.length - 1)
        ];
      retriesRef.current += 1;
      setTimeout(connect, delay);
    };

    ws.onerror = () => {
      ws.close();
    };
  }, [path]);

  useEffect(() => {
    if (enabled) {
      connect();
    }

    return () => {
      enabledRef.current = false;
      wsRef.current?.close();
    };
  }, [connect, enabled]);

  const send = useCallback((data) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(typeof data === "string" ? data : JSON.stringify(data));
    }
  }, []);

  return { lastMessage, isConnected, send };
}
