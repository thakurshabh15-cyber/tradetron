import { createContext, useContext, useEffect, useRef, useState, useCallback } from "react";
import { getWsUrl, API_BASE } from "../config";

const MarketContext = createContext({
  quotes: {},
  getQuote: () => null,
  isConnected: false,
  tickCount: 0,
  lastUpdated: null,
});

const RECONNECT_INTERVALS = [1000, 2000, 3000, 5000, 10000];

export function MarketProvider({ children }) {
  const [quotes, setQuotes] = useState({});
  const [isConnected, setIsConnected] = useState(false);
  const [tickCount, setTickCount] = useState(0);
  const [lastUpdated, setLastUpdated] = useState(null);

  const wsRef = useRef(null);
  const retryCountRef = useRef(0);
  const reconnectTimeoutRef = useRef(null);

  // 1. Initial snapshot fetch to ensure all symbols have authentic base prices immediately
  useEffect(() => {
    let isMounted = true;
    fetch(`${API_BASE}/api/market-data`)
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        if (!isMounted || !data?.market) return;
        const initialMap = {};
        for (const item of data.market) {
          if (item && item.symbol) {
            initialMap[item.symbol] = item;
          }
        }
        setQuotes((prev) => ({ ...initialMap, ...prev }));
      })
      .catch((err) => console.debug("Initial market data fetch:", err));

    return () => {
      isMounted = false;
    };
  }, []);

  // 2. Single central WebSocket connection to /ws/market/stream for the entire user session
  const connectWebSocket = useCallback(() => {
    if (wsRef.current && (wsRef.current.readyState === WebSocket.OPEN || wsRef.current.readyState === WebSocket.CONNECTING)) {
      return;
    }

    try {
      const url = getWsUrl("/ws/market/stream");
      const ws = new WebSocket(url);
      wsRef.current = ws;

      ws.onopen = () => {
        setIsConnected(true);
        retryCountRef.current = 0;
        console.info("⚡ Central Market Data Stream connected via WebSocket");
      };

      ws.onmessage = (event) => {
        try {
          const tick = JSON.parse(event.data);
          if (tick && tick.symbol) {
            setQuotes((prev) => ({
              ...prev,
              [tick.symbol]: tick,
            }));
            setTickCount((c) => c + 1);
            setLastUpdated(Date.now());
          }
        } catch {
          // Ignore non-JSON heartbeat
        }
      };

      ws.onclose = () => {
        setIsConnected(false);
        wsRef.current = null;

        const delay = RECONNECT_INTERVALS[Math.min(retryCountRef.current, RECONNECT_INTERVALS.length - 1)];
        retryCountRef.current += 1;
        reconnectTimeoutRef.current = setTimeout(connectWebSocket, delay);
      };

      ws.onerror = () => {
        ws.close();
      };
    } catch (err) {
      console.warn("WebSocket stream error:", err);
    }
  }, []);

  useEffect(() => {
    connectWebSocket();

    return () => {
      if (reconnectTimeoutRef.current) {
        clearTimeout(reconnectTimeoutRef.current);
      }
      if (wsRef.current) {
        wsRef.current.close();
        wsRef.current = null;
      }
    };
  }, [connectWebSocket]);

  const getQuote = useCallback(
    (symbol) => {
      if (!symbol) return null;
      const clean = String(symbol).toUpperCase().trim();
      return quotes[clean] || null;
    },
    [quotes]
  );

  return (
    <MarketContext.Provider
      value={{
        quotes,
        getQuote,
        isConnected,
        tickCount,
        lastUpdated,
      }}
    >
      {children}
    </MarketContext.Provider>
  );
}

export function useMarket() {
  return useContext(MarketContext);
}
