import { useState } from "react";
import StrategyBuilder from "../components/StrategyBuilder";
import StrategyList from "../components/StrategyList";
import StrategyWizardScreen from "../components/StrategyWizardScreen";
import StrategyConfiguratorScreen from "../components/StrategyConfiguratorScreen";
import { useApi } from "../hooks/useApi";
import { useAuthStore } from "../stores/useAuthStore";
import { Sliders, Wand2 } from "lucide-react";

export default function Strategies() {
  const [activeTab, setActiveTab] = useState("builder"); // 'builder' | 'wizard' | 'config'
  const [selectedStrategyForConfig, setSelectedStrategyForConfig] = useState(null);
  const currentUserRole = (useAuthStore((state) => state.user?.role) || "").toUpperCase();
  const isAdmin = currentUserRole === "ADMIN" || currentUserRole === "SUPERADMIN";

  const {
    data: strategies,
    loading,
    error,
    post,
    patch,
    del,
  } = useApi("/api/strategies");

  const handleCreate = async (payload) => {
    await post(payload);
    setActiveTab("builder");
  };

  const handleToggle = async (id, enabled) => {
    await patch(id, { enabled });
  };

  const handleDelete = async (id) => {
    await del(id);
  };

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-white">
            Strategy Automation
          </h1>
          <p className="text-xs text-slate-400">
            Build, configure, and deploy rule-based trading algorithms with instant execution
          </p>
        </div>

        {/* Mode switcher tabs */}
        <div className="flex items-center gap-1.5 p-1 rounded-lg bg-surface-800 border border-white/[0.06]">
          <button
            onClick={() => setActiveTab("builder")}
            className={`flex items-center gap-1.5 px-3 py-1.5 rounded-md text-xs font-semibold transition-all ${
              activeTab === "builder"
                ? "bg-accent-500 text-slate-950 shadow-sm"
                : "text-slate-400 hover:text-white"
            }`}
          >
            <Sliders size={13} /> Visual Builder
          </button>
          <button
            onClick={() => setActiveTab("wizard")}
            className={`flex items-center gap-1.5 px-3 py-1.5 rounded-md text-xs font-semibold transition-all ${
              activeTab === "wizard"
                ? "bg-accent-500 text-slate-950 shadow-sm"
                : "text-slate-400 hover:text-white"
            }`}
          >
            <Wand2 size={13} /> Strategy Wizard
          </button>
        </div>
      </div>

      {error && (
        <div className="rounded-lg bg-loss-500/10 border border-loss-500/20 p-4 text-xs text-loss-400">
          Error: {error}
        </div>
      )}

      {/* 1. VISUAL BUILDER */}
      {activeTab === "builder" && (
        <StrategyBuilder onSubmit={handleCreate} isSubmitting={loading} />
      )}

      {/* 2. GUIDED STRATEGY WIZARD */}
      {activeTab === "wizard" && (
        <StrategyWizardScreen
          onSubmit={handleCreate}
          onCancel={() => setActiveTab("builder")}
          isSubmitting={loading}
        />
      )}

      {/* 3. PARAMETER CONFIGURATOR */}
      {activeTab === "config" && selectedStrategyForConfig && (
        <StrategyConfiguratorScreen
          strategy={selectedStrategyForConfig}
          onSave={() => setActiveTab("builder")}
          onCancel={() => setActiveTab("builder")}
        />
      )}

      {/* ACTIVE STRATEGIES LIST */}
      <StrategyList
        strategies={strategies || []}
        onToggle={handleToggle}
        onDelete={handleDelete}
        isAdmin={isAdmin}
        onConfigure={(strat) => {
          setSelectedStrategyForConfig(strat);
          setActiveTab("config");
        }}
      />
    </div>
  );
}
