/* eslint-disable react-refresh/only-export-components -- Provider + useMarket hook pairing is the intended context-module pattern */
import { createContext, useContext, useEffect, useMemo } from "react";
import { useMarketStore } from "../stores/useMarketStore";

const MarketContext = createContext({
  quotes: {},
  getQuote: () => null,
  isConnected: false,
  tickCount: 0,
  lastUpdated: null,
  snapshotLoading: false,
  snapshotError: null,
  fetchInitialSnapshot: () => Promise.resolve(),
});

export function MarketProvider({ children }) {
  const quotes = useMarketStore((state) => state.quotes);
  const isConnected = useMarketStore((state) => state.isConnected);
  const tickCount = useMarketStore((state) => state.tickCount);
  const lastUpdated = useMarketStore((state) => state.lastUpdated);
  const snapshotLoading = useMarketStore((state) => state.snapshotLoading);
  const snapshotError = useMarketStore((state) => state.snapshotError);
  const getQuote = useMarketStore((state) => state.getQuote);
  const fetchInitialSnapshot = useMarketStore((state) => state.fetchInitialSnapshot);
  const connectWebSocket = useMarketStore((state) => state.connectWebSocket);
  const disconnectWebSocket = useMarketStore((state) => state.disconnectWebSocket);

  // Single REST snapshot per app session (deduped + in-flight guarded in the
  // store). WebSocket ticks then keep the shared quote map live for every page.
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
      snapshotLoading,
      snapshotError,
      fetchInitialSnapshot,
    }),
    [quotes, getQuote, isConnected, tickCount, lastUpdated, snapshotLoading, snapshotError, fetchInitialSnapshot]
  );

  return <MarketContext.Provider value={value}>{children}</MarketContext.Provider>;
}

export function useMarket() {
  return useContext(MarketContext);
}

export default MarketProvider;

