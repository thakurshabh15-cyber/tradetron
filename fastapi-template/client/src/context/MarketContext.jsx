import { createContext, useContext, useEffect } from "react";
import { useMarketStore } from "../stores/useMarketStore";

const MarketContext = createContext({
  quotes: {},
  getQuote: () => null,
  isConnected: false,
  tickCount: 0,
  lastUpdated: null,
});

export function MarketProvider({ children }) {
  const quotes = useMarketStore((state) => state.quotes);
  const isConnected = useMarketStore((state) => state.isConnected);
  const tickCount = useMarketStore((state) => state.tickCount);
  const lastUpdated = useMarketStore((state) => state.lastUpdated);
  const getQuote = useMarketStore((state) => state.getQuote);
  const fetchInitialSnapshot = useMarketStore((state) => state.fetchInitialSnapshot);
  const connectWebSocket = useMarketStore((state) => state.connectWebSocket);
  const disconnectWebSocket = useMarketStore((state) => state.disconnectWebSocket);

  useEffect(() => {
    fetchInitialSnapshot();
    connectWebSocket();

    return () => {
      disconnectWebSocket();
    };
  }, [fetchInitialSnapshot, connectWebSocket, disconnectWebSocket]);

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
  const context = useContext(MarketContext);
  // If context is available, use context; otherwise direct Zustand store access
  const store = useMarketStore();
  return context && Object.keys(context.quotes || {}).length > 0 ? context : store;
}

export default MarketProvider;

