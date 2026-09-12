/**
 * Phase 15C frontend / mobile audit
 * ──────────────────────────────────
 * 1. Registers a fresh paper user.
 * 2. Places a PAPER order to seed an open position.
 * 3. Verifies API protection truth.
 * 4. At three mobile viewports (360×800, 390×844, 412×915):
 *    a. Navigates to /dashboard, /portfolio, /login
 *    b. Checks no page errors, no unexpected console errors
 *    c. Checks no horizontal overflow
 *    d. Checks dashboard body renders
 * 5. Confirms UI protection-field rendering gap.
 */
import { execSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const API = "http://127.0.0.1:8080";
const APP = "http://127.0.0.1:5173";

const results = [];
function step(name, ok, detail = "") {
  results.push({ name, ok, detail });
  console.log(`[${ok ? "PASS" : "FAIL"}] ${name}${detail ? ` — ${detail}` : ""}`);
  if (!ok) process.exitCode = 1;
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const ci = (hay, needle) => String(hay).toUpperCase().includes(String(needle).toUpperCase());

async function bodyText(page) {
  let lastErr;
  for (let i = 0; i < 6; i++) {
    try { return await page.locator("body").innerText({ timeout: 15000 }); }
    catch (e) { lastErr = e; }
    await sleep(5000);
  }
  throw lastErr;
}

let __browser = null;
const email = `p15c_${Date.now()}@tt-e2e.in`;
const password = "SmokeTest@2026";
const VIEWPORTS = [
  { width: 360, height: 800, label: "360x800" },
  { width: 390, height: 844, label: "390x844" },
  { width: 412, height: 915, label: "412x915" },
];

async function main() {
  // 0. Register
  let reg;
  try {
    const regRes = await fetch(`${API}/api/auth/register`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password, full_name: "P15C Audit Tester" }),
    });
    reg = await regRes.json();
    step("register paper user", regRes.status === 201 && !!reg.access_token, `status=${regRes.status}`);
  } catch (e) { step("register paper user", false, String(e)); return; }
  const token = reg.access_token;
  const headers = { Authorization: `Bearer ${token}`, "Content-Type": "application/json" };

  // 1. Seed a PAPER open position — real manual-order route: POST /api/trades/order
  //    (alias POST /api/trades/place; handler place_manual_order in app/api/trades.py).
  //    ManualOrderRequest schema: symbol, side, quantity, order_type, price, mode.
  //    stop_loss_pct / take_profit_pct are DMA-only fields (POST /api/v1/orders/execute-dma)
  //    and must NOT be sent here — the manual model has no such fields, Pydantic would
  //    silently ignore them, and a manual PAPER position is born with null SL/TP and
  //    protection_state=UNPROTECTED (the exact API truth the UI must not over-claim).
  try {
    const orderRes = await fetch(`${API}/api/trades/order`, {
      method: "POST", headers,
      body: JSON.stringify({
        symbol: "RELIANCE", side: "BUY", quantity: 25,
        order_type: "MARKET", price: 2480.50, mode: "PAPER",
      }),
    });
    const orderBody = await orderRes.json().catch(() => ({}));
    step("seed PAPER order", orderRes.ok, `status=${orderRes.status}`);
  } catch (e) { step("seed PAPER order", false, String(e)); }
  await sleep(3000);

  // 2. API protection-truth verification — real positions route: GET /api/trades/positions
  //    (bare /api/positions is NOT a real route; the servers route prefix is /api/trades,
  //    whose legacy alias yields /api/trades/api/positions).
  try {
    const posRes = await fetch(`${API}/api/trades/positions`, { headers });
    const positions = await posRes.json();
    step("API /api/trades/positions reachable", posRes.ok, `count=${Array.isArray(positions) ? positions.length : "?"}`);
    if (Array.isArray(positions) && positions.length > 0) {
      const pos = positions[0];
      const want = ["stop_loss_price","take_profit_price","protection_state","protection_error","protected_at"];
      const present = want.filter((f) => f in pos);
      step("API protection fields present", present.length === 5,
        `present=[${present.join(",")}] missing=[${want.filter((f) => !present.includes(f)).join(",")}]`);
      step("API protection_state UNPROTECTED for PAPER",
        pos.protection_state === "UNPROTECTED" || pos.protection_state === "PAPER",
        `protection_state=${pos.protection_state}`);
    } else {
      step("API protection fields present", false, "no positions seeded");
    }
  } catch (e) { step("API protection-truth", false, String(e)); }

  // 3. Browser launch + Vite warm-up
  const browser = await chromium.launch({ headless: true });
  __browser = browser;
  try { await fetch(`${APP}/`, { signal: AbortSignal.timeout(45000) }); } catch { /* warm-up */ }

  // ────────────────────────────────────────────────────────────────────────────
  // PHASE B — AUTHENTICATED BROWSER SESSION (deterministic, real backend tokens).
  //
  // POST /api/auth/register returns a REAL TokenResponse (access_token +
  // refresh_token + user) for an is_active user. The SPA persists exactly these
  // three values via setTokens() into localStorage, then validates the session
  // on boot with GET /api/auth/me (initializeSession) before ProtectedRoute
  // unlocks any page. Seeding those backend-issued tokens into localStorage
  // BEFORE the app scripts run replays the app's own supported session state —
  // no OTP is fabricated, no production authentication is modified/weakened,
  // and the browser still authenticates every request through the real API.
  // ────────────────────────────────────────────────────────────────────────────
  const ctxA = await browser.newContext({ viewport: { width: 1280, height: 800 } });
  await ctxA.addInitScript(({ accessToken, refreshToken, user }) => {
    localStorage.setItem("tradetron_access_token", accessToken);
    localStorage.setItem("tradetron_refresh_token", refreshToken);
    localStorage.setItem("tradetron_user", JSON.stringify(user));
  }, {
    accessToken: token,
    refreshToken: reg.refresh_token || "",
    user: reg.user || { email, full_name: "P15C Audit Tester", role: "USER" },
  });

  const page = await ctxA.newPage();
  const pageErrors = [];
  const consoleErrors = [];
  const failedAuth = [];
  const apiCalls = [];
  page.on("pageerror", (err) => pageErrors.push(String(err)));
  page.on("console", (msg) => { if (msg.type() === "error") consoleErrors.push(msg.text()); });
  page.on("response", (res) => {
    const u = res.url();
    if (!u.includes("/api/")) return;
    const path = u.replace(API, "").split("?")[0];
    const status = res.status();
    apiCalls.push({ path, status });
    if (status === 401 || status === 403) failedAuth.push({ path, status });
  });
  const apiStatus = (path) => {
    const hits = apiCalls.filter((c) => c.path === path);
    return hits.length ? hits[hits.length - 1].status : null;
  };

  // 4. Desktop authenticated dashboard baseline
  let dashDesktop = "";
  try {
    await page.goto(`${APP}/dashboard`, { waitUntil: "load", timeout: 60000 });
    await page.waitForTimeout(5000);
    dashDesktop = await bodyText(page);
  } catch (e) {
    dashDesktop = "";
    console.error("dashboard nav error", e);
  }
  step("Authenticated /dashboard renders",
    ci(dashDesktop, "Open Positions") || ci(dashDesktop, "Dashboard") || ci(dashDesktop, "Market"),
    `bodyLen=${dashDesktop.length}`);
  step("Browser session validated: GET /api/auth/me = 200",
    apiStatus("/api/auth/me") === 200, `status=${apiStatus("/api/auth/me")}`);
  step("Protected positions fetch from browser = 200",
    apiStatus("/api/trades/positions") === 200, `status=${apiStatus("/api/trades/positions")}`);
  step("No 401/403 on any API call from authenticated pages",
    failedAuth.length === 0,
    failedAuth.length === 0 ? "none captured" : failedAuth.slice(0, 4).map((f) => `${f.path}→${f.status}`).join(" | "));
  step("User-scoped data loads (seeded RELIANCE position visible)",
    ci(dashDesktop, "RELIANCE"), `bodyLen=${dashDesktop.length}`);

  // Protection truth on the dashboard body: an UNPROTECTED PAPER position must
  // never render as PROTECTED (fail-closed rendering, no fabricated claim).
  const upper = dashDesktop.toUpperCase();
  const claimsProtected = /\bPROTECTED\b/.test(upper);
  step("No fabricated PROTECTED claim for UNPROTECTED PAPER position",
    !claimsProtected,
    claimsProtected ? "FAIL: UI renders PROTECTED while API says UNPROTECTED" : "no PROTECTED claim anywhere on dashboard");
  const hasProtCol = ci(dashDesktop, "Protection") && (ci(dashDesktop, "SL Status") || ci(dashDesktop, "Protection State"));
  step("Protection columns/state rendered in positions UI",
    hasProtCol,
    hasProtCol
      ? "Protection column present"
      : "CONFIRMED GAP (documented follow-up): protection_state / SL / TP are API truth but not rendered in the positions table");

  // ────────────────────────────────────────────────────────────────────────────
  // PHASE C — protected pages (desktop, authenticated session)
  // ────────────────────────────────────────────────────────────────────────────
  const pageChecks = [
    { path: "/portfolio", marker: ["Portfolio & Risk", "Portfolio", "Net Worth", "Account Equity"] },
    { path: "/execution", marker: ["Live Execution", "Mission Control", "Risk Sentinel"] },
    { path: "/settings", marker: ["Trader Profile", "Settings", "Notification Preferences"] },
    { path: "/broker-sessions", marker: ["Broker Sessions"] },
  ];
  for (const pg of pageChecks) {
    try {
      await page.goto(`${APP}${pg.path}`, { waitUntil: "load", timeout: 60000 });
      await page.waitForTimeout(3500);
      const txt = await bodyText(page);
      step(`${pg.path} renders authenticated`, pg.marker.some((m) => ci(txt, m)), `bodyLen=${txt.length}`);
    } catch (e) { step(`${pg.path} renders authenticated`, false, String(e)); }
  }
  step("No 401/403 across all protected pages", failedAuth.length === 0,
    failedAuth.length === 0 ? "none captured" : failedAuth.slice(0, 4).map((f) => `${f.path}→${f.status}`).join(" | "));

  // ────────────────────────────────────────────────────────────────────────────
  // PHASE C2 — REAL UI PASSWORD LOGIN (clean context, no seeded tokens).
  // The current AuthModal default tab is a password "Sign In" (OTP is a
  // separate optional tab). Filling the real form proves the app's actual login
  // UI establishes the session — no OTP is fabricated anywhere.
  // ────────────────────────────────────────────────────────────────────────────
  const ctxB = await browser.newContext({ viewport: { width: 1280, height: 800 } });
  const pageB = await ctxB.newPage();
  const loginApi = [];
  pageB.on("response", (res) => {
    const u = res.url();
    if (!u.includes("/api/")) return;
    loginApi.push({ path: u.replace(API, "").split("?")[0], status: res.status() });
  });
  const lastStatus = (arr, p) => {
    const hits = arr.filter((c) => c.path === p);
    return hits.length ? hits[hits.length - 1].status : null;
  };

  await pageB.goto(`${APP}/login`, { waitUntil: "load", timeout: 60000 });
  await pageB.waitForTimeout(3000);
  const loginRendered = await bodyText(pageB).catch(() => "");
  step("Login page renders AuthModal Sign In",
    ci(loginRendered, "Welcome Back") || ci(loginRendered, "Sign In with Password") || ci(loginRendered, "TradeThrone"),
    `bodyLen=${loginRendered.length}`);

  const identF = pageB.locator("input[placeholder*='trader' i], input[type='email']").first();
  const passF = pageB.locator("input[type='password']").first();
  const submitB = pageB.locator("button[type='submit']").first();
  const formVisible =
    (await identF.isVisible({ timeout: 8000 }).catch(() => false)) &&
    (await passF.isVisible().catch(() => false)) &&
    (await submitB.isVisible().catch(() => false));
  step("Sign In form (identifier + password + submit) visible", formVisible, "");
  if (formVisible) {
    await identF.fill(email);
    await passF.fill(password);
    await submitB.click();
    await pageB.waitForTimeout(7000);
    const afterLogin = await bodyText(pageB).catch(() => "");
    step("UI password login POST /api/auth/login = 200", lastStatus(loginApi, "/api/auth/login") === 200,
      `status=${lastStatus(loginApi, "/api/auth/login")}`);
    // After an SPA in-memory login the app does NOT re-hit /api/auth/me at boot
    // (AuthContext updates from the login response). The session evidence is:
    // authenticated dashboard data (positions fetch = 200) + zero 401/403.
    const uiSessionOk =
      lastStatus(loginApi, "/api/trades/positions") === 200 &&
      !loginApi.some((c) => c.status === 401 || c.status === 403);
    step("Session valid after UI login (positions fetch 200, no 401/403)", uiSessionOk,
      `me=${lastStatus(loginApi, "/api/auth/me")} positions=${lastStatus(loginApi, "/api/trades/positions")} 4xx=${loginApi.filter((c) => c.status >= 400).length}`);
    step("Dashboard loads after UI login",
      ci(afterLogin, "Open Positions") || ci(afterLogin, "Dashboard") || ci(afterLogin, "Market"),
      `bodyLen=${afterLogin.length}`);
  } else {
    step("UI password login POST /api/auth/login = 200", false, "login form not visible — cannot submit");
  }
  await ctxB.close().catch(() => {});

  // ────────────────────────────────────────────────────────────────────────────
  // PHASE D — PAPER protective-order E2E through the real UI + real routes
  // ────────────────────────────────────────────────────────────────────────────
  await page.goto(`${APP}/dashboard`, { waitUntil: "load", timeout: 60000 });
  await page.waitForTimeout(3500);
  const dashD = await bodyText(page);
  step("OrderTerminal (DMA) present on dashboard",
    ci(dashD, "DMA Terminal") || ci(dashD, "Institutional DMA"), "");

  // D1 — PLACE SL/TP at entry: the DMA terminal is the ONLY UI control that
  // attaches SL/TP to a position (manual /api/trades/order has no SL/TP fields
  // by contract; Pydantic silently ignores extras).
  const sltpFound = await page.evaluate(() => {
    const t = Array.from(document.querySelectorAll("label")).map((l) => l.textContent.trim());
    return t.some((x) => /stop-loss/i.test(x)) && t.some((x) => /take-profit/i.test(x));
  });
  step("PLACE SL/TP controls present (OrderTerminal Stop-Loss % / Take-Profit %)",
    sltpFound, sltpFound ? "" : "no SL/TP inputs rendered in terminal");

  const terminalState = await page.evaluate(() => {
    const btn = Array.from(document.querySelectorAll("button")).find((b) => /TRANSMIT|Awaiting market feed|Routing to broker/i.test(b.textContent));
    return { text: btn ? btn.textContent.trim().replace(/\s+/g, " ") : null, disabled: btn ? btn.disabled : null };
  });
  const transmitEnabled = !!terminalState.text && terminalState.disabled === false && /TRANSMIT/.test(terminalState.text);
  step("OrderTerminal transmit availability determined", !!terminalState.text,
    terminalState.text ? `"${terminalState.text}" disabled=${terminalState.disabled}` : "transmit button not found");

  let uiDmaOk = null;
  if (transmitEnabled) {
    await page.locator("button:has-text('TRANSMIT')").first().click();
    await page.waitForTimeout(5000);
    const resp = apiStatus("/api/v1/orders/execute-dma");
    step("PAPER DMA order placed via UI terminal (with SL/TP)", resp === 200, `status=${resp}`);
    uiDmaOk = resp === 200;
  } else {
    step("PAPER DMA order placed via UI terminal (with SL/TP)", false,
      "cannot click — TRANSMIT disabled (fail-closed, no live quote)");
  }
  if (uiDmaOk !== true) {
    const seedResp = await fetch(`${API}/api/v1/orders/execute-dma`, {
      method: "POST", headers,
      body: JSON.stringify({ symbol: "NIFTY50", side: "BUY", lots: 1, product: "MIS", order_type: "MARKET", limit_price: null, stop_loss_pct: 0.5, take_profit_pct: 1.0, mode: "PAPER" }),
    });
    step("PAPER DMA order seeded via API (payload identical to OrderTerminal)", seedResp.ok, `status=${seedResp.status}`);
  }
  await sleep(2500);

  // D2 — backend truth after entry: SL/TP persisted, protection state honest.
  const posRes2 = await fetch(`${API}/api/trades/positions`, { headers });
  const posList2 = await posRes2.json();
  const dmaPos = (Array.isArray(posList2) ? posList2 : []).find((p) => p.symbol === "NIFTY50") || null;
  const dmaPosId = dmaPos ? dmaPos.id : null;
  step("PAPER DMA position open with SL/TP levels persisted",
    !!dmaPos && dmaPos.stop_loss_price != null && dmaPos.take_profit_price != null,
    dmaPos ? `sl=${dmaPos.stop_loss_price} tp=${dmaPos.take_profit_price}` : "no NIFTY50 position found");
  step("PAPER protection state honest (no broker arm in PAPER → UNPROTECTED)",
    !!dmaPos && (dmaPos.protection_state || "UNPROTECTED") === "UNPROTECTED",
    dmaPos ? `state=${dmaPos.protection_state}` : "no position");

  // D3 — REPLACE SL/TP exactly as the chart drag does (PATCH risk-targets).
  if (dmaPosId && dmaPos) {
    const newSl = Number((dmaPos.stop_loss_price - 5).toFixed(2));
    const newTp = Number((dmaPos.take_profit_price + 10).toFixed(2));
    const patchRes = await fetch(`${API}/api/v1/orders/positions/${dmaPosId}/risk-targets`, {
      method: "PATCH", headers,
      body: JSON.stringify({ stop_loss_price: newSl, take_profit_price: newTp }),
    });
    step("REPLACE SL/TP via chart-drag endpoint PATCH risk-targets = 200",
      patchRes.status === 200, `status=${patchRes.status}`);
    const posRes3 = await fetch(`${API}/api/trades/positions`, { headers });
    const posList3 = await posRes3.json();
    const dmaPos3 = (Array.isArray(posList3) ? posList3 : []).find((p) => p.id === dmaPosId) || null;
    step("Backend reflects replaced SL/TP",
      !!dmaPos3 && dmaPos3.stop_loss_price === newSl && dmaPos3.take_profit_price === newTp,
      dmaPos3 ? `sl=${dmaPos3.stop_loss_price} tp=${dmaPos3.take_profit_price}` : "position lost after replace");
    const badRes = await fetch(`${API}/api/v1/orders/positions/${dmaPosId}/risk-targets`, {
      method: "PATCH", headers,
      body: JSON.stringify({ stop_loss_price: (dmaPos3 ? dmaPos3.entry_price : 0) + 1000 }),
    });
    step("Invalid REPLACE rejected fail-closed (LONG SL above entry → 422)",
      badRes.status === 422, `status=${badRes.status}`);
  } else {
    step("REPLACE SL/TP via chart-drag endpoint PATCH risk-targets = 200", false, "no DMA position to replace");
  }

  // ────────────────────────────────────────────────────────────────────────────
  // PHASE D4 — CANCEL SL/TP (honest gap check) + D5 — CLOSE via the real UI
  // ────────────────────────────────────────────────────────────────────────────
  // D5 — CLOSE positions through the real OpenPositionsPanel Close button,
  // then verify terminal cleanup on BOTH the backend and the UI.
  await page.goto(`${APP}/dashboard`, { waitUntil: "load", timeout: 60000 });
  await page.waitForTimeout(4000);
  const openBefore = await (await fetch(`${API}/api/trades/positions`, { headers })).json();
  const toClose = (Array.isArray(openBefore) ? openBefore : []).filter((p) => p.status === "OPEN");
  for (const op of toClose) {
    try {
      const tables = page.locator("table");
      const n = await tables.count();
      let targetRow = null;
      for (let i = 0; i < n; i++) {
        const t = tables.nth(i);
        if ((await t.locator("tr", { hasText: op.symbol }).count()) > 0 && (await t.locator("button:has-text('Close')").count()) > 0) {
          targetRow = t.locator("tr", { hasText: op.symbol }).first();
          break;
        }
      }
      if (targetRow) {
        await targetRow.locator("button:has-text('Close')").first().click();
        await page.waitForTimeout(3500);
      } else {
        console.warn("close row not found for", op.symbol);
      }
    } catch (e) {
      console.warn("close click failed for", op.symbol, String(e).slice(0, 200));
    }
  }
  const close200 = apiCalls.filter((c) => c.path.includes("/positions/") && c.path.endsWith("/close") && c.status === 200).length;
  step("Close position via real UI button (OpenPositionsPanel Close)",
    toClose.length === 0 ? true : close200 >= toClose.length,
    toClose.length ? `close HTTP 200 count=${close200} of ${toClose.length}` : "no OPEN positions to close");
  await page.waitForTimeout(2000);
  const openAfter = await (await fetch(`${API}/api/trades/positions`, { headers })).json();
  const stillOpen = (Array.isArray(openAfter) ? openAfter : []).filter((p) => p.status === "OPEN").length;
  step("Backend terminal cleanup: no OPEN positions remain", stillOpen === 0, `open=${stillOpen}`);
  await page.reload({ waitUntil: "load", timeout: 60000 });
  await page.waitForTimeout(3500);
  const dashAfterClose = await bodyText(page);
  step("UI terminal cleanup: positions panel shows empty state", ci(dashAfterClose, "No Open Positions"),
    `bodyLen=${dashAfterClose.length}`);

  // ────────────────────────────────────────────────────────────────────────────
  // PHASE E — FAIL-CLOSED UI + mobile viewports
  // ────────────────────────────────────────────────────────────────────────────
  const modePill = await page.evaluate(() => {
    const el = Array.from(document.querySelectorAll("span,div")).find((x) => /PAPER SIMULATION|LIVE EXECUTION/.test(x.textContent) && x.children.length === 0);
    return el ? el.textContent.trim() : null;
  });
  step("Execution mode pill honest with no broker (PAPER SIMULATION)",
    modePill === "PAPER SIMULATION", modePill ? `pill="${modePill}"` : "mode pill not found");

  for (const vp of VIEWPORTS) {
    await page.setViewportSize({ width: vp.width, height: vp.height });
    console.log(`\n── Viewport: ${vp.label} ──`);
    const mobileRoutes = [
      { url: "/dashboard", good: ["Dashboard", "Market", "Open Positions", "Trade"] },
      { url: "/portfolio", good: ["Portfolio", "Net Worth", "Account"] },
      { url: "/execution", good: ["Live Execution", "Risk Sentinel", "Execution"] },
    ];
    for (const r of mobileRoutes) {
      await page.goto(`${APP}${r.url}`, { waitUntil: "load", timeout: 60000 });
      await page.waitForTimeout(3000);
      const txt = await bodyText(page);
      const over = await page.evaluate(() => document.scrollingElement.scrollWidth > window.innerWidth + 2);
      step(`[${vp.label}] ${r.url} renders`, r.good.some((m) => ci(txt, m)), `bodyLen=${txt.length}`);
      step(`[${vp.label}] ${r.url} no horizontal overflow`, !over,
        `scrollW=${await page.evaluate(() => document.scrollingElement.scrollWidth)} vw=${vp.width}`);
    }
  }

  const ctxLogin = await browser.newContext({ viewport: { width: 390, height: 844 } });
  const pageLogin = await ctxLogin.newPage();
  for (const vp of VIEWPORTS) {
    await pageLogin.setViewportSize({ width: vp.width, height: vp.height });
    await pageLogin.goto(`${APP}/login`, { waitUntil: "load", timeout: 60000 });
    await pageLogin.waitForTimeout(2500);
    const txt = await bodyText(pageLogin);
    const over = await pageLogin.evaluate(() => document.scrollingElement.scrollWidth > window.innerWidth + 2);
    step(`[${vp.label}] /login renders Sign In`, ci(txt, "Welcome Back") || ci(txt, "Sign In") || ci(txt, "TradeThrone"), `bodyLen=${txt.length}`);
    step(`[${vp.label}] /login no horizontal overflow`, !over, "");
  }
  await ctxLogin.close().catch(() => {});

  // 8. Error audit (authenticated pages during the whole run)
  step("pageerrors: 0", pageErrors.length === 0, pageErrors.slice(0, 3).join(" | "));
  const benign = ["Failed to load resource","WebSocket","net::","fetch","401","403","429",
    "AbortError","favicon","loading chunk","ERR_","Connection closed","proxy","ECONNREFUSED"];
  const unexpected = consoleErrors.filter((c) => !benign.some((b) => c.toLowerCase().includes(b.toLowerCase())));
  step("console errors: no unexpected exceptions", unexpected.length === 0, unexpected.slice(0, 3).join(" | "));

  // 7. Summary
  await browser.close();
  const fails = results.filter((r) => !r.ok).length;
  console.log(`\nRESULT: ${results.length - fails}/${results.length} steps passed`);
}

main().catch(async (e) => {
  console.error("E2E FATAL:", e);
  if (__browser) { try { await __browser.close(); } catch { /* */ } __browser = null; }
  process.exitCode = 1;
});
