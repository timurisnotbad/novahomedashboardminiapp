import { chromium } from "playwright";
const B = "http://127.0.0.1:8000";
const browser = await chromium.launch({ executablePath: "/opt/pw-browsers/chromium-1194/chrome-linux/chrome" });
const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
const page = await ctx.newPage(); const errors = []; page.on("pageerror", (e) => errors.push(e.message));
await page.goto(B + "/crm/"); await page.fill("#auth-email", "admin@novahome.uz"); await page.fill("#auth-pass", "secret123"); await page.click("#auth-btn"); await page.waitForSelector("#app:not(.hidden)");
await page.goto(B + "/crm/#integrations"); await page.waitForSelector("#i-mc", { timeout: 30000, state: "attached" }); for (const t of ["flow", "notif", "conn"]) { await page.click(`[data-itab=${t}]`); await page.waitForTimeout(300); if (await page.locator("#i-mc").isVisible()) { console.log("tab:", t); break; } }
console.log("card:", await page.locator("#i-mc").isChecked(), "| ru:", (await page.locator("#i-mc-ru").inputValue()).slice(0, 40), "| en:", (await page.locator("#i-mc-en").inputValue()).slice(0, 30));
await page.fill("#i-mc-en", "Hello! We missed your call. Please text us here."); await page.click("#i-mc-save"); await page.waitForTimeout(600);
const st = await page.evaluate(() => fetch("/api/crm/integrations", { credentials: "include" }).then((r) => r.json()));
console.log("saved en:", st.settings.missed_call_en);
await page.locator("#i-mc").uncheck(); await page.waitForTimeout(500);
console.log("switch off saved:", (await page.evaluate(() => fetch("/api/crm/integrations", { credentials: "include" }).then((r) => r.json()))).settings.missed_call_reply);
await page.locator("#i-mc").check(); await page.waitForTimeout(400);
await page.screenshot({ path: process.env.SP + "/shot_mc.png", clip: { x: 240, y: 0, width: 1040, height: 900 } });
console.log("errors:", errors); await browser.close(); if (errors.length || st.settings.missed_call_en !== "Hello! We missed your call. Please text us here.") process.exit(1);
