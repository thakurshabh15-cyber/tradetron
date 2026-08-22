import { useState, useEffect, useCallback } from "react";
import { Search, Filter, Play, Star, TrendingUp, ShieldCheck, Users, RefreshCw, ChevronLeft, ChevronRight, Zap } from "lucide-react";
import DeploymentModal from "../components/DeploymentModal";
import { useDebounce } from "../hooks/useDebounce";
import { API_BASE } from "../config";
const CATEGORIES = ["All", "Momentum", "Mean Reversion", "Breakout", "Trend Following", "Options"];

export default function Marketplace() {
  const [items, setItems] = useState([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [totalPages, setTotalPages] = useState(1);
  const [category, setCategory] = useState("All");
  const [symbolSearch, setSymbolSearch] = useState("");
  const [textSearch, setTextSearch] = useState("");
  const [pricingType, setPricingType] = useState("All");
  const [sortBy, setSortBy] = useState("roi_desc");
  const [loading, setLoading] = useState(false);

  const debouncedTextSearch = useDebounce(textSearch, 350);
  const debouncedSymbolSearch = useDebounce(symbolSearch, 350);

  // Deployment modal state
  const [selectedStrategy, setSelectedStrategy] = useState(null);
  const [isDeployOpen, setIsDeployOpen] = useState(false);

  const fetchMarketplace = useCallback(async () => {
    setLoading(true);
    try {
      const params = new URLSearchParams({
        page: page.toString(),
        limit: "6",
        category,
        pricing_type: pricingType,
        sort_by: sortBy,
      });
      if (debouncedSymbolSearch.trim()) params.append("symbol", debouncedSymbolSearch.trim());
      if (debouncedTextSearch.trim()) params.append("search", debouncedTextSearch.trim());

      const res = await fetch(`${API_BASE}/api/strategies/marketplace?${params.toString()}`);
      if (res.ok) {
        const data = await res.json();
        setItems(data.items || []);
        setTotal(data.total || 0);
        setTotalPages(data.totalPages || 1);
      }
    } catch (err) {
      console.error("Failed to fetch marketplace:", err);
    } finally {
      setLoading(false);
    }
  }, [page, category, pricingType, sortBy, debouncedSymbolSearch, debouncedTextSearch]);

  useEffect(() => {
    fetchMarketplace();
  }, [fetchMarketplace]);

  const handleSearchSubmit = (e) => {
    e.preventDefault();
    setPage(1);
    fetchMarketplace();
  };

  const handleOpenDeploy = (strat) => {
    setSelectedStrategy(strat);
    setIsDeployOpen(true);
  };

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-white">
            Algo Strategy Marketplace
          </h1>
          <p className="text-xs text-slate-400">
            Discover, subscribe, and 1-click deploy verified algorithmic strategies
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button onClick={fetchMarketplace} className="btn-ghost text-xs">
            <RefreshCw size={14} className={loading ? "animate-spin" : ""} /> Refresh
          </button>
        </div>
      </div>

      {/* Filter and Search Bar */}
      <div className="glass-card p-4 space-y-3">
        <form onSubmit={handleSearchSubmit} className="grid gap-3 md:grid-cols-4">
          <div className="relative md:col-span-2">
            <Search size={15} className="absolute left-3 top-3 text-slate-500" />
            <input
              type="text"
              placeholder="Search strategy title, creator, or description..."
              value={textSearch}
              onChange={(e) => setTextSearch(e.target.value)}
              className="input-field pl-9 text-xs"
            />
          </div>

          <div>
            <input
              type="text"
              placeholder="Filter by Symbol (e.g. AAPL, NVDA)"
              value={symbolSearch}
              onChange={(e) => setSymbolSearch(e.target.value)}
              className="input-field text-xs"
            />
          </div>

          <div className="flex items-center gap-2">
            <select
              value={sortBy}
              onChange={(e) => setSortBy(e.target.value)}
              className="select-field text-xs flex-1"
            >
              <option value="roi_desc">Sort: Highest ROI</option>
              <option value="subscribers_desc">Sort: Most Subscribed</option>
              <option value="rating_desc">Sort: Highest Rated</option>
              <option value="drawdown_asc">Sort: Lowest Drawdown</option>
            </select>
            <button type="submit" className="btn-primary text-xs px-4">
              Search
            </button>
          </div>
        </form>

        {/* Category Pills */}
        <div className="flex flex-wrap items-center gap-1.5 pt-2 border-t border-white/[0.04]">
          <span className="text-[11px] font-semibold text-slate-400 mr-2 flex items-center gap-1">
            <Filter size={12} /> Category:
          </span>
          {CATEGORIES.map((cat) => (
            <button
              key={cat}
              onClick={() => { setCategory(cat); setPage(1); }}
              className={`px-2.5 py-1 rounded-md text-xs font-medium transition-all ${
                category === cat
                  ? "bg-accent-500 text-slate-950 font-bold shadow-sm"
                  : "bg-surface-800 text-slate-400 hover:text-white hover:bg-surface-700"
              }`}
            >
              {cat}
            </button>
          ))}

          <div className="ml-auto flex items-center gap-2">
            <span className="text-[11px] text-slate-500 font-mono">
              Pricing:
            </span>
            <select
              value={pricingType}
              onChange={(e) => { setPricingType(e.target.value); setPage(1); }}
              className="select-field text-[11px] py-0.5 px-2"
            >
              <option value="All">All Plans</option>
              <option value="FREE">Free Only</option>
              <option value="PAID">Premium Paid</option>
            </select>
          </div>
        </div>
      </div>

      {/* Strategy Grid */}
      {loading ? (
        <div className="grid gap-5 sm:grid-cols-2 lg:grid-cols-3">
          {[1, 2, 3].map((i) => (
            <div key={i} className="card p-6 bg-slate-900/60 border border-slate-800 animate-pulse h-64 rounded-xl" />
          ))}
        </div>
      ) : items.length === 0 ? (
        <div className="glass-card p-12 text-center text-slate-400 space-y-2">
          <p className="text-sm">No marketplace strategies found matching your search criteria.</p>
          <button onClick={() => { setCategory("All"); setSymbolSearch(""); setTextSearch(""); }} className="text-xs text-accent-400 hover:underline">
            Reset All Filters
          </button>
        </div>
      ) : (
        <div className="grid gap-4 sm:gap-5 sm:grid-cols-2 lg:grid-cols-3">
          {items.map((strat) => (
            <div
              key={strat.id}
              className="glass-card p-4 sm:p-5 flex flex-col justify-between hover:border-accent-500/40 transition-all group"
            >
              <div>
                {/* Header info */}
                <div className="flex items-start justify-between gap-2">
                  <div>
                    <span className="text-[10px] font-semibold text-accent-400 uppercase tracking-wider">
                      {strat.category}
                    </span>
                    <h3 className="text-base font-bold text-white group-hover:text-accent-300 transition-colors mt-0.5">
                      {strat.name}
                    </h3>
                    <p className="text-[11px] text-slate-400">by {strat.creator_name}</p>
                  </div>
                  <span className={`text-[10px] px-2 py-0.5 rounded-full font-bold uppercase ${
                    strat.pricing_type === "FREE"
                      ? "bg-emerald-500/10 text-emerald-400 border border-emerald-500/20"
                      : "bg-purple-500/10 text-purple-400 border border-purple-500/20"
                  }`}>
                    {strat.pricing_type === "FREE" ? "Free" : `$${strat.price}/mo`}
                  </span>
                </div>

                <p className="text-xs text-slate-400 mt-2 line-clamp-2">
                  {strat.description}
                </p>

                {/* Key Metrics */}
                <div className="grid grid-cols-3 gap-2 my-4 p-2.5 rounded-lg bg-surface-800/80 border border-white/[0.04] text-center">
                  <div>
                    <span className="text-[10px] text-slate-500 block">Total ROI</span>
                    <span className="text-xs font-bold text-profit-400 font-mono">
                      +{strat.total_return_pct}%
                    </span>
                  </div>
                  <div>
                    <span className="text-[10px] text-slate-500 block">Win Rate</span>
                    <span className="text-xs font-bold text-white font-mono">
                      {strat.win_rate}%
                    </span>
                  </div>
                  <div>
                    <span className="text-[10px] text-slate-500 block">Max DD</span>
                    <span className="text-xs font-bold text-loss-400 font-mono">
                      -{strat.max_drawdown_pct}%
                    </span>
                  </div>
                </div>

                {/* Assets badges */}
                <div className="flex items-center gap-1.5 flex-wrap">
                  {(strat.symbols || []).map((sym) => (
                    <span
                      key={sym}
                      className="text-[10px] font-mono px-1.5 py-0.5 rounded bg-surface-700 text-slate-300"
                    >
                      {sym}
                    </span>
                  ))}
                </div>
              </div>

              {/* Card Footer: Rating, Subscribers & Deploy */}
              <div className="pt-4 mt-4 border-t border-white/[0.06] flex items-center justify-between">
                <div className="flex items-center gap-3 text-xs text-slate-400">
                  <div className="flex items-center gap-1 text-amber-400 font-bold">
                    <Star size={13} className="fill-amber-400" />
                    <span>{strat.rating}</span>
                  </div>
                  <div className="flex items-center gap-1 text-[11px] text-slate-400">
                    <Users size={12} />
                    <span>{strat.subscribers_count}</span>
                  </div>
                </div>

                <button
                  onClick={() => handleOpenDeploy(strat)}
                  className="btn-primary text-xs py-1.5 px-3"
                >
                  <Play size={12} /> Deploy
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Pagination Controls */}
      {totalPages > 1 && (
        <div className="flex items-center justify-between pt-4 border-t border-white/[0.06]">
          <span className="text-xs text-slate-400 font-mono">
            Showing page {page} of {totalPages} ({total} total strategies)
          </span>

          <div className="flex items-center gap-2">
            <button
              onClick={() => setPage((p) => Math.max(1, p - 1))}
              disabled={page === 1}
              className="btn-ghost text-xs py-1.5 px-3 disabled:opacity-40"
            >
              <ChevronLeft size={14} /> Previous
            </button>
            <button
              onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
              disabled={page === totalPages}
              className="btn-ghost text-xs py-1.5 px-3 disabled:opacity-40"
            >
              Next <ChevronRight size={14} />
            </button>
          </div>
        </div>
      )}

      {/* Deployment Modal */}
      <DeploymentModal
        isOpen={isDeployOpen}
        onClose={() => setIsDeployOpen(false)}
        strategy={selectedStrategy}
        onDeployed={() => {
          fetchMarketplace();
        }}
      />
    </div>
  );
}
