/**
 * Aggressive reproduction: rapid timeframe toggles + symbol switches + tick
 * updates to trigger the lightweight-charts "reading 'year'" race.
 */
import { chromium } from "playwright";

const BASE = "https://tradethrone.vercel.app";
const EMAIL = "e2esmoke1789018271@tt-e2e.in";
const PASSWORD = "SmokeTest@2026";

const errors = [];

(async () => {
  const browser = await chromium.launch({ channel: "chrome", headless: true });
  const ctx = await browser.newContext({
    viewport: { width: 1600, height: 1000 },
    serviceWorkers: "allow",
  });
  const page = await ctx.newPage();

  page.on("pageerror", (e) => {
    const stack = (e.stack || "").split("\n").slice(0, 10).join("\n");
    errors.push(`${e.message}\n${stack}`);
    console.log(`\n=== PAGE ERROR ===\n${e.message}\n${stack}`);
  });
  page.on("console", (m) => {
    if (m.type() === "error" && /year|lightweight|charts/i.test(m.text())) {
      console.log(`[console.error] ${m.text().slice(0, 300)}`);
    }
  });

  try {
    await page.goto(`${BASE}/login`, { waitUntil: "load", timeout: 90000 });
    await page.waitForSelector('button:text-is("Sign In")', { timeout: 30000 });
    await page.click('button:text-is("Sign In")');
    await page.waitForTimeout(400);
    await page.fill('input[placeholder="trader@tradetron.io"]', EMAIL);
    await page.fill('input[type="password"]', PASSWORD);
    await page.click('button:text-is("Sign In with Password")');
    await page.waitForURL("**/dashboard**", { timeout: 30000 });
    await page.waitForTimeout(2500);
    console.log("Logged in");

    // Visit NIFTY50 market detail
    await page.goto(`${BASE}/markets/NIFTY50`, { waitUntil: "domcontentloaded", timeout: 60000 });
    await page.waitForTimeout(5000);

    // Hammer: rapid timeframe switching without waiting for data between clicks
    const tfs = ["1s", "1m", "5m", "15m", "1h", "1D"];
    for (let pass = 0; pass < 2; pass++) {
      for (const tf of tfs) {
        await page.click(`button:has-text("${tf}")`).catch(() => {});
        await page.waitForTimeout(120);
      }
    }
    await page.waitForTimeout(4000);
    console.log(`After rapid timeframe cycling: errors=${errors.length}`);

    // Rapid symbol navigation
    for (const sym of ["BANKNIFTY", "FINNIFTY", "SENSEX", "RELIANCE", "TCS", "INFY", "HDFCBANK", "GOLD", "SILVER", "CRUDE", "BTCUSDT", "ETHUSDT", "USDINR", "EURINR"]) {
      await page.goto(`${BASE}/markets/${sym}`, { waitUntil: "domcontentloaded", timeout: 60000 });
      await page.waitForTimeout(250);
      await page.click("button:has-text('5m')").catch(() => {});
      await page.waitForTimeout(350);
      if (errors.length > 0) break;
    }
    await page.waitForTimeout(5000);
    console.log(`After symbol cycling: errors=${errors.length}`);

    // Now stay on one symbol and wait 30s for tick updates
    await page.goto(`${BASE}/markets/NIFTY50`, { waitUntil: "domcontentloaded", timeout: 60000 });
    await page.waitForTimeout(30000);
    console.log(`After 30s idle on NIFTY50: errors=${errors.length}`);
  } catch (err) {
    console.log("\nFATAL:", err.message.split("\n")[0]);
  } finally {
    console.log(`\nTOTAL PAGE ERRORS: ${errors.length}`);
    if (errors.length) console.log(errors.join("\n\n----\n\n"));
    await browser.close();
    process.exit(errors.length ? 1 : 0);
  }
})();