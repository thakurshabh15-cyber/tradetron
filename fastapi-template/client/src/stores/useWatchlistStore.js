import { create } from "zustand";
import { authFetch } from "../services/apiClient";

export const useWatchlistStore = create((set, get) => ({
  watchlist: [],
  activeSymbol: "NIFTY50",
  alerts: [],
  searchFilter: "",
  categoryFilter: "ALL",
  isLoading: false,

  setActiveSymbol: (symbol) => set({ activeSymbol: symbol }),
  setSearchFilter: (query) => set({ searchFilter: query }),
  setCategoryFilter: (category) => set({ categoryFilter: category }),

  fetchWatchlist: async () => {
    set({ isLoading: true });
    try {
      const res = await authFetch("/api/watchlist");
      if (res.ok) {
        const data = await res.json();
        set({ watchlist: Array.isArray(data) ? data : [], isLoading: false });
      } else {
        set({ isLoading: false });
      }
    } catch {
      set({ isLoading: false });
    }
  },

  addToWatchlist: async (symbol) => {
    try {
      const res = await authFetch("/api/watchlist", {
        method: "POST",
        body: JSON.stringify({ symbol }),
      });
      if (res.ok) {
        get().fetchWatchlist();
      }
    } catch (err) {
      console.error("[WatchlistStore] Error adding symbol:", err);
    }
  },

  removeFromWatchlist: async (id) => {
    try {
      const res = await authFetch(`/api/watchlist/${id}`, {
        method: "DELETE",
      });
      if (res.ok) {
        set((state) => ({
          watchlist: state.watchlist.filter((item) => item.id !== id),
        }));
      }
    } catch (err) {
      console.error("[WatchlistStore] Error removing symbol:", err);
    }
  },

  fetchAlerts: async () => {
    try {
      const res = await authFetch("/api/watchlist/alerts");
      if (res.ok) {
        const data = await res.json();
        set({ alerts: Array.isArray(data) ? data : [] });
      }
    } catch (err) {
      console.debug("[WatchlistStore] Error loading alerts:", err);
    }
  },

  createAlert: async (alertData) => {
    try {
      const res = await authFetch("/api/watchlist/alerts", {
        method: "POST",
        body: JSON.stringify(alertData),
      });
      if (res.ok) {
        get().fetchAlerts();
      }
      return res.ok;
    } catch {
      return false;
    }
  },

  deleteAlert: async (alertId) => {
    try {
      const res = await authFetch(`/api/watchlist/alerts/${alertId}`, {
        method: "DELETE",
      });
      if (res.ok) {
        set((state) => ({
          alerts: state.alerts.filter((a) => a.id !== alertId),
        }));
      }
    } catch (err) {
      console.error("[WatchlistStore] Error deleting alert:", err);
    }
  },
}));
