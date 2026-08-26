import { useState, useEffect, useRef } from "react";
import {
  AlertTriangle,
  FileCheck,
  Upload,
  X,
  CheckCircle2,
  AlertCircle,
  Clock,
  Lock,
} from "lucide-react";
import { authFetch } from "../services/apiClient";

export default function KYCModal({ isOpen, onClose, onKYCUpdated }) {
  const [kycData, setKycData] = useState(null);
  const [panNumber, setPanNumber] = useState("");
  const [idProofType, setIdProofType] = useState("PAN_CARD");
  const [idProofDoc, setIdProofDoc] = useState(null);
  const [docFileName, setDocFileName] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [msg, setMsg] = useState(null);
  const fileInputRef = useRef(null);

  const fetchKYC = async () => {
    try {
      const res = await authFetch("/api/user/kyc");
      if (res.ok) {
        const data = await res.json();
        setKycData(data);
        if (data.pan_number) setPanNumber(data.pan_number);
        if (data.id_proof_type) setIdProofType(data.id_proof_type);
      }
    } catch {
      // ignore
    }
  };

  useEffect(() => {
    if (isOpen) {
      fetchKYC();
      setMsg(null);
    }
  }, [isOpen]);

  const handleFileUpload = (e) => {
    const file = e.target.files?.[0];
    if (!file) return;

    if (file.size > 5 * 1024 * 1024) {
      setMsg({ type: "error", text: "Document file size must be less than 5MB" });
      return;
    }

    setDocFileName(file.name);
    const reader = new FileReader();
    reader.onload = () => {
      setIdProofDoc(reader.result);
      setMsg(null);
    };
    reader.readAsDataURL(file);
  };

  const handleSubmit = async (e) => {
    e.preventDefault();
    setSubmitting(true);
    setMsg(null);

    const panClean = panNumber.trim().toUpperCase();
    const panRegex = /^[A-Z]{5}[0-9]{4}[A-Z]{1}$/;
    if (!panRegex.test(panClean)) {
      setMsg({
        type: "error",
        text: "Invalid PAN format. Please enter a valid 10-character PAN (e.g. ABCDE1234F)",
      });
      setSubmitting(false);
      return;
    }

    if (!idProofDoc) {
      setMsg({
        type: "error",
        text: "Please upload a photo or scan of your ID proof document",
      });
      setSubmitting(false);
      return;
    }

    try {
      const res = await authFetch("/api/user/kyc/submit", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          pan_number: panClean,
          id_proof_type: idProofType,
          id_proof_doc: idProofDoc,
        }),
      });

      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "KYC submission failed");

      setMsg({
        type: "success",
        text: "KYC submitted successfully! Your account is now Pending compliance verification.",
      });
      fetchKYC();
      if (onKYCUpdated) onKYCUpdated(data);
    } catch (err) {
      setMsg({ type: "error", text: err.message });
    } finally {
      setSubmitting(false);
    }
  };

  if (!isOpen) return null;

  const status = kycData?.kyc_status || "NOT_SUBMITTED";

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/80 backdrop-blur-md animate-fade-in">
      <div className="relative w-full max-w-lg rounded-2xl bg-surface-900 border border-slate-700/80 p-6 shadow-2xl space-y-5 max-h-[90vh] overflow-y-auto">
        {/* Header */}
        <div className="flex items-start justify-between border-b border-slate-800 pb-4">
          <div className="flex items-center gap-3">
            <div className="p-2.5 rounded-xl bg-violet-500/10 text-violet-400 border border-violet-500/30">
              <FileCheck size={22} />
            </div>
            <div>
              <h3 className="text-base font-display font-bold text-white">
                SEBI KYC Identity Verification
              </h3>
              <p className="text-xs text-slate-400 font-medium mt-0.5">
                Mandatory for live broker execution and real capital trading
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

        {/* Status Banner */}
        {status === "VERIFIED" && (
          <div className="p-4 rounded-xl bg-emerald-500/10 border border-emerald-500/30 space-y-2 animate-fade-in">
            <div className="flex items-center gap-2 text-emerald-400 font-bold text-sm">
              <CheckCircle2 size={18} />
              <span>KYC Status: VERIFIED & ACTIVE</span>
            </div>
            <p className="text-xs text-slate-300">
              Your PAN <strong>{kycData?.pan_number}</strong> has been audited and approved. Real-money live broker routing (Zerodha, Angel One, Binance, Upstox) is unlocked.
            </p>
          </div>
        )}

        {status === "PENDING" && (
          <div className="p-4 rounded-xl bg-amber-500/10 border border-amber-500/30 space-y-2 animate-fade-in">
            <div className="flex items-center gap-2 text-amber-400 font-bold text-sm">
              <Clock size={18} className="animate-spin" />
              <span>KYC Status: PENDING COMPLIANCE REVIEW</span>
            </div>
            <p className="text-xs text-slate-300">
              Your documents for PAN <strong>{kycData?.pan_number}</strong> were submitted on {kycData?.kyc_submitted_at ? new Date(kycData.kyc_submitted_at).toLocaleString() : "recently"}. Our compliance desk reviews submissions within 24 hours.
            </p>
          </div>
        )}

        {status === "REJECTED" && (
          <div className="p-4 rounded-xl bg-rose-500/10 border border-rose-500/30 space-y-2 animate-fade-in">
            <div className="flex items-center gap-2 text-rose-400 font-bold text-sm">
              <AlertTriangle size={18} />
              <span>KYC Status: REJECTED</span>
            </div>
            <p className="text-xs text-rose-300">
              Reason: {kycData?.kyc_rejection_reason || "Document details did not match PAN registry. Please re-upload clear photos."}
            </p>
          </div>
        )}

        {msg && (
          <div
            className={`p-3 rounded-xl text-xs flex items-center gap-2 ${
              msg.type === "success"
                ? "bg-emerald-500/10 border border-emerald-500/30 text-emerald-300"
                : "bg-rose-500/10 border border-rose-500/30 text-rose-300"
            }`}
          >
            {msg.type === "success" ? <CheckCircle2 size={16} /> : <AlertCircle size={16} />}
            <span>{msg.text}</span>
          </div>
        )}

        {/* KYC Form (Editable if not verified) */}
        {status !== "VERIFIED" && (
          <form onSubmit={handleSubmit} className="space-y-4">
            <div>
              <label className="text-xs font-semibold text-slate-300 block mb-1">
                Permanent Account Number (PAN) *
              </label>
              <input
                type="text"
                maxLength={10}
                required
                value={panNumber}
                onChange={(e) => setPanNumber(e.target.value.toUpperCase())}
                placeholder="e.g. ABCDE1234F"
                className="w-full bg-slate-950 border border-slate-800 rounded-xl px-3.5 py-2.5 text-xs text-white uppercase font-mono tracking-wider focus:outline-none focus:border-violet-500"
              />
              <span className="text-[10px] text-slate-500 mt-1 block">
                10-character alphanumeric PAN registered with Income Tax Department
              </span>
            </div>

            <div>
              <label className="text-xs font-semibold text-slate-300 block mb-1">
                ID Document Proof Type *
              </label>
              <select
                value={idProofType}
                onChange={(e) => setIdProofType(e.target.value)}
                className="w-full bg-slate-950 border border-slate-800 rounded-xl px-3.5 py-2.5 text-xs text-white focus:outline-none focus:border-violet-500"
              >
                <option value="PAN_CARD">PAN Card Copy</option>
                <option value="AADHAAR">Aadhaar Card (Front & Back)</option>
                <option value="PASSPORT">Indian Passport</option>
                <option value="VOTER_ID">Voter Identity Card</option>
                <option value="DRIVING_LICENSE">Driving License</option>
              </select>
            </div>

            <div>
              <label className="text-xs font-semibold text-slate-300 block mb-1">
                Upload Document Proof (PDF / JPG / PNG) *
              </label>
              <div
                onClick={() => fileInputRef.current?.click()}
                className="border-2 border-dashed border-slate-800 hover:border-violet-500/50 rounded-xl p-4 text-center cursor-pointer transition-colors bg-slate-950/50"
              >
                <Upload size={24} className="mx-auto text-slate-400 mb-1" />
                <p className="text-xs text-slate-300 font-semibold">
                  {docFileName ? docFileName : "Click to select or drag and drop document"}
                </p>
                <p className="text-[10px] text-slate-500 mt-0.5">Maximum file size 5MB</p>
                <input
                  type="file"
                  ref={fileInputRef}
                  onChange={handleFileUpload}
                  accept="image/jpeg,image/png,image/webp,application/pdf"
                  className="hidden"
                />
              </div>
            </div>

            <button
              type="submit"
              disabled={submitting}
              className="w-full py-3 rounded-xl bg-gradient-to-r from-violet-600 to-indigo-600 hover:from-violet-500 hover:to-indigo-500 text-white font-bold text-xs transition-all shadow-lg shadow-violet-600/30 flex items-center justify-center gap-2 disabled:opacity-50"
            >
              <Lock size={14} />
              {submitting ? "Submitting Verification..." : status === "PENDING" ? "Re-submit Updated KYC" : "Submit KYC Verification"}
            </button>
          </form>
        )}

        {status === "VERIFIED" && (
          <div className="pt-2">
            <button
              type="button"
              onClick={onClose}
              className="w-full py-2.5 rounded-xl bg-slate-800 hover:bg-slate-700 text-white text-xs font-semibold"
            >
              Close
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
