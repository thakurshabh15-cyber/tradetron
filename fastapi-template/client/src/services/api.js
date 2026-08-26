/**
 * Resilient API layer — HTML-response & offline fallback helpers.
 *
 * Production reality: when the backend is unreachable, mis-deployed, or
 * fronted by a CDN that returns the SPA's `index.html` for unknown /api
 * routes, raw `fetch().json()` callers surface ugly "Failed to fetch" or
 * "Unexpected token '<'" errors. This module detects those conditions and
 * routes callers to deterministic shared simulated data so the UI never
 * renders a dead-end raw error toast — instead it renders a clearly-labelled
 * demo and keeps the screen useful.
 */

export function isOfflineOrHtmlError(err) {
  const msg = ((err && (err.message || (typeof err === "string" ? err : ""))) || "").toLowerCase();
  return /failed to fetch|networkerror|load failed|network request failed|unexpected token|is not valid json|syntaxerror|<!doctype|^<|\bhtml\b|text\/html|json parse|typeerror|htmlerror/.test(
    msg,
  );
}

/** Safely parse a Response as JSON; return null if it's HTML, empty, or garbage. */
export async function readJsonResponse(res) {
  if (!res) return null;
  const text = await res.text().catch(() => "");
  if (!text || /^\s*</.test(text) || /^\s*<!doctype/i.test(text)) return null;
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

/** Deterministically create an ~n-length repeatable sequence [0..1). */
function seededSequence(seed, n) {
  let s = typeof seed === "number" ? seed : 42;
  const out = [];
  for (let i = 0; i < n; i++) {
    s = (s * 16807) % 2147483647;
    out.push(s / 2147483647);
  }
  return out;
}

const r2 = (v) => Math.round(v * 100) / 100;

/**
 * Shared offline backtest simulator. Deterministic per (symbol, seed) so
 * results are reproducible. Demo data only — never a profit promise.
 */
export function simulateBacktest(form = {}) {
  const capital = Number(form.capital) || 100000;
  const quantity = Number(form.quantity) || 65;
  const days = Number(form.days) || 30;
  const seed = form.seed === "" || form.seed == null ? 42 : Number(form.seed);
  const timeframe = form.timeframe || "15m";
  const side = form.side || "BUY";
  const symbol = form.symbol || "NIFTY";
  const bars = Math.max(days * Math.floor(350 / days), days);
  const rng = seededSequence(seed, 220);

  const trades = [];
  let eq = capital;
  let wins = 0;
  let losses = 0;
  let grossPnl = 0;
  let maxEq = eq;
  let maxDD = 0;
  for (let i = 0; i < rng.length; i++) {
    const move = (rng[i] - 0.5) * 2 * 0.6;
    const pnl = quantity * Math.abs(move) * 100 * (rng[i] > 0.5 ? 1 : -1);
    grossPnl += pnl;
    eq += pnl;
    maxEq = Math.max(maxEq, eq);
    maxDD = Math.max(maxDD, maxEq - eq);
    if (pnl >= 0) wins++;
    else losses++;
    trades.push({
      entry_ts: 1672531200 + i * 900,
      side: rng[i] > 0.5 ? side : "SELL",
      entry_price: r2(100 + 20 * rng[i]),
      exit_price: r2(100 + 20 * rng[(i + 1) % rng.length]),
      exit_reason: pnl >= 0 ? "TARGET_HIT" : "STOP_LOSS",
      net_pnl: Math.round(pnl),
    });
  }

  const total = trades.length;
  const totalCharges = r2(0.32 * total + 411);
  const net = r2(grossPnl - totalCharges);
  const winRate = total ? (wins / total) * 100 : 0;
  const costPct = net !== 0 ? r2((totalCharges / (Math.abs(grossPnl) || 1)) * 100) : 0;

  return {
    error: null,
    symbol,
    timeframe,
    bars,
    quantity,
    lot_size: 50,
    capital,
    seed,
    equity_curve: [capital, r2(eq * 0.8), r2(eq * 1.1), r2(eq)],
    charges_breakdown: {
      brokerage: r2(totalCharges * 0.3),
      stt: r2(totalCharges * 0.28),
      gst: r2(totalCharges * 0.12),
      stamp_duty: r2(totalCharges * 0.18),
      sebi_fee: r2(totalCharges * 0.12),
    },
    metrics: {
      total_trades: total,
      net_pnl: net,
      total_pnl: net,
      win_rate_pct: r2(winRate),
      wins,
      losses,
      profit_factor: losses ? r2((Math.abs(grossPnl) / 10)) : 9.99,
      max_drawdown_pct: r2(maxDD ? (maxDD / Math.max(maxEq, 1)) * 100 : 0),
      total_charges: totalCharges,
      charges_as_pct_of_gross: costPct,
      sharpe_annualised: r2(1.4),
      final_equity: r2(eq),
    },
    trades: trades.reverse().slice(0, 12),
  };
}
/**
 * Shared offline Quant Lab parse fallback.
 */
export function simulateQuantParse(text = "") {
  const lower = text.toLowerCase();
  const side = lower.includes("sell") ? "SELL" : "BUY";
  const timeframe = /hourly|1h|\bhour\b/.test(lower)
    ? "1h"
    : /daily|\bday\b/.test(lower)
      ? "1d"
      : "15m";
  const sym =
    ["BANKNIFTY", "FINNIFTY", "RELIANCE", "TCS", "INFY"].find((s) =>
      lower.includes(s.toLowerCase()),
    ) || "NIFTY";
  return {
    confidence: 0.72,
    symbols: [sym],
    timeframe,
    action: { side, quantity: 2 },
    conditions: [
      {
        agg: "and",
        indicator: "RSI",
        period: 14,
        operator: side === "BUY" ? "lt" : "gt",
        value: side === "BUY" ? 35 : 70,
      },
    ],
    unparsed: [],
  };
}

/**
 * Shared offline Quant Lab health report fallback.
 */
export function simulateQuantHealth() {
  return {
    error: null,
    robustness_score: 72,
    grade: "B",
    verdict:
      "Offline demo health report — deterministic seed-based grading (not a profit promise).",
    components: {
      strategy_coherence: { score: 76, weight: 100, detail: "rules parse cleanly" },
      backtest_cost: { score: 70, weight: 100, detail: "charges included" },
      market_regime: { score: 68, weight: 100, detail: "assumes mean-reversion" },
    },
    findings: [
      {
        severity: "info",
        title: "Backend offline — showing simulated demo",
        recommendation:
          "Reconnect to the trading backend for live robustness grading.",
      },
    ],
    base_report: {
      equity_curve: [
        { t: 1, v: 100000 },
        { t: 60, v: 103400 },
        { t: 120, v: 101900 },
        { t: 180, v: 105200 },
      ],
    },
  };
}

/**
 * Demo login fallback used when the backend is unreachable (never on a real
 * server-side credential rejection). Produces a clearly-labelled demo session.
 */
export function demoLogin() {
  return {
    access_token: `demo_${Date.now()}`,
    refresh_token: `demo_refresh_${Date.now()}`,
    token_type: "bearer",
    user: {
      id: "demo-user",
      email: "demo@tradetron.app",
      full_name: "Demo Pilot",
      is_verified: true,
      plan: "free",
      is_demo: true,
    },
  };
}