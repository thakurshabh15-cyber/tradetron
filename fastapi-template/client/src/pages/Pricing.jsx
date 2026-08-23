import { useEffect, useState } from "react";
import { Check, CreditCard, LockKeyhole, Sparkles } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { authFetch } from "../services/apiClient";

const fallbackPlans = [
  { code: "FREE", name: "Free", price_monthly: 0, price_yearly: 0, max_brokers: 1, max_algos: 1, copy_trading_allowed: false },
  { code: "PRO", name: "Pro", price_monthly: 1999, price_yearly: 19190, max_brokers: 3, max_algos: 5, copy_trading_allowed: true },
  { code: "INSTITUTIONAL", name: "Institutional", price_monthly: 4999, price_yearly: 47990, max_brokers: 99, max_algos: 99, copy_trading_allowed: true },
];

export default function Pricing() {
  const navigate = useNavigate();
  const [billingCycle, setBillingCycle] = useState("MONTHLY");
  const [plans, setPlans] = useState(fallbackPlans);
  const [currentPlan, setCurrentPlan] = useState("FREE");

  useEffect(() => {
    Promise.all([authFetch("/api/subscriptions/plans"), authFetch("/api/subscriptions/current")])
      .then(async ([plansRes, currentRes]) => {
        if (plansRes.ok) setPlans(await plansRes.json());
        if (currentRes.ok) setCurrentPlan((await currentRes.json()).plan_code || "FREE");
      })
      .catch(() => {});
  }, []);

  return (
    <div className="max-w-6xl space-y-8 animate-fade-in">
      <div className="flex flex-col gap-5 border-b border-white/[0.06] pb-6 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <div className="mb-3 flex items-center gap-2 text-cyan-400">
            <Sparkles size={18} />
            <span className="text-[11px] font-bold uppercase tracking-[0.2em]">Membership desk</span>
          </div>
          <h1 className="text-3xl font-bold text-white">Subscription & Pricing</h1>
          <p className="mt-2 max-w-xl text-sm text-slate-400">Choose the execution capacity that matches your trading operation.</p>
        </div>
        <div className="flex items-center gap-1 rounded-xl border border-slate-700 bg-surface-900 p-1">
          {[["MONTHLY", "Monthly"], ["YEARLY", "Yearly · 20% off"]].map(([value, label]) => (
            <button
              key={value}
              onClick={() => setBillingCycle(value)}
              className={`rounded-lg px-3 py-2 text-xs font-bold transition-colors ${billingCycle === value ? "bg-cyan-400 text-slate-950" : "text-slate-400 hover:text-white"}`}
            >
              {label}
            </button>
          ))}
        </div>
      </div>

      <div className="grid gap-4 lg:grid-cols-3">
        {plans.map((plan, index) => {
          const isCurrent = currentPlan === plan.code;
          const price = billingCycle === "YEARLY" ? plan.price_yearly : plan.price_monthly;
          const features = [
            `${plan.max_brokers} broker${plan.max_brokers === 1 ? "" : "s"}`,
            `${plan.max_algos} strateg${plan.max_algos === 1 ? "y" : "ies"}`,
            plan.copy_trading_allowed ? "Copy trading included" : "Paper trading included",
          ];
          return (
            <section key={plan.code} className={`relative flex flex-col border p-6 ${index === 1 ? "border-cyan-400/60 bg-cyan-400/[0.06] shadow-glow-cyan" : "border-slate-800 bg-surface-900/70"}`}>
              {index === 1 && <span className="absolute right-5 top-5 rounded-full bg-cyan-400 px-2.5 py-1 text-[10px] font-black uppercase tracking-wider text-slate-950">Most popular</span>}
              <h2 className="text-lg font-bold text-white">{plan.name}</h2>
              <div className="mt-6 flex items-baseline gap-1">
                <span className="text-4xl font-black tabular-nums text-white">₹{Number(price).toLocaleString("en-IN")}</span>
                <span className="text-xs text-slate-500">/{billingCycle === "YEARLY" ? "year" : "month"}</span>
              </div>
              <ul className="my-7 flex-1 space-y-3 border-y border-white/[0.06] py-6">
                {features.map((feature) => <li key={feature} className="flex items-center gap-2 text-sm text-slate-300"><Check size={15} className="text-emerald-400" />{feature}</li>)}
              </ul>
              <button onClick={() => navigate("/settings")} className={`flex min-h-11 items-center justify-center gap-2 text-sm font-bold ${isCurrent ? "cursor-default border border-slate-700 text-slate-500" : "btn-primary"}`} disabled={isCurrent}>
                {isCurrent ? <><LockKeyhole size={15} />Current plan</> : <><CreditCard size={15} />Choose {plan.name}</>}
              </button>
            </section>
          );
        })}
      </div>
    </div>
  );
}