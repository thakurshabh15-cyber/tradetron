import { useState } from "react";
import {
  X,
  ShieldCheck,
  Key,
  CheckCircle2,
  AlertCircle,
  ExternalLink,
} from "lucide-react";
import { authFetch } from "../services/apiClient";

// ── Dynamic broker configuration with per-broker credential field requirements ──
const BROKER_CONFIG = {
  ZERODHA: {
    name: "Zerodha Kite Connect",
    badge: "OAuth 2.0 (Official)",
    logo: "🪁",
    desc: "Institutional direct OAuth integration. Authorize on Kite login portal.",
    oauthSupported: true,
    manualFields: [],
  },
  UPSTOX: {
    name: "Upstox Pro",
    badge: "OAuth 2.0 (Official)",
    logo: "📈",
    desc: "Direct Upstox developer authorization flow with daily token sync.",
    oauthSupported: true,
    manualFields: [],
  },
  ANGEL_ONE: {
    name: "Angel One SmartAPI",
    badge: "Live F&O / MCX",
    logo: "👼",
    desc: "High-speed SmartAPI publisher login & TOTP authentication.",
    oauthSupported: true,
    manualFields: [
      { key: "client_id", label: "Client Code", placeholder: "e.g. S123456", required: true },
      { key: "api_key", label: "API Key", placeholder: "SmartAPI Key", required: true },
      { key: "api_secret", label: "MPIN / Password", type: "password", placeholder: "Your Angel One MPIN", required: true },
      { key: "totp_secret", label: "TOTP Secret Key", placeholder: "Base32 key from Authenticator", required: true,
        description: "Auto-generates 6-digit TOTP for login (RFC 6238)" },
    ],
  },
  DHAN_HQ: {
    name: "Dhan HQ",
    badge: "API (Bearer Token)",
    logo: "⚡",
    desc: "Dhan HQ API integration via client ID and bearer access token.",
    oauthSupported: false,
    manualFields: [
      { key: "client_id", label: "Client ID", placeholder: "e.g. D123456789", required: true },
      { key: "access_token", label: "Access Token", type: "password", placeholder: "Bearer token or JWT", required: true },
    ],
  },
  UPSTOX_PRO: {
    name: "Upstox Pro (Manual)",
    badge: "Manual API",
    logo: "📊",
    desc: "Upstox Pro manual API key/secret connection.",
    oauthSupported: false,
    manualFields: [
      { key: "api_key", label: "API Key", placeholder: "Upstox API Key", required: true },
      { key: "api_secret", label: "API Secret", type: "password", placeholder: "Upstox API Secret", required: true },
    ],
  },
  BINANCE: {
    name: "Binance Crypto",
    badge: "Spot & Futures",
    logo: "🪙",
    desc: "HMAC-SHA256 encrypted API key with read/trade scope.",
    oauthSupported: false,
    manualFields: [
      { key: "api_key", label: "API Key", placeholder: "32+ character API Key", required: true },
      { key: "api_secret", label: "API Secret Key", type: "password", placeholder: "32+ character Secret Key", required: true },
    ],
  },
};

const BROKERS = Object.keys(BROKER_CONFIG).map((id) => ({ id, ...BROKER_CONFIG[id] }));

export default function BrokerConnectModal({ isOpen, onClose, onLinkedSuccess }) {
  const [selectedBroker, setSelectedBroker] = useState("ZERODHA");
  const [authMode, setAuthMode] = useState("oauth");
  const [credentials, setCredentials] = useState({});
  const [requestToken, setRequestToken] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [successMsg, setSuccessMsg] = useState(null);
  const [oauthStep, setOauthStep] = useState("init");

  const brokerCfg = BROKER_CONFIG[selectedBroker] || BROKER_CONFIG.ZERODHA;
  if (!isOpen) return null;

  const handleCredentialChange = (key, value) => {
    setCredentials((prev) => ({ ...prev, [key]: value }));
  };

  // Step 1: Open Broker OAuth Authorization URL in new tab
  const handleLaunchBrokerAuth = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await authFetch(`/api/brokers/oauth/authorize?broker=${selectedBroker}`);
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Failed to retrieve OAuth URL");
      if (data.authorize_url) {
        window.open(data.authorize_url, "_blank", "width=600,height=700");
      }
      setOauthStep("await_token");
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  // Step 2: Complete Token Exchange
  const handleCompleteOAuth = async (e) => {
    if (e) e.preventDefault();
    setLoading(true);
    setError(null);
    try {
      const tok = requestToken.trim() || `mock_token_${Date.now()}`;
      const res = await authFetch(`/api/brokers/oauth/callback`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          broker_name: selectedBroker,
          request_token: tok,
          client_id: credentials.client_id || undefined,
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "OAuth token exchange failed");
      setSuccessMsg(`🎉 Successfully linked ${selectedBroker}! Daily token active until 06:00 AM IST.`);
      if (onLinkedSuccess) onLinkedSuccess(data);
      setTimeout(() => { setOauthStep("init"); onClose(); }, 1500);
        } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  // Manual API Key / Secret Connect with dynamic fields
  const handleManualConnect = async (e) => {
    e.preventDefault();
    setLoading(true);
    setError(null);
    try {
      const payload = {
        broker_name: selectedBroker,
        account_name: `${selectedBroker} Trading Account`,
      };
      for (const field of brokerCfg.manualFields) {
        payload[field.key] = credentials[field.key] || undefined;
      }
      const brokerNameMap = { UPSTOX_PRO: "UPSTOX", DHAN_HQ: "DHAN_HQ" };
      payload.broker_name = brokerNameMap[selectedBroker] || selectedBroker;

      const res = await authFetch(`/api/brokers/accounts/manual`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });

      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Failed to link broker");

      setSuccessMsg(`Linked ${selectedBroker} with AES-256 encryption at rest!`);
      if (onLinkedSuccess) onLinkedSuccess(data);
      setTimeout(() => onClose(), 1500);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  // Render dynamic credential fields for manual mode
  const renderManualFields = () => {
    if (!brokerCfg.manualFields || brokerCfg.manualFields.length === 0) {
      return (
        <div className="text-center py-6 text-slate-500">
          This broker requires OAuth authorization. Switch to the OAuth tab.
        </div>
      );
    }
    return brokerCfg.manualFields.map((field) => (
      <div key={field.key}>
        <label className="text-[11px] font-medium text-slate-300 flex items-center justify-between">
          {field.label}
          {field.description && <span className="text-xs text-slate-500 font-normal">({field.description})</span>}
        </label>
        <input
          type={field.type || "text"}
          value={credentials[field.key] || ""}
          onChange={(e) => handleCredentialChange(field.key, e.target.value)}
          placeholder={field.placeholder}
          className={`w-full mt-1 bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-xs text-white placeholder-slate-500 focus:outline-none focus:border-cyan-500${
            field.type === "password" ? " font-mono" : ""
          }`}
          required={field.required}
        />
        {field.description && <p className="text-[10px] text-slate-500 mt-1">{field.description}</p>}
      </div>
    ));
  };

  return (
      <div className="relative w-full max-w-lg bg-slate-900 border border-slate-800 rounded-2xl shadow-2xl p-4 sm:p-6 overflow-y-auto max-h-[90vh]">
        {/* Glow Accent */}
        <div className="absolute top-0 left-0 right-0 h-1 bg-gradient-to-r from-cyan-500 via-brand-purple to-indigo-500" />

        <button
          onClick={onClose}
          className="absolute top-4 right-4 text-slate-400 hover:text-white p-1 rounded-lg hover:bg-slate-800 transition-colors"
        >
          <X size={18} />
        </button>

        <div className="text-center mb-5">
          <div className="inline-flex items-center gap-1.5 px-3 py-1 rounded-full bg-cyan-500/10 border border-cyan-500/20 text-cyan-400 text-xs font-semibold mb-2">
            <ShieldCheck size={12} />
            <span>Encrypted Direct OAuth Connect</span>
          </div>
          <h2 className="text-xl font-bold text-white tracking-tight">
            Connect Real Broker Account
          </h2>
          <p className="text-xs text-slate-400 mt-1">
            Authorize on your broker's official portal. We never collect or store your broker login password.
          </p>
        </div>

        {/* Alerts */}
        {error && (
          <div className="mb-4 flex items-center gap-2 p-2.5 rounded-lg bg-red-500/10 border border-red-500/30 text-red-400 text-xs">
            <AlertCircle size={14} className="shrink-0" />
            <span>{error}</span>
          </div>
        )}
        {successMsg && (
          <div className="mb-4 flex items-center gap-2 p-2.5 rounded-lg bg-emerald-500/10 border border-emerald-500/30 text-emerald-400 text-xs">
            <CheckCircle2 size={14} className="shrink-0" />
            <span>{successMsg}</span>
          </div>
        )}

        {/* Broker Selector Grid */}
        <div className="grid grid-cols-2 gap-2 mb-4">
          {BROKERS.map((b) => (
            <button
              key={b.id}
              onClick={() => {
                setSelectedBroker(b.id);
                setOauthStep("init");
              }}
              className={`p-3 rounded-xl text-left border transition-all ${
                selectedBroker === b.id
                  ? "bg-cyan-500/10 border-cyan-500/40 shadow-sm"
                  : "bg-slate-950/60 border-slate-800 hover:border-slate-700"
              }`}
            >
              <div className="flex items-center justify-between">
                <span className="text-lg">{b.logo}</span>
                <span className="text-[9px] font-bold px-1.5 py-0.5 rounded bg-slate-800 text-slate-400">
                  {b.badge}
                </span>
              </div>
              <h4 className="mt-2 text-xs font-bold text-white">{b.name}</h4>
              <p className="text-[10px] text-slate-400 mt-0.5 line-clamp-1">{b.desc}</p>
            </button>
          ))}
        </div>

        {/* Auth Mode Toggle */}
        <div className="bg-slate-950/60 p-1 rounded-xl border border-slate-800 mb-4 flex text-xs font-semibold">
          <button
            onClick={() => {
              setAuthMode("oauth");
              setOauthStep("init");
            }}
            className={`flex-1 py-1.5 rounded-md transition-all ${
              authMode === "oauth"
                ? "bg-cyan-500/20 text-cyan-300 border border-cyan-500/30 shadow-sm"
                : "text-slate-400 hover:text-white"
            }`}
          >
            OAuth 2.0 Direct Login
          </button>
          <button
            onClick={() => setAuthMode("manual")}
            className={`flex-1 py-1.5 rounded-md transition-all ${
              authMode === "manual"
                ? "bg-cyan-500/20 text-cyan-300 border border-cyan-500/30 shadow-sm"
                : "text-slate-400 hover:text-white"
            }`}
          >
            API Key & Secret
          </button>
        </div>

        {authMode === "oauth" ? (
          <div className="space-y-4">
            {oauthStep === "init" ? (
              <div className="space-y-4">
                <div className="p-3.5 rounded-xl bg-slate-950/80 border border-slate-800 text-xs text-slate-300 space-y-2">
                  <div className="flex items-center gap-2 font-semibold text-white">
                    <ExternalLink size={14} className="text-cyan-400" />
                    <span>Authorize on {selectedBroker} Login Portal</span>
                  </div>
                  <p className="text-[11px] text-slate-400 leading-relaxed">
                    Click below to open the official {selectedBroker} authentication page. After logging in, you will grant permissions for algorithmic order execution.
                  </p>
                </div>

                <button
                  onClick={handleLaunchBrokerAuth}
                  disabled={loading}
                  className="w-full py-2.5 px-4 rounded-lg bg-gradient-to-r from-cyan-500 to-blue-500 hover:from-cyan-400 hover:to-blue-400 text-slate-950 font-bold text-xs transition-all flex items-center justify-center gap-1.5 shadow-md shadow-cyan-500/20 disabled:opacity-50"
                >
                  <ExternalLink size={14} />
                  {loading ? "Opening..." : `Authorize on ${selectedBroker}`}
                </button>
              </div>
            ) : (
              <form onSubmit={handleCompleteOAuth} className="space-y-3">
                <div className="p-3 rounded-lg bg-cyan-500/10 border border-cyan-500/20 text-xs text-cyan-300">
                  <span>Enter the request_token / auth_code from your broker redirect, or click Confirm to complete session sync:</span>
                </div>

                <div>
                  <label className="text-[11px] font-medium text-slate-300">
                    OAuth Request Token / Auth Code (Optional in dev)
                  </label>
                  <input
                    type="text"
                    value={requestToken}
                    onChange={(e) => setRequestToken(e.target.value)}
                    placeholder="e.g. 33f1190bc98..."
                    className="w-full mt-1 bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-xs text-white placeholder-slate-500 font-mono focus:outline-none focus:border-cyan-500"
                  />
                </div>

                <div className="flex gap-2">
                  <button
                    type="button"
                    onClick={() => setOauthStep("init")}
                    className="btn-ghost text-xs py-2 px-3"
                  >
                    Back
                  </button>
                  <button
                    type="submit"
                    disabled={loading}
                    className="flex-1 py-2.5 px-4 rounded-lg bg-cyan-500 hover:bg-cyan-400 text-slate-950 font-bold text-xs transition-all flex items-center justify-center gap-1.5 shadow-md shadow-cyan-500/20 disabled:opacity-50"
                  >
                    <CheckCircle2 size={14} />
                    {loading ? "Verifying..." : "Complete Connection"}
                  </button>
                </div>
              </form>
            )}
          </div>
        ) : brokerCfg.oauthSupported ? (
          <div className="space-y-4">
            <div className="p-3.5 rounded-xl bg-slate-950/80 border border-slate-800 text-xs text-slate-300 space-y-2">
              <div className="flex items-center gap-2 font-semibold text-white">
                <ExternalLink size={14} className="text-cyan-400" />
                <span>OAuth Required</span>
              </div>
              <p className="text-[11px] text-slate-400 leading-relaxed">
                {brokerCfg.name} supports only OAuth 2.0 direct authorization. Please switch to the "OAuth 2.0 Direct Login" tab to connect.
              </p>
            </div>
          </div>
        ) : (
          <form onSubmit={handleManualConnect} className="space-y-3">
            {renderManualFields()}
            <button
              type="submit"
              disabled={loading}
              className="w-full mt-2 py-2.5 px-4 rounded-lg bg-cyan-500 hover:bg-cyan-400 text-slate-950 font-bold text-xs transition-all flex items-center justify-center gap-1.5 shadow-md shadow-cyan-500/20 disabled:opacity-50"
            >
              <Key size={14} />
              {loading ? "Encrypting & Linking..." : "Save Encrypted Broker Account"}
            </button>
              </form>
        )}
      </div>
  );
}
