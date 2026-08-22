import { useState } from "react";
import { X, Lock, Mail, User, ShieldCheck, ArrowRight, CheckCircle2, AlertCircle, KeyRound, Smartphone } from "lucide-react";
import { triggerOAuthFlow } from "../services/oauth";
import { API_BASE, setTokens } from "../services/apiClient";

export default function AuthModal({ isOpen, onClose, onAuthSuccess }) {
  const [tab, setTab] = useState("login"); // 'login' | 'register' | 'register_verify' | 'otp' | 'forgot_password'
  const [email, setEmail] = useState("");
  const [phone, setPhone] = useState("");
  const [password, setPassword] = useState("");
  const [fullName, setFullName] = useState("");
  const [rememberMe, setRememberMe] = useState(false);
  const [otpCode, setOtpCode] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [otpSent, setOtpSent] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [successMsg, setSuccessMsg] = useState(null);

  if (!isOpen) return null;

  const handleSaveTokens = (data) => {
    setTokens(data);
    if (onAuthSuccess) onAuthSuccess(data.user);
    onClose();
  };

  const handlePasswordLogin = async (e) => {
    e.preventDefault();
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`${API_BASE}/api/auth/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          identifier: email.trim(),
          password,
          remember_me: rememberMe,
        }),
      });

      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Authentication failed");

      if (data.two_factor_required) {
        setSuccessMsg("2FA required. Please enter authenticator code.");
        setTab("otp");
        setOtpSent(true);
        return;
      }

      setSuccessMsg("Logged in successfully!");
      setTimeout(() => handleSaveTokens(data), 500);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  const handleRegister = async (e) => {
    e.preventDefault();
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`${API_BASE}/api/auth/register`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          email: email.trim(),
          phone: phone.trim() || null,
          password,
          full_name: fullName.trim() || "Trader",
        }),
      });

      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Registration failed");

      // Check if user is already verified or needs OTP
      if (data.user?.is_verified) {
        setSuccessMsg("Account created and activated successfully!");
        setTimeout(() => handleSaveTokens(data), 600);
      } else {
        setSuccessMsg(`Verification code sent to ${email.trim()}!`);
        setTab("register_verify");
        setOtpCode("");
      }
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  const handleVerifyRegistration = async (e) => {
    e.preventDefault();
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`${API_BASE}/api/auth/verify-registration`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          identifier: email.trim(),
          otp_code: otpCode.trim(),
        }),
      });

      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Verification failed");

      setSuccessMsg("Account verified & activated! Logging in...");
      setTimeout(() => handleSaveTokens(data), 600);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  const handleRequestOtp = async (e) => {
    e.preventDefault();
    if (!email) {
      setError("Please enter your email or phone");
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`${API_BASE}/api/auth/request-otp`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ identifier: email.trim() }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Failed to send OTP");
      setOtpSent(true);
      setSuccessMsg(`6-digit verification code sent to ${email.trim()}`);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  const handleVerifyOtp = async (e) => {
    e.preventDefault();
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`${API_BASE}/api/auth/verify-otp`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          identifier: email.trim(),
          otp_code: otpCode.trim(),
          full_name: fullName || "Trader",
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Invalid OTP code");

      setSuccessMsg("OTP Verified! Logging in...");
      setTimeout(() => handleSaveTokens(data), 600);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  const handleForgotPassword = async (e) => {
    e.preventDefault();
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`${API_BASE}/api/auth/forgot-password`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ identifier: email.trim() }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Request failed");
      setOtpSent(true);
      setSuccessMsg("Password reset code sent to your registered email!");
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  const handleResetPassword = async (e) => {
    e.preventDefault();
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`${API_BASE}/api/auth/reset-password`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          identifier: email.trim(),
          otp_code: otpCode.trim(),
          new_password: newPassword,
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Failed to reset password");
      setSuccessMsg("Password updated successfully! You can now log in.");
      setTimeout(() => {
        setTab("login");
        setOtpSent(false);
        setOtpCode("");
        setNewPassword("");
      }, 1200);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  const handleOAuth = async (provider) => {
    setLoading(true);
    setError(null);
    try {
      const authResult = await triggerOAuthFlow(provider);
      setSuccessMsg(`Signed in with ${provider.toUpperCase()}!`);
      setTimeout(() => handleSaveTokens(authResult), 600);
    } catch (err) {
      setError(err.message || `Failed to sign in with ${provider}`);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/80 backdrop-blur-sm animate-fade-in">
      <div className="relative w-full max-w-md bg-slate-900 border border-slate-800 rounded-2xl shadow-2xl p-6 overflow-hidden">
        {/* Glow Header Accent */}
        <div className="absolute top-0 left-0 right-0 h-1 bg-gradient-to-r from-cyan-500 via-blue-500 to-indigo-500" />

        {/* Close Button */}
        <button
          onClick={onClose}
          className="absolute top-4 right-4 text-slate-400 hover:text-white p-1 rounded-lg hover:bg-slate-800 transition-colors"
        >
          <X size={18} />
        </button>

        {/* Title / Badge */}
        <div className="text-center mb-5">
          <div className="inline-flex items-center gap-1.5 px-3 py-1 rounded-full bg-cyan-500/10 border border-cyan-500/20 text-cyan-400 text-xs font-semibold mb-2">
            <Lock size={12} />
            <span>Tradetron Secure Auth</span>
          </div>
          <h2 className="text-xl font-bold text-white tracking-tight">
            {tab === "forgot_password"
              ? "Reset Password"
              : tab === "register"
              ? "Create Trader Account"
              : tab === "register_verify"
              ? "Verify Account"
              : tab === "otp"
              ? "OTP Authentication"
              : "Welcome Back"}
          </h2>
          <p className="text-xs text-slate-400 mt-1">
            {tab === "register_verify"
              ? `Enter the 6-digit code sent to ${email}`
              : tab === "forgot_password"
              ? "Enter your email or phone to reset your password"
              : "Institutional grade algorithmic execution & strategy marketplace"}
          </p>
        </div>

        {/* Navigation Tabs */}
        {tab !== "forgot_password" && tab !== "register_verify" && (
          <div className="bg-slate-950/60 p-1 rounded-xl border border-slate-800 mb-5">
            <div className="flex text-xs font-semibold">
              <button
                onClick={() => { setTab("login"); setError(null); setSuccessMsg(null); }}
                className={`flex-1 py-1.5 rounded-md transition-all ${
                  tab === "login" ? "bg-cyan-500/20 text-cyan-300 shadow-sm border border-cyan-500/30" : "text-slate-400 hover:text-white"
                }`}
              >
                Sign In
              </button>
              <button
                onClick={() => { setTab("otp"); setError(null); setSuccessMsg(null); }}
                className={`flex-1 py-1.5 rounded-md transition-all ${
                  tab === "otp" ? "bg-cyan-500/20 text-cyan-300 shadow-sm border border-cyan-500/30" : "text-slate-400 hover:text-white"
                }`}
              >
                OTP Login
              </button>
              <button
                onClick={() => { setTab("register"); setError(null); setSuccessMsg(null); }}
                className={`flex-1 py-1.5 rounded-md transition-all ${
                  tab === "register" ? "bg-cyan-500/20 text-cyan-300 shadow-sm border border-cyan-500/30" : "text-slate-400 hover:text-white"
                }`}
              >
                Register
              </button>
            </div>
          </div>
        )}

        {/* Feedback Alerts */}
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

        {/* 1. PASSWORD LOGIN */}
        {tab === "login" && (
          <form onSubmit={handlePasswordLogin} className="space-y-3">
            <div>
              <label className="text-[11px] font-medium text-slate-300">Email Address or Phone</label>
              <div className="relative mt-1">
                <Mail size={14} className="absolute left-3 top-3 text-slate-500" />
                <input
                  type="text"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  placeholder="trader@tradetron.io"
                  className="w-full bg-slate-800 border border-slate-700 rounded-lg pl-9 pr-3 py-2 text-xs text-white placeholder-slate-500 focus:outline-none focus:border-cyan-500"
                  required
                />
              </div>
            </div>

            <div>
              <div className="flex items-center justify-between">
                <label className="text-[11px] font-medium text-slate-300">Password</label>
                <button
                  type="button"
                  onClick={() => { setTab("forgot_password"); setError(null); setSuccessMsg(null); }}
                  className="text-[10px] text-cyan-400 hover:underline"
                >
                  Forgot Password?
                </button>
              </div>
              <div className="relative mt-1">
                <Lock size={14} className="absolute left-3 top-3 text-slate-500" />
                <input
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder="••••••••"
                  className="w-full bg-slate-800 border border-slate-700 rounded-lg pl-9 pr-3 py-2 text-xs text-white placeholder-slate-500 focus:outline-none focus:border-cyan-500"
                  required
                />
              </div>
            </div>

            <div className="flex items-center gap-2 pt-0.5">
              <input
                type="checkbox"
                id="rememberMe"
                checked={rememberMe}
                onChange={(e) => setRememberMe(e.target.checked)}
                className="w-3.5 h-3.5 rounded bg-slate-800 border-slate-700 text-cyan-500 focus:ring-0 focus:ring-offset-0 cursor-pointer"
              />
              <label htmlFor="rememberMe" className="text-[11px] text-slate-400 cursor-pointer select-none">
                Remember me (30-day session)
              </label>
            </div>

            <button
              type="submit"
              disabled={loading}
              className="w-full mt-2 py-2.5 px-4 rounded-lg bg-cyan-500 hover:bg-cyan-400 text-slate-950 font-bold text-xs transition-all flex items-center justify-center gap-1.5 shadow-md shadow-cyan-500/20 disabled:opacity-50"
            >
              {loading ? "Authenticating..." : "Sign In with Password"}
              <ArrowRight size={14} />
            </button>
          </form>
        )}

        {/* 2. REGISTRATION */}
        {tab === "register" && (
          <form onSubmit={handleRegister} className="space-y-3">
            <div>
              <label className="text-[11px] font-medium text-slate-300">Full Name</label>
              <div className="relative mt-1">
                <User size={14} className="absolute left-3 top-3 text-slate-500" />
                <input
                  type="text"
                  value={fullName}
                  onChange={(e) => setFullName(e.target.value)}
                  placeholder="Trader Name"
                  className="w-full bg-slate-800 border border-slate-700 rounded-lg pl-9 pr-3 py-2 text-xs text-white placeholder-slate-500 focus:outline-none focus:border-cyan-500"
                  required
                />
              </div>
            </div>

            <div>
              <label className="text-[11px] font-medium text-slate-300">Email Address</label>
              <div className="relative mt-1">
                <Mail size={14} className="absolute left-3 top-3 text-slate-500" />
                <input
                  type="email"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  placeholder="trader@tradetron.io"
                  className="w-full bg-slate-800 border border-slate-700 rounded-lg pl-9 pr-3 py-2 text-xs text-white placeholder-slate-500 focus:outline-none focus:border-cyan-500"
                  required
                />
              </div>
            </div>

            <div>
              <label className="text-[11px] font-medium text-slate-300">Mobile Phone (Optional for SMS OTP)</label>
              <div className="relative mt-1">
                <Smartphone size={14} className="absolute left-3 top-3 text-slate-500" />
                <input
                  type="text"
                  value={phone}
                  onChange={(e) => setPhone(e.target.value)}
                  placeholder="+91 98765 43210"
                  className="w-full bg-slate-800 border border-slate-700 rounded-lg pl-9 pr-3 py-2 text-xs text-white placeholder-slate-500 focus:outline-none focus:border-cyan-500"
                />
              </div>
            </div>

            <div>
              <label className="text-[11px] font-medium text-slate-300">Password</label>
              <div className="relative mt-1">
                <Lock size={14} className="absolute left-3 top-3 text-slate-500" />
                <input
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder="Minimum 6 characters"
                  className="w-full bg-slate-800 border border-slate-700 rounded-lg pl-9 pr-3 py-2 text-xs text-white placeholder-slate-500 focus:outline-none focus:border-cyan-500"
                  required
                  minLength={6}
                />
              </div>
            </div>

            <button
              type="submit"
              disabled={loading}
              className="w-full mt-2 py-2.5 px-4 rounded-lg bg-cyan-500 hover:bg-cyan-400 text-slate-950 font-bold text-xs transition-all flex items-center justify-center gap-1.5 shadow-md shadow-cyan-500/20 disabled:opacity-50"
            >
              {loading ? "Creating Account..." : "Create Account & Send OTP"}
              <ArrowRight size={14} />
            </button>
          </form>
        )}

        {/* 3. REGISTRATION OTP VERIFICATION */}
        {tab === "register_verify" && (
          <form onSubmit={handleVerifyRegistration} className="space-y-3 animate-fade-in">
            <div>
              <label className="text-[11px] font-medium text-slate-300">Enter 6-Digit Activation Code</label>
              <div className="relative mt-1">
                <ShieldCheck size={14} className="absolute left-3 top-3 text-slate-500" />
                <input
                  type="text"
                  maxLength={8}
                  value={otpCode}
                  onChange={(e) => setOtpCode(e.target.value)}
                  placeholder="Enter code"
                  className="w-full bg-slate-800 border border-slate-700 rounded-lg pl-9 pr-3 py-2 text-center text-sm font-mono tracking-widest text-white placeholder-slate-500 focus:outline-none focus:border-cyan-500"
                  required
                  autoFocus
                />
              </div>
            </div>

            <button
              type="submit"
              disabled={loading}
              className="w-full mt-2 py-2.5 px-4 rounded-lg bg-emerald-500 hover:bg-emerald-400 text-slate-950 font-bold text-xs transition-all flex items-center justify-center gap-1.5 shadow-md shadow-emerald-500/20 disabled:opacity-50"
            >
              {loading ? "Verifying..." : "Verify & Activate Account"}
              <CheckCircle2 size={14} />
            </button>

            <button
              type="button"
              onClick={() => { setTab("register"); setOtpCode(""); }}
              className="w-full text-center text-[11px] text-slate-400 hover:text-cyan-400 transition-colors"
            >
              Change Email / Back
            </button>
          </form>
        )}

        {/* 4. OTP LOGIN FLOW */}
        {tab === "otp" && (
          <div className="space-y-3">
            {!otpSent ? (
              <form onSubmit={handleRequestOtp} className="space-y-3">
                <div>
                  <label className="text-[11px] font-medium text-slate-300">Email or Phone</label>
                  <div className="relative mt-1">
                    <Mail size={14} className="absolute left-3 top-3 text-slate-500" />
                    <input
                      type="text"
                      value={email}
                      onChange={(e) => setEmail(e.target.value)}
                      placeholder="trader@tradetron.io or +919876543210"
                      className="w-full bg-slate-800 border border-slate-700 rounded-lg pl-9 pr-3 py-2 text-xs text-white placeholder-slate-500 focus:outline-none focus:border-cyan-500"
                      required
                    />
                  </div>
                </div>

                <button
                  type="submit"
                  disabled={loading}
                  className="w-full mt-2 py-2.5 px-4 rounded-lg bg-cyan-500 hover:bg-cyan-400 text-slate-950 font-bold text-xs transition-all flex items-center justify-center gap-1.5 shadow-md shadow-cyan-500/20 disabled:opacity-50"
                >
                  {loading ? "Sending OTP..." : "Request 6-Digit OTP"}
                  <ArrowRight size={14} />
                </button>
              </form>
            ) : (
              <form onSubmit={handleVerifyOtp} className="space-y-3 animate-fade-in">
                <div>
                  <label className="text-[11px] font-medium text-slate-300">Enter 6-Digit Code</label>
                  <div className="relative mt-1">
                    <ShieldCheck size={14} className="absolute left-3 top-3 text-slate-500" />
                    <input
                      type="text"
                      maxLength={8}
                      value={otpCode}
                      onChange={(e) => setOtpCode(e.target.value)}
                      placeholder="Enter code"
                      className="w-full bg-slate-800 border border-slate-700 rounded-lg pl-9 pr-3 py-2 text-center text-sm font-mono tracking-widest text-white placeholder-slate-500 focus:outline-none focus:border-cyan-500"
                      required
                      autoFocus
                    />
                  </div>
                </div>

                <button
                  type="submit"
                  disabled={loading}
                  className="w-full mt-2 py-2.5 px-4 rounded-lg bg-cyan-500 hover:bg-cyan-400 text-slate-950 font-bold text-xs transition-all flex items-center justify-center gap-1.5 shadow-md shadow-cyan-500/20 disabled:opacity-50"
                >
                  {loading ? "Verifying..." : "Verify & Log In"}
                  <CheckCircle2 size={14} />
                </button>

                <button
                  type="button"
                  onClick={() => setOtpSent(false)}
                  className="w-full text-center text-[11px] text-slate-400 hover:text-cyan-400 transition-colors"
                >
                  Change Email / Resend
                </button>
              </form>
            )}
          </div>
        )}

        {/* 5. FORGOT / RESET PASSWORD FLOW */}
        {tab === "forgot_password" && (
          <div className="space-y-3">
            {!otpSent ? (
              <form onSubmit={handleForgotPassword} className="space-y-3">
                <div>
                  <label className="text-[11px] font-medium text-slate-300">Registered Email or Phone</label>
                  <div className="relative mt-1">
                    <Mail size={14} className="absolute left-3 top-3 text-slate-500" />
                    <input
                      type="text"
                      value={email}
                      onChange={(e) => setEmail(e.target.value)}
                      placeholder="trader@tradetron.io"
                      className="w-full bg-slate-800 border border-slate-700 rounded-lg pl-9 pr-3 py-2 text-xs text-white placeholder-slate-500 focus:outline-none focus:border-cyan-500"
                      required
                    />
                  </div>
                </div>

                <button
                  type="submit"
                  disabled={loading}
                  className="w-full mt-2 py-2.5 px-4 rounded-lg bg-cyan-500 hover:bg-cyan-400 text-slate-950 font-bold text-xs transition-all flex items-center justify-center gap-1.5 shadow-md shadow-cyan-500/20 disabled:opacity-50"
                >
                  {loading ? "Sending Reset Code..." : "Send Reset Code"}
                  <ArrowRight size={14} />
                </button>

                <button
                  type="button"
                  onClick={() => { setTab("login"); setError(null); setSuccessMsg(null); }}
                  className="w-full text-center text-[11px] text-slate-400 hover:text-white transition-colors"
                >
                  Back to Sign In
                </button>
              </form>
            ) : (
              <form onSubmit={handleResetPassword} className="space-y-3 animate-fade-in">
                <div>
                  <label className="text-[11px] font-medium text-slate-300">Enter 6-Digit Code</label>
                  <div className="relative mt-1">
                    <ShieldCheck size={14} className="absolute left-3 top-3 text-slate-500" />
                    <input
                      type="text"
                      maxLength={8}
                      value={otpCode}
                      onChange={(e) => setOtpCode(e.target.value)}
                      placeholder="Enter code"
                      className="w-full bg-slate-800 border border-slate-700 rounded-lg pl-9 pr-3 py-2 text-center text-sm font-mono tracking-widest text-white placeholder-slate-500 focus:outline-none focus:border-cyan-500"
                      required
                    />
                  </div>
                </div>

                <div>
                  <label className="text-[11px] font-medium text-slate-300">New Password</label>
                  <div className="relative mt-1">
                    <KeyRound size={14} className="absolute left-3 top-3 text-slate-500" />
                    <input
                      type="password"
                      value={newPassword}
                      onChange={(e) => setNewPassword(e.target.value)}
                      placeholder="Minimum 6 characters"
                      className="w-full bg-slate-800 border border-slate-700 rounded-lg pl-9 pr-3 py-2 text-xs text-white placeholder-slate-500 focus:outline-none focus:border-cyan-500"
                      required
                      minLength={6}
                    />
                  </div>
                </div>

                <button
                  type="submit"
                  disabled={loading}
                  className="w-full mt-2 py-2.5 px-4 rounded-lg bg-cyan-500 hover:bg-cyan-400 text-slate-950 font-bold text-xs transition-all flex items-center justify-center gap-1.5 shadow-md shadow-cyan-500/20 disabled:opacity-50"
                >
                  {loading ? "Updating..." : "Reset Password & Continue"}
                  <CheckCircle2 size={14} />
                </button>

                <button
                  type="button"
                  onClick={() => setOtpSent(false)}
                  className="w-full text-center text-[11px] text-slate-400 hover:text-cyan-400 transition-colors"
                >
                  Resend Code / Change Email
                </button>
              </form>
            )}
          </div>
        )}

        {/* 6. OAUTH DIVIDER & SOCIAL BUTTONS */}
        {tab !== "forgot_password" && tab !== "register_verify" && (
          <>
            <div className="relative my-4">
              <div className="absolute inset-0 flex items-center">
                <div className="w-full border-t border-slate-800" />
              </div>
              <div className="relative flex justify-center text-[10px] uppercase">
                <span className="bg-slate-900 px-2 text-slate-500">Or continue with</span>
              </div>
            </div>

            <div className="grid grid-cols-2 gap-2">
              <button
                onClick={() => handleOAuth("google")}
                disabled={loading}
                className="flex items-center justify-center gap-2 py-2 px-3 rounded-lg bg-slate-800 hover:bg-slate-750 border border-slate-700 text-white text-xs font-semibold transition-all hover:border-slate-600 disabled:opacity-50"
              >
                <svg className="w-3.5 h-3.5" viewBox="0 0 24 24">
                  <path
                    fill="#EA4335"
                    d="M12 5c1.6 0 3 .6 4.1 1.7l3.1-3.1C17.3 1.8 14.8 1 12 1 7.5 1 3.7 3.6 1.9 7.3l3.7 2.9C6.5 7.3 9 5 12 5z"
                  />
                  <path
                    fill="#4285F4"
                    d="M23.5 12.3c0-.8-.1-1.6-.2-2.3H12v4.5h6.5c-.3 1.5-1.1 2.8-2.4 3.7l3.7 2.9c2.2-2 3.7-5 3.7-8.8z"
                  />
                  <path
                    fill="#FBBC05"
                    d="M5.6 14.8c-.2-.7-.4-1.5-.4-2.3s.2-1.6.4-2.3L1.9 7.3C.7 9.7 0 12.3 0 15s.7 5.3 1.9 7.7l3.7-2.9z"
                  />
                  <path
                    fill="#34A853"
                    d="M12 23c3.2 0 6-1.1 8-3l-3.7-2.9c-1.1.7-2.5 1.2-4.3 1.2-3 0-5.5-2.3-6.4-5.2L1.9 16C3.7 19.7 7.5 23 12 23z"
                  />
                </svg>
                Google
              </button>
              <button
                onClick={() => handleOAuth("apple")}
                disabled={loading}
                className="flex items-center justify-center gap-2 py-2 px-3 rounded-lg bg-slate-800 hover:bg-slate-750 border border-slate-700 text-white text-xs font-semibold transition-all hover:border-slate-600 disabled:opacity-50"
              >
                <svg className="w-3.5 h-3.5 fill-current" viewBox="0 0 170 170">
                  <path d="M150.37 130.25c-2.45 5.66-5.35 10.87-8.71 15.66-4.58 6.53-8.33 11.05-11.22 13.56-4.48 4.12-9.28 6.23-14.42 6.35-3.69 0-8.14-1.05-13.32-3.18-5.19-2.12-9.97-3.17-14.34-3.17-4.58 0-9.49 1.05-14.75 3.17-5.26 2.13-9.5 3.24-12.74 3.35-4.35.13-9.16-1.9-14.42-6.08-3.69-3.06-7.76-7.96-12.2-14.71-6.19-9.45-11.04-20.2-14.54-32.26-3.51-12.06-5.26-23.2-5.26-33.42 0-14.97 3.82-27.26 11.45-36.87 7.63-9.61 17.02-14.52 28.17-14.75 4.88 0 10.37 1.25 16.48 3.76 6.11 2.51 10.18 3.83 12.2 3.96 1.8.13 6.06-1.23 12.78-4.08 6.72-2.85 12.2-4.07 16.45-3.66 12.73.91 22.84 5.75 30.34 14.52-11.08 6.72-16.51 15.91-16.29 27.57.22 9.07 3.73 16.74 10.53 23 6.8 6.26 14.79 9.8 23.97 10.63-2.14 6.55-4.8 13.06-7.98 19.53zM119.22 33.15c0-7.39 2.65-14.28 7.95-20.67 5.3-6.39 11.77-10.42 19.41-12.08.11 1.22.17 2.22.17 3 0 7.39-2.78 14.36-8.34 20.91-5.56 6.55-12.3 10.47-20.23 11.76-.11-1-.17-1.97-.17-2.92z" />
                </svg>
                Apple
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
