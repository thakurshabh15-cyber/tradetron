/**
 * Targeted reproduction of the lightweight-charts "reading 'year'" error
 * on the live production MarketDetail / OptionChain page.
 * Run from .verify-live:  node repro-optionchain.mjs
 */
import { chromium } from "playwright";

const BASE = "https://tradethrone.vercel.app";
const EMAIL = "e2esmoke1789018271@tt-e2e.in";
const PASSWORD = "SmokeTest@2026";

const errors = [];

(async () => {
  const browser = await chromium.launch({ channel: "chrome", headless: true });
  const ctx = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    serviceWorkers: "allow",
  });
  const page = await ctx.newPage();

  page.on("pageerror", (e) => {
    const rec = { message: e.message, stack: (e.stack || "").split("\n").slice(0, 8).join("\n") };
    errors.push(rec);
    console.log(`\n=== PAGE ERROR: ${e.message}`);
    console.log(rec.stack);
  });
  page.on("console", (m) => {
    if (m.type() === "error") console.log(`[console.error] ${m.text().slice(0, 220)}`);
  });

  try {
    // ---- login ----
    await page.goto(`${BASE}/login`, { waitUntil: "load", timeout: 90000 });
    await page.waitForSelector('button:text-is("Sign In")', { timeout: 30000 });
    await page.click('button:text-is("Sign In")');
    await page.waitForTimeout(400);
    await page.fill('input[placeholder="trader@tradetron.io"]', EMAIL);
    await page.fill('input[type="password"]', PASSWORD);
    await page.click('button:text-is("Sign In with Password")');
    await page.waitForURL("**/dashboard**", { timeout: 30000 });
    await page.waitForTimeout(2500);
    console.log("Logged in; URL:", page.url());

    // ---- MarketDetail NIFTY50 (OptionChain + TradingChart) ----
    await page.goto(`${BASE}/markets/NIFTY50`, { waitUntil: "domcontentloaded", timeout: 60000 });
    await page.waitForTimeout(9000);
    console.log("\n[markets/NIFTY50] after 9s — title:", await page.title());
    await page.screenshot({ path: "repro-nifty50.png", fullPage: true });

    const chartInfo = await page.evaluate(() => {
      const body = document.querySelector("body")?.innerText || "";
      const hasChartText = /\bInstitutional\b|\bLIVE DMA\b/.test(body);
      const hasChainText = /Option Chain|Strike|CE OI|PE OI/.test(body);
      const errorText = body.match(/Cannot read properties of undefined \(reading 'year'\)/g) || [];
      return { hasChartText, hasChainText, yearErrorInstances: errorText.length };
    });
    console.log("NIFTY50 chartInfo:", JSON.stringify(chartInfo));

    // ---- Try additional symbols that stream real feeds ----
    for (const sym of ["BANKNIFTY", "RELIANCE", "BTCUSDT", "GOLD", "USDINR"]) {
      await page.goto(`${BASE}/markets/${sym}`, { waitUntil: "domcontentloaded", timeout: 90000 });
      await page.waitForTimeout(7000);
      const txt = await page.evaluate(() => (document.querySelector("body")?.innerText || "").slice(0, 300).replace(/\s+/g, " "));
      console.log(`\n[markets/${sym}] errors=${errors.length} pageText="${txt.slice(0, 140)}"`);
      if (errors.length > 0) break;
    }
  } catch (err) {
    console.log("\nFATAL:", err.message.split("\n")[0]);
  } finally {
    console.log(`\nTOTAL PAGE ERRORS: ${errors.length}`);
    await browser.close();
    process.exit(errors.length ? 1 : 0);
  }
})();