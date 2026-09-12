import { create } from "zustand";
import { getWsUrl, API_BASE } from "../config";

const RECONNECT_INTERVALS = [1000, 2000, 3000, 5000, 10000];

let wsInstance = null;
let reconnectTimeout = null;
let retryCount = 0;
let snapshotInFlight = null;

export const useMarketStore = create((set, get) => ({
  quotes: {},
  isConnected: false,
  tickCount: 0,
  lastUpdated: null,
  snapshotLoading: false,
  snapshotError: null,
  snapshotReady: false,

  setQuotes: (quotes) => set((state) => ({ quotes: { ...state.quotes, ...quotes } })),
  
  updateQuote: (tick) => {
    if (!tick || !tick.symbol) return;
    set((state) => ({
      quotes: {
        ...state.quotes,
        [tick.symbol]: tick,
      },
      tickCount: state.tickCount + 1,
      lastUpdated: Date.now(),
    }));
  },

  getQuote: (symbol) => {
    if (!symbol) return null;
    const clean = String(symbol).toUpperCase().trim();
    return get().quotes[clean] || null;
  },

  fetchInitialSnapshot: async (force = false) => {
    // Single-owner REST snapshot: fail-closed with true loading/error state so
    // consumers never fabricate quotes while the fetch is pending or failed.
    if (snapshotInFlight) return snapshotInFlight;
    if (!force && get().snapshotReady) return Promise.resolve(get().quotes);

    const request = (async () => {
      set({ snapshotLoading: true, snapshotError: null });
      try {
        const res = await fetch(`${API_BASE}/api/market-data`);
        if (!res.ok) {
          throw new Error(`HTTP ${res.status}`);
        }
        const data = await res.json();
        if (data && data.market && Array.isArray(data.market)) {
          const initialMap = {};
          for (const item of data.market) {
            if (item && item.symbol) {
              initialMap[item.symbol] = item;
            }
          }
          set((state) => ({
            quotes: { ...initialMap, ...state.quotes },
            snapshotLoading: false,
            snapshotError: null,
            snapshotReady: true,
          }));
        }
        return data;
      } catch (err) {
        console.debug("[MarketStore] Initial market data fetch:", err);
        set({ snapshotLoading: false, snapshotError: err.message || "market data fetch failed" });
        return null;
      } finally {
        snapshotInFlight = null;
      }
    })();
    snapshotInFlight = request;
    return request;
  },

  connectWebSocket: () => {
    if (wsInstance && (wsInstance.readyState === WebSocket.OPEN || wsInstance.readyState === WebSocket.CONNECTING)) {
      return;
    }

    try {
      const url = getWsUrl("/ws/market/stream");
      const ws = new WebSocket(url);
      wsInstance = ws;

      ws.onopen = () => {
        set({ isConnected: true });
        retryCount = 0;
        console.info("⚡ [MarketStore] Central Market Data Stream connected via WebSocket");
      };

      ws.onmessage = (event) => {
        try {
          const tick = JSON.parse(event.data);
          if (tick && tick.symbol) {
            get().updateQuote(tick);
          }
        } catch {
          // Heartbeat or non-JSON message
        }
      };

      ws.onclose = () => {
        set({ isConnected: false });
        wsInstance = null;

        const delay = RECONNECT_INTERVALS[Math.min(retryCount, RECONNECT_INTERVALS.length - 1)];
        retryCount += 1;
        reconnectTimeout = setTimeout(() => {
          get().connectWebSocket();
        }, delay);
      };

      ws.onerror = () => {
        if (wsInstance) {
          wsInstance.close();
        }
      };
    } catch (err) {
      console.warn("[MarketStore] WebSocket stream error:", err);
    }
  },

  disconnectWebSocket: () => {
    if (reconnectTimeout) {
      clearTimeout(reconnectTimeout);
      reconnectTimeout = null;
    }
    if (wsInstance) {
      wsInstance.close();
      wsInstance = null;
    }
    set({ isConnected: false });
  },
}));
