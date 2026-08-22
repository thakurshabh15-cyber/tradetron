import { useState } from "react";
import { Zap, ShieldCheck, AlertCircle, ArrowUpRight, ArrowDownRight, CheckCircle2 } from "lucide-react";
import { API_BASE } from "../config";

export default function FastOrderPanel({ symbol = "NIFTY50", currentPrice = 24850.0, onOrderPlaced }) {
  const [side, setSide] = useState("BUY");
  const [quantity, setQuantity] = useState(50);
  const [orderType, setOrderType] = useState("MARKET");
  const [customPrice, setCustomPrice] = useState(currentPrice);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [fillResult, setFillResult] = useState(null);

  const price = orderType === "MARKET" ? currentPrice : Number(customPrice);
  const totalValue = quantity * price;
  const marginRequired = totalValue * 0.2; // 5x leverage approx 20% margin for intraday

  const sym = symbol || "";
  const isINR = sym.includes("NIFTY") || sym.includes("RELIANCE") || sym.includes("INR") || sym.includes("TCS");
  const currencySymbol = isINR ? "₹" : "$";

  const handlePlaceOrder = async () => {
    setLoading(true);
    setError(null);
    setFillResult(null);
    try {
      const token = localStorage.getItem("tradetron_access_token") || "";
      const res = await fetch(`${API_BASE}/api/trades/order`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({
          symbol,
          side,
          quantity: Number(quantity),
          order_type: orderType,
          price: orderType === "LIMIT" ? Number(customPrice) : null,
        }),
      });

      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Order execution rejected");

      setFillResult(data);
      if (onOrderPlaced) onOrderPlaced(data);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="glass-panel rounded-2xl p-5 border border-slate-800/80 shadow-glass-md flex flex-col justify-between space-y-4">
      {/* Panel Header */}
      <div className="flex items-center justify-between border-b border-slate-800/60 pb-3">
        <div className="flex items-center gap-2">
          <div className={`p-2 rounded-xl border ${side === "BUY" ? "bg-emerald-500/10 text-emerald-400 border-emerald-500/25" : "bg-rose-500/10 text-rose-400 border-rose-500/25"}`}>
            <Zap size={16} />
          </div>
          <div>
            <h3 className="font-display font-bold text-sm text-white">Instant Order Execution</h3>
            <span className="text-[10px] text-slate-400">Institutional Sub-millisecond DMA Route</span>
          </div>
        </div>
        <span className="badge-neutral font-mono">{symbol}</span>
      </div>

      {error && (
        <div className="p-2.5 rounded-xl bg-red-500/10 border border-red-500/30 text-red-400 text-xs flex items-center gap-2">
          <AlertCircle size={14} className="shrink-0" />
          <span>{error}</span>
        </div>
      )}

      {fillResult && (
        <div className="p-2.5 rounded-xl bg-emerald-500/10 border border-emerald-500/30 text-emerald-300 text-xs flex items-center justify-between font-mono">
          <div className="flex items-center gap-1.5 font-bold">
            <CheckCircle2 size={14} className="text-emerald-400" />
            <span>ORDER FILLED: {fillResult.status}</span>
          </div>
          <span className="text-[10px] text-slate-400">{fillResult.broker_order_id}</span>
        </div>
      )}

      {/* Buy / Sell Selector Buttons */}
      <div className="grid grid-cols-2 gap-2 bg-surface-950 p-1.5 rounded-xl border border-slate-800">
        <button
          type="button"
          onClick={() => setSide("BUY")}
          className={`py-2.5 rounded-lg text-xs font-bold transition-all flex items-center justify-center gap-1.5 ${
            side === "BUY"
              ? "bg-emerald-600 text-white shadow-md shadow-emerald-600/30"
              : "text-slate-400 hover:text-white"
          }`}
        >
          <ArrowUpRight size={14} /> BUY / LONG
        </button>
        <button
          type="button"
          onClick={() => setSide("SELL")}
          className={`py-2.5 rounded-lg text-xs font-bold transition-all flex items-center justify-center gap-1.5 ${
            side === "SELL"
              ? "bg-rose-600 text-white shadow-md shadow-rose-600/30"
              : "text-slate-400 hover:text-white"
          }`}
        >
          <ArrowDownRight size={14} /> SELL / SHORT
        </button>
      </div>

      {/* Order Type & Quantity Inputs */}
      <div className="space-y-3">
        <div>
          <label className="text-[11px] font-semibold text-slate-400">Order Type</label>
          <div className="grid grid-cols-3 gap-1.5 mt-1">
            {["MARKET", "LIMIT", "SL-M"].map((type) => (
              <button
                key={type}
                type="button"
                onClick={() => setOrderType(type)}
                className={`py-1.5 rounded-lg text-xs font-mono font-semibold border transition-all ${
                  orderType === type
                    ? "bg-brand-purple/20 text-brand-purple border-brand-purple/40 font-bold"
                    : "bg-surface-900 border-slate-800 text-slate-400 hover:text-white"
                }`}
              >
                {type}
              </button>
            ))}
          </div>
        </div>

        <div>
          <div className="flex justify-between text-[11px] font-semibold text-slate-400">
            <span>Quantity (Lots)</span>
            <div className="flex gap-1 text-[10px] text-brand-purple font-mono">
              <button type="button" onClick={() => setQuantity(25)} className="hover:underline">+25</button>
              <span>•</span>
              <button type="button" onClick={() => setQuantity(50)} className="hover:underline">+50</button>
              <span>•</span>
              <button type="button" onClick={() => setQuantity(100)} className="hover:underline">+100</button>
            </div>
          </div>
          <input
            type="number"
            value={quantity}
            onChange={(e) => setQuantity(Math.max(1, parseInt(e.target.value) || 1))}
            className="input-field mt-1 font-mono tabular-nums font-bold text-sm"
            min="1"
          />
        </div>

        {orderType === "LIMIT" && (
          <div>
            <label className="text-[11px] font-semibold text-slate-400">Limit Price ({currencySymbol})</label>
            <input
              type="number"
              step="0.05"
              value={customPrice}
              onChange={(e) => setCustomPrice(parseFloat(e.target.value) || 0)}
              className="input-field mt-1 font-mono tabular-nums font-bold text-sm"
            />
          </div>
        )}
      </div>

      {/* Pre-Trade Margin & Cost Calculator */}
      <div className="p-3 rounded-xl bg-surface-950/80 border border-slate-800/80 space-y-1.5 font-mono text-[11px] tabular-nums">
        <div className="flex justify-between text-slate-400">
          <span>Gross Value:</span>
          <span className="font-bold text-white">{currencySymbol}{totalValue.toLocaleString(undefined, { minimumFractionDigits: 2 })}</span>
        </div>
        <div className="flex justify-between text-slate-400">
          <span>Required Margin (5x):</span>
          <span className="font-bold text-cyan-400">{currencySymbol}{marginRequired.toLocaleString(undefined, { minimumFractionDigits: 2 })}</span>
        </div>
        <div className="flex justify-between text-slate-400 pt-1 border-t border-slate-800/60">
          <span className="flex items-center gap-1 text-slate-500"><ShieldCheck size={11} /> Est. Slippage/Tax:</span>
          <span className="text-slate-300">~{currencySymbol}{(totalValue * 0.0003).toFixed(2)}</span>
        </div>
      </div>

      {/* Submit Action Button */}
      <button
        type="button"
        disabled={loading}
        onClick={handlePlaceOrder}
        className={`w-full py-3 rounded-xl text-white font-bold text-xs shadow-lg transition-all flex items-center justify-center gap-2 ${
          side === "BUY"
            ? "bg-gradient-to-r from-emerald-600 to-emerald-500 hover:from-emerald-500 hover:to-emerald-400 shadow-emerald-600/30 active:scale-[0.98]"
            : "bg-gradient-to-r from-rose-600 to-rose-500 hover:from-rose-500 hover:to-rose-400 shadow-rose-600/30 active:scale-[0.98]"
        } disabled:opacity-50`}
      >
        <Zap size={14} />
        {loading ? "Routing to Broker..." : `TRANSMIT ${side} ${quantity} ${symbol} @ ${orderType}`}
      </button>
    </div>
  );
}
