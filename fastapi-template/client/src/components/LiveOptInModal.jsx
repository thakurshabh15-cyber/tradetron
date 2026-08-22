import { useState } from "react";
import { AlertTriangle, ShieldCheck, X, CheckSquare, Square, ArrowRight } from "lucide-react";

export default function LiveOptInModal({ isOpen, onClose, onConfirm, onConnectBroker }) {
  const [hasAgreedTerms, setHasAgreedTerms] = useState(false);
  const [hasAgreedRisk, setHasAgreedRisk] = useState(false);

  if (!isOpen) return null;

  const canActivate = hasAgreedTerms && hasAgreedRisk;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/80 backdrop-blur-md animate-fade-in">
      <div className="relative w-full max-w-lg rounded-2xl bg-surface-900 border border-rose-500/40 p-6 shadow-2xl shadow-rose-950/50 space-y-5">
        {/* Header */}
        <div className="flex items-start justify-between">
          <div className="flex items-center gap-3">
            <div className="p-2.5 rounded-xl bg-rose-500/10 text-rose-400 border border-rose-500/30">
              <AlertTriangle size={22} />
            </div>
            <div>
              <h3 className="text-base font-display font-bold text-white">
                Live Broker Real-Money Activation
              </h3>
              <p className="text-xs text-rose-400 font-medium mt-0.5">
                Regulatory Risk Disclosure & Compliance Authorization
              </p>
            </div>
          </div>
          <button
            onClick={onClose}
            className="p-1 rounded-lg text-slate-400 hover:text-white hover:bg-slate-800 transition-colors"
          >
            <X size={18} />
          </button>
        </div>

        {/* Warning Content */}
        <div className="space-y-3 p-4 rounded-xl bg-rose-950/30 border border-rose-900/40 text-xs text-slate-300 leading-relaxed">
          <p className="font-semibold text-rose-300">
            ⚠️ Attention Trader: You are about to enable LIVE execution mode.
          </p>
          <ul className="list-disc pl-4 space-y-1.5 text-[11px] text-slate-300">
            <li>
              Every algorithmic signal and manual order will place <strong>real orders with real capital</strong> through your linked broker (Zerodha Kite, Angel One, Binance).
            </li>
            <li>
              Orders are subject to market slippage, exchange latency, margin requirements, and brokerage fees.
            </li>
            <li>
              Past backtested performance is no guarantee of future returns.
            </li>
          </ul>
        </div>

        {/* Checkbox Opt-Ins */}
        <div className="space-y-3">
          <button
            type="button"
            onClick={() => setHasAgreedRisk(!hasAgreedRisk)}
            className="w-full text-left flex items-start gap-2.5 p-3 rounded-xl bg-surface-950 border border-slate-800 hover:border-slate-700 transition-all text-xs"
          >
            <div className="mt-0.5 text-rose-400">
              {hasAgreedRisk ? <CheckSquare size={16} /> : <Square size={16} />}
            </div>
            <span className="text-slate-200 text-[11px]">
              I acknowledge that algorithmic trading carries financial risk and I am authorizing real order execution.
            </span>
          </button>

          <button
            type="button"
            onClick={() => setHasAgreedTerms(!hasAgreedTerms)}
            className="w-full text-left flex items-start gap-2.5 p-3 rounded-xl bg-surface-950 border border-slate-800 hover:border-slate-700 transition-all text-xs"
          >
            <div className="mt-0.5 text-rose-400">
              {hasAgreedTerms ? <CheckSquare size={16} /> : <Square size={16} />}
            </div>
            <span className="text-slate-200 text-[11px]">
              I confirm that my broker account is authenticated and I accept the risk parameters and emergency kill-switch controls.
            </span>
          </button>
        </div>

        {/* Actions */}
        <div className="flex items-center justify-between pt-2">
          <button
            type="button"
            onClick={() => {
              onClose();
              if (onConnectBroker) onConnectBroker();
            }}
            className="text-xs text-cyan-400 hover:text-cyan-300 underline font-semibold"
          >
            Check / Connect Broker
          </button>

          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={onClose}
              className="btn-secondary text-xs min-h-[40px]"
            >
              Keep in Paper Mode
            </button>
            <button
              type="button"
              disabled={!canActivate}
              onClick={() => {
                onConfirm();
                onClose();
              }}
              className="btn-danger text-xs min-h-[40px] flex items-center gap-1.5 disabled:opacity-40 disabled:cursor-not-allowed"
            >
              <ShieldCheck size={14} />
              <span>Confirm & Enable LIVE</span>
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
