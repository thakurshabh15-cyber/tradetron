/**
 * Reports Controller
 * Isolated service layer for trading performance metrics, summary aggregations, and CSV download export.
 * Does not modify or depend on existing strategy or trading controllers.
 */

import { API_BASE } from "../config";

export const reportsController = {
  /**
   * Fetch aggregated strategy & account performance metrics
   */
  async getPerformance(params = {}) {
    const query = new URLSearchParams(params).toString();
    const url = `${API_BASE}/api/reports/performance${query ? `?${query}` : ""}`;
    const res = await fetch(url);
    if (!res.ok) throw new Error("Failed to fetch performance report");
    return res.json();
  },

  /**
   * Fetch trade velocity and volume summaries
   */
  async getTradesSummary(period = "all") {
    const res = await fetch(`${API_BASE}/api/reports/trades/summary?period=${period}`);
    if (!res.ok) throw new Error("Failed to fetch trade summary");
    return res.json();
  },

  /**
   * Trigger browser CSV file download for complete trade logs
   */
  async downloadCsvExport() {
    const url = `${API_BASE}/api/reports/export?format=csv`;
    const res = await fetch(url);
    if (!res.ok) throw new Error("Failed to download CSV export");

    const blob = await res.blob();
    const downloadUrl = window.URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = downloadUrl;
    a.download = `tradetron_trades_export_${new Date().toISOString().slice(0, 10)}.csv`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    window.URL.revokeObjectURL(downloadUrl);
  },
};
