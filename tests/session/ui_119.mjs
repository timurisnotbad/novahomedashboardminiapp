// v119: a reply typed while the phone has no connection waits in the chat and is sent once.
// Server: the app with a stub channel that appends every text really sent to $SP/sent119.txt
import { createRequire } from "module";
const { chromium } = createRequire(import.meta.url)("playwright");
import fs from "fs";
const B = "http://127.0.0.1:8000", SP = process.env.SP;
const sent = () => (fs.existsSync(SP + "/sent119.txt") ? fs.readFileSync(SP + "/sent119.txt", "utf8").split("\n").filter(Boolean) : []);
const times = (t) => sent().filter((x) => x === t).length;
const browser = await chromium.launch();
const ctx = await browser.newContext({ viewport: { width: 393, height: 800 }, deviceScaleFactor: 2, isMobile: true, hasTouch: true });
const page = await ctx.newPage(); const errors = []; page.on("pageerror", (e) => errors.push(e.message)); page.on("dialog", (d) => d.accept());
const ok = (name, cond, extra) => { console.log((cond ? "PASS " : "FAIL ") + name + (extra ? " | " + extra : "")); if (!cond) process.exitCode = 1; };
await page.goto(B + "/crm/"); await page.fill("#auth-email", "admin@novahome.uz"); await page.fill("#auth-pass", "secret123"); await page.click("#auth-btn"); await page.waitForSelector("#app:not(.hidden)");
const openChat = async () => { await page.goto(B + "/inbox/"); await page.waitForSelector(".ib-chat"); await page.locator(".ib-chat").first().click(); await page.waitForSelector(".ib-m.in"); };
const say = async (t) => { await page.fill("#text", t); await page.click("#send"); };
const SEND = "**/api/inbox/chats/*/send";
const R = " #" + (Date.now() % 100000);  // run tag: repeatable on the same database
await openChat();

// 1. the usual case
await say("Здравствуйте" + R); await page.waitForFunction(() => !document.querySelector(".ib-m.queued"), null, { timeout: 6000 }); await page.waitForTimeout(300);
ok("normal send", times("Здравствуйте" + R) === 1 && (await page.locator(".ib-m.queued").count()) === 0);

// 2. the request never leaves the phone
let blocked = 0;
await page.route(SEND, (r) => { blocked++; r.abort("internetdisconnected"); });
await say("У нас заезд в 14.00" + R); await page.waitForSelector(".ib-m__wait", { timeout: 8000 });
ok("queued bubble + note, composer empty, no toast", (await page.locator(".ib-m.queued").count()) === 1 && (await page.inputValue("#text")) === "" && await page.locator("#toast").isHidden(),
  (await page.locator(".ib-m__wait").innerText()).replace(/\n/g, " "));
await page.waitForTimeout(4500);
await page.screenshot({ path: SP + "/shot119_waiting.png" });
ok("retried by itself", blocked >= 2 && times("У нас заезд в 14.00" + R) === 0, "attempts " + blocked);
await page.unroute(SEND);
await page.waitForFunction(() => !document.querySelector(".ib-m.queued"), null, { timeout: 15000 });
await page.waitForTimeout(600);
ok("delivered once after the network came back", times("У нас заезд в 14.00" + R) === 1 && (await page.locator(".ib-m.out .ib-m__t", { hasText: "У нас заезд в 14.00" + R }).count()) === 1);

// 3. the server got the reply, the answer was lost on the way back
let n = 0;
await page.route(SEND, async (r) => { n++; if (n === 1) { await r.fetch().catch(() => {}); await r.abort("connectionreset"); } else await r.continue(); });
await say("Сейчас спустимся" + R); await page.waitForTimeout(6000);
await page.unroute(SEND);
ok("lost answer: guest got it once, chat shows it once", times("Сейчас спустимся" + R) === 1 && (await page.locator(".ib-m.out .ib-m__t", { hasText: "Сейчас спустимся" + R }).count()) === 1 && (await page.locator(".ib-m.queued").count()) === 0, "requests " + n);

// 4. no internet at all: the strip explains, two replies keep their order
await ctx.setOffline(true);
await say("первое" + R); await say("второе" + R);
await page.waitForSelector(".ib-conv .ib-net:not(.hidden)", { timeout: 20000 });
ok("offline strip", /Нет интернета на этом устройстве/.test(await page.locator(".ib-conv .ib-net").innerText()), await page.locator(".ib-conv .ib-net").innerText());
ok("two queued", (await page.locator(".ib-m.queued").count()) === 2);
await page.screenshot({ path: SP + "/shot119_offline.png" });
await ctx.setOffline(false);
await page.waitForFunction(() => !document.querySelector(".ib-m.queued"), null, { timeout: 20000 });
await page.waitForTimeout(800);
const tail = sent().slice(-2);
ok("both sent in order, strip gone", tail[0] === "первое" + R && tail[1] === "второе" + R && await page.locator(".ib-conv .ib-net").isHidden(), tail.join(" → "));

// 5. the page is closed / reloaded while the reply waits
await page.route(SEND, (r) => r.abort("internetdisconnected"));
await say("после перезагрузки" + R); await page.waitForSelector(".ib-m__wait");
await openChat(); await page.waitForSelector(".ib-m.queued", { timeout: 6000 });
ok("survives a reload", times("после перезагрузки" + R) === 0);
await page.unroute(SEND);
await page.waitForFunction(() => !document.querySelector(".ib-m.queued"), null, { timeout: 20000 });
await page.waitForTimeout(500);
ok("sent after the reload", times("после перезагрузки" + R) === 1);

// 6. «отменить» gives the text back
await page.route(SEND, (r) => r.abort("internetdisconnected"));
await say("передумал" + R); await page.waitForSelector(".ib-m__wait"); await page.click("[data-ob-cancel]"); await page.waitForTimeout(300);
ok("cancel", (await page.locator(".ib-m.queued").count()) === 0 && (await page.inputValue("#text")) === "передумал" + R);
await page.unroute(SEND); await page.waitForTimeout(4000);
ok("cancelled reply never sent", times("передумал" + R) === 0);
await page.fill("#text", "");

// 7. an old reply is not sent silently
await page.evaluate((t) => localStorage.setItem("nh_outbox", JSON.stringify([{ cid: "old" + Date.now(), chat: 1, text: t, at: Date.now() - 3600e3, user: "", tries: 3 }])), "вчерашнее" + R);
await openChat(); await page.waitForSelector("[data-ob-retry]", { timeout: 6000 }); await page.waitForTimeout(1500);
ok("stale reply waits for a tap", times("вчерашнее" + R) === 0, (await page.locator(".ib-m__err").last().innerText()).replace(/\n/g, " "));
await page.screenshot({ path: SP + "/shot119_stale.png" });
await page.click("[data-ob-retry]"); await page.waitForFunction(() => !document.querySelector(".ib-m.queued"), null, { timeout: 8000 }); await page.waitForTimeout(400);
ok("sent on tap", times("вчерашнее" + R) === 1);

// 8. the server answers with a reason (not a network problem): as before — toast + text back
await page.route(SEND, (r) => r.fulfill({ status: 424, contentType: "application/json", body: JSON.stringify({ detail: "WhatsApp не подключён" }) }));
await say("с причиной" + R); await page.waitForTimeout(900);
ok("reasoned refusal: toast and draft", /WhatsApp не подключён/.test(await page.locator("#toast").innerText()) && (await page.inputValue("#text")) === "с причиной" + R && (await page.locator(".ib-m.queued").count()) === 0);
await page.unroute(SEND);
ok("no page errors", errors.length === 0, errors.join("; "));
console.log("sent file:", sent().join(" | "));
await browser.close();
