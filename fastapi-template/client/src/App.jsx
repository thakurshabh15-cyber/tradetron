import { useState, useEffect, useCallback } from "react";
import { BrowserRouter, Routes, Route } from "react-router-dom";
import Sidebar from "./components/Sidebar";
import Dashboard from "./pages/Dashboard";
import Marketplace from "./pages/Marketplace";
import Strategies from "./pages/Strategies";
import TradeHistory from "./pages/TradeHistory";
import Watchlist from "./pages/Watchlist";
import Settings from "./pages/Settings";
import Admin from "./pages/Admin";
import KillSwitchModal from "./components/KillSwitchModal";
import BrokerConnectModal from "./components/BrokerConnectModal";
import LiveOptInModal from "./components/LiveOptInModal";
import KYCModal from "./components/KYCModal";
import { authFetch } from "./services/apiClient";

import { ShieldAlert, ShieldCheck, Power, Zap, Radio, Link as LinkIcon, AlertCircle, FileCheck } from "lucide-react";

export default function App() {
  const [executionMode, setExecutionMode] = useState("PAPER"); // 'PAPER' is strictly enforced default
  const [brokerAccounts, setBrokerAccounts] = useState([]);
  const [kycStatus, setKycStatus] = useState("NOT_SUBMITTED");
  const [isKillSwitchOpen, setIsKillSwitchOpen] = useState(false);
  const [isBrokerModalOpen, setIsBrokerModalOpen] = useState(false);
  const [isLiveOptInOpen, setIsLiveOptInOpen] = useState(false);
  const [isKYCModalOpen, setIsKYCModalOpen] = useState(false);
  const [brokerWarning, setBrokerWarning] = useState(null);

  const fetchBrokers = useCallback(async () => {
    try {
      const res = await authFetch("/api/brokers/accounts");
      if (res.ok) {
        const data = await res.json();
        const accounts = Array.isArray(data) ? data : [];
        setBrokerAccounts(accounts);

        // If in LIVE mode and no broker is connected/valid, auto-revert to PAPER
        const validBrokers = accounts.filter(
          (b) => b.status === "CONNECTED" && !b.is_token_expired
        );
        if (validBrokers.length === 0 && executionMode === "LIVE") {
          setExecutionMode("PAPER");
        }
      }
    } catch {
      setBrokerAccounts([]);
    }
  }, [executionMode]);

  const fetchKYCStatus = useCallback(async () => {
    try {
      const res = await authFetch("/api/user/kyc");
      if (res.ok) {
        const data = await res.json();
        setKycStatus(data.kyc_status || "NOT_SUBMITTED");
      }
    } catch {
      setKycStatus("NOT_SUBMITTED");
    }
  }, []);

  useEffect(() => {
    fetchBrokers();
    fetchKYCStatus();
  }, [fetchBrokers, fetchKYCStatus]);

  // Verified active brokers only
  const connectedBrokers = brokerAccounts.filter(
    (b) => b.status === "CONNECTED" && !b.is_token_expired
  );

  const isLiveActive = executionMode === "LIVE" && connectedBrokers.length > 0 && kycStatus === "VERIFIED";

  const handleModeSwitch = (mode) => {
    setBrokerWarning(null);
    if (mode === "LIVE") {
      if (kycStatus !== "VERIFIED") {
        setBrokerWarning(
          "KYC Verification Required: SEBI regulatory compliance mandates that your KYC status must be VERIFIED before enabling Live Broker execution."
        );
        setIsKYCModalOpen(true);
        return;
      }
      if (connectedBrokers.length === 0) {
        setBrokerWarning(
          "Cannot enable Live Execution: No verified broker account connected. Please connect Zerodha, Angel One, Upstox, or Binance first."
        );
        setIsBrokerModalOpen(true);
      } else {
        setIsLiveOptInOpen(true);
      }
    } else {
      setExecutionMode("PAPER");
    }
  };

  const handleOpenBrokerModal = () => {
    setBrokerWarning(null);
    if (kycStatus !== "VERIFIED") {
      setBrokerWarning(
        "KYC Verification Required: SEBI compliance mandates that your KYC status must be VERIFIED before connecting a live broker account."
      );
      setIsKYCModalOpen(true);
    } else {
      setIsBrokerModalOpen(true);
    }
  };

  return (
    <BrowserRouter>
      <div className="flex min-h-screen bg-surface-950 text-slate-300 selection:bg-accent-500/20 selection:text-accent-400">
        <Sidebar onOpenKYC={() => setIsKYCModalOpen(true)} kycStatus={kycStatus} />
        <main className="flex-1 lg:ml-64 pt-16 pb-20 lg:pt-6 lg:pb-8 px-4 sm:px-6 lg:px-8 space-y-6 overflow-x-hidden">
          {/* Top Execution Control & Mode Banner */}
          <div className="flex flex-col md:flex-row items-stretch md:items-center justify-between gap-3 p-3 rounded-xl bg-slate-900/90 border border-slate-800/80 shadow-lg">
            {/* Mode Indicator & Regulatory Disclaimer */}
            <div className="flex items-center gap-3">
              <div
                className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg border text-xs font-bold transition-all ${
                  isLiveActive
                    ? "bg-rose-500/15 border-rose-500/40 text-rose-400 shadow-sm shadow-rose-500/20 animate-pulse"
                    : "bg-emerald-500/15 border-emerald-500/40 text-emerald-300 shadow-sm shadow-emerald-500/10"
                }`}
              >
                {isLiveActive ? <Radio size={14} className="text-rose-400" /> : <Zap size={14} className="text-emerald-400" />}
                <span>{isLiveActive ? "LIVE BROKER EXECUTION" : "PAPER TRADING (SIMULATION)"}</span>
              </div>

              <span className="hidden xl:inline-block text-[11px] text-slate-400">
                {isLiveActive
                  ? `Real capital active across ${connectedBrokers.map((b) => b.broker_name).join(", ")}.`
                  : kycStatus !== "VERIFIED"
                  ? "Paper Simulation Mode Active (KYC Required for Live Broker Execution)."
                  : connectedBrokers.length === 0
                  ? "Virtual paper balance ₹10,00,000 active. No live broker connected (Simulation Mode Only)."
                  : "Virtual paper balance ₹10,00,000 active. Real funds are protected."}
              </span>
            </div>

            {/* Mode Switcher, KYC Gate, Broker Link, and Panic Button */}
            <div className="flex items-center gap-2">
              <button
                onClick={() => setIsKYCModalOpen(true)}
                className={`flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg border text-xs font-semibold transition-all ${
                  kycStatus === "VERIFIED"
                    ? "bg-emerald-500/10 border-emerald-500/30 text-emerald-400"
                    : kycStatus === "PENDING"
                    ? "bg-amber-500/10 border-amber-500/30 text-amber-400"
                    : "bg-violet-600/20 border-violet-500/40 text-violet-300 hover:bg-violet-600/30"
                }`}
                title="SEBI KYC Status"
              >
                <ShieldCheck size={13} />
                <span>
                  {kycStatus === "VERIFIED"
                    ? "KYC Verified"
                    : kycStatus === "PENDING"
                    ? "KYC Pending"
                    : "Verify KYC"}
                </span>
              </button>

              <div className="bg-slate-950 p-1 rounded-lg border border-slate-800 flex text-[11px] font-semibold">
                <button
                  onClick={() => handleModeSwitch("PAPER")}
                  className={`px-2.5 py-1 rounded-md transition-all ${
                    !isLiveActive
                      ? "bg-emerald-500/20 text-emerald-300 border border-emerald-500/30"
                      : "text-slate-400 hover:text-white"
                  }`}
                >
                  Paper
                </button>
                <button
                  onClick={() => handleModeSwitch("LIVE")}
                  className={`px-2.5 py-1 rounded-md transition-all ${
                    isLiveActive
                      ? "bg-rose-500/20 text-rose-300 border border-rose-500/30"
                      : "text-slate-400 hover:text-white"
                  }`}
                >
                  Live Broker
                </button>
              </div>

              <button
                onClick={handleOpenBrokerModal}
                className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-750 border border-slate-700 text-white text-xs font-semibold transition-all"
              >
                <LinkIcon size={13} className={connectedBrokers.length > 0 ? "text-emerald-400" : "text-cyan-400"} />
                <span className="hidden sm:inline">
                  {connectedBrokers.length > 0 ? `${connectedBrokers.length} Broker Linked` : "Connect Broker"}
                </span>
              </button>

              <button
                onClick={() => setIsKillSwitchOpen(true)}
                className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-gradient-to-r from-red-600 to-rose-600 hover:from-red-500 hover:to-rose-500 text-white text-xs font-bold transition-all shadow-md shadow-red-600/30"
                title="Emergency Pause All Strategies"
              >
                <Power size={13} />
                <span>KILL SWITCH</span>
              </button>
            </div>
          </div>

          {brokerWarning && (
            <div className="flex items-center gap-2 p-3 rounded-xl bg-amber-500/10 border border-amber-500/30 text-amber-300 text-xs animate-fade-in">
              <AlertCircle size={15} className="shrink-0 text-amber-400" />
              <span>{brokerWarning}</span>
            </div>
          )}

          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/marketplace" element={<Marketplace />} />
            <Route path="/watchlist" element={<Watchlist />} />
            <Route path="/strategies" element={<Strategies />} />
            <Route path="/history" element={<TradeHistory />} />
            <Route path="/settings" element={<Settings />} />
            <Route path="/admin" element={<Admin />} />
          </Routes>
        </main>

        {/* Global Modals */}
        <KillSwitchModal
          isOpen={isKillSwitchOpen}
          onClose={() => setIsKillSwitchOpen(false)}
        />
        <BrokerConnectModal
          isOpen={isBrokerModalOpen}
          onClose={() => setIsBrokerModalOpen(false)}
          onLinkedSuccess={() => {
            fetchBrokers();
            setBrokerWarning(null);
          }}
        />
        <KYCModal
          isOpen={isKYCModalOpen}
          onClose={() => setIsKYCModalOpen(false)}
          onKYCUpdated={() => {
            fetchKYCStatus();
            setBrokerWarning(null);
          }}
        />
        <LiveOptInModal
          isOpen={isLiveOptInOpen}
          onClose={() => setIsLiveOptInOpen(false)}
          onConfirm={() => {
            if (connectedBrokers.length > 0 && kycStatus === "VERIFIED") {
              setExecutionMode("LIVE");
            } else {
              setExecutionMode("PAPER");
              if (kycStatus !== "VERIFIED") setIsKYCModalOpen(true);
              else setIsBrokerModalOpen(true);
            }
          }}
          onConnectBroker={() => {
            if (kycStatus !== "VERIFIED") setIsKYCModalOpen(true);
            else setIsBrokerModalOpen(true);
          }}
        />
      </div>
    </BrowserRouter>
  );
}