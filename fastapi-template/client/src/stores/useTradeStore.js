import { create } from "zustand";
import { authFetch } from "../services/apiClient";
import { useAuthStore } from "./useAuthStore";

export const useTradeStore = create((set, get) => ({
  positions: [],
  orders: [],
  trades: [],
  tradeStats: {
    total_trades: 0,
    winning_trades: 0,
    losing_trades: 0,
    total_pnl: 0,
    win_rate: 0,
  },
  isLoadingPositions: false,
  isLoadingTrades: false,
  isPlacingOrder: false,
  error: null,

  fetchPositions: async () => {
    set({ isLoadingPositions: true, error: null });
    try {
      const res = await authFetch("/api/trades/positions");
      if (!res.ok) throw new Error("Failed to load open positions");
      const data = await res.json();
      set({ positions: Array.isArray(data) ? data : [], isLoadingPositions: false });
    } catch (err) {
      set({ error: err.message, isLoadingPositions: false });
    }
  },

  closePosition: async (positionId) => {
    const res = await authFetch(`/api/trades/positions/${positionId}/close`, {
      method: "POST",
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Failed to close position");

    // Update state
    set((state) => ({
      positions: state.positions.filter((p) => p.id !== positionId),
    }));

    // Update paper balance if returned
    if (data.paper_balance !== undefined) {
      useAuthStore.getState().setPaperBalance(data.paper_balance);
    }

    // Re-fetch trade stats & history
    get().fetchTradeStats();
    get().fetchTrades();

    return data;
  },

  executeOrder: async ({ symbol, side, quantity, order_type = "MARKET", price = null, mode = "PAPER" }) => {
    set({ isPlacingOrder: true, error: null });
    try {
      const res = await authFetch("/api/trades/order", {
        method: "POST",
        body: JSON.stringify({
          symbol,
          side,
          quantity: Number(quantity),
          order_type,
          price: price ? Number(price) : null,
          mode,
        }),
      });

      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.detail || data.error || "Order execution failed");
      }

      // Refresh positions & trades
      get().fetchPositions();
      get().fetchTrades();
      set({ isPlacingOrder: false });

      return data;
    } catch (err) {
      set({ error: err.message, isPlacingOrder: false });
      throw err;
    }
  },

  fetchTradeStats: async () => {
    try {
      const res = await authFetch("/api/trades/stats");
      if (res.ok) {
        const data = await res.json();
        set({ tradeStats: data });
      }
    } catch (err) {
      console.debug("[TradeStore] Error loading stats:", err);
    }
  },

  fetchTrades: async (limit = 50, symbol = null) => {
    set({ isLoadingTrades: true });
    try {
      const url = symbol
        ? `/api/trades?limit=${limit}&symbol=${encodeURIComponent(symbol)}`
        : `/api/trades?limit=${limit}`;
      const res = await authFetch(url);
      if (res.ok) {
        const data = await res.json();
        set({ trades: Array.isArray(data) ? data : [], isLoadingTrades: false });
      } else {
        set({ isLoadingTrades: false });
      }
    } catch (err) {
      set({ isLoadingTrades: false });
      console.debug("[TradeStore] Error loading trades:", err);
    }
  },
}));
