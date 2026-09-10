/**
 * Live PROD verification of the LIVE-mode deploy fail-closed behavior.
 * Captures: actual HTTP status + body + UI error of the deploy attempt.
 */
import { chromium } from "playwright";

const BASE = "https://tradethrone.vercel.app";
const API = "https://tradetron-8jkz.onrender.com";
const EMAIL = "e2esmoke1789018271@tt-e2e.in";
const PASSWORD = "SmokeTest@2026";

const pageErrors = [];
let deployStatus = null;
let deployBody = null;

(async () => {
  const browser = await chromium.launch({ channel: "chrome", headless: true });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, serviceWorkers: "allow" });
  const page = await ctx.newPage();
  page.on("pageerror", (e) => pageErrors.push(`${e.message} @ ${page.url()}`));
  page.on("response", async (r) => {
    if (/\/api\/strategies\/[^/]+\/deploy$/.test(r.url())) {
      deployStatus = r.status();
      deployBody = await r.text().catch(() => "");
      if (deployStatus >= 400) console.log(`\n[DEPLOY RESPONSE] HTTP ${deployStatus}\nBODY: ${deployBody.slice(0, 600)}`);
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

    // Marketplace → Deploy modal
    await page.goto(`${BASE}/marketplace`, { waitUntil: "domcontentloaded", timeout: 60000 });
    await page.waitForSelector("button:has-text('Deploy')", { timeout: 30000 });
    await page.locator("button:has-text('Deploy')").first().click();
    await page.waitForSelector("text=Deploy Strategy", { state: "visible", timeout: 15000 });
    await page.waitForTimeout(800);

    // LIVE mode
    await page.click("button:has-text('Live Broker')");
    await page.waitForTimeout(600);

    // What does the deployed modal now hold as broker name state? (inspect form)
    const modalState = await page.evaluate(() => {
      const modal = Array.from(document.querySelectorAll("h2"))
        .find((h) => h.innerText.includes("Deploy Strategy"))?.closest("div[class*=card]");
      const select = modal?.querySelector("select");
      return {
        selectValue: select?.value || null,
        execButtons: Array.from(modal?.querySelectorAll("button") || []).map((b) => b.innerText.trim()).filter((t) => /Paper|Live/i.test(t)).slice(0, 4),
      };
    });
    console.log("Modal state after clicking Live Broker:", JSON.stringify(modalState));

    // Submit deploy
    await page.click("button:has-text('Confirm & Deploy to Engine')");
    await page.waitForTimeout(4000);

    const uiError = await page.evaluate(() => {
      const modal = Array.from(document.querySelectorAll("h2"))
        .find((h) => h.innerText.includes("Deploy Strategy"))?.closest("div[class*=card]");
      return modal?.innerText?.match(/(?:Cannot deploy|Deployment rejected|broker account|validation|422)[^\\n]*/i)?.[0] || "(no inline error)";
    });
    console.log("UI error visible:", JSON.stringify(uiError));
    console.log(`\ndeployStatus=${deployStatus}  deployBody=${JSON.stringify(deployBody)}`);
    console.log(`pageErrors=${pageErrors.length}`);

    if (deployStatus === 422) {
      console.log("\n>>> CONFIRMED: live prod still sends broker_name:null → Pydantic 422 (the fix is NOT deployed)");
      process.exit(0);
    }
  } catch (err) {
    console.log("FATAL:", err.message.split("\n")[0]);
    process.exit(2);
  } finally {
    await browser.close();
  }
})();