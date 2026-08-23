import { useState, useEffect, useCallback } from "react";
import { BrowserRouter, Routes, Route } from "react-router-dom";
import Sidebar from "./components/Sidebar";
import Dashboard from "./pages/Dashboard";
import CopyTrading from "./pages/CopyTrading";
import Marketplace from "./pages/Marketplace";
import Strategies from "./pages/Strategies";
import TradeHistory from "./pages/TradeHistory";
import Watchlist from "./pages/Watchlist";
import Settings from "./pages/Settings";
import Admin from "./pages/Admin";
import BrokerSessions from "./pages/BrokerSessions";
import KillSwitchModal from "./components/KillSwitchModal";
import BrokerConnectModal from "./components/BrokerConnectModal";
import LiveOptInModal from "./components/LiveOptInModal";
import KYCModal from "./components/KYCModal";
import ErrorBoundary from "./components/ErrorBoundary";
import { MarketProvider } from "./context/MarketContext";
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
  const [balances, setBalances] = useState({
    paper_balance: 1000000.0,
    live_balance: {
      connected: false,
      broker_name: null,
      available_cash: null,
      message: "Connect broker to view live balance.",
    },
  });

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

  const fetchBalances = useCallback(async () => {
    try {
      const res = await authFetch("/api/brokers/balance");
      if (res.ok) {
        const data = await res.json();
        setBalances(data);
      }
    } catch {
      // keep fallback
    }
  }, []);

  useEffect(() => {
    fetchBrokers();
    fetchKYCStatus();
    fetchBalances();
    const interval = setInterval(fetchBalances, 8000);
    return () => clearInterval(interval);
  }, [fetchBrokers, fetchKYCStatus, fetchBalances]);

  // Verified active brokers only
  const connectedBrokers = brokerAccounts.filter(
    (b) => b.status === "CONNECTED" && !b.is_token_expired
  );

  const isLiveActive = executionMode === "LIVE" && connectedBrokers.length > 0;

  const handleModeSwitch = (mode) => {
    setBrokerWarning(null);
    if (mode === "LIVE") {
      if (connectedBrokers.length === 0) {
        setBrokerWarning(
          "Cannot enable Live Execution: No active broker account connected. Please connect Zerodha, Angel One, Upstox, or Binance first."
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
    setIsBrokerModalOpen(true);
  };

  return (
    <MarketProvider>
      <BrowserRouter>
        <div className="flex min-h-screen bg-surface-950 text-slate-300 selection:bg-accent-500/20 selection:text-accent-400">
          <Sidebar onOpenKYC={() => setIsKYCModalOpen(true)} kycStatus={kycStatus} />
          <main className="flex-1 lg:ml-64 pt-16 pb-20 lg:pt-6 lg:pb-8 px-3.5 sm:px-6 lg:px-8 space-y-6 overflow-x-hidden w-full max-w-full">
            {/* Top Execution Control & Mode Banner */}
            <div className="flex flex-col md:flex-row items-stretch md:items-center justify-between gap-3 p-3 sm:p-3.5 rounded-xl bg-slate-900/90 border border-slate-800/80 shadow-lg">
              {/* Mode Indicator & Regulatory Disclaimer */}
              <div className="flex flex-wrap items-center justify-between sm:justify-start gap-2.5">
                <div
                  className={`flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg border text-xs font-bold transition-all ${
                    isLiveActive
                      ? "bg-rose-500/15 border-rose-500/40 text-rose-400 shadow-sm shadow-rose-500/20 animate-pulse"
                      : "bg-emerald-500/15 border-emerald-500/40 text-emerald-300 shadow-sm shadow-emerald-500/10"
                  }`}
                >
                  {isLiveActive ? <Radio size={13} className="text-rose-400" /> : <Zap size={13} className="text-emerald-400" />}
                  <span>{isLiveActive ? "LIVE EXECUTION" : "PAPER SIMULATION"}</span>
                </div>

                <div className="text-[11px] text-slate-400">
                  {isLiveActive ? (
                    balances.live_balance?.connected && balances.live_balance?.available_cash !== null ? (
                      <span className="flex items-center gap-1 font-mono">
                        <span className="text-rose-400 font-bold">
                          Margin: ₹
                          {Number(balances.live_balance.available_cash || 0).toLocaleString("en-IN", {
                            minimumFractionDigits: 2,
                          })}
                        </span>
                      </span>
                    ) : (
                      <span className="text-amber-400 font-medium flex items-center gap-1">
                        <span>No broker</span>
                        <button
                          onClick={handleOpenBrokerModal}
                          className="underline hover:text-white text-[11px]"
                        >
                          Connect
                        </button>
                      </span>
                    )
                  ) : (
                    <span>
                      Bal: <strong className="text-emerald-400 font-mono">
                        ₹
                        {Number(balances.paper_balance || 1000000).toLocaleString("en-IN", {
                          minimumFractionDigits: 0,
                          maximumFractionDigits: 2,
                        })}
                      </strong>
                    </span>
                  )}
                </div>
              </div>

              {/* Mode Switcher, KYC Gate, Broker Link, and Panic Button */}
              <div className="flex flex-wrap items-center gap-1.5 sm:gap-2">
                <button
                  onClick={() => setIsKYCModalOpen(true)}
                  className={`flex items-center gap-1 px-2.5 py-1.5 rounded-lg border text-xs font-semibold transition-all ${
                    kycStatus === "VERIFIED"
                      ? "bg-emerald-500/10 border-emerald-500/30 text-emerald-400"
                      : kycStatus === "PENDING"
                      ? "bg-amber-500/10 border-amber-500/30 text-amber-400"
                      : "bg-violet-600/20 border-violet-500/40 text-violet-300 hover:bg-violet-600/30"
                  }`}
                >
                  <FileCheck size={13} />
                  <span>
                    {kycStatus === "VERIFIED" ? "KYC Verified" : kycStatus === "PENDING" ? "Pending" : "KYC"}
                  </span>
                </button>

                <div className="flex rounded-lg bg-surface-950 p-0.5 border border-white/[0.06]">
                  <button
                    onClick={() => handleModeSwitch("PAPER")}
                    className={`px-2.5 py-1 rounded-md text-xs font-semibold transition-all ${
                      !isLiveActive
                        ? "bg-emerald-500 text-slate-950 shadow-sm"
                        : "text-slate-400 hover:text-white"
                    }`}
                  >
                    Paper
                  </button>
                  <button
                    onClick={() => handleModeSwitch("LIVE")}
                    className={`px-2.5 py-1 rounded-md text-xs font-semibold transition-all ${
                      isLiveActive
                        ? "bg-rose-500 text-white shadow-sm shadow-rose-500/30"
                        : "text-slate-400 hover:text-white"
                    }`}
                  >
                    Live
                  </button>
                </div>

                <button
                  onClick={handleOpenBrokerModal}
                  className="flex items-center gap-1 px-2.5 py-1.5 rounded-lg border border-slate-700 bg-surface-800 hover:bg-surface-750 text-white text-xs font-medium transition-all"
                >
                  <LinkIcon size={12} className="text-cyan-400" />
                  <span>
                    {connectedBrokers.length > 0
                      ? `${connectedBrokers.length} Linked`
                      : "Broker"}
                  </span>
                </button>

                <button
                  onClick={() => setIsKillSwitchOpen(true)}
                  className="flex items-center gap-1 px-2.5 py-1.5 rounded-lg bg-gradient-to-r from-red-600 to-rose-600 hover:from-red-500 hover:to-rose-500 text-white text-xs font-bold transition-all shadow-md shadow-red-600/30 ml-auto sm:ml-0"
                  title="Emergency Pause All Strategies"
                >
                  <Power size={12} />
                  <span>KILL</span>
                </button>
              </div>
            </div>

            {brokerWarning && (
              <div className="flex items-center gap-2 p-3 rounded-xl bg-amber-500/10 border border-amber-500/30 text-amber-300 text-xs animate-fade-in">
                <AlertCircle size={15} className="shrink-0 text-amber-400" />
                <span>{brokerWarning}</span>
              </div>
            )}

            <ErrorBoundary>
              <Routes>
                <Route path="/" element={<Dashboard />} />
                <Route path="/copy-trading" element={<CopyTrading />} />
                <Route path="/marketplace" element={<Marketplace />} />
                <Route path="/watchlist" element={<Watchlist />} />
                <Route path="/strategies" element={<Strategies />} />
                <Route path="/history" element={<TradeHistory />} />
                <Route path="/settings" element={<Settings />} />
                <Route path="/admin" element={<Admin />} />
                <Route path="/broker-sessions" element={<BrokerSessions />} />
              </Routes>
            </ErrorBoundary>
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
              if (connectedBrokers.length > 0) {
                setExecutionMode("LIVE");
              } else {
                setExecutionMode("PAPER");
                setIsBrokerModalOpen(true);
              }
            }}
            onConnectBroker={() => {
              setIsBrokerModalOpen(true);
            }}
          />
        </div>
      </BrowserRouter>
    </MarketProvider>
  );
}