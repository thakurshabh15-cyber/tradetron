(async () => {
  const { chromium } = require("playwright");
  const b = await chromium.launch({ channel: "chrome", headless: true });
  const ctx = await b.newContext({ serviceWorkers: "allow" });
  const pg = await ctx.newPage();
  await pg.goto("https://tradethrone.vercel.app/", { waitUntil: "load", timeout: 90000 });
  await pg.waitForTimeout(5000);
  console.log("1st URL:", pg.url());
  await pg.goto("https://tradethrone.vercel.app/login", { waitUntil: "load", timeout: 90000 });
  await pg.waitForTimeout(5000);
  console.log("2nd URL:", pg.url());
  const r = await pg.locator("button:has-text('Register')").count();
  console.log("Register buttons:", r);
  const vis = await pg.locator("button:has-text('Register')").first().isVisible().catch(() => false);
  console.log("visible:", vis);
  if (!r) {
    console.log("BODY:", JSON.stringify((await pg.evaluate(() => document.querySelector("body")?.innerText.slice(0, 400) || ""))));
  }
  await b.close();
})().catch((e) => { console.error("ERR", e.message.split("\n")[0]); process.exit(1); });