/**
 * TradeThrone — Production E2E Browser Smoke Test
 * Target: https://tradethrone.vercel.app (Vercel) + https://tradetron-8jkz.onrender.com (API)
 *
 * Covers the full trader journey:
 *   landing → register (OTP-gate UI) → login → dashboard → price integrity →
 *   watchlist (add symbol) → strategies → marketplace → deployment modal
 *   (dynamic broker resolution, no hardcoded Angel One) → broker sessions →
 *   paper order → positions (close) → trade history → settings → portfolio →
 *   logout → re-login → mobile viewport → error/empty/loading states.
 *
 * Run:  set NODE_PATH to playwright's node_modules, then `node e2e-smoke.mjs`
 */
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const BASE = "https://tradethrone.vercel.app";
const API = "https://tradetron-8jkz.onrender.com";
const EMAIL = "e2esmoke1789018271@tt-e2e.in";
const PASSWORD = "SmokeTest@2026";
const SHOT_DIR = fileURLToPath(new URL("./e2e-shots/", import.meta.url));

const results = [];
let stepIndex = 0;

function step(name, ok, detail = "") {
  stepIndex += 1;
  results.push({ name, ok, detail });
  const mark = ok ? "PASS" : "FAIL";
  console.log(`[${mark}] ${stepIndex}. ${name}${detail ? ` — ${detail}` : ""}`);
  if (!ok) process.exitCode = 1;
}

(async () => {
  const browser = await chromium.launch({ channel: "chrome", headless: true });
  const ctx = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    locale: "en-IN",
    serviceWorkers: "allow",
  });
  const page = await ctx.newPage();
  const consoleErrors = [];
  const pageErrors = [];
  const failedRequests = [];
  const httpErrors = [];
  const deployResponses = [];
  page.on("console", (m) => {
    if (m.type() === "error") consoleErrors.push(m.text());
  });
  page.on("pageerror", (e) => pageErrors.push(`${e.message} @ ${page.url()}`));
  page.on("requestfailed", (r) => {
    const u = r.url();
    if (u.includes("/v1/chat")) return;
    failedRequests.push(`${u} :: ${r.failure()?.errorText || "failed"}`);
  });
  page.on("response", (r) => {
    const u = r.url();
    if (r.status() >= 400) {
      httpErrors.push(`${r.status()} ${u.split("?")[0]}`);
    }
    if (/\/api\/strategies\/[^/]+\/deploy$/.test(u)) {
      deployResponses.push(`${r.status()} ${u}`);
    }
  });

  try {
    // 1. BACKEND PRE-FLIGHT
    const health = await (await fetch(`${API}/api/health`)).json();
    step(
      "Backend health: engine running + simulated broker mode",
      health?.status === "healthy" && !!health?.engine_running && health?.broker_mode === "simulated",
      JSON.stringify({ status: health?.status, broker_mode: health?.broker_mode, engine_running: health?.engine_running })
    );
    const md = await (await fetch(`${API}/api/market-data`)).json();
    const niftyEntry = md?.market?.find((m) => m.symbol === "NIFTY50");
    const liveNifty = niftyEntry?.price;
    step("Market data: NIFTY50 live price from backend", liveNifty > 0, liveNifty ? `LTP=${liveNifty}` : "no NIFTY50");

    // 2. LANDING PAGE (unauthenticated)
    await page.goto(BASE, { waitUntil: "load", timeout: 90000 });
    await page.waitForSelector("text=TradeThrone", { state: "attached", timeout: 60000 }).catch(() => {});
    await page.waitForTimeout(3000);
    const landingText = await page.evaluate(() => (document.querySelector("body")?.innerText || "").slice(0, 400).replace(/\s+/g, " "));
    const shellOk = landingText.includes("TradeThrone") || landingText.includes("Trade") || landingText.length > 40;
    step("Landing: app shell loads (header/body content present)", shellOk, `bodyPreview="${landingText.slice(0, 80)}"`);
    await page.screenshot({ path: `${SHOT_DIR}01-landing.png`, fullPage: true });
    // A 401 on the pre-auth /api/positions fetch is EXPECTED (public shell,
    // protected endpoint) and handled internally by the app — only true JS
    // pageerrors are fatal.
    const fatalPageErrors = pageErrors.filter((e) => !/401|Failed to load resource/i.test(e));
    step("Landing: no fatal JS page error on first paint", fatalPageErrors.length === 0, fatalPageErrors.join(" | "));
// 3. REGISTER UI → OTP-gate transition
    await page.goto(`${BASE}/login`, { waitUntil: "load", timeout: 90000 });
    await page.waitForSelector("button:has-text('Register'):visible", { timeout: 30000 });
    step("Register: AuthModal opens on /login with Register tab", true);

    await page.click("button:has-text('Register'):visible");
    await page.waitForSelector("button:has-text('Create Account & Send OTP'):visible", { state: "visible", timeout: 10000 });
    await page.waitForTimeout(300);
    await page.fill('input[placeholder="Trader Name"]', `Reg UI ${Date.now() % 100000}`);
    const regEmail = `regui${Date.now()}@tt-e2e.in`;
    await page.fill('input[placeholder="trader@tradetron.io"]', regEmail);
    await page.fill('input[placeholder="Minimum 6 characters"]', PASSWORD);
    await page.click("button:has-text('Create Account & Send OTP')");
    const otpGate = await page
      .waitForSelector("text=Verification code sent to", { state: "visible", timeout: 30000 })
      .then(() => true)
      .catch(() => false);
    await page.waitForSelector("text=Verify & Activate Account", { state: "visible", timeout: 10000 }).catch(() => {});
    step(
      "Register: submits and transitions to OTP verification gate (prod requires email OTP)",
      otpGate,
      otpGate ? `OTP sent to ${regEmail}` : "no verification-success message"
    );
    await page.screenshot({ path: `${SHOT_DIR}02-register-otp-gate.png` });

    // Register error-path: duplicate email shows inline error (null-safety)
    await page.click("button:has-text('Change Email / Back')");
    await page.waitForTimeout(300);
    await page.fill('input[placeholder="Trader Name"]', "Dup User");
    await page.fill('input[placeholder="trader@tradetron.io"]', EMAIL);
    await page.fill('input[placeholder="Minimum 6 characters"]', PASSWORD);
    await page.click("button:has-text('Create Account & Send OTP')");
    const dupErr = await page
      .waitForSelector("text=Email address or phone number is already registered", { state: "visible", timeout: 30000 })
      .then(() => true)
      .catch(() => false);
    step("Register: duplicate email surfaces inline error (not a crash)", dupErr, dupErr ? `duplicate: ${EMAIL}` : "no error banner shown");
// 4. LOGIN VIA UI
    await page.click('button:text-is("Sign In")');
    await page.waitForTimeout(300);
    await page.fill('input[placeholder="trader@tradetron.io"]', EMAIL);
    await page.fill('input[type="password"]', PASSWORD);
    await page.click('button:text-is("Sign In with Password")');
    await page.waitForURL("**/dashboard**", { timeout: 30000 });
    step("Login: signs in via UI and lands on /dashboard", true);
    await page.waitForTimeout(2500);

    // 5. DASHBOARD + PRICE INTEGRITY
    await page.waitForSelector("text=Institutional DMA Terminal", { timeout: 25000 });
    step("Dashboard: OrderTerminal (DMA) present", true);
    await page.screenshot({ path: `${SHOT_DIR}03-dashboard.png`, fullPage: true });

    // Price integrity: NIFTY50 LTP must track the live backend price and never
    // show the fabricated 24850 as the current LTP.
    const ltpOk = await page
      .waitForFunction(
        (live) => {
          const txt = document.querySelector("body")?.innerText || "";
          const m = txt.match(/LTP: ₹([\d,]+\.\d{2})/);
          if (!m) return false;
          const shown = Number(m[1].replace(/,/g, ""));
          const liveN = Number(live);
          return Math.abs(shown - liveN) / liveN < 0.02 && Math.abs(shown - 24850) > 1;
        },
        String(liveNifty),
        { timeout: 30000 }
      )
      .then(() => true)
      .catch(() => false);
    const ltpShown = await page.evaluate(() => {
      const m = (document.querySelector("body")?.innerText || "").match(/LTP: ₹[\d,]+\.\d{2}/);
      return m ? m[0] : "(none)";
    });
    step(
      "Price integrity: live NIFTY50 LTP matches backend band (≠ fabricated 24850)",
      ltpOk,
      `shown=${ltpShown} backend=${liveNifty}`
    );

    // Awaiting-feed guard: dashboard shows live LTP OR literal "Awaiting feed…"
    // placeholder — never a fabricated number.
    await page.waitForSelector("text=Awaiting feed", { state: "attached", timeout: 8000 }).catch(() => {});
    const awaitingOrLive = await page.evaluate(() => {
      const txt = document.querySelector("body")?.innerText || "";
      return { hasAwait: /Awaiting feed/.test(txt), hasLtp: /LTP: ₹[\d,]+\.\d{2}/.test(txt) };
    });
    step(
      "Price integrity: 'Awaiting feed' placeholder OR live LTP shown (never fabricated)",
      awaitingOrLive.hasAwait || awaitingOrLive.hasLtp,
      JSON.stringify(awaitingOrLive)
    );
// 6. WATCHLIST — add a symbol via universal search
    await page.goto(`${BASE}/watchlist`, { waitUntil: "domcontentloaded", timeout: 60000 });
    await page.waitForSelector("text=Universal Market Watchlist", { timeout: 25000 });
    step("Watchlist: page renders", true);
    const searchInput = page.locator('input[placeholder^="Search any symbol"]');
    // Try candidates in order so the test is resilient to leftover state from
    // previous runs (a symbol already present returns 400 "already in watchlist").
    const wlCandidates = ["RELIANCE", "TCS", "INFY", "TRENT", "ZOMATO"];
    let added = false;
    let addDetail = "no candidate returned 201";
    for (const cand of wlCandidates) {
      await searchInput.fill(cand);
      // Debounced (350ms) search + backend instrument lookup — wait for the
      // Watch action instead of a fixed 1s guess (Render can be slow).
      const watchBtn = page.locator('button:has-text("Watch")').first();
      const watchVisible = await watchBtn.waitFor({ state: "visible", timeout: 20000 }).then(() => true).catch(() => false);
      step(
        `Watchlist: search returns ${cand} results with Watch action`,
        watchVisible,
        `${cand} search awaited up to 20s`
      );
      if (!watchVisible) continue;
      const addResp = page.waitForResponse(
        (r) => r.request().method() === "POST" && /\/api\/watchlist$/.test(r.url()),
        { timeout: 15000 }
      ).catch(() => null);
      await watchBtn.click();
      const resp = await addResp;
      const toastOk = await page.waitForSelector("text=added to your active watchlist", { state: "visible", timeout: 12000 }).then(() => true).catch(() => false);
      if ((resp && resp.status() === 201) || toastOk) {
        added = true;
        addDetail = `${cand} POST /api/watchlist ${resp?.status ?? "toast"}`;
        break;
      }
      addDetail = `${cand} add -> ${resp ? `HTTP ${resp.status}` : "no response"}`;
      await page.waitForTimeout(1200);
    }
    step("Watchlist: symbol added successfully", added, addDetail);
    await page.screenshot({ path: `${SHOT_DIR}04-watchlist.png`, fullPage: true });

    // 7. STRATEGIES PAGE
    await page.goto(`${BASE}/strategies`, { waitUntil: "domcontentloaded", timeout: 60000 });
    await page.waitForTimeout(3000);
    const stratText = await page.evaluate(() => document.querySelector("body")?.innerText || "");
    step("Strategies: page renders (own strategy list)", /strateg|deploy|create/i.test(stratText));
    await page.screenshot({ path: `${SHOT_DIR}05-strategies.png`, fullPage: true });
// 8. MARKETPLACE + DEPLOYMENT MODAL (dynamic broker resolution)
    await page.goto(`${BASE}/marketplace`, { waitUntil: "domcontentloaded", timeout: 60000 });
    await page.waitForSelector("button:has-text('Deploy')", { timeout: 30000 });
    step("Marketplace: strategy cards with Deploy visible", true);
    await page.locator("button:has-text('Deploy')").first().click();
    await page.waitForSelector("text=Deploy Strategy", { state: "visible", timeout: 15000 });
    step("DeploymentModal: opens with selected strategy", true);
    await page.waitForTimeout(800);

    // Broker resolution: no connected broker for this fresh user → only
    // "Simulated Mock Broker" option; no hardcoded Angel One anywhere.
    const deployOptions = await page.evaluate(() => {
      const modal = Array.from(document.querySelectorAll("h2"))
        .find((h) => h.innerText.includes("Deploy Strategy"))
        ?.closest("div[class*=card]") ||
        Array.from(document.querySelectorAll(".fixed.inset-0"))[0];
      const select = modal?.querySelector("select");
      const options = select ? Array.from(select.options).map((o) => o.text.trim()) : [];
      const optsText = modal?.innerText || "";
      return { options, optsText };
    });
    const onlySimulated = deployOptions.options.length === 1 && /Simulated/.test(deployOptions.options[0]);
    const hasAngelInModal = /Angel One/i.test(deployOptions.optsText);
    step(
      "Broker resolution: deploy select shows ONLY Simulated (no broker connected)",
      onlySimulated,
      `options=[${deployOptions.options.join(" | ")}]`
    );
    step("Broker resolution: no hardcoded 'Angel One' inside deployment modal", !hasAngelInModal);

    // LIVE tab without a broker → must not invent a broker name.
    await page.click("button:has-text('Live Broker')");
    await page.waitForTimeout(700);
    const liveBtnText = await page.evaluate(() => {
      const btn = Array.from(document.querySelectorAll("button")).find((b) => b.innerText.trim().startsWith("Live Broker"));
      return btn ? btn.innerText.trim() : "";
    });
    step("Broker resolution: LIVE w/o connected broker does not fabricate a target", !/Live Broker \([A-Za-z ]+\)/.test(liveBtnText), `liveBtn="${liveBtnText}"`);
    await page.screenshot({ path: `${SHOT_DIR}06-deployment-live.png` });

    // Submit LIVE w/o broker → backend must reject (fail-closed), not fall back to Angel One.
    // With the DeploymentModal fix, the frontend sends broker_name="Simulated"
    // (never null), so the Pydantic layer accepts the payload and the backend's
    // business-logic guard returns the canonical 400 "Cannot deploy to Live
    // Mode without a connected broker account" — a deterministic, authenticated
    // fail-closed rejection (a 401/422/500 would indicate a different defect).
    await page.click("button:has-text('Confirm & Deploy to Engine')");
    const rejectResp = await page
      .waitForResponse((r) => /\/api\/strategies\/[^/]+\/deploy$/.test(r.url()) && r.status() >= 400, { timeout: 30000 })
      .then(async (r) => {
        const body = await r.json().catch(() => ({}));
        const detail = Array.isArray(body.detail)
          ? body.detail.map((d) => d.msg || JSON.stringify(d)).join("; ")
          : String(body.detail || "");
        return { status: r.status(), detail };
      })
      .catch(() => ({ status: "none", detail: "" }));
    await page.waitForTimeout(800);
    const rejectShown = await page.evaluate(() => {
      const txt = document.querySelector("body")?.innerText || "";
      return /Cannot deploy|rejected|HTTP \d{3}|broker account|failed|error/i.test(txt);
    });
    const failClosed =
      rejectResp.status === 400 &&
      /broker/i.test(rejectResp.detail) &&
      rejectShown;
    step(
      "Deployment: LIVE w/o broker rejected by backend (fail-closed, deterministic 400)",
      failClosed,
      `http=${rejectResp.status} detail="${rejectResp.detail.slice(0, 90)}" inlineError=${rejectShown}`
    );
    await page.waitForTimeout(700);

    // Deploy in PAPER mode and confirm success path
    await page.click("button:has-text('Paper Trading (Simulated)')");
    await page.click("button:has-text('Confirm & Deploy to Engine')");
    const deployedOk = await page
      .waitForSelector("text=Strategy Deployed!", { state: "visible", timeout: 30000 })
      .then(() => true)
      .catch(() => false);
    step("Deployment: PAPER deploy succeeds end-to-end", deployedOk);
    await page.screenshot({ path: `${SHOT_DIR}07-deploy-success.png` });
    await page.waitForTimeout(1500);
// 9. BROKER SESSIONS (Angel One legitimately listed in registry metadata)
    await page.goto(`${BASE}/broker-sessions`, { waitUntil: "domcontentloaded", timeout: 60000 });
    await page.waitForTimeout(3500);
    const brokerNames = await page.evaluate(() => {
      const txt = document.querySelector("body")?.innerText || "";
      return {
        angel: /Angel One/i.test(txt),
        zerodha: /Zerodha/i.test(txt),
        binance: /Binance/i.test(txt),
        upstox: /Upstox/i.test(txt),
      };
    });
    step(
      "Broker Sessions: registry metadata renders (Angel One is a broker, not a deployment target)",
      Object.values(brokerNames).some(Boolean),
      JSON.stringify(brokerNames)
    );
    await page.screenshot({ path: `${SHOT_DIR}08-broker-sessions.png`, fullPage: true });

    // 10. PAPER ORDER + POSITION CLOSE
// Deterministic leftover-state cleanup for the shared OTP-legacy account:
    // registration cannot complete in prod, so EVERY run signs in as
    // e2esmoke1789018271@tt-e2e.in — an OPEN position abandoned by an
    // interrupted previous run would otherwise keep the panel non-empty even
    // after THIS run's own position closes, making the empty-state assertion
    // inherit a different run's book. Close every OPEN position via the API
    // now (same pattern as the watchlist cleanup below), then wait until the
    // backend reports zero OPEN positions. The dashboard is mounted AFTER
    // this, so the panel starts from a deterministic zero-position book.
    const posCleanup = await page.evaluate(async (api) => {
      const token = localStorage.getItem("tradetron_access_token") || "";
      const headers = { Authorization: `Bearer ${token}` };
      const listRes = await fetch(`${api}/api/trades/positions`, { headers });
      if (!listRes.ok) return { error: `list ${listRes.status}` };
      const positions = await listRes.json();
      if (!Array.isArray(positions)) return { error: "non-array-list", raw: positions };
      const closed = [];
      for (const p of positions) {
        const r = await fetch(`${api}/api/trades/positions/${encodeURIComponent(p.id)}/close`, {
          method: "POST",
          headers,
        });
        closed.push(`${p.symbol}:${p.id.slice(0, 8)}:${p.status}:${r.status}`);
      }
      return { before: positions.length, results: closed };
    }, API);
    const zeroOpenBefore = await page
      .waitForFunction(
        async (api) => {
          const token = localStorage.getItem("tradetron_access_token") || "";
          const res = await fetch(`${api}/api/trades/positions`, {
            headers: { Authorization: `Bearer ${token}` },
          });
          if (!res.ok) return false;
          const data = await res.json();
          return Array.isArray(data) && data.length === 0;
        },
        API,
        { timeout: 30000, polling: 750 }
      )
      .then(() => true)
      .catch(() => false);
    step(
      "Positions: deterministic setup — zero OPEN positions before journey",
      zeroOpenBefore,
      JSON.stringify(posCleanup)
    );
    await page.goto(`${BASE}/dashboard`, { waitUntil: "domcontentloaded", timeout: 60000 });
    await page.waitForSelector("text=Institutional DMA Terminal", { timeout: 25000 });
    await page.waitForTimeout(1500);
    const transmit = page.locator("button:has-text('TRANSMIT BUY 65 NIFTY50')").first();
    const transmitVisible = await transmit.isVisible().catch(() => false);
    step("Order terminal: TRANSMIT BUY NIFTY50 available", transmitVisible);
    if (transmitVisible) {
      await transmit.click();
      const orderToast = await page
        .waitForSelector("text=filled @", { state: "visible", timeout: 30000 })
        .then(() => true)
        .catch(() => false);
      step("Order terminal: PAPER BUY fill confirmed via toast", orderToast);
      await page.waitForTimeout(2500);
      const hasPos = await page.evaluate(() => {
        const txt = document.querySelector("body")?.innerText || "";
        return /Open Positions/.test(txt) && /NIFTY50/.test(txt) && !/No Open Positions/.test(txt);
      });
      step("Positions: open NIFTY50 position appears in panel", hasPos);

      const closeBtn = page.locator('button[title="Close position at current market price"]').first();
      const closeVisible = await closeBtn
        .waitFor({ state: "visible", timeout: 20000 })
        .then(() => true)
        .catch(() => false);
      step("Positions: Close action available", closeVisible, closeVisible ? "Close button visible in panel" : "Close button not found in panel");
      if (closeVisible) {
        // Deterministic wait sequence (no arbitrary sleep):
        //   1) the close API responds 2xx, 2) the positions API reports zero
        // OPEN positions, 3) THEN the empty-state UI must render.
        // Waiting on the UI text alone proves neither close HTTP success nor
        // backend convergence; each transition is awaited in order so a stale
        // panel, a failed close, or a refetch race is reported precisely.
        const closeResp = page
          .waitForResponse(
            (r) => r.request().method() === "POST" && /\/api\/trades\/positions\/[^/]+\/close$/.test(r.url()),
            { timeout: 30000 }
          )
          .catch(() => null);
        await closeBtn.click();
        const closeRes = await closeResp;
        const zeroOpen = await page
          .waitForFunction(
            async (api) => {
              const token = localStorage.getItem("tradetron_access_token") || "";
              const res = await fetch(`${api}/api/trades/positions`, {
                headers: { Authorization: `Bearer ${token}` },
              });
              if (!res.ok) return false;
              const data = await res.json();
              return Array.isArray(data) && data.length === 0;
            },
            API,
            { timeout: 30000, polling: 750 }
          )
          .then(() => true)
          .catch(() => false);
        const emptyUi = await page
          .waitForSelector("text=No Open Positions", { state: "visible", timeout: 15000 })
          .then(() => true)
          .catch(() => false);
        const closed = !!closeRes && closeRes.status() === 200 && zeroOpen && emptyUi;
        step(
          "Positions: position closed and panel shows empty state",
          closed,
          `closeHttp=${closeRes ? closeRes.status() : "none"} zeroOpen=${zeroOpen} emptyUi=${emptyUi}`
        );
      }
    }
    await page.screenshot({ path: `${SHOT_DIR}09-paper-order.png`, fullPage: true });
// 11. TRADE HISTORY
    await page.goto(`${BASE}/history`, { waitUntil: "domcontentloaded", timeout: 60000 });
    await page.waitForTimeout(2500);
    let hasTrade = false;
    for (let retry = 0; retry < 4 && !hasTrade; retry++) {
      hasTrade = await page.evaluate(() => {
        const txt = document.querySelector("body")?.innerText || "";
        return /NIFTY50/.test(txt);
      });
      if (!hasTrade) await page.waitForTimeout(2000);
    }
    step("Trade history: NIFTY50 trade appears", hasTrade, hasTrade ? "NIFTY50 row rendered" : "no NIFTY50 text within 10s");
    await page.screenshot({ path: `${SHOT_DIR}10-trade-history.png`, fullPage: true });

    // 12. SETTINGS — profile loads without null-crash
    await page.goto(`${BASE}/settings`, { waitUntil: "domcontentloaded", timeout: 60000 });
    await page.waitForTimeout(3000);
    const emailShown = await page.evaluate(
      (e) => (document.querySelector("body")?.innerText || "").includes(e),
      EMAIL
    );
    step("Settings: authenticated profile displays registered email", emailShown);
    await page.screenshot({ path: `${SHOT_DIR}11-settings.png`, fullPage: true });

    // 13. PORTFOLIO — balances/positions null-safe
    await page.goto(`${BASE}/portfolio`, { waitUntil: "domcontentloaded", timeout: 60000 });
    await page.waitForTimeout(3000);
    const portfolioOk = await page.evaluate(() => {
      const txt = document.querySelector("body")?.innerText || "";
      return /portfoli|exposure|balance|position|pnl|equity/i.test(txt);
    });
    step("Portfolio: page renders balance/exposure summary null-safe", portfolioOk);
    await page.screenshot({ path: `${SHOT_DIR}12-portfolio.png`, fullPage: true });
// 14. LOGOUT → RE-LOGIN round trip
    await page.goto(`${BASE}/settings`, { waitUntil: "domcontentloaded", timeout: 60000 });
    await page.waitForTimeout(2500);
    const logoutBtn = page.locator('button[title="Log Out"]');
    const logoutVisible = await logoutBtn.isVisible().catch(() => false);
    step("Logout: Log Out control visible in sidebar", logoutVisible);
    if (logoutVisible) {
      await logoutBtn.click();
      const wentLogin = await page
        .waitForSelector("text=You have been logged out.", { state: "visible", timeout: 20000 })
        .then(() => true)
        .catch(() => false);
      step("Logout: lands back on /login with notice", wentLogin);
      await page.waitForTimeout(500);
      await page.fill('input[placeholder="trader@tradetron.io"]', EMAIL);
      await page.fill('input[type="password"]', PASSWORD);
      await page.click("button:has-text('Sign In with Password')");
      await page.waitForURL("**/dashboard**", { timeout: 30000 });
      step("Re-login: returns to authenticated dashboard", true);
    }
// 15. MOBILE VIEWPORT (390x844)
    const mobCtx = await browser.newContext({ viewport: { width: 390, height: 844 }, locale: "en-IN", isMobile: true, hasTouch: true });
    const mobPage = await mobCtx.newPage();
    const mobErrors = [];
    mobPage.on("pageerror", (e) => mobErrors.push(String(e)));
    await mobPage.goto(`${BASE}/markets`, { waitUntil: "domcontentloaded", timeout: 75000 });
    await mobPage.waitForTimeout(4000);
    const mobShell = await mobPage.evaluate(() => {
      const txt = document.querySelector("body")?.innerText || "";
      return /TradeThrone/.test(txt);
    });
    step("Mobile: /markets renders at 390x844", mobShell, `pageerrors=${mobErrors.length}`);
    await mobPage.screenshot({ path: `${SHOT_DIR}13-mobile-markets.png`, fullPage: true });
    const mobHOverflow = await mobPage.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 2);
    step("Mobile: no horizontal overflow on /markets", !mobHOverflow, mobHOverflow ? "scrollWidth exceeds viewport" : "OK");
    await mobPage.goto(`${BASE}/dashboard`, { waitUntil: "domcontentloaded", timeout: 75000 });
    await mobPage.waitForTimeout(4000);
    const mobDash = await mobPage.evaluate(() => document.querySelector("body")?.innerText.includes("TradeThrone") || false);
    step("Mobile: dashboard loads at 390x844", mobDash, `pageerrors=${mobErrors.length}`);
    await mobPage.screenshot({ path: `${SHOT_DIR}14-mobile-dashboard.png`, fullPage: true });
    await mobCtx.close();
// 16. ERROR / EMPTY / LOADING STATE BATCH
    // (a) Remove all watchlist symbols, then verify empty placeholder — no crash
    await page.goto(`${BASE}/watchlist`, { waitUntil: "load", timeout: 90000 });
    await page.waitForTimeout(4000);
    // Snapshot the backend watchlist first (token from localStorage).
    const wlBefore = await page.evaluate(async () => {
      try {
        const token = localStorage.getItem("tradetron_access_token") || "";
        const res = await fetch("https://tradetron-8jkz.onrender.com/api/watchlist", {
          headers: { Authorization: `Bearer ${token}` },
        });
        if (!res.ok) return { error: res.status };
        const data = await res.json();
        const arr = Array.isArray(data) ? data : data.items || [];
        return { symbols: arr.map((x) => x.symbol) };
      } catch (e) {
        return { error: String(e) };
      }
    });
    // UI deletion loop (bounded, re-render tolerant).
    let uiDeletes = 0;
    for (let attempt = 0; attempt < 40; attempt++) {
      let removeBtn = page.locator('button[title="Remove from Watchlist"]').first();
      let removeVisible = await removeBtn.isVisible().catch(() => false);
      if (!removeVisible) {
        await page.waitForTimeout(1200);
        removeBtn = page.locator('button[title="Remove from Watchlist"]').first();
        removeVisible = await removeBtn.isVisible().catch(() => false);
        if (!removeVisible) break;
      }
      const delResp = page.waitForResponse(
        (r) => r.request().method() === "DELETE" && /\/api\/watchlist\//.test(r.url()),
        { timeout: 10000 }
      ).catch(() => null);
      await removeBtn.click();
      const dr = await delResp;
      if (dr) uiDeletes += 1;
      await page.waitForTimeout(1200);
    }
    step("Empty state: UI deletions issued for all visible watchlist rows", uiDeletes > 0 || (wlBefore.symbols || []).length === 0, `uiDeletes=${uiDeletes} before=${JSON.stringify(wlBefore)}`);
    // API cleanup fallback for any rows the UI pass could not reach (makes the
    // test deterministic regardless of stale frontend cache/state).
    const apiCleanup = await page.evaluate(async (syms) => {
      const token = localStorage.getItem("tradetron_access_token") || "";
      const results = [];
      const res = await fetch("https://tradetron-8jkz.onrender.com/api/watchlist", {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!res.ok) return { error: res.status };
      const data = await res.json();
      const arr = Array.isArray(data) ? data : data.items || [];
      for (const item of arr) {
        const r = await fetch(`https://tradetron-8jkz.onrender.com/api/watchlist/${encodeURIComponent(item.symbol)}`, {
          method: "DELETE",
          headers: { Authorization: `Bearer ${token}` },
        });
        results.push(`${item.symbol}:${r.status}`);
      }
      return { symbolExistsBefore: syms, results };
    }, (wlBefore.symbols || []));
    // UI state: after removal the empty placeholder must be visible.
    await page.waitForTimeout(2000);
    const emptyOk = await page.evaluate(() => {
      const txt = document.querySelector("body")?.innerText || "";
      return /Your watchlist is empty|search box above/i.test(txt);
    });
    step("Empty state: watchlist returns to empty placeholder after removal (null-safe)", emptyOk, emptyOk ? "placeholder visible" : `apiCleanup=${JSON.stringify(apiCleanup)}`);
    // Persistence: reload the watchlist and confirm the empty placeholder
    // still renders — the DELETE flow must have actually changed backend state.
    await page.reload({ waitUntil: "load", timeout: 90000 });
    await page.waitForTimeout(4500);
    const emptyPersists = await page.evaluate(() => {
      const txt = document.querySelector("body")?.innerText || "";
      return /Your watchlist is empty|search box above/i.test(txt);
    });
    // Backend-state proof: fetch /api/watchlist with the real session token.
    const wlBackendCount = await page.evaluate(async () => {
      try {
        const token = localStorage.getItem("tradetron_access_token") || "";
        const res = await fetch("https://tradetron-8jkz.onrender.com/api/watchlist", {
          headers: { Authorization: `Bearer ${token}` },
        });
        if (!res.ok) return -1;
        const data = await res.json();
        return Array.isArray(data) ? data.length : Array.isArray(data?.items) ? data.items.length : -2;
      } catch {
        return -3;
      }
    });
    step(
      "Empty state: placeholder persists after reload + backend returns zero symbols",
      emptyPersists && wlBackendCount === 0,
      `UI=${emptyPersists} backendCount=${wlBackendCount}`
    );
    // (b) loading state — navigate to an unvisited lazy route to trigger "Loading module…" skeleton
    const loadPage = await ctx.newPage();
    loadPage.on("pageerror", (e) => pageErrors.push(`${e.message} @ loading-state`));
    await loadPage.goto(`${BASE}/copy-trading`, { waitUntil: "commit", timeout: 60000 });
    // The lazy chunk loads asynchronously — check for loading text OR skeleton DOM within 4s
    const loadingOk = await loadPage.evaluate(async () => {
      // Check immediately
      const check = () => {
        const txt = document.querySelector("body")?.innerText || "";
        if (/Loading module|Awaiting feed|loading|skeleton|refreshing/i.test(txt)) return true;
        // Also check for skeleton-box CSS class elements in the DOM
        if (document.querySelectorAll(".skeleton-box, [role=status], .animate-spin").length > 0) return true;
        return false;
      };
      if (check()) return true;
      await new Promise((r) => setTimeout(r, 2000));
      if (check()) return true;
      await new Promise((r) => setTimeout(r, 2000));
      return check();
    });
    await loadPage.close();
    step("Loading state: feed/loading indicators rendered", loadingOk);
    // Confirm the lazy route actually RESOLVES (proves the fallback was real):
    // a fresh page to /copy-trading must eventually render strategy/copy-trading
    // content, not remain stuck on the suspended fallback.
    const resolvePage = await ctx.newPage();
    await resolvePage.goto(`${BASE}/copy-trading`, { waitUntil: "domcontentloaded", timeout: 60000 });
    let resolved = false;
    for (let i = 0; i < 8 && !resolved; i++) {
      await resolvePage.waitForTimeout(2000);
      resolved = await resolvePage.evaluate(() => {
        const txt = document.querySelector("body")?.innerText || "";
        // Page-specific CopyTrading strings (not the sidebar nav label),
        // and the "Loading module…" placeholder is gone.
        return /Master Fan-Out|Master Copy|Active Copy Groups|No public master copy groups|Copy Trading & Master/i.test(txt) && !/Loading module/.test(txt);
      });
    }
    await resolvePage.close();
    step("Lazy loading: copy-trading route resolves to real content (fallback was genuine)", resolved, resolved ? "chunk loaded & content rendered" : "stayed on fallback");
    await page.goto(`${BASE}/markets/NIFTY50`, { waitUntil: "domcontentloaded", timeout: 60000 });
    let detailOk = false;
    for (let retry = 0; retry < 4 && !detailOk; retry++) {
      await page.waitForTimeout(2000);
      detailOk = await page.evaluate(() => {
        const txt = document.querySelector("body")?.innerText || "";
        return /NIFTY50/.test(txt) && /trade|market|chart|option|buy|sell/i.test(txt);
      });
    }
    if (!detailOk) await page.screenshot({ path: `${SHOT_DIR}13-marketdetail-fail.png`, fullPage: true }).catch(() => {});
    step("Error/edge: MarketDetail renders symbol page null-safe", detailOk, detailOk ? "" : "NIFTY50 page text not matched within 8s");

    // Fatal-chart-guard: the lightweight-charts "reading 'year'" pageerror
    // (caused by setCrosshairPosition(..., undefined, ...) on v5.2.1) must
    // never appear anywhere during the whole journey.
    const lwYearErrors = pageErrors.filter((e) => /reading 'year'|lightweight-charts/i.test(e));
    step(
      "Chart safety: no lightweight-charts 'reading \\'year\\'' page error during full journey",
      lwYearErrors.length === 0,
      lwYearErrors.length ? lwYearErrors[0] : "no chart page errors"
    );
} catch (err) {
    step("E2E run", false, `FATAL: ${err.message.split("\n")[0]}`);
    try { await page.screenshot({ path: `${SHOT_DIR}FATAL-error.png`, fullPage: true }); } catch {}
  } finally {
    const pass = results.filter((r) => r.ok).length;
    const fail = results.filter((r) => !r.ok).length;
    console.log("\n══════════════════════════════════════════════════");
    console.log(`RESULTS: ${pass} passed / ${fail} failed / ${results.length} total`);
    if (consoleErrors.length) {
      console.log(`\nConsole errors (${consoleErrors.length}):`);
      console.log([...new Set(consoleErrors)].join("\n").slice(0, 3500));
    } else {
      console.log("\nNo console errors captured.");
    }
    if (pageErrors.length) {
      console.log(`\nPage errors (${pageErrors.length}):`);
      console.log([...new Set(pageErrors)].join("\n").slice(0, 3500));
    } else {
      console.log("No page errors.");
    }
    if (failedRequests.length) {
      console.log(`\nFailed requests (${failedRequests.length}):`);
      console.log([...new Set(failedRequests)].join("\n").slice(0, 3500));
    } else {
      console.log("No failed requests.");
    }
    if (httpErrors.length) {
      console.log(`\nHTTP errors (${httpErrors.length}):`);
      console.log([...new Set(httpErrors)].join("\n").slice(0, 3500));
    } else {
      console.log("No HTTP 4xx/5xx responses observed.");
    }
    const failedSteps = results.filter((r) => !r.ok).map((r) => `${r.name} :: ${r.detail}`);
    if (failedSteps.length) {
      console.log("\nFAILED STEPS:");
      failedSteps.forEach((f) => console.log("  ✗ " + f));
    }
    console.log("══════════════════════════════════════════════════");
    await browser.close();
  }
})();