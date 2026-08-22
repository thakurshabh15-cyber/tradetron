import { useState } from "react";
import { useApi } from "../hooks/useApi";
import StatusBadge from "../components/StatusBadge";
import { History, Filter, Download } from "lucide-react";
import { reportsController } from "../services/reportsController";

export default function TradeHistory() {
  const [selectedSymbol, setSelectedSymbol] = useState("");
  const [isExporting, setIsExporting] = useState(false);
  const url = selectedSymbol
    ? `/api/trades?symbol=${selectedSymbol}&limit=100`
    : `/api/trades?limit=100`;

  const { data: trades, loading, refetch } = useApi(url);
  const { data: stats } = useApi("/api/trades/stats");

  const handleExport = async () => {
    setIsExporting(true);
    try {
      await reportsController.downloadCsvExport();
    } catch (err) {
      console.error("Export error:", err);
    } finally {
      setIsExporting(false);
    }
  };

  return (
    <div className="space-y-6">
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-white">
            Audit & Trade History
          </h1>
          <p className="text-xs text-slate-400">
            Immutable SQLite transaction logs and fill performance
          </p>
        </div>
        <button
          onClick={handleExport}
          disabled={isExporting}
          className="btn-primary text-xs py-2 px-3 flex items-center gap-1.5 self-start sm:self-auto"
        >
          <Download size={14} />
          {isExporting ? "Exporting..." : "Export CSV Report"}
        </button>
      </div>

      {/* Aggregate Stats Cards */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 sm:gap-4">
        <div className="glass-card p-3.5 sm:p-5">
          <div className="text-[11px] sm:text-xs text-slate-500 uppercase tracking-wider font-semibold">
            Total Trades
          </div>
          <div className="font-mono text-xl sm:text-2xl font-bold text-white mt-1">
            {stats?.total_trades || 0}
          </div>
        </div>
        <div className="glass-card p-3.5 sm:p-5">
          <div className="text-[11px] sm:text-xs text-slate-500 uppercase tracking-wider font-semibold">
            Winning Fills
          </div>
          <div className="font-mono text-xl sm:text-2xl font-bold text-profit-400 mt-1">
            {stats?.winning_trades || 0}
          </div>
        </div>
        <div className="glass-card p-3.5 sm:p-5">
          <div className="text-[11px] sm:text-xs text-slate-500 uppercase tracking-wider font-semibold">
            Losing Fills
          </div>
          <div className="font-mono text-xl sm:text-2xl font-bold text-loss-400 mt-1">
            {stats?.losing_trades || 0}
          </div>
        </div>
        <div className="glass-card p-3.5 sm:p-5">
          <div className="text-[11px] sm:text-xs text-slate-500 uppercase tracking-wider font-semibold">
            Net Realized PnL
          </div>
          <div
            className={`font-mono text-xl sm:text-2xl font-bold mt-1 ${
              (stats?.total_pnl || 0) >= 0 ? "text-profit-400" : "text-loss-400"
            }`}
          >
            ${Number(stats?.total_pnl || 0).toFixed(2)}
          </div>
        </div>
      </div>

      {/* Filter and Table */}
      <div className="glass-card space-y-4 overflow-hidden">
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 border-b border-white/[0.06] pb-3">
          <div className="flex items-center gap-2">
            <Filter size={16} className="text-accent-400" />
            <span className="text-xs font-semibold uppercase text-slate-300">
              Filter by Asset
            </span>
          </div>
          <select
            value={selectedSymbol}
            onChange={(e) => setSelectedSymbol(e.target.value)}
            className="select-field text-xs w-full sm:w-48"
          >
            <option value="">All Instruments</option>
            <option value="AAPL">AAPL</option>
            <option value="MSFT">MSFT</option>
            <option value="NVDA">NVDA</option>
            <option value="GOOGL">GOOGL</option>
            <option value="AMZN">AMZN</option>
          </select>
        </div>

        <div className="overflow-x-auto">
          <table className="w-full text-left text-xs min-w-[580px]">
            <thead className="border-b border-white/[0.06] text-slate-400 uppercase tracking-wider">
              <tr>
                <th className="pb-3 font-medium">Timestamp</th>
                <th className="pb-3 font-medium">Symbol</th>
                <th className="pb-3 font-medium">Strategy</th>
                <th className="pb-3 font-medium">Side</th>
                <th className="pb-3 font-medium">Quantity</th>
                <th className="pb-3 font-medium text-right">Execution Price</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-white/[0.02]">
              {loading ? (
                <tr>
                  <td colSpan="6" className="py-8 text-center text-slate-500">
                    Loading trade records...
                  </td>
                </tr>
              ) : trades?.length === 0 ? (
                <tr>
                  <td colSpan="6" className="py-8 text-center text-slate-500">
                    No trades found matching criteria.
                  </td>
                </tr>
              ) : (
                trades?.map((t) => (
                  <tr key={t.id} className="hover:bg-white/[0.02] transition-colors">
                    <td className="py-3 font-mono text-slate-400">
                      {new Date(t.executed_at).toLocaleString()}
                    </td>
                    <td className="py-3 font-semibold text-white">{t.symbol}</td>
                    <td className="py-3 text-slate-300">{t.strategy_name || "Built-in"}</td>
                    <td className="py-3">
                      <StatusBadge status={t.side} />
                    </td>
                    <td className="py-3 font-mono text-slate-200">{t.quantity}</td>
                    <td className="py-3 text-right font-mono font-medium text-white">
                      ${Number(t.price).toFixed(2)}
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
