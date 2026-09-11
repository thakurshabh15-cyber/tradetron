/**
 * Price Alert Service
 * Subscribes to the existing market-data streams without modifying the pollers/engine.
 * Evaluates ABOVE/BELOW thresholds and dispatches notifications when conditions trigger.
 */

import { authFetch } from "./apiClient";
import { readJsonResponse } from "./api";
import { apiErrorMessage } from "../utils/apiErrors";

class AlertService {
  constructor() {
    this.alerts = [];
    this.listeners = new Set();
    this.triggeredAlertIds = new Set();
    this.init();
  }

  async init() {
    await this.fetchAlerts();
  }

  async fetchAlerts() {
    try {
      const res = await authFetch("/api/watchlist/alerts/list");
      if (res.ok) {
        this.alerts = await res.json();
        this.notifyListeners();
      }
    } catch (err) {
      console.warn("[AlertService] Could not fetch alerts from backend:", err);
    }
  }

  /**
   * Process an incoming price tick for a symbol
   */
  processPriceTick(symbol, currentPrice) {
    if (!currentPrice || isNaN(currentPrice)) return;
    const sym = symbol.toUpperCase();

    for (const alert of this.alerts) {
      if (!alert.is_active || alert.is_triggered || this.triggeredAlertIds.has(alert.id)) {
        continue;
      }

      if (alert.symbol.toUpperCase() === sym) {
        let isTriggered = false;
        if (alert.condition === "ABOVE" && currentPrice >= alert.target_price) {
          isTriggered = true;
        } else if (alert.condition === "BELOW" && currentPrice <= alert.target_price) {
          isTriggered = true;
        }

        if (isTriggered) {
          this.triggeredAlertIds.add(alert.id);
          alert.is_triggered = true;
          alert.triggered_at = new Date().toISOString();

          this.dispatchTriggerNotification(alert, currentPrice);
          this.notifyListeners();
        }
      }
    }
  }

  dispatchTriggerNotification(alert, currentPrice) {
    const title = `🚨 Price Alert Triggered: ${alert.symbol}`;
    const symbol = alert.symbol.toUpperCase();
    const isINR = /NIFTY|BANKNIFTY|FINNIFTY|RELIANCE|TCS|GOLD|CRUDE|INR/.test(symbol);
    const curr = isINR ? "₹" : "$";
    const message = `${alert.symbol} crossed ${alert.condition} ${curr}${alert.target_price.toFixed(2)} (Current: ${curr}${currentPrice.toFixed(2)})`;

    console.info(`[AlertService] ${title} - ${message}`);

    // In-browser Web Notification if permitted
    if (typeof window !== "undefined" && "Notification" in window && Notification.permission === "granted") {
      try {
        new Notification(title, { body: message, icon: "/favicon.ico" });
      } catch {
        // notification fallback
      }
    }
  }

  async createAlert(symbol, condition, targetPrice) {
    const res = await authFetch("/api/watchlist/alerts", {
      method: "POST",
      body: JSON.stringify({
        symbol: symbol.toUpperCase(),
        condition: condition.toUpperCase(),
        target_price: Number(targetPrice),
      }),
    });
    if (!res.ok) {
      // Fail truthfully: surface the backend's rejection as a real Error so
      // the caller's catch block shows the failure toast — never a fabricated
      // "alert created" success when the backend refused to persist it.
      const body = await readJsonResponse(res);
      throw new Error(apiErrorMessage(body, `Could not create alert (HTTP ${res.status})`));
    }
    const newAlert = await res.json();
    this.alerts.unshift(newAlert);
    this.notifyListeners();
    return newAlert;
  }

  async deleteAlert(alertId) {
    try {
      const res = await authFetch(`/api/watchlist/alerts/${alertId}`, {
        method: "DELETE",
      });
      if (res.ok) {
        this.alerts = this.alerts.filter((a) => a.id !== alertId);
        this.triggeredAlertIds.delete(alertId);
        this.notifyListeners();
      }
    } catch (err) {
      console.error("[AlertService] Failed to delete alert:", err);
    }
  }

  async toggleAlert(alertId) {
    try {
      const res = await authFetch(`/api/watchlist/alerts/${alertId}/toggle`, {
        method: "PATCH",
      });
      if (res.ok) {
        const updated = await res.json();
        this.alerts = this.alerts.map((a) =>
          a.id === alertId ? { ...a, is_active: updated.is_active } : a
        );
        this.notifyListeners();
      }
    } catch (err) {
      console.error("[AlertService] Failed to toggle alert:", err);
    }
  }

  subscribe(listener) {
    this.listeners.add(listener);
    listener(this.alerts);
    return () => this.listeners.delete(listener);
  }

  notifyListeners() {
    for (const listener of this.listeners) {
      listener([...this.alerts]);
    }
  }
}

export const alertService = new AlertService();
