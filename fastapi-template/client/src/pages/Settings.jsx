import { useState, useEffect, useRef } from "react";
import {
  User,
  Camera,
  Bell,
  Mail,
  Send,
  Smartphone,
  Shield,
  Save,
  CheckCircle2,
  AlertCircle,
  RefreshCw,
  CreditCard,
  Zap,
  Check,
  Download,
  AlertTriangle,
  Receipt,
} from "lucide-react";
import { useApi } from "../hooks/useApi";
import { authFetch, getStoredUser } from "../services/apiClient";
import { API_BASE } from "../config";

export default function Settings() {
  const storedUser = getStoredUser();
  const [activeTab, setActiveTab] = useState("profile"); // "profile" | "billing"
  const { data: profileData, refetch: refetchProfile } = useApi("/api/user/profile");
  const { data: notifData, refetch: refetchNotifs } = useApi("/api/user/notifications");
  const { data: subscriptionData, refetch: refetchSub } = useApi("/api/billing/subscription");
  const { data: plansData } = useApi("/api/billing/plans");
  const { data: invoicesData, refetch: refetchInvoices } = useApi("/api/billing/invoices");

  // Profile Form State
  const [fullName, setFullName] = useState(storedUser?.full_name || "");
  const [profilePhoto, setProfilePhoto] = useState(null);
  const [isSavingProfile, setIsSavingProfile] = useState(false);
  const [profileMsg, setProfileMsg] = useState(null);
  const fileInputRef = useRef(null);

  // Billing State
  const [billingCycle, setBillingCycle] = useState("MONTHLY");
  const [isCheckingOut, setIsCheckingOut] = useState(false);
  const [billingMsg, setBillingMsg] = useState(null);
  const [isCancelling, setIsCancelling] = useState(false);
  const [showCancelModal, setShowCancelModal] = useState(false);

  // Notification Preferences State
  const [notifs, setNotifs] = useState({
    email_enabled: true,
    email_address: storedUser?.email || "",
    telegram_enabled: false,
    telegram_chat_id: "",
    push_enabled: true,
    order_executed_notify: true,
    trade_closed_notify: true,
    sl_tp_trigger_notify: true,
    price_alert_notify: true,
  });
  const [isSavingNotifs, setIsSavingNotifs] = useState(false);
  const [notifMsg, setNotifMsg] = useState(null);
  const [telegramMsg, setTelegramMsg] = useState(null);
  const [isTestingTelegram, setIsTestingTelegram] = useState(false);

  useEffect(() => {
    if (profileData) {
      setFullName(profileData.full_name || "");
      setProfilePhoto(profileData.profile_photo || null);
    }
  }, [profileData]);

  useEffect(() => {
    if (notifData) {
      setNotifs(notifData);
    }
  }, [notifData]);

  // Load Razorpay Checkout script dynamically
  const loadRazorpayScript = () => {
    return new Promise((resolve) => {
      if (window.Razorpay) {
        resolve(true);
        return;
      }
      const script = document.createElement("script");
      script.src = "https://checkout.razorpay.com/v1/checkout.js";
      script.onload = () => resolve(true);
      script.onerror = () => resolve(false);
      document.body.appendChild(script);
    });
  };

  // Handle Photo File -> Base64
  const handlePhotoUpload = (e) => {
    const file = e.target.files?.[0];
    if (!file) return;

    if (file.size > 2 * 1024 * 1024) {
      setProfileMsg({ type: "error", text: "Image size must be less than 2MB" });
      return;
    }

    const reader = new FileReader();
    reader.onload = () => {
      setProfilePhoto(reader.result);
    };
    reader.readAsDataURL(file);
  };

  const handleSaveProfile = async (e) => {
    e.preventDefault();
    setIsSavingProfile(true);
    setProfileMsg(null);
    try {
      const res = await authFetch(`/api/user/profile`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          full_name: fullName,
          profile_photo: profilePhoto,
        }),
      });

      if (!res.ok) throw new Error("Failed to update profile");
      const updated = await res.json();

      try {
        const stored = localStorage.getItem("tradetron_user");
        if (stored) {
          const u = JSON.parse(stored);
          u.full_name = updated.full_name;
          u.profile_photo = updated.profile_photo;
          localStorage.setItem("tradetron_user", JSON.stringify(u));
        }
      } catch {
        // ignore
      }

      setProfileMsg({ type: "success", text: "Profile updated successfully!" });
      refetchProfile();
    } catch (err) {
      setProfileMsg({ type: "error", text: err.message });
    } finally {
      setIsSavingProfile(false);
    }
  };

  const handleSaveNotifications = async (e) => {
    e.preventDefault();
    setIsSavingNotifs(true);
    setNotifMsg(null);
    try {
      const res = await authFetch(`/api/user/notifications`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(notifs),
      });

      if (!res.ok) throw new Error("Failed to save notification preferences");
      setNotifMsg({ type: "success", text: "Preferences saved successfully!" });
      refetchNotifs();
    } catch (err) {
      setNotifMsg({ type: "error", text: err.message });
    } finally {
      setIsSavingNotifs(false);
    }
  };

  const handleSaveTelegramSettings = async () => {
    setTelegramMsg(null);
    try {
      const res = await authFetch("/api/alerts/settings", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          chat_id: notifs.telegram_chat_id,
          telegram_alerts_enabled: notifs.telegram_enabled,
          order_fills_enabled: notifs.order_executed_notify,
          sl_tp_enabled: notifs.sl_tp_trigger_notify,
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Failed to save Telegram settings");
      setTelegramMsg({ type: "success", text: "Telegram alert settings saved." });
    } catch (err) {
      setTelegramMsg({ type: "error", text: err.message });
    }
  };

  const handleTestTelegram = async () => {
    setIsTestingTelegram(true);
    setTelegramMsg(null);
    try {
      const res = await authFetch("/api/alerts/telegram/test", { method: "POST" });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Telegram test failed");
      setTelegramMsg({ type: "success", text: data.message });
    } catch (err) {
      setTelegramMsg({ type: "error", text: err.message });
    } finally {
      setIsTestingTelegram(false);
    }
  };

  // Razorpay Checkout Trigger
  const handleUpgradePlan = async (planName) => {
    setIsCheckingOut(true);
    setBillingMsg(null);

    try {
      const isLoaded = await loadRazorpayScript();
      if (!isLoaded) {
        throw new Error("Failed to load Razorpay payment SDK. Please check your connection.");
      }

      // 1. Create order on backend
      const orderRes = await authFetch(`/api/billing/create-order`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          plan_name: planName,
          billing_cycle: billingCycle,
        }),
      });

      if (!orderRes.ok) {
        const err = await orderRes.json();
        throw new Error(err.detail || "Failed to initiate payment");
      }

      const orderData = await orderRes.json();

      // 2. Launch Razorpay Modal Options
      const options = {
        key: orderData.key_id,
        amount: orderData.amount,
        currency: orderData.currency,
        name: "Tradetron Technologies",
        description: `Upgrade to ${planName} Plan (${billingCycle})`,
        order_id: orderData.order_id,
        prefill: {
          name: orderData.user_details?.name || fullName,
          email: orderData.user_details?.email || profileData?.email,
        },
        theme: {
          color: "#6366f1",
        },
        handler: async function (response) {
          // 3. Server-side HMAC Signature Verification
          try {
            const verifyRes = await authFetch(`/api/billing/verify-payment`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                razorpay_order_id: response.razorpay_order_id,
                razorpay_payment_id: response.razorpay_payment_id,
                razorpay_signature: response.razorpay_signature,
                plan_name: planName,
                billing_cycle: billingCycle,
              }),
            });

            if (!verifyRes.ok) {
              const vErr = await verifyRes.json();
              throw new Error(vErr.detail || "Signature verification failed");
            }

            const vData = await verifyRes.json();
            setBillingMsg({
              type: "success",
              text: `🎉 ${vData.message}! Invoice #${vData.invoice_number} generated.`,
            });
            refetchSub();
            refetchInvoices();
          } catch (verErr) {
            setBillingMsg({ type: "error", text: verErr.message });
          }
        },
        modal: {
          ondismiss: function () {
            setIsCheckingOut(false);
          },
        },
      };

      const rzp = new window.Razorpay(options);
      rzp.on("payment.failed", function (response) {
        setBillingMsg({
          type: "error",
          text: `Payment failed: ${response.error.description} (Code: ${response.error.code})`,
        });
      });
      rzp.open();
    } catch (err) {
      setBillingMsg({ type: "error", text: err.message });
    } finally {
      setIsCheckingOut(false);
    }
  };

  // Cancel Subscription
  const handleCancelSubscription = async () => {
    setIsCancelling(true);
    try {
      const res = await authFetch(`/api/billing/cancel-subscription`, {
        method: "POST",
      });
      if (!res.ok) throw new Error("Failed to cancel subscription");
      setBillingMsg({
        type: "success",
        text: "Subscription cancelled successfully. You maintain access until current period ends.",
      });
      setShowCancelModal(false);
      refetchSub();
    } catch (err) {
      setBillingMsg({ type: "error", text: err.message });
    } finally {
      setIsCancelling(false);
    }
  };

  return (
    <div className="space-y-6 max-w-5xl">
      {/* Page Title & Tab Nav */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 border-b border-white/[0.06] pb-4">
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-white">
            Settings & Account
          </h1>
          <p className="text-xs text-slate-400">
            Manage your trader identity, notifications, membership plans, and Razorpay invoices
          </p>
        </div>
        <div className="flex items-center gap-2 bg-surface-800/80 p-1 rounded-xl border border-white/[0.06]">
          <button
            onClick={() => setActiveTab("profile")}
            className={`px-3 py-1.5 rounded-lg text-xs font-semibold transition-colors flex items-center gap-1.5 ${
              activeTab === "profile"
                ? "bg-cyan-500 text-slate-950 shadow-md shadow-cyan-500/20"
                : "text-slate-400 hover:text-white"
            }`}
          >
            <User size={14} /> Profile & Alerts
          </button>
          <button
            onClick={() => setActiveTab("billing")}
            className={`px-3 py-1.5 rounded-lg text-xs font-semibold transition-colors flex items-center gap-1.5 ${
              activeTab === "billing"
                ? "bg-brand-purple text-white shadow-md shadow-brand-purple/20"
                : "text-slate-400 hover:text-white"
            }`}
          >
            <CreditCard size={14} /> Plans & Invoices
          </button>
        </div>
      </div>

      {activeTab === "profile" ? (
        <div className="grid gap-6 lg:grid-cols-2">
          {/* Profile Card */}
          <div className="glass-card p-4 sm:p-6 space-y-6">
            <div className="flex items-center gap-2 border-b border-white/[0.06] pb-3">
              <User size={18} className="text-cyan-400" />
              <h2 className="text-sm font-semibold text-white">Trader Profile</h2>
            </div>

            {profileMsg && (
              <div
                className={`p-3 rounded-lg border text-xs flex items-center gap-2 ${
                  profileMsg.type === "success"
                    ? "bg-emerald-500/10 border-emerald-500/20 text-emerald-400"
                    : "bg-red-500/10 border-red-500/20 text-red-400"
                }`}
              >
                {profileMsg.type === "success" ? <CheckCircle2 size={14} /> : <AlertCircle size={14} />}
                {profileMsg.text}
              </div>
            )}

            <form onSubmit={handleSaveProfile} className="space-y-5">
              {/* Avatar / Photo Upload */}
              <div className="flex items-center gap-5">
                <div className="relative group">
                  <div className="w-20 h-20 rounded-full border-2 border-cyan-500/30 overflow-hidden bg-slate-800 flex items-center justify-center text-cyan-400 font-bold text-2xl shadow-lg shadow-cyan-500/10">
                    {profilePhoto ? (
                      <img
                        src={profilePhoto}
                        alt="Profile"
                        className="w-full h-full object-cover"
                      />
                    ) : (
                      <span>{fullName ? fullName[0]?.toUpperCase() : "T"}</span>
                    )}
                  </div>
                  <button
                    type="button"
                    onClick={() => fileInputRef.current?.click()}
                    className="absolute inset-0 bg-black/60 rounded-full flex flex-col items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity text-white text-[10px]"
                  >
                    <Camera size={18} />
                    Change
                  </button>
                  <input
                    type="file"
                    ref={fileInputRef}
                    onChange={handlePhotoUpload}
                    accept="image/png, image/jpeg, image/webp"
                    className="hidden"
                  />
                </div>

                <div>
                  <p className="text-xs font-semibold text-white">Profile Photo</p>
                  <p className="text-[11px] text-slate-400 mt-0.5">
                    JPG, PNG or WEBP (Max 2MB). Uploaded as Base64.
                  </p>
                  <div className="flex gap-2 mt-2">
                    <button
                      type="button"
                      onClick={() => fileInputRef.current?.click()}
                      className="btn-ghost text-[11px] py-1 px-2.5"
                    >
                      Upload Image
                    </button>
                    {profilePhoto && (
                      <button
                        type="button"
                        onClick={() => setProfilePhoto(null)}
                        className="text-[11px] text-red-400 hover:text-red-300 py-1 px-2"
                      >
                        Remove
                      </button>
                    )}
                  </div>
                </div>
              </div>

              {/* Profile Fields */}
              <div className="space-y-3">
                <div>
                  <label className="text-xs font-medium text-slate-400 mb-1 block">
                    Full Name / Display Name
                  </label>
                  <input
                    type="text"
                    required
                    value={fullName}
                    onChange={(e) => setFullName(e.target.value)}
                    className="input-field text-xs"
                    placeholder="Enter your full name"
                  />
                </div>

                <div>
                  <label className="text-xs font-medium text-slate-400 mb-1 block">
                    Account Email
                  </label>
                  <input
                    type="email"
                    disabled
                    value={profileData?.email || storedUser?.email || ""}
                    className="input-field text-xs opacity-60 cursor-not-allowed bg-slate-900"
                  />
                </div>

                <div>
                  <label className="text-xs font-medium text-slate-400 mb-1 block">
                    Trader Role
                  </label>
                  <div className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md bg-cyan-500/10 text-cyan-400 border border-cyan-500/20 text-xs font-medium uppercase font-mono">
                    <Shield size={12} />
                    {profileData?.role || storedUser?.role || "trader"}
                  </div>
                </div>
              </div>

              <button
                type="submit"
                disabled={isSavingProfile}
                className="btn-primary text-xs py-2 px-4 w-full justify-center"
              >
                <Save size={14} />
                {isSavingProfile ? "Saving..." : "Save Profile Changes"}
              </button>
            </form>
          </div>

          {/* Notifications & Channels Card */}
          <div className="glass-card p-4 sm:p-6 space-y-6">
            <div className="flex items-center gap-2 border-b border-white/[0.06] pb-3">
              <Bell size={18} className="text-amber-400" />
              <h2 className="text-sm font-semibold text-white">Notification Preferences</h2>
            </div>

            {notifMsg && (
              <div
                className={`p-3 rounded-lg border text-xs flex items-center gap-2 ${
                  notifMsg.type === "success"
                    ? "bg-emerald-500/10 border-emerald-500/20 text-emerald-400"
                    : "bg-red-500/10 border-red-500/20 text-red-400"
                }`}
              >
                {notifMsg.type === "success" ? <CheckCircle2 size={14} /> : <AlertCircle size={14} />}
                {notifMsg.text}
              </div>
            )}

            <form onSubmit={handleSaveNotifications} className="space-y-4">
              {/* Delivery Channels */}
              <div className="space-y-3">
                <h3 className="text-xs font-bold text-slate-400 uppercase tracking-wider">
                  Delivery Channels
                </h3>

                {/* Email Delivery */}
                <div className="p-3 rounded-lg bg-surface-800/60 border border-white/[0.06] space-y-2">
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-2">
                      <Mail size={15} className="text-cyan-400" />
                      <span className="text-xs font-semibold text-white">Email Delivery</span>
                    </div>
                    <input
                      type="checkbox"
                      checked={notifs.email_enabled}
                      onChange={(e) =>
                        setNotifs((prev) => ({ ...prev, email_enabled: e.target.checked }))
                      }
                      className="h-4 w-4 rounded border-slate-700 bg-slate-900 text-cyan-500 focus:ring-cyan-500"
                    />
                  </div>
                  {notifs.email_enabled && (
                    <input
                      type="email"
                      value={notifs.email_address || ""}
                      onChange={(e) =>
                        setNotifs((prev) => ({ ...prev, email_address: e.target.value }))
                      }
                      placeholder="alerts@domain.com"
                      className="input-field text-xs mt-1"
                    />
                  )}
                </div>

                {/* Telegram Delivery */}
                <div className="p-3 rounded-lg bg-surface-800/60 border border-cyan-500/20 space-y-3">
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-2">
                      <Send size={15} className="text-blue-400" />
                      <span className="text-xs font-semibold text-white">Telegram & Mobile Alerts</span>
                    </div>
                    <input
                      type="checkbox"
                      checked={notifs.telegram_enabled}
                      onChange={(e) =>
                        setNotifs((prev) => ({ ...prev, telegram_enabled: e.target.checked }))
                      }
                      className="h-4 w-4 rounded border-slate-700 bg-slate-900 text-blue-500 focus:ring-blue-500"
                    />
                  </div>
                  <input
                    type="text"
                    value={notifs.telegram_chat_id || ""}
                    onChange={(e) =>
                      setNotifs((prev) => ({ ...prev, telegram_chat_id: e.target.value }))
                    }
                    placeholder="Telegram Chat ID (e.g. 123456789)"
                    className="input-field text-xs font-mono"
                  />
                  <div className="grid gap-2 sm:grid-cols-2">
                    <label className="flex items-center justify-between rounded-lg border border-white/[0.04] bg-surface-900/50 px-2.5 py-2 text-[11px] text-slate-300">
                      Order fills
                      <input type="checkbox" checked={notifs.order_executed_notify} onChange={(e) => setNotifs((prev) => ({ ...prev, order_executed_notify: e.target.checked }))} className="h-4 w-4 rounded border-slate-700 bg-slate-900 text-cyan-500" />
                    </label>
                    <label className="flex items-center justify-between rounded-lg border border-white/[0.04] bg-surface-900/50 px-2.5 py-2 text-[11px] text-slate-300">
                      SL / TP alerts
                      <input type="checkbox" checked={notifs.sl_tp_trigger_notify} onChange={(e) => setNotifs((prev) => ({ ...prev, sl_tp_trigger_notify: e.target.checked }))} className="h-4 w-4 rounded border-slate-700 bg-slate-900 text-cyan-500" />
                    </label>
                  </div>
                  <div className="flex flex-wrap items-center gap-2">
                    <button type="button" onClick={handleSaveTelegramSettings} className="btn-primary text-[10px] py-1.5 px-3">Save Telegram Settings</button>
                    <button type="button" onClick={handleTestTelegram} disabled={isTestingTelegram} className="btn-ghost text-[10px] py-1.5 px-3 flex items-center gap-1.5"><Send size={12} />{isTestingTelegram ? "Sending..." : "Send Test Alert"}</button>
                  </div>
                  {telegramMsg && <p className={`text-[11px] ${telegramMsg.type === "success" ? "text-emerald-400" : "text-rose-400"}`}>{telegramMsg.text}</p>}
                </div>

                {/* Browser Push */}
                <div className="p-3 rounded-lg bg-surface-800/60 border border-white/[0.06] flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <Smartphone size={15} className="text-emerald-400" />
                    <div>
                      <span className="text-xs font-semibold text-white block">
                        Browser Push Notifications
                      </span>
                      <span className="text-[10px] text-slate-400">
                        Real-time desktop alerts on order fills
                      </span>
                    </div>
                  </div>
                  <button
                    type="button"
                    onClick={async () => {
                      if ("Notification" in window) {
                        const p = await Notification.requestPermission();
                        if (p === "granted") setNotifs((prev) => ({ ...prev, push_enabled: true }));
                      }
                    }}
                    className="btn-ghost text-[10px] py-1 px-2"
                  >
                    Enable Push
                  </button>
                </div>
              </div>

              {/* Event Triggers */}
              <div className="space-y-2 pt-2">
                <h3 className="text-xs font-bold text-slate-400 uppercase tracking-wider">
                  Event Subscriptions
                </h3>

                <div className="grid grid-cols-2 gap-2 text-xs">
                  <label className="flex items-center gap-2 p-2 rounded-lg bg-surface-800/40 border border-white/[0.04] cursor-pointer hover:bg-surface-800">
                    <input
                      type="checkbox"
                      checked={notifs.order_executed_notify}
                      onChange={(e) =>
                        setNotifs((p) => ({ ...p, order_executed_notify: e.target.checked }))
                      }
                      className="rounded bg-slate-900 text-cyan-500"
                    />
                    <span>Order Fills</span>
                  </label>

                  <label className="flex items-center gap-2 p-2 rounded-lg bg-surface-800/40 border border-white/[0.04] cursor-pointer hover:bg-surface-800">
                    <input
                      type="checkbox"
                      checked={notifs.trade_closed_notify}
                      onChange={(e) =>
                        setNotifs((p) => ({ ...p, trade_closed_notify: e.target.checked }))
                      }
                      className="rounded bg-slate-900 text-cyan-500"
                    />
                    <span>Trade PnL Exits</span>
                  </label>

                  <label className="flex items-center gap-2 p-2 rounded-lg bg-surface-800/40 border border-white/[0.04] cursor-pointer hover:bg-surface-800">
                    <input
                      type="checkbox"
                      checked={notifs.sl_tp_trigger_notify}
                      onChange={(e) =>
                        setNotifs((p) => ({ ...p, sl_tp_trigger_notify: e.target.checked }))
                      }
                      className="rounded bg-slate-900 text-cyan-500"
                    />
                    <span>SL / TP Triggers</span>
                  </label>

                  <label className="flex items-center gap-2 p-2 rounded-lg bg-surface-800/40 border border-white/[0.04] cursor-pointer hover:bg-surface-800">
                    <input
                      type="checkbox"
                      checked={notifs.price_alert_notify}
                      onChange={(e) =>
                        setNotifs((p) => ({ ...p, price_alert_notify: e.target.checked }))
                      }
                      className="rounded bg-slate-900 text-cyan-500"
                    />
                    <span>Price Alerts</span>
                  </label>
                </div>
              </div>

              <button
                type="submit"
                disabled={isSavingNotifs}
                className="btn-primary text-xs py-2 px-4 w-full justify-center bg-amber-500 hover:bg-amber-400 text-slate-950 shadow-amber-500/20"
              >
                <Save size={14} />
                {isSavingNotifs ? "Saving..." : "Save Notification Preferences"}
              </button>
            </form>
          </div>
        </div>
      ) : (
        /* Plans & Billing (Razorpay) Tab */
        <div className="space-y-8">
          {billingMsg && (
            <div
              className={`p-4 rounded-xl border text-xs flex items-center justify-between ${
                billingMsg.type === "success"
                  ? "bg-emerald-500/10 border-emerald-500/20 text-emerald-400"
                  : "bg-red-500/10 border-red-500/20 text-red-400"
              }`}
            >
              <div className="flex items-center gap-2">
                {billingMsg.type === "success" ? <CheckCircle2 size={16} /> : <AlertCircle size={16} />}
                <span>{billingMsg.text}</span>
              </div>
              <button
                onClick={() => setBillingMsg(null)}
                className="text-slate-400 hover:text-white"
              >
                ✕
              </button>
            </div>
          )}

          {/* Current Subscription Hero */}
          <div className="glass-card p-6 bg-gradient-to-r from-brand-purple/10 via-surface-800/40 to-cyan-500/10 border border-brand-purple/20 flex flex-col sm:flex-row sm:items-center justify-between gap-4">
            <div className="space-y-1">
              <div className="flex items-center gap-2">
                <span className="text-xs font-mono uppercase tracking-wider text-slate-400">Current Plan</span>
                <span
                  className={`px-2 py-0.5 text-[10px] font-bold rounded-full uppercase ${
                    subscriptionData?.status === "ACTIVE"
                      ? "bg-emerald-500/20 text-emerald-400 border border-emerald-500/30"
                      : "bg-amber-500/20 text-amber-400 border border-amber-500/30"
                  }`}
                >
                  {subscriptionData?.status || "ACTIVE"}
                </span>
              </div>
              <h2 className="text-2xl font-black text-white flex items-center gap-2">
                <Zap className="text-brand-purple" size={24} />
                {subscriptionData?.plan_name || "FREE"} Tier
              </h2>
              <p className="text-xs text-slate-400">
                {subscriptionData?.plan_name === "FREE"
                  ? "Starter sandbox with simulated ticks and 1 live strategy deployment."
                  : `Active until ${
                      subscriptionData?.end_date
                        ? new Date(subscriptionData.end_date).toLocaleDateString()
                        : "Renewal"
                    } (Billed ${subscriptionData?.billing_cycle || "Monthly"})`}
              </p>
            </div>

            {subscriptionData?.plan_name !== "FREE" && subscriptionData?.status === "ACTIVE" && (
              <button
                onClick={() => setShowCancelModal(true)}
                className="btn-ghost text-xs text-red-400 border-red-500/20 hover:bg-red-500/10"
              >
                Cancel Subscription
              </button>
            )}
          </div>

          {/* Billing Cycle Switcher */}
          <div className="flex items-center justify-center gap-3">
            <span
              className={`text-xs font-semibold ${
                billingCycle === "MONTHLY" ? "text-white" : "text-slate-400"
              }`}
            >
              Monthly Billing
            </span>
            <button
              type="button"
              onClick={() =>
                setBillingCycle((c) => (c === "MONTHLY" ? "YEARLY" : "MONTHLY"))
              }
              className={`w-12 h-6 flex items-center rounded-full p-1 transition-colors ${
                billingCycle === "YEARLY" ? "bg-brand-purple" : "bg-slate-700"
              }`}
            >
              <div
                className={`bg-white w-4 h-4 rounded-full shadow-md transform transition-transform ${
                  billingCycle === "YEARLY" ? "translate-x-6" : ""
                }`}
              />
            </button>
            <span
              className={`text-xs font-semibold flex items-center gap-1.5 ${
                billingCycle === "YEARLY" ? "text-brand-purple" : "text-slate-400"
              }`}
            >
              Annual Billing
              <span className="px-2 py-0.5 text-[10px] bg-emerald-500/20 text-emerald-400 rounded-full font-bold">
                Save 17%
              </span>
            </span>
          </div>

          {/* Pricing Grid */}
          <div className="grid md:grid-cols-3 gap-6">
            {(plansData || []).map((plan) => {
              const isCurrent = subscriptionData?.plan_name === plan.name;
              const price =
                billingCycle === "YEARLY" ? plan.price_yearly : plan.price_monthly;
              const features = plan.features || {};

              return (
                <div
                  key={plan.id}
                  className={`glass-card p-6 flex flex-col justify-between space-y-6 relative transition-all ${
                    isCurrent
                      ? "border-cyan-500/50 shadow-lg shadow-cyan-500/10 bg-surface-800/80"
                      : plan.name === "PRO"
                      ? "border-brand-purple/40 shadow-lg shadow-brand-purple/10 bg-surface-800/40 hover:border-brand-purple"
                      : "border-white/[0.06] hover:border-white/20"
                  }`}
                >
                  {plan.name === "PRO" && (
                    <div className="absolute -top-3 left-1/2 -translate-x-1/2 px-3 py-0.5 bg-brand-purple text-white text-[10px] font-bold rounded-full uppercase tracking-wider shadow-md">
                      Most Popular
                    </div>
                  )}

                  <div className="space-y-4">
                    <div>
                      <h3 className="text-lg font-bold text-white">{plan.display_name}</h3>
                      <p className="text-xs text-slate-400 mt-1 min-h-[32px]">
                        {plan.description}
                      </p>
                    </div>

                    <div className="flex items-baseline gap-1">
                      <span className="text-3xl font-black text-white">
                        ₹{price.toLocaleString("en-IN")}
                      </span>
                      <span className="text-xs text-slate-400">
                        /{billingCycle === "YEARLY" ? "yr" : "mo"}
                      </span>
                    </div>

                    <ul className="space-y-2.5 text-xs text-slate-300 pt-3 border-t border-white/[0.06]">
                      <li className="flex items-center gap-2">
                        <Check size={14} className="text-emerald-400 flex-shrink-0" />
                        <span>{features.max_live_strategies || 1} Live Strategy Deployments</span>
                      </li>
                      <li className="flex items-center gap-2">
                        <Check size={14} className="text-emerald-400 flex-shrink-0" />
                        <span>{features.max_brokers || 1} Broker Connections (Zerodha/Angel/Binance)</span>
                      </li>
                      <li className="flex items-center gap-2">
                        <Check size={14} className="text-emerald-400 flex-shrink-0" />
                        <span>{features.tick_speed || "1s"} Tick Streaming</span>
                      </li>
                      <li className="flex items-center gap-2">
                        <Check size={14} className="text-emerald-400 flex-shrink-0" />
                        <span>{features.historical_candles || "15m"} Real Historical Candles</span>
                      </li>
                      <li className="flex items-center gap-2">
                        <Check
                          size={14}
                          className={
                            features.priority_support
                              ? "text-emerald-400 flex-shrink-0"
                              : "text-slate-600 flex-shrink-0"
                          }
                        />
                        <span className={features.priority_support ? "" : "text-slate-500"}>
                          Priority Support
                        </span>
                      </li>
                    </ul>
                  </div>

                  <div>
                    {isCurrent ? (
                      <button
                        disabled
                        className="btn-ghost w-full justify-center text-xs py-2.5 opacity-60 cursor-default"
                      >
                        Current Plan
                      </button>
                    ) : plan.price_monthly === 0 ? (
                      <button
                        disabled
                        className="btn-ghost w-full justify-center text-xs py-2.5 opacity-50"
                      >
                        Included Starter
                      </button>
                    ) : (
                      <button
                        onClick={() => handleUpgradePlan(plan.name)}
                        disabled={isCheckingOut}
                        className={`w-full justify-center text-xs py-2.5 rounded-xl font-bold flex items-center gap-2 shadow-lg transition-all ${
                          plan.name === "PRO"
                            ? "bg-brand-purple hover:bg-brand-purple/90 text-white shadow-brand-purple/20"
                            : "bg-cyan-500 hover:bg-cyan-400 text-slate-950 shadow-cyan-500/20"
                        }`}
                      >
                        <CreditCard size={14} />
                        {isCheckingOut ? "Connecting..." : `Upgrade via Razorpay`}
                      </button>
                    )}
                  </div>
                </div>
              );
            })}
          </div>

          {/* Invoices Section */}
          <div className="glass-card p-6 space-y-4">
            <div className="flex items-center justify-between border-b border-white/[0.06] pb-3">
              <div className="flex items-center gap-2">
                <Receipt size={18} className="text-cyan-400" />
                <h3 className="text-sm font-semibold text-white">GST Tax Invoices</h3>
              </div>
              <span className="text-[11px] text-slate-400">GSTIN: 27AABCT9988C1Z4</span>
            </div>

            {(invoicesData || []).length === 0 ? (
              <p className="text-xs text-slate-500 py-4 text-center">
                No past invoices found. Invoices are generated automatically on checkout.
              </p>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-left text-xs">
                  <thead>
                    <tr className="border-b border-white/[0.06] text-slate-400">
                      <th className="pb-2 font-medium">Invoice #</th>
                      <th className="pb-2 font-medium">Plan</th>
                      <th className="pb-2 font-medium">Date</th>
                      <th className="pb-2 font-medium">Amount (inc. GST)</th>
                      <th className="pb-2 font-medium">Status</th>
                      <th className="pb-2 font-medium text-right">Action</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-white/[0.04]">
                    {(invoicesData || []).map((inv) => (
                      <tr key={inv.id} className="hover:bg-white/[0.02]">
                        <td className="py-3 font-mono text-cyan-400 font-bold">
                          {inv.invoice_number}
                        </td>
                        <td className="py-3 text-slate-200">{inv.plan_name}</td>
                        <td className="py-3 text-slate-400">
                          {new Date(inv.issued_at).toLocaleDateString()}
                        </td>
                        <td className="py-3 font-semibold text-white">
                          ₹{inv.total_amount.toLocaleString("en-IN")}
                        </td>
                        <td className="py-3">
                          <span className="px-2 py-0.5 text-[10px] font-bold rounded-full bg-emerald-500/20 text-emerald-400">
                            {inv.status}
                          </span>
                        </td>
                        <td className="py-3 text-right">
                          <a
                            href={`${API_BASE}${inv.download_url}`}
                            target="_blank"
                            rel="noreferrer"
                            className="btn-ghost text-[11px] py-1 px-2.5 inline-flex items-center gap-1.5"
                          >
                            <Download size={12} /> View Tax Invoice
                          </a>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </div>
      )}

      {/* Cancel Confirmation Modal */}
      {showCancelModal && (
        <div className="fixed inset-0 z-50 bg-black/80 backdrop-blur-sm flex items-center justify-center p-4">
          <div className="glass-panel p-6 max-w-md w-full rounded-2xl border border-red-500/30 space-y-4 shadow-2xl">
            <div className="flex items-center gap-3 text-red-400">
              <AlertTriangle size={24} />
              <h3 className="text-base font-bold text-white">Cancel Subscription?</h3>
            </div>
            <p className="text-xs text-slate-300 leading-relaxed">
              Are you sure you want to cancel your{" "}
              <strong>{subscriptionData?.plan_name} Plan</strong>? You will continue to have full access to all features until the end of your billing cycle.
            </p>
            <div className="flex justify-end gap-3 pt-2">
              <button
                onClick={() => setShowCancelModal(false)}
                className="btn-ghost text-xs"
              >
                Keep Plan
              </button>
              <button
                onClick={handleCancelSubscription}
                disabled={isCancelling}
                className="btn-primary bg-red-600 hover:bg-red-500 text-white text-xs px-4 py-2"
              >
                {isCancelling ? "Cancelling..." : "Confirm Cancellation"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
