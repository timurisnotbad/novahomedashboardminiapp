import { chromium } from "playwright";
const B = "http://127.0.0.1:8000";
const browser = await chromium.launch({ executablePath: "/opt/pw-browsers/chromium-1194/chrome-linux/chrome" });
const IPHONE_UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1";
async function login(page) { await page.goto(B + "/crm/"); await page.fill("#auth-email", "admin@novahome.uz"); await page.fill("#auth-pass", "secret123"); await page.click("#auth-btn"); await page.waitForSelector("#app:not(.hidden)", { timeout: 10000 }); }
const errors = [];
// A) iPhone UA, nothing reported by the "OS": the guessed keyboard
{
  const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true, userAgent: IPHONE_UA });
  await ctx.route(/telegram\.org/, (r) => r.fulfill({ status: 200, body: "" }));
  const page = await ctx.newPage(); page.on("pageerror", (e) => errors.push("A " + e.message));
  await login(page);
  await page.goto(B + "/crm/#messages"); await page.waitForTimeout(1500);
  const frame = page.frames().find((f) => f.url().includes("/inbox/"));
  await frame.waitForSelector(".ib-chat", { timeout: 8000 });
  await frame.locator(".ib-chat").first().tap(); await frame.waitForSelector("#compose textarea, textarea", { timeout: 8000 }); await page.waitForTimeout(600);
  const ta = frame.locator("textarea").first();
  await ta.focus(); await page.waitForTimeout(1100);
  const st = await page.evaluate(() => ({ kb: document.body.classList.contains("kb"), vvh: getComputedStyle(document.documentElement).getPropertyValue("--vvh"), tab: getComputedStyle(document.getElementById("tabbar")).display, frameH: document.querySelector(".inbox-frame").getBoundingClientRect().height, frameTop: document.querySelector(".inbox-frame").getBoundingClientRect().top, embed: !!document.body.classList }));
  const taRect = await ta.evaluate((el) => { const r = el.getBoundingClientRect(); return { top: r.top, bottom: r.bottom, innerH: window.innerHeight, embed: document.body.classList.contains("embed") }; });
  console.log("A iOS guess:", st, "textarea in frame:", taRect, "=> visible bottom on screen:", st.frameTop + taRect.bottom, "<= 844*0.54=", Math.round(844 - 844 * 0.46));
  await page.screenshot({ path: process.env.SP + "/shot_kb_guess.png" });
  await page.evaluate(() => NH.keyboard.debug(true)); await page.waitForTimeout(100); console.log("debug:", await page.locator("#nh-kbdbg").innerText());
  await ta.evaluate((el) => el.blur()); await page.waitForTimeout(1000);
  console.log("A after blur:", await page.evaluate(() => ({ kb: document.body.classList.contains("kb"), vvh: getComputedStyle(document.documentElement).getPropertyValue("--vvh"), tab: getComputedStyle(document.getElementById("tabbar")).display })));
  // cash page in the shell: a field focus also lifts the frame
  await page.goto(B + "/crm/#cash"); await page.waitForTimeout(1500);
  const cf = page.frames().find((f) => f.url().includes("/cash/"));
  await cf.waitForSelector("[data-amt='0']"); await cf.locator("[data-amt='0']").focus(); await page.waitForTimeout(1100);
  console.log("A cash focus:", await page.evaluate(() => ({ kb: document.body.classList.contains("kb"), vvh: getComputedStyle(document.documentElement).getPropertyValue("--vvh") })));
  await ctx.close();
}
// B) Android-like: the window itself shrinks (interactive-widget=resizes-content) while a field is focused
{
  const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
  await ctx.route(/telegram\.org/, (r) => r.fulfill({ status: 200, body: "" }));
  const page = await ctx.newPage(); page.on("pageerror", (e) => errors.push("B " + e.message));
  await login(page);
  await page.goto(B + "/crm/#messages"); await page.waitForTimeout(1500);
  const frame = page.frames().find((f) => f.url().includes("/inbox/"));
  await frame.waitForSelector(".ib-chat"); await frame.locator(".ib-chat").first().tap(); await frame.waitForSelector("textarea"); await page.waitForTimeout(500);
  await frame.locator("textarea").first().focus(); await page.setViewportSize({ width: 390, height: 500 }); await page.waitForTimeout(900);
  const r = await frame.locator("textarea").first().evaluate((el) => el.getBoundingClientRect().bottom);
  const st = await page.evaluate(() => ({ kb: document.body.classList.contains("kb"), vvh: getComputedStyle(document.documentElement).getPropertyValue("--vvh"), frameTop: document.querySelector(".inbox-frame").getBoundingClientRect().top, frameH: document.querySelector(".inbox-frame").getBoundingClientRect().height, learned: localStorage.getItem("nh_kbh") }));
  console.log("B shrink:", st, "textarea bottom on screen:", st.frameTop + r, "<= 500");
  await page.screenshot({ path: process.env.SP + "/shot_kb_shrink.png" });
  await frame.locator("textarea").first().evaluate((el) => el.blur()); await page.setViewportSize({ width: 390, height: 844 }); await page.waitForTimeout(900);
  console.log("B restored:", await page.evaluate(() => ({ kb: document.body.classList.contains("kb"), vvh: getComputedStyle(document.documentElement).getPropertyValue("--vvh"), tab: getComputedStyle(document.getElementById("tabbar")).display })));
  // standalone chats page (opened from the bot) with the keyboard
  await page.goto(B + "/inbox/"); await page.waitForSelector(".ib-chat"); await page.locator(".ib-chat").first().tap(); await page.waitForSelector("textarea"); await page.waitForTimeout(400);
  await page.locator("textarea").first().focus(); await page.setViewportSize({ width: 390, height: 500 }); await page.waitForTimeout(900);
  console.log("B inbox standalone:", await page.evaluate(() => ({ kb: document.body.classList.contains("kb"), vvh: getComputedStyle(document.documentElement).getPropertyValue("--vvh"), taBottom: document.querySelector("textarea").getBoundingClientRect().bottom, ibH: document.querySelector(".ib").getBoundingClientRect().height })));
  await ctx.close();
}
// C) desktop: nothing changes
{
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 860 } });
  const page = await ctx.newPage(); page.on("pageerror", (e) => errors.push("C " + e.message));
  await login(page); await page.goto(B + "/crm/#messages"); await page.waitForTimeout(1200);
  console.log("C desktop:", await page.evaluate(() => ({ kb: document.body.classList.contains("kb"), vvh: getComputedStyle(document.documentElement).getPropertyValue("--vvh") || "(unset)", frameH: document.querySelector(".inbox-frame").getBoundingClientRect().height })));
  await ctx.close();
}
console.log("errors:", errors);
await browser.close();
