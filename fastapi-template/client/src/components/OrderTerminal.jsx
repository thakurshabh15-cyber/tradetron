import React, { useState, useMemo } from "react";
import { Zap, ShieldCheck, AlertCircle, Loader2, Gauge, Receipt } from "lucide-react";
import { useMarketStore } from "../stores/useMarketStore";
import { authFetch } from "../services/apiClient";
import { useToast } from "./Toast";

// Mirror of backend dma_engine constants for instant pre-trade preview
const LOT_SIZES = { NIFTY: 65, BANKNIFTY: 30, SENSEX: 20, FINNIFTY: 65 };
const MARGIN_MULT = { INDEX_FNO: 0.20, EQUITY_MIS: 0.25, EQUITY_CNC: 1.0, COMMODITY: 0.12, CRYPTO: 1.0, FOREX: 0.20 };

function getLotSize(symbol) {
  const s = String(symbol || "").toUpperCase();
  for (const [k, v] of Object.entries(LOT_SIZES)) if (s.includes(k)) return v;
  return 1;
}
function classifyAsset(symbol, product) {
  const s = String(symbol || "").toUpperCase();
  if (/^(BTC|ETH|SOL|BNB|XRP|DOGE)/.test(s) && s.includes("USDT")) return "CRYPTO";
  if (/(NIFTY|BANKNIFTY|FINNIFTY|MIDCPNIFTY|SENSEX)/.test(s)) return "INDEX_FNO";
  return product === "CNC" ? "EQUITY_CNC" : "EQUITY_MIS";
}
function estimateCharges(symbol, side, product, qty, price) {
  const turnover = qty * price;
  const asset = classifyAsset(symbol, product);
  const brokerage = 20.0;
  let stt = 0;
  if (asset === "INDEX_FNO") stt = side === "SELL" ? turnover * 0.000625 : 0;
  else if (asset === "EQUITY_CNC") stt = turnover * 0.001;
  else if (asset !== "CRYPTO") stt = side === "SELL" ? turnover * 0.00025 : 0;
  const exchRate = asset === "INDEX_FNO" ? 0.00173 : 0.00297;
  const txn = (turnover * exchRate) / 100;
  const sebi = turnover * 0.0001;
  const gst = (brokerage + txn + sebi) * 0.18;
  let stamp = 0;
  if (side === "BUY") stamp = asset === "EQUITY_CNC" ? turnover * 0.00015 : asset !== "CRYPTO" ? turnover * 0.00003 : 0;
  const total = brokerage + stt + txn + sebi + gst + stamp;
  return {
    turnover, brokerage, stt_ctt: stt, exchange_transaction: txn,
    sebi_fees: sebi, gst, stamp_duty: stamp, total,
  };
}
const inr = (v, d = 2) => `₹${Number(v || 0).toLocaleString("en-IN", { minimumFractionDigits: d, maximumFractionDigits: d })}`;

function OrderTerminal({ symbol = "NIFTY50", currentPrice = 24850.0, onOrderPlaced }) {
  const liveQuote = useMarketStore((st) => st.quotes[String(symbol).toUpperCase().trim()]);
  const toast = useToast();

  const [side, setSide] = useState("BUY");
  const [lots, setLots] = useState(1);
  const [product, setProduct] = useState("MIS");
  const [orderType, setOrderType] = useState("MARKET");
  const [limitPrice, setLimitPrice] = useState("");
  const [slPct, setSlPct] = useState("0.5");
  const [tpPct, setTpPct] = useState("1.0");
  const [mode, setMode] = useState("PAPER");
  const [submitting, setSubmitting] = useState(false);

  const px = liveQuote?.price ?? currentPrice;
  const lotSize = getLotSize(symbol);
  const safeLots = Math.max(1, parseInt(lots, 10) || 1);
  const quantity = safeLots * lotSize;
  const effPrice = orderType === "LIMIT" && Number(limitPrice) > 0 ? Number(limitPrice) : px;
  const assetClass = classifyAsset(symbol, product);
  const marginMult = MARGIN_MULT[assetClass] ?? 1;
  const marginReq = useMemo(() => quantity * effPrice * marginMult, [quantity, effPrice, marginMult]);
  const charges = useMemo(() => estimateCharges(symbol, side, product, quantity, effPrice), [symbol, side, product, quantity, effPrice]);
  const totalDebit = useMemo(() => marginReq + charges.total, [marginReq, charges.total]);

  const transmit = async () => {
    setSubmitting(true);
    try {
      const res = await authFetch("/api/v1/orders/execute-dma", {
        method: "POST",
        body: JSON.stringify({
          symbol,
          side,
          lots: safeLots,
          product,
          order_type: orderType,
          limit_price: orderType === "LIMIT" ? Number(limitPrice) : null,
          stop_loss_pct: slPct ? Number(slPct) : null,
          take_profit_pct: tpPct ? Number(tpPct) : null,
          mode,
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || `Execution rejected (HTTP ${res.status})`);

      const lat = Number(data.latency_ms ?? 0);
      toast.success(
        `${data.side} ${data.quantity} ${data.symbol} filled @ ${inr(data.executed_price)}`,
        {
          description: `Order ${data.order_id} · margin ${inr(data.margin_required)} · charges ${inr(data.charges?.total)}`,
          duration: lat <= 50 ? 4000 : 6000,
        }
      );
      toast.info(
        lat <= 50 ? `⚡ Sub-50ms execution: ${lat}ms` : `Execution latency: ${lat}ms`,
        { duration: 3500 }
      );
      if (onOrderPlaced) onOrderPlaced(data);
    } catch (err) {
      toast.error("DMA order rejected", { description: err.message });
    } finally {
      setSubmitting(false);
    }
  };

  const sideBtn = (s) =>
    `flex-1 py-2 rounded-xl text-xs font-bold transition-all border ${
      side === s
        ? s === "BUY"
          ? "bg-emerald-500/20 text-emerald-300 border-emerald-500/40"
          : "bg-rose-500/20 text-rose-300 border-rose-500/40"
        : "bg-surface-900 border-slate-800 text-slate-400 hover:text-white"
    }`;

  return (
    <div className="glass-panel rounded-2xl p-4 sm:p-5 border border-slate-800/80 shadow-glass-md space-y-3">
      <div className="flex items-center justify-between border-b border-slate-800/60 pb-3">
        <div className="flex items-center gap-2">
          <div className={`p-2 rounded-xl border ${side === "BUY" ? "bg-emerald-500/10 text-emerald-400 border-emerald-500/25" : "bg-rose-500/10 text-rose-400 border-rose-500/25"}`}>
            <Zap size={16} />
          </div>
          <div>
            <h3 className="font-display font-bold text-sm text-white">Institutional DMA Terminal</h3>
            <span className="text-[10px] text-slate-400">Lot-corrected · Pre-trade analytics · &lt;50ms SLO</span>
          </div>
        </div>
        <span className="badge-neutral font-mono">{symbol}</span>
      </div>

      <div className="flex gap-2">
        <button type="button" onClick={() => setSide("BUY")} className={sideBtn("BUY")}>BUY / LONG</button>
        <button type="button" onClick={() => setSide("SELL")} className={sideBtn("SELL")}>SELL / SHORT</button>
      </div>

      <div className="grid grid-cols-3 gap-2">
        <div>
          <label className="text-[10px] font-semibold text-slate-400 uppercase">Lots</label>
          <input type="number" min="1" value={lots}
            onChange={(e) => setLots(e.target.value)}
            className="input-field mt-1 font-mono font-bold text-sm tabular-nums" />
          <p className="text-[10px] text-brand-purple font-mono mt-0.5">lot × {lotSize} → {quantity} qty</p>
        </div>
        <div>
          <label className="text-[10px] font-semibold text-slate-400 uppercase">Product</label>
          <select value={product} onChange={(e) => setProduct(e.target.value)} className="select-field mt-1 text-xs">
            <option>MIS</option><option>CNC</option><option>NRML</option>
          </select>
        </div>
        <div>
          <label className="text-[10px] font-semibold text-slate-400 uppercase">Type</label>
          <select value={orderType} onChange={(e) => setOrderType(e.target.value)} className="select-field mt-1 text-xs">
            <option>MARKET</option><option>LIMIT</option>
          </select>
        </div>
      </div>

      {orderType === "LIMIT" && (
        <div>
          <label className="text-[10px] font-semibold text-slate-400 uppercase">Limit Price</label>
          <input type="number" step="0.05" value={limitPrice} onChange={(e) => setLimitPrice(e.target.value)}
            placeholder={fmtPx(px)} className="input-field mt-1 font-mono text-sm tabular-nums" />
        </div>
      )}

      <div className="grid grid-cols-2 gap-2">
        <div>
          <label className="text-[10px] font-semibold text-rose-400 uppercase">Stop-Loss %</label>
          <input type="number" step="0.05" min="0" value={slPct} onChange={(e) => setSlPct(e.target.value)} className="input-field mt-1 font-mono text-xs" />
        </div>
        <div>
          <label className="text-[10px] font-semibold text-emerald-400 uppercase">Take-Profit %</label>
          <input type="number" step="0.05" min="0" value={tpPct} onChange={(e) => setTpPct(e.target.value)} className="input-field mt-1 font-mono text-xs" />
        </div>
      </div>

      <div className="p-2.5 rounded-xl bg-surface-950/80 border border-slate-800/80 space-y-1 font-mono text-[11px] tabular-nums">
        <Row k="Notional" v={inr(charges.turnover)} />
        <Row k={`Margin (${(marginMult * 100).toFixed(0)}%)`} v={inr(marginReq)} accent="text-cyan-400" />
        <div className="pt-1 border-t border-slate-800/60" />
        <Row k="Brokerage ₹20 flat" v={inr(charges.brokerage)} />
        <Row k="STT/CTT" v={inr(charges.stt_ctt)} />
        <Row k="Exch txn + SEBI" v={inr(charges.exchange_transaction + charges.sebi_fees, 3)} />
        <Row k="GST @18%" v={inr(charges.gst)} />
        <Row k="Stamp duty" v={inr(charges.stamp_duty)} />
        <div className="pt-1 border-t border-slate-800/60" />
        <Row k="Total debit" v={inr(totalDebit)} bold />
      </div>

      <button
        type="button"
        disabled={submitting}
        onClick={transmit}
        className={`w-full py-3 rounded-xl text-white font-bold text-xs shadow-lg transition-all flex items-center justify-center gap-2 active:scale-[0.98] disabled:opacity-50 ${
          side === "BUY"
            ? "bg-gradient-to-r from-emerald-600 to-emerald-500 shadow-emerald-600/30 hover:from-emerald-500"
            : "bg-gradient-to-r from-rose-600 to-rose-500 shadow-rose-600/30 hover:from-rose-500"
        }`}
      >
        {submitting ? <Loader2 size={14} className="animate-spin" /> : <Zap size={14} />}
        {submitting ? "Routing to broker…" : `TRANSMIT ${side} ${quantity} ${symbol}`}
      </button>
      <p className="flex items-center justify-center gap-1 text-[9px] text-slate-500 font-mono">
        <ShieldCheck size={9} /> AES-256 vault · DMA direct route · statutory-exact
      </p>
    </div>
  );
}

function Row({ k, v, accent, bold }) {
  return (
    <div className="flex justify-between">
      <span className="text-slate-400">{k}</span>
      <span className={`${bold ? "text-white font-bold" : accent || "text-slate-200"}`}>{v}</span>
    </div>
  );
}

const fmtPx = (v) => Number(v || 0).toFixed(2);

export default React.memo(OrderTerminal);
