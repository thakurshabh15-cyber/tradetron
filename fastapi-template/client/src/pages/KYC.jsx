import { useState } from "react";
import { BadgeCheck, ShieldCheck, FileText, Clock } from "lucide-react";
import { useApi } from "../hooks/useApi";
import KYCModal from "../components/KYCModal";

export default function KYC() {
  const { data, refetch } = useApi("/api/user/kyc");
  const [modalOpen, setModalOpen] = useState(false);

  const status =
    data?.kyc_status === "VERIFIED" || data?.status === "VERIFIED"
      ? "VERIFIED"
      : data?.kyc_status === "PENDING" || data?.status === "PENDING" || (data?.pan_number ? "PENDING" : null)
      ? "IN_PROGRESS"
      : "NOT_STARTED";

  const tone =
    status === "VERIFIED" ? "text-profit-400 bg-profit-500/10 border-profit-500/30"
    : status === "IN_PROGRESS" ? "text-warning-400 bg-warning-500/10 border-warning-500/30"
    : "text-slate-400 bg-surface-800 border-slate-700";

  const steps = [
    { label: "Account created", done: true },
    { label: "PAN submitted", done: Boolean(data?.pan_number) },
    { label: "ID proof uploaded", done: Boolean(data?.id_proof_type) },
    { label: "Verified by desk", done: status === "VERIFIED" },
  ];

  return (
    <div className="max-w-3xl space-y-5">
      <div className="flex items-center gap-2">
        <BadgeCheck size={20} className="text-brand-electric" />
        <h1 className="font-display text-xl font-bold text-white">KYC Center</h1>
      </div>

      <div className={`glass-panel rounded-2xl p-5 border flex items-start gap-4 ${tone.split(" ").slice(1).join(" ")}`}>
        <ShieldCheck size={28} className={tone.split(" ")[0]} />
        <div className="flex-1">
          <p className="font-display text-lg font-bold text-white">
            {{ NOT_STARTED: "Verification Not Started", IN_PROGRESS: "Verification In Progress", VERIFIED: "Identity Verified" }[status]}
          </p>
          <p className="text-xs text-slate-400 mt-0.5">
            {status === "VERIFIED"
              ? "Your identity is verified. Live deployment eligibility unlocked."
              : "Complete verification to unlock live broker deployment. Paper trading stays available."}
          </p>
          {data?.pan_number && (
            <p className="text-[11px] font-mono text-slate-500 mt-1">PAN: XXXXXX{String(data.pan_number).slice(-4)}</p>
          )}
        </div>
        <span className={`px-2.5 py-1 rounded-full text-[10px] font-bold border ${tone}`}>{status.replace("_", " ")}</span>
      </div>

      <div className="glass-panel rounded-2xl p-5 border border-edge space-y-3">
        <h2 className="text-sm font-bold text-white flex items-center gap-2"><FileText size={14} className="text-brand-electric" /> COMPLETION TIMELINE</h2>
        {steps.map((s) => (
          <div key={s.label} className="flex items-center gap-3 text-xs">
            <span className={`w-4 h-4 rounded-full border flex items-center justify-center ${s.done ? "bg-profit-500/20 border-profit-500/50 text-profit-400" : "border-slate-600 text-slate-600"}`}>
              {s.done ? "✓" : ""}
            </span>
            <span className={s.done ? "text-slate-200" : "text-slate-500"}>{s.label}</span>
            {!s.done && <Clock size={10} className="ml-auto text-slate-600" />}
          </div>
        ))}
        <button onClick={() => setModalOpen(true)} disabled={status === "VERIFIED"}
          className="btn-primary w-full mt-2 disabled:opacity-40">
          {status === "NOT_STARTED" ? "Start Verification" : status === "IN_PROGRESS" ? "Update / Resubmit Documents" : "Verified ✓"}
        </button>
      </div>

      <KYCModal isOpen={modalOpen} onClose={() => setModalOpen(false)} onKYCUpdated={() => refetch()} />
    </div>
  );
}
