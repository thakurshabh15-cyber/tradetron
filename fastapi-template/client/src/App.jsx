import { useState } from "react";
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

import { ShieldAlert, ShieldCheck, Power, Zap, Radio, Link as LinkIcon } from "lucide-react";

export default function App() {
  const [executionMode, setExecutionMode] = useState("PAPER"); // 'PAPER' is mandatory default
  const [isKillSwitchOpen, setIsKillSwitchOpen] = useState(false);
  const [isBrokerModalOpen, setIsBrokerModalOpen] = useState(false);
  const [isLiveOptInOpen, setIsLiveOptInOpen] = useState(false);

  const handleModeSwitch = (mode) => {
    if (mode === "LIVE") {
      setIsLiveOptInOpen(true);
    } else {
      setExecutionMode("PAPER");
    }
  };

  return (
    <BrowserRouter>
      <div className="flex min-h-screen bg-surface-950 text-slate-300 selection:bg-accent-500/20 selection:text-accent-400">
        <Sidebar />
        <main className="flex-1 lg:ml-64 pt-16 pb-20 lg:pt-6 lg:pb-8 px-4 sm:px-6 lg:px-8 space-y-6 overflow-x-hidden">
          {/* Top Execution Control & Mode Banner */}
          <div className="flex flex-col md:flex-row items-stretch md:items-center justify-between gap-3 p-3 rounded-xl bg-slate-900/90 border border-slate-800/80 shadow-lg">
            {/* Mode Indicator & Regulatory Disclaimer */}
            <div className="flex items-center gap-3">
              <div
                className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg border text-xs font-bold transition-all ${
                  executionMode === "LIVE"
                    ? "bg-rose-500/15 border-rose-500/40 text-rose-400 shadow-sm shadow-rose-500/20 animate-pulse"
                    : "bg-emerald-500/15 border-emerald-500/40 text-emerald-300 shadow-sm shadow-emerald-500/10"
                }`}
              >
                {executionMode === "LIVE" ? <Radio size={14} className="text-rose-400" /> : <Zap size={14} className="text-emerald-400" />}
                <span>{executionMode === "LIVE" ? "LIVE BROKER EXECUTION" : "PAPER TRADING (SIMULATION)"}</span>
              </div>

              <span className="hidden xl:inline-block text-[11px] text-slate-400">
                {executionMode === "LIVE"
                  ? "Real capital active across Zerodha / Angel One / Binance broker routes."
                  : "Virtual paper balance ₹10,00,000 active. No real funds at risk."}
              </span>
            </div>

            {/* Mode Switcher, Broker Link, and Emergency Kill-Switch Panic Button */}
            <div className="flex items-center gap-2">
              <div className="bg-slate-950 p-1 rounded-lg border border-slate-800 flex text-[11px] font-semibold">
                <button
                  onClick={() => handleModeSwitch("PAPER")}
                  className={`px-2.5 py-1 rounded-md transition-all ${
                    executionMode === "PAPER"
                      ? "bg-emerald-500/20 text-emerald-300 border border-emerald-500/30"
                      : "text-slate-400 hover:text-white"
                  }`}
                >
                  Paper
                </button>
                <button
                  onClick={() => handleModeSwitch("LIVE")}
                  className={`px-2.5 py-1 rounded-md transition-all ${
                    executionMode === "LIVE"
                      ? "bg-rose-500/20 text-rose-300 border border-rose-500/30"
                      : "text-slate-400 hover:text-white"
                  }`}
                >
                  Live Broker
                </button>
              </div>

              <button
                onClick={() => setIsBrokerModalOpen(true)}
                className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-750 border border-slate-700 text-white text-xs font-semibold transition-all"
              >
                <LinkIcon size={13} className="text-cyan-400" />
                <span className="hidden sm:inline">Connect Broker</span>
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
        />
        <LiveOptInModal
          isOpen={isLiveOptInOpen}
          onClose={() => setIsLiveOptInOpen(false)}
          onConfirm={() => setExecutionMode("LIVE")}
          onConnectBroker={() => setIsBrokerModalOpen(true)}
        />
      </div>
    </BrowserRouter>
  );
}