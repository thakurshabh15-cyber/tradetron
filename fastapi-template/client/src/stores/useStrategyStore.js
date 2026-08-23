import { create } from "zustand";
import { authFetch } from "../services/apiClient";

export const useStrategyStore = create((set, get) => ({
  strategies: [],
  activeStrategy: null,
  backtestResults: null,
  isLoading: false,
  isBacktesting: false,

  setActiveStrategy: (strategy) => set({ activeStrategy: strategy }),

  fetchStrategies: async () => {
    set({ isLoading: true });
    try {
      const res = await authFetch("/api/strategies");
      if (res.ok) {
        const data = await res.json();
        set({ strategies: Array.isArray(data) ? data : [], isLoading: false });
      } else {
        set({ isLoading: false });
      }
    } catch {
      set({ isLoading: false });
    }
  },

  createStrategy: async (strategyData) => {
    try {
      const res = await authFetch("/api/strategies", {
        method: "POST",
        body: JSON.stringify(strategyData),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Failed to create strategy");
      get().fetchStrategies();
      return data;
    } catch (err) {
      throw err;
    }
  },

  toggleStrategy: async (id, isActive) => {
    try {
      const res = await authFetch(`/api/strategies/${id}/toggle`, {
        method: "POST",
        body: JSON.stringify({ is_active: isActive }),
      });
      if (res.ok) {
        set((state) => ({
          strategies: state.strategies.map((s) =>
            s.id === id ? { ...s, is_active: isActive } : s
          ),
        }));
      }
    } catch (err) {
      console.error("[StrategyStore] Error toggling strategy:", err);
    }
  },

  runBacktest: async (backtestParams) => {
    set({ isBacktesting: true });
    try {
      const res = await authFetch("/api/strategies/backtest", {
        method: "POST",
        body: JSON.stringify(backtestParams),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Backtest execution failed");
      set({ backtestResults: data, isBacktesting: false });
      return data;
    } catch (err) {
      set({ isBacktesting: false });
      throw err;
    }
  },
}));
