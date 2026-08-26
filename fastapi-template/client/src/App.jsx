import { useState, useEffect, useCallback, lazy, Suspense } from "react";
import { BrowserRouter, Routes, Route, Navigate, useLocation, useNavigate } from "react-router-dom";
import Sidebar from "./components/Sidebar";
import BottomNav from "./components/BottomNav";
import KillSwitchModal from "./components/KillSwitchModal";
import BrokerConnectModal from "./components/BrokerConnectModal";
import LiveOptInModal from "./components/LiveOptInModal";
import KYCModal from "./components/KYCModal";
import ErrorBoundary from "./components/ErrorBoundary";
import AuthModal from "./components/AuthModal";
import { MarketProvider } from "./context/MarketContext";
import { authFetch } from "./services/apiClient";
import { useAuthStore } from "./stores/useAuthStore";
import { ToastProvider } from "./components/Toast";
import CommandPalette from "./components/CommandPalette";
import DataEngineChip from "./components/DataEngineChip";

// Route-level code splitting: each terminal screen ships as its own chunk,
// keeping the critical execution-shell bundle lean on first paint.
const Dashboard = lazy(() => import("./pages/Dashboard"));
const CopyTrading = lazy(() => import("./pages/CopyTrading"));
const Marketplace = lazy(() => import("./pages/Marketplace"));
const Strategies = lazy(() => import("./pages/Strategies"));
const TradeHistory = lazy(() => import("./pages/TradeHistory"));
const Watchlist = lazy(() => import("./pages/Watchlist"));
const Settings = lazy(() => import("./pages/Settings"));
const Admin = lazy(() => import("./pages/Admin"));
const BrokerSessions = lazy(() => import("./pages/BrokerSessions"));
const Pricing = lazy(() => import("./pages/Pricing"));
const VisualBuilder = lazy(() => import("./pages/VisualBuilder"));
const QuantLab = lazy(() => import("./pages/QuantLab"));
const BacktestLab = lazy(() => import("./pages/BacktestLab"));
const Markets = lazy(() => import("./pages/Markets"));
const MarketDetail = lazy(() => import("./pages/MarketDetail"));
const PortfolioPage = lazy(() => import("./pages/Portfolio"));
const ExecutionPage = lazy(() => import("./pages/Execution"));
const KYC = lazy(() => import("./pages/KYC"));
const TradeJournal = lazy(() => import("./pages/TradeJournal"));

import { Power, Link as LinkIcon, AlertCircle, FileCheck, Radio, Zap } from "lucide-react";

/** Full-viewport skeleton shown while a lazily-loaded route chunk streams in */
function RouteFallback() {
  return (
    <div className="flex min-h-[60vh] items-center justify-center" role="status" aria-label="Loading module">
      <div className="flex flex-col items-center gap-3">
        <div className="h-10 w-10 rounded-full border-2 border-brand-purple/30 border-t-brand-purple animate-spin" />
        <p className="font-mono text-[11px] uppercase tracking-widest text-slate-500">Loading module…</p>
      </div>
    </div>
  );
}

function ProtectedRoute({ children }) {
  const isAuthenticated = useAuthStore((state) => state.isAuthenticated);
  const isLoading = useAuthStore((state) => state.isLoading);
  const location = useLocation();

  if (isLoading) return <div className="p-8 text-sm text-slate-400">Checking your session...</div>;
  if (!isAuthenticated) {
    return <Navigate to="/login" replace state={{ notice: "Please sign in to continue." , from: location.pathname }} />;
  }
  return children;
}

function LoginScreen() {
  const navigate = useNavigate();
  const location = useLocation();
  const isAuthenticated = useAuthStore((state) => state.isAuthenticated);
  const setAuthData = useAuthStore((state) => state.setAuthData);

  if (isAuthenticated) {
    return <Navigate to={location.state?.from || "/dashboard"} replace />;
  }

  return (
    <AuthModal
      isOpen
      onClose={() => navigate("/dashboard", { replace: true })}
      onAuthSuccess={(_, data) => {
        setAuthData(data);
        navigate(location.state?.from || "/dashboard", { replace: true });
      }}
      notice={location.state?.notice}
    />
  );
}

function AppShell() {
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

  const isAuthenticated = useAuthStore((state) => state.isAuthenticated);

  useEffect(() => {
    if (!isAuthenticated) return undefined;
    fetchBrokers();
    fetchKYCStatus();
    fetchBalances();
    const interval = setInterval(fetchBalances, 8000);
    return () => clearInterval(interval);
  }, [fetchBrokers, fetchKYCStatus, fetchBalances, isAuthenticated]);

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
    <BrowserRouter>
      <div className="flex min-h-screen bg-surface-950 text-slate-300 selection:bg-accent-500/20 selection:text-accent-400">
          <Sidebar onOpenKYC={() => setIsKYCModalOpen(true)} kycStatus={kycStatus} />
          <main className="flex-1 md:ml-64 pt-16 pb-24 md:pt-6 md:pb-8 px-3.5 sm:px-6 md:px-8 space-y-6 overflow-x-hidden w-full max-w-full">
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
              <Suspense fallback={<RouteFallback />}>
                <Routes>
                  <Route path="/" element={<Dashboard />} />
                  <Route path="/dashboard" element={<Dashboard />} />
                  <Route path="/login" element={<LoginScreen />} />
                  <Route path="/copy-trading" element={<ProtectedRoute><CopyTrading /></ProtectedRoute>} />
                  <Route path="/marketplace" element={<Marketplace />} />
                  <Route path="/watchlist" element={<Watchlist />} />
                  <Route path="/strategies" element={<Strategies />} />
                  <Route path="/history" element={<TradeHistory />} />
                  <Route path="/settings" element={<ProtectedRoute><Settings /></ProtectedRoute>} />
                  <Route path="/admin" element={<Admin />} />
                  <Route path="/broker-sessions" element={<BrokerSessions />} />
                  <Route path="/pricing" element={<Pricing />} />
                  <Route path="/visual-builder" element={<ProtectedRoute><VisualBuilder /></ProtectedRoute>} />
                  <Route path="/quant-lab" element={<ProtectedRoute><QuantLab /></ProtectedRoute>} />
                  <Route path="/backtest" element={<BacktestLab />} />
                <Route path="/command-center" element={<Dashboard />} />
                <Route path="/markets" element={<Markets />} />
                <Route path="/markets/:symbol" element={<MarketDetail />} />
                <Route path="/portfolio" element={<ProtectedRoute><PortfolioPage /></ProtectedRoute>} />
                <Route path="/execution" element={<ProtectedRoute><ExecutionPage /></ProtectedRoute>} />
                <Route path="/risk-center" element={<ProtectedRoute><ExecutionPage /></ProtectedRoute>} />
                <Route path="/kyc" element={<KYC />} />
                <Route path="/trade-journal" element={<ProtectedRoute><TradeJournal /></ProtectedRoute>} />
                <Route path="/reality-mode" element={<BacktestLab />} />
                </Routes>
              </Suspense>
            </ErrorBoundary>
          </main>
          <DataEngineChip />
          <CommandPalette />
          <BottomNav />

          {/* Global Modals */}
          <KillSwitchModal
            isOpen={isKillSwitchOpen}
            onClose={() => setIsKillSwitchOpen(false)}
            onKillSuccess={() => {
              fetchBrokers();
              fetchBalances();
            }}
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
  );
}

/**
 * Application root — mounts the global market data provider and the
 * institutional toast notification layer above the entire router tree,
 * so every page, modal and layout component can raise notifications.
 */
export default function App() {
  return (
    <MarketProvider>
      <ToastProvider>
        <AppShell />
      </ToastProvider>
    </MarketProvider>
  );
}