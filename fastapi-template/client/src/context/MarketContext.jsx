/* eslint-disable react-refresh/only-export-components -- Provider + useMarket hook pairing is the intended context-module pattern */
import { createContext, useContext, useEffect, useMemo } from "react";
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

  // Stable identity: consumers re-render only when real market data changes,
  // never because the provider re-created its context object.
  const value = useMemo(
    () => ({
      quotes,
      getQuote,
      isConnected,
      tickCount,
      lastUpdated,
    }),
    [quotes, getQuote, isConnected, tickCount, lastUpdated]
  );

  return <MarketContext.Provider value={value}>{children}</MarketContext.Provider>;
}

export function useMarket() {
  return useContext(MarketContext);
}

export default MarketProvider;

