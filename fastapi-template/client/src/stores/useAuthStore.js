import { create } from "zustand";
import {
  getStoredUser,
  getAccessToken,
  getRefreshToken,
  setTokens as persistTokens,
  clearTokens as removeTokens,
  logoutUser,
  initializeSession as initApiSession,
  refreshAccessToken as apiRefresh,
  API_BASE,
} from "../services/apiClient";

export const useAuthStore = create((set, get) => ({
  user: getStoredUser(),
  accessToken: getAccessToken(),
  refreshToken: getRefreshToken(),
  isAuthenticated: !!getAccessToken(),
  isLoading: true,
  paperBalance: getStoredUser()?.paper_balance ?? 1000000.0,

  initialize: async () => {
    set({ isLoading: true });
    try {
      const user = await initApiSession();
      if (user) {
        set({
          user,
          accessToken: getAccessToken(),
          refreshToken: getRefreshToken(),
          isAuthenticated: true,
          paperBalance: user.paper_balance ?? 1000000.0,
          isLoading: false,
        });
      } else {
        set({
          user: null,
          accessToken: "",
          refreshToken: "",
          isAuthenticated: false,
          isLoading: false,
        });
      }
    } catch {
      set({
        user: null,
        accessToken: "",
        refreshToken: "",
        isAuthenticated: false,
        isLoading: false,
      });
    }
  },

  setAuthData: (data) => {
    persistTokens(data);
    set({
      user: data.user || get().user,
      accessToken: data.access_token || get().accessToken,
      refreshToken: data.refresh_token || get().refreshToken,
      isAuthenticated: true,
      paperBalance: data.user?.paper_balance ?? get().paperBalance,
    });
  },

  updateUser: (updatedUser) => {
    const merged = { ...get().user, ...updatedUser };
    localStorage.setItem("tradetron_user", JSON.stringify(merged));
    set({
      user: merged,
      paperBalance: merged.paper_balance ?? get().paperBalance,
    });
  },

  setPaperBalance: (balance) => {
    set({ paperBalance: balance });
    const user = get().user;
    if (user) {
      const updated = { ...user, paper_balance: balance };
      localStorage.setItem("tradetron_user", JSON.stringify(updated));
      set({ user: updated });
    }
  },

  login: async (email, password) => {
    const res = await fetch(`${API_BASE}/api/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    });
    const data = await res.json();
    if (!res.ok) {
      throw new Error(data.detail || "Authentication failed");
    }
    get().setAuthData(data);
    return data;
  },

  logout: async () => {
    await logoutUser();
    removeTokens();
    set({
      user: null,
      accessToken: "",
      refreshToken: "",
      isAuthenticated: false,
      paperBalance: 1000000.0,
    });
  },

  refresh: async () => {
    const token = await apiRefresh();
    if (token) {
      set({
        accessToken: token,
        refreshToken: getRefreshToken(),
        user: getStoredUser(),
        isAuthenticated: true,
      });
    } else {
      get().logout();
    }
    return token;
  },
}));
