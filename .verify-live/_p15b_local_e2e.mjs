/**
 * Phase 15B LOCAL broker-state E2E — verifies the browser rendering of the
 * persisted, freshness-labelled broker_state snapshot on Dashboard + Portfolio
 * (PAPER / LIVE / STALE / ERROR) plus mobile. Uses the local backend (8080) and
 * local vite dev server (5173). The LIVE/STALE/ERROR snapshots are SYNTHETIC
 * local fixtures seeded directly into the dev SQLite DB (see _p15b_seed.py) —
 * they prove the UI rendering + API freshness wiring, NOT real broker access.
 */
import { execSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const API = "http://127.0.0.1:8080";
const APP = "http://127.0.0.1:5173";
const SEED_PY = fileURLToPath(new URL("./_p15b_seed.py", import.meta.url));
const PY = "C:\\Users\\HP\\Desktop\\tradetron\\fastapi-template\\.venv\\Scripts\\python.exe";
const FASTAPI = "C:\\Users\\HP\\Desktop\\tradetron\\fastapi-template";

const results = [];
function step(name, ok, detail = "") {
  results.push({ name, ok, detail });
  console.log(`[${ok ? "PASS" : "FAIL"}] ${name}${detail ? ` — ${detail}` : ""}`);
  if (!ok) process.exitCode = 1;
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
// Labels are rendered with CSS `text-transform: uppercase`, so compare
// case-insensitively. Numbers (en-IN grouping) are matched exactly.
const ci = (hay, needle) => String(hay).toUpperCase().includes(String(needle).toUpperCase());
// Middle dot rendered in LIVE badges ("LIVE · ZERODHA"). The older script had
// this stored as mojibake bytes; build it at runtime to stay encoding-proof.
const DOT = String.fromCharCode(0xb7);

// Resilient body-text reader: retries through slow Vite dev cold-starts instead
// of failing the whole run on one flaky 30s innerText timeout.
async function bodyText(page) {
  let lastErr;
  for (let i = 0; i < 6; i++) {
    try {
      return await page.locator("body").innerText({ timeout: 15000 });
    } catch (e) {
      lastErr = e;
    }
    await sleep(5000);
  }
  throw lastErr;
}

let __browser = null;

const email = `p15b_${Date.now()}@tt-e2e.in`;
const password = "SmokeTest@2026";

async function main() {
  // 0. Register a fresh paper user
  let reg;
  try {
    const regRes = await fetch(`${API}/api/auth/register`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password, full_name: "P15B Tester" }),
    });
    reg = await regRes.json();
    step("register paper user", regRes.status === 201 && !!reg.access_token, `status=${regRes.status}`);
  } catch (e) {
    step("register paper user", false, String(e));
    return;
  }
  const token = reg.access_token;
  const headers = { Authorization: `Bearer ${token}`, "Content-Type": "application/json" };
  const userId = reg.user?.id || "";

  // 1. API: summary equity block must be honest PAPER for a pure paper tenant
  const sum0 = await (await fetch(`${API}/api/dashboard/summary`, { headers })).json();
  step(
    "API summary equity = PAPER (paper tenant)",
    sum0?.equity?.status === "PAPER" && sum0?.equity?.total_equity === 1000000,
    JSON.stringify(sum0?.equity),
  );

  const browser = await chromium.launch({ headless: true });
  __browser = browser;
  // Warm up Vite first-load (dep-optimization) OUTSIDE the browser so the first
  // measured navigation is not a cold transform.
  try { await fetch(`${APP}/`, { signal: AbortSignal.timeout(45000) }); } catch { /* warm-up best effort */ }
  await sleep(2000);
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale: "en-IN" });
  const page = await ctx.newPage();
  const consoleErrors = [];
  const pageErrors = [];
  page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(m.text()); });
  page.on("pageerror", (e) => pageErrors.push(`${e.message} @ ${page.url()}`));

  await page.addInitScript(([t, r, u]) => {
    localStorage.setItem("tradetron_access_token", t);
    localStorage.setItem("tradetron_refresh_token", r);
    localStorage.setItem("tradetron_user", JSON.stringify(u));
  }, [token, `${token}_r`, reg.user || { id: userId, email, full_name: "P15B Tester", paper_balance: 1000000, is_verified: true, plan: "free", role: "trader" }]);

  // 2. Dashboard — PAPER strip
  await page.goto(`${APP}/dashboard`, { waitUntil: "load", timeout: 60000 });
  await page.waitForTimeout(3000);
  const dashBody = await bodyText(page);
  step("Dashboard PAPER strip: 'Broker Account State' + PAPER badge", ci(dashBody, "Broker Account State") && ci(dashBody, "PAPER"), ci(dashBody, "Broker Account State") ? "strip rendered" : "strip missing");
  step("Dashboard PAPER strip: Paper Balance value present", ci(dashBody, "Paper Balance") && (dashBody.includes("10,00,000") || dashBody.includes("1,000,000")), "");

  // 3. Portfolio — PAPER hero
  await page.goto(`${APP}/portfolio`, { waitUntil: "load", timeout: 60000 });
  await page.waitForTimeout(2500);
  const portBody = await bodyText(page);
  step("Portfolio PAPER hero: 'Net Worth (Paper)' + PAPER badge", ci(portBody, "Net Worth (Paper)") && ci(portBody, "PAPER"), "");
// 4. Seed LIVE broker fixture + verify Dashboard + Portfolio
  execSync(`"${PY}" "${SEED_PY}" ${userId} live`, { cwd: FASTAPI });
  await page.goto(`${APP}/dashboard`, { waitUntil: "load", timeout: 60000 });
  await page.waitForTimeout(3000);
  const dashLive = await bodyText(page);
  step("Dashboard LIVE strip: LIVE badge (ZERODHA, not STALE)", ci(dashLive, "LIVE") && ci(dashLive, "ZERODHA") && !ci(dashLive, "STALE") && dashLive.includes(`LIVE ${DOT} ZERODHA`), "");
  step("Dashboard LIVE strip: broker equity + available cash rendered", ci(dashLive, "Equity") && dashLive.includes("98,000.5") && dashLive.includes("75,000"), "");
  await page.goto(`${APP}/portfolio`, { waitUntil: "load", timeout: 60000 });
  await page.waitForTimeout(2500);
  const portLive = await bodyText(page);
  step("Portfolio LIVE hero: live broker net worth + available cash", ci(portLive, "Net Worth (Live") && ci(portLive, "ZERODHA") && ci(portLive, "available cash") && portLive.includes("75,000"), "");
  step("Portfolio LIVE risk: broker-truth margin usage", ci(portLive, "23,000 used") && ci(portLive, "broker-truth"), "");

  // 5. Seed STALE fixture
  execSync(`"${PY}" "${SEED_PY}" ${userId} stale`, { cwd: FASTAPI });
  await page.goto(`${APP}/dashboard`, { waitUntil: "load", timeout: 60000 });
  await page.waitForTimeout(2500);
  const dashStale = await bodyText(page);
  step("Dashboard STALE badge honest (not LIVE)", ci(dashStale, "STALE") && !dashStale.includes(`LIVE ${DOT} ZERODHA`), "");
  await page.goto(`${APP}/portfolio`, { waitUntil: "load", timeout: 60000 });
  await page.waitForTimeout(2500);
  const portStale = await bodyText(page);
  step("Portfolio STALE hero: title + amber tone", ci(portStale, "STALE") && ci(portStale, "Net Worth (Live"), `${ci(portStale, "STALE") ? "no-Net-Worth-Live-title" : "no-STALE-text"} | ${portStale.slice(0, 500)}`);

  // 6. Trigger a REAL in-app sync attempt → simulated mode must FAIL CLOSED (502):
  //    the real adapter path is hard-blocked while BROKER_MODE != live, producing an
  //    ERROR snapshot.  The 502 body carries the broker-mode guard message.
  const accounts = await (await fetch(`${API}/api/brokers/accounts`, { headers })).json();
  const bid = Array.isArray(accounts) ? accounts.find((a) => a.state?.status === "STALE")?.id : null;
  let syncStatus = "no-account";
  let syncBody = "";
  if (bid) {
    const syncRes = await fetch(`${API}/api/brokers/accounts/${bid}/sync`, { method: "POST", headers });
    syncStatus = String(syncRes.status);
    syncBody = (await syncRes.json().catch(() => ({}))).detail || "";
    step("POST /sync under BROKER_MODE=simulated fails closed (502)", syncRes.status === 502 && /BROKER_MODE/i.test(syncBody), `status=${syncRes.status} ${syncBody}`);
  } else {
    step("POST /sync under BROKER_MODE=simulated fails closed", false, `seeded account not listed | accounts=${JSON.stringify(accounts).slice(0, 300)}`);
  }
  await page.goto(`${APP}/dashboard`, { waitUntil: "load", timeout: 60000 });
  await page.waitForTimeout(2500);
  const dashErr = await bodyText(page);
  step("Dashboard ERROR badge surfaces honest broker-truth failure", ci(dashErr, "Broker Account State") && ci(dashErr, "ERROR"), ci(dashErr, "ERROR") ? "no-strip" : `no-ERROR | ${dashErr.slice(0, 400)}`);

  // 7. Mobile viewport
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(`${APP}/dashboard`, { waitUntil: "load", timeout: 60000 });
  await page.waitForTimeout(2500);
  const mDash = await bodyText(page);
  const hOverflow = await page.evaluate(() => document.scrollingElement.scrollWidth > window.innerWidth + 2);
  step("Mobile Dashboard: broker-state strip renders", ci(mDash, "Broker Account State"), "");
  step("Mobile Dashboard: no horizontal overflow", !hOverflow, `scrollW=${await page.evaluate(() => document.scrollingElement.scrollWidth)} vw=${await page.evaluate(() => window.innerWidth)}`);
  await page.goto(`${APP}/portfolio`, { waitUntil: "load", timeout: 60000 });
  await page.waitForTimeout(2500);
  const mPort = await bodyText(page);
  const hOverflow2 = await page.evaluate(() => document.scrollingElement.scrollWidth > window.innerWidth + 2);
  step("Mobile Portfolio: hero renders", ci(mPort, "Account Equity") || ci(mPort, "Net Worth"), "");
  step("Mobile Portfolio: no horizontal overflow", !hOverflow2, "");

  // 8. Console/page error audit
  step("pageerrors: 0", pageErrors.length === 0, pageErrors.slice(0, 3).join(" | "));
  const benign = ["Failed to load resource", "WebSocket", "net::", "fetch", "401", "403", "429", "AbortError", "favicon", "loading chunk", "ERR_", "Connection closed"];
  const unexpected = consoleErrors.filter((c) => !benign.some((b) => c.toLowerCase().includes(b.toLowerCase())));
  step("console errors: no unexpected exceptions", unexpected.length === 0, unexpected.slice(0, 3).join(" | "));

  await browser.close();
  const fails = results.filter((r) => !r.ok).length;
  console.log(`\nRESULT: ${results.length - fails}/${results.length} steps passed`);
}

main().catch(async (e) => {
  console.error("E2E FATAL:", e);
  if (__browser) {
    try { await __browser.close(); } catch { /* already closed */ }
    __browser = null;
  }
  process.exitCode = 1;
});