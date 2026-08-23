import { useEffect, useState } from "react";
import { Check, Layers3, Play, Plus, Save, Trash2 } from "lucide-react";
import { authFetch } from "../services/apiClient";

const templates = {
  "Short Straddle": [{ type: "CE", strike: "ATM", action: "SELL", lots: 1 }, { type: "PE", strike: "ATM", action: "SELL", lots: 1 }],
  "Long Strangle": [{ type: "CE", strike: "OTM1", action: "BUY", lots: 1 }, { type: "PE", strike: "OTM1", action: "BUY", lots: 1 }],
  "Iron Condor": [{ type: "CE", strike: "OTM1", action: "SELL", lots: 1 }, { type: "CE", strike: "OTM2", action: "BUY", lots: 1 }, { type: "PE", strike: "OTM1", action: "SELL", lots: 1 }, { type: "PE", strike: "OTM2", action: "BUY", lots: 1 }],
  "Bull Call Spread": [{ type: "CE", strike: "ATM", action: "BUY", lots: 1 }, { type: "CE", strike: "OTM1", action: "SELL", lots: 1 }],
};

const blankCondition = { indicator: "RSI", operator: "gt", value: 70, period: 14 };

export default function VisualBuilder() {
  const [template, setTemplate] = useState("Short Straddle");
  const [name, setName] = useState("My Options Strategy");
  const [underlying, setUnderlying] = useState("NIFTY50");
  const [mode, setMode] = useState("PAPER");
  const [legs, setLegs] = useState(templates["Short Straddle"]);
  const [conditions, setConditions] = useState([blankCondition]);
  const [targetProfit, setTargetProfit] = useState(5000);
  const [maxLoss, setMaxLoss] = useState(2500);
  const [saved, setSaved] = useState([]);
  const [message, setMessage] = useState(null);

  const loadSaved = async () => {
    const response = await authFetch("/api/visual-strategies");
    if (response.ok) setSaved(await response.json());
  };
  useEffect(() => { loadSaved().catch(() => {}); }, []);

  const chooseTemplate = (value) => {
    setTemplate(value);
    setLegs(templates[value].map((leg) => ({ ...leg })));
  };

  const updateLeg = (index, field, value) => setLegs((current) => current.map((leg, i) => i === index ? { ...leg, [field]: field === "lots" ? Number(value) : value } : leg));
  const updateCondition = (index, field, value) => setConditions((current) => current.map((condition, i) => i === index ? { ...condition, [field]: (field === "value" && condition.indicator !== "TIME") || field === "period" ? Number(value) : value } : condition));

  const saveStrategy = async (event) => {
    event.preventDefault();
    setMessage(null);
    const response = await authFetch("/api/visual-strategies", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, underlying, entry_conditions: conditions, exit_conditions: { target_profit: targetProfit, max_loss: maxLoss }, legs, is_active: false, mode }),
    });
    if (!response.ok) {
      setMessage({ error: "Could not save this visual strategy." });
      return;
    }
    setMessage({ success: "Visual strategy saved." });
    loadSaved();
  };

  return (
    <form onSubmit={saveStrategy} className="mx-auto max-w-6xl space-y-6 animate-fade-in">
      <div className="flex flex-col gap-4 border-b border-white/[0.06] pb-5 sm:flex-row sm:items-end sm:justify-between">
        <div><div className="mb-2 flex items-center gap-2 text-emerald-400"><Layers3 size={18} /><span className="text-[11px] font-bold uppercase tracking-[0.2em]">Options studio</span></div><h1 className="text-3xl font-bold text-white">Visual Strategy Builder</h1><p className="mt-1 text-sm text-slate-400">Compose multi-leg option strategies without writing code.</p></div>
        <div className="flex rounded-lg border border-slate-700 bg-surface-900 p-1"><button type="button" onClick={() => setMode("PAPER")} className={`px-3 py-2 text-xs font-bold ${mode === "PAPER" ? "bg-emerald-400 text-slate-950" : "text-slate-400"}`}>Paper</button><button type="button" onClick={() => setMode("LIVE")} className={`px-3 py-2 text-xs font-bold ${mode === "LIVE" ? "bg-rose-400 text-slate-950" : "text-slate-400"}`}>Live</button></div>
      </div>
      {message && <div className={`border px-3 py-2 text-xs ${message.error ? "border-rose-500/30 text-rose-300" : "border-emerald-500/30 text-emerald-300"}`}>{message.error || message.success}</div>}
      <div className="grid gap-4 md:grid-cols-2"><label className="text-xs text-slate-400">Strategy name<input value={name} onChange={(e) => setName(e.target.value)} className="input-field mt-1" required /></label><label className="text-xs text-slate-400">Underlying<input value={underlying} onChange={(e) => setUnderlying(e.target.value.toUpperCase())} className="input-field mt-1 font-mono" required /></label></div>
      <section className="space-y-3 border border-slate-800 bg-surface-900/70 p-5"><div className="flex items-center justify-between"><h2 className="text-sm font-bold text-white">Start from a template</h2><span className="text-[11px] text-slate-500">{legs.length} legs</span></div><div className="grid gap-2 sm:grid-cols-4">{Object.keys(templates).map((item) => <button key={item} type="button" onClick={() => chooseTemplate(item)} className={`border px-3 py-3 text-left text-xs font-bold ${template === item ? "border-emerald-400 bg-emerald-400/10 text-emerald-300" : "border-slate-700 text-slate-400 hover:border-slate-500"}`}>{template === item && <Check size={13} className="mb-1" />}{item}</button>)}</div></section>
      <section className="space-y-4 border border-slate-800 bg-surface-900/70 p-5"><div className="flex items-center justify-between"><h2 className="text-sm font-bold text-white">Entry conditions</h2><button type="button" onClick={() => setConditions([...conditions, { ...blankCondition }])} className="flex items-center gap-1 text-xs font-bold text-emerald-400"><Plus size={14} /> Add rule</button></div>{conditions.map((condition, index) => <div key={index} className="grid gap-2 md:grid-cols-[1fr_1fr_110px_90px_auto]"><select value={condition.indicator} onChange={(e) => updateCondition(index, "indicator", e.target.value)} className="select-field text-xs"><option value="RSI">RSI</option><option value="VWAP">VWAP</option><option value="PRICE">Price</option><option value="TIME">Time</option></select><select value={condition.operator} onChange={(e) => updateCondition(index, "operator", e.target.value)} className="select-field text-xs"><option value="gt">Greater than</option><option value="lt">Less than</option><option value="cross_above">VWAP crossover above</option><option value="cross_below">VWAP crossover below</option><option value="eq">Equals</option></select><input type={condition.indicator === "TIME" ? "time" : "number"} value={condition.value} onChange={(e) => updateCondition(index, "value", e.target.value)} className="input-field text-xs" /><input type="number" min="1" value={condition.period} onChange={(e) => updateCondition(index, "period", e.target.value)} className="input-field text-xs" /><button type="button" onClick={() => setConditions(conditions.filter((_, i) => i !== index))} className="p-2 text-slate-500 hover:text-rose-400" title="Remove rule"><Trash2 size={15} /></button></div>)}</section>
      <section className="space-y-4 border border-slate-800 bg-surface-900/70 p-5"><div className="flex items-center justify-between"><h2 className="text-sm font-bold text-white">Option legs</h2><button type="button" onClick={() => setLegs([...legs, { type: "CE", strike: "ATM", action: "BUY", lots: 1 }])} className="flex items-center gap-1 text-xs font-bold text-emerald-400"><Plus size={14} /> Add leg</button></div>{legs.map((leg, index) => <div key={index} className="grid gap-2 md:grid-cols-[1fr_1fr_1fr_100px_auto]"><select value={leg.type} onChange={(e) => updateLeg(index, "type", e.target.value)} className="select-field text-xs"><option>CE</option><option>PE</option></select><select value={leg.strike} onChange={(e) => updateLeg(index, "strike", e.target.value)} className="select-field text-xs"><option>ATM</option><option>OTM1</option><option>OTM2</option><option>ITM1</option></select><select value={leg.action} onChange={(e) => updateLeg(index, "action", e.target.value)} className="select-field text-xs"><option>BUY</option><option>SELL</option></select><input type="number" min="1" value={leg.lots} onChange={(e) => updateLeg(index, "lots", e.target.value)} className="input-field text-xs" placeholder="Lots" /><button type="button" onClick={() => setLegs(legs.filter((_, i) => i !== index))} className="p-2 text-slate-500 hover:text-rose-400" title="Remove leg"><Trash2 size={15} /></button></div>)}</section>
      <section className="grid gap-4 border border-slate-800 bg-surface-900/70 p-5 sm:grid-cols-2"><label className="text-xs text-slate-400">Target profit (₹)<input type="number" min="0" value={targetProfit} onChange={(e) => setTargetProfit(Number(e.target.value))} className="input-field mt-1" /></label><label className="text-xs text-slate-400">Maximum loss (₹)<input type="number" min="0" value={maxLoss} onChange={(e) => setMaxLoss(Number(e.target.value))} className="input-field mt-1" /></label></section>
      <div className="flex justify-end"><button type="submit" className="btn-primary flex items-center gap-2"><Save size={15} /> Save visual strategy</button></div>
      {saved.length > 0 && <section className="border-t border-white/[0.06] pt-5"><h2 className="mb-3 text-sm font-bold text-white">Saved visual strategies</h2><div className="grid gap-2 sm:grid-cols-2">{saved.map((item) => <div key={item.id} className="flex items-center justify-between border border-slate-800 bg-surface-900/60 px-3 py-3 text-xs"><span className="font-semibold text-slate-200">{item.name}<small className="ml-2 text-slate-500">{item.underlying}</small></span><Play size={14} className={item.is_active ? "text-emerald-400" : "text-slate-600"} /></div>)}</div></section>}
    </form>
  );
}
