import { chromium } from "playwright";
const BASE = "https://tradethrone.vercel.app";
const EMAIL = "e2esmoke1789018271@tt-e2e.in";
const PASSWORD = "SmokeTest@2026";
(async () => {
  const b = await chromium.launch({ channel: "chrome", headless: true });
  const ctx = await b.newContext({ viewport: { width: 1440, height: 900 }, serviceWorkers: "allow" });
  const pg = await ctx.newPage();
  await pg.goto(`${BASE}/login`, { waitUntil: "load", timeout: 90000 });
  await pg.waitForSelector('button:text-is("Sign In")', { timeout: 30000 });
  await pg.click('button:text-is("Sign In")');
  await pg.waitForTimeout(400);
  await pg.fill('input[placeholder="trader@tradetron.io"]', EMAIL);
  await pg.fill('input[type="password"]', PASSWORD);
  await pg.click('button:text-is("Sign In with Password")');
  await pg.waitForURL("**/dashboard**", { timeout: 30000 });
  await pg.waitForTimeout(2000);
  await pg.goto(`${BASE}/copy-trading`, { waitUntil: "domcontentloaded", timeout: 60000 });
  for (const t of [2000, 4000, 8000]) {
    await pg.waitForTimeout(t === 2000 ? 0 : t - (t === 4000 ? 2000 : 4000));
    const txt = await pg.evaluate(() => (document.querySelector("body")?.innerText || "").replace(/\s+/g, " ").slice(0, 1600));
    const hasMS = /Master Copy|Active Copy Groups|No public master copy groups/i.test(txt);
    const hasLoad = /Loading module/.test(txt);
    console.log(`--- after ${t}ms: MasterCopy=${hasMS} LoadingModule=${hasLoad}`);
    console.log(txt.slice(0, 700));
    console.log("...");
  }
  await b.close();
})();