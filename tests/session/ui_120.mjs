// v120: a file (PDF instruction) on a quick command and on a stay step; sent to the guest with the text.
// Server: the app with a stub channel that appends «text|file name» of everything really sent to $SP/sent120.txt
import { createRequire } from "module";
import fs from "fs";
const { chromium } = createRequire(import.meta.url)("playwright");
const B = "http://127.0.0.1:8000", SP = process.env.SP, FN = "Инструкция Nova Home.pdf",
  PDF = { name: FN, mimeType: "application/pdf", buffer: Buffer.from("%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n") };  // a real file with a Cyrillic path is not picked up by the test browser here
const sent = () => (fs.existsSync(SP + "/sent120.txt") ? fs.readFileSync(SP + "/sent120.txt", "utf8").split("\n").filter(Boolean) : []);
const ok = (name, cond, extra) => { console.log((cond ? "PASS " : "FAIL ") + name + (extra ? " | " + extra : "")); if (!cond) process.exitCode = 1; };
const R = "t" + (Date.now() % 100000);
const browser = await chromium.launch();
const errors = [];
// ---------------- CRM on a computer: the owner attaches the PDF
let ctx = await browser.newContext({ viewport: { width: 1280, height: 900 }, deviceScaleFactor: 2 });
let page = await ctx.newPage(); page.on("pageerror", (e) => errors.push(e.message)); page.on("dialog", (d) => d.accept());
await page.goto(B + "/crm/"); await page.fill("#auth-email", "admin@novahome.uz"); await page.fill("#auth-pass", "secret123"); await page.click("#auth-btn"); await page.waitForSelector("#app:not(.hidden)");
await page.goto(B + "/crm/#commands"); await page.waitForSelector("#cmd-add"); await page.click("#cmd-add");
await page.fill("#cm-cmd", R); await page.fill("#cm-title", "Инструкция заезда " + R); await page.fill("#cm-text", "Здравствуйте! Инструкция по заселению — в файле. " + R);
await page.setInputFiles("#cm-file input[type=file]", PDF); await page.waitForSelector("#cm-file [data-unfile]", { timeout: 8000 });
await page.screenshot({ path: SP + "/shot120_cmdform.png" });
await page.click("#cm-go"); await page.waitForSelector(".filetag", { timeout: 6000 });
ok("quick command saved with the file", (await page.locator(".list__row", { hasText: R }).locator(".filetag").innerText()).includes(FN));
// edit without touching the file keeps it; «Убрать» takes it off; attach again
await page.locator(".list__row", { hasText: R }).locator("[data-edit]").click(); await page.waitForSelector("#cm-file [data-unfile]");
await page.fill("#cm-title", "Инструкция " + R); await page.click("#cm-go"); await page.waitForTimeout(700);
ok("edit keeps the file", (await page.locator(".list__row", { hasText: R }).locator(".filetag").count()) === 1);
await page.locator(".list__row", { hasText: R }).locator("[data-edit]").click(); await page.click("#cm-file [data-unfile]"); await page.click("#cm-go"); await page.waitForTimeout(700);
ok("«Убрать» removes it", (await page.locator(".list__row", { hasText: R }).locator(".filetag").count()) === 0);
await page.locator(".list__row", { hasText: R }).locator("[data-edit]").click(); await page.setInputFiles("#cm-file input[type=file]", PDF); await page.waitForSelector("#cm-file [data-unfile]"); await page.click("#cm-go"); await page.waitForTimeout(700);
// a long quick command (text and file will be two messages)
const LONG = "Длинная инструкция " + R + ". " + "Вход со стороны улицы, код домофона, лифт на этаж. ".repeat(25);
await page.click("#cmd-add"); await page.fill("#cm-cmd", R + "long"); await page.fill("#cm-title", "Длинная " + R); await page.fill("#cm-text", LONG);
await page.setInputFiles("#cm-file input[type=file]", PDF); await page.waitForSelector("#cm-file [data-unfile]"); await page.click("#cm-go"); await page.waitForTimeout(700);
// the stay step «Отправить инструкцию по заселению»
await page.goto(B + "/crm/#auto"); await page.waitForSelector("[data-stepedit]");
const row = page.locator("div:has(> div > b)", { hasText: "Отправить инструкцию по заселению" }).last();
await page.locator("[data-stepedit]").nth(2).click(); await page.waitForSelector("#sf-file");
ok("step form is the instruction step", /инструкц/i.test(await page.inputValue("#sf-title")), await page.inputValue("#sf-title"));
await page.setInputFiles("#sf-file input[type=file]", PDF); await page.waitForSelector("#sf-file [data-unfile]", { timeout: 8000 });
await page.screenshot({ path: SP + "/shot120_stepform.png" });
await page.click("#sf-go"); await page.waitForTimeout(900);
const steps = await page.evaluate(() => fetch("/api/crm/checklist", { credentials: "include" }).then((r) => r.json()));
const st = steps.steps.find((x) => /инструкц/i.test(x.title));
ok("step saved with the file", st && st.file_name === FN && !!st.file_url, st && st.file_name);
ok("step list shows the file", (await page.locator("#main").innerText()).includes("📎 " + FN));
const pdf = await page.evaluate((u) => fetch(u).then((r) => r.status + " " + r.headers.get("content-type")), st.file_url);
ok("file opens by its link", /^200 application\/pdf/.test(pdf), pdf);
await page.screenshot({ path: SP + "/shot120_steps.png" });
await ctx.close();

// ---------------- the chat on a phone: staff sends the instruction
ctx = await browser.newContext({ viewport: { width: 393, height: 800 }, deviceScaleFactor: 2, isMobile: true, hasTouch: true, storageState: undefined });
page = await ctx.newPage(); page.on("pageerror", (e) => errors.push(e.message)); page.on("dialog", (d) => d.accept());
await page.goto(B + "/crm/"); await page.fill("#auth-email", "admin@novahome.uz"); await page.fill("#auth-pass", "secret123"); await page.click("#auth-btn"); await page.waitForSelector("#app:not(.hidden)");
await page.goto(B + "/inbox/"); await page.waitForSelector(".ib-chat"); await page.locator(".ib-chat").first().click(); await page.waitForSelector(".ib-m.in");
const idle = () => page.waitForFunction(() => !document.querySelector(".ib-m.queued"), null, { timeout: 15000 });
// «/команда» → text + file bar → send
await page.fill("#text", "/" + R); await page.locator("#text").dispatchEvent("input"); await page.waitForSelector("#cmd-box [data-cmd]", { timeout: 5000 });
await page.locator("#cmd-box [data-cmd]").first().dispatchEvent("mousedown"); await page.waitForSelector("#attach-bar:not(.hidden)");
ok("file bar above the composer", (await page.locator("#attach-name").innerText()).includes(FN), await page.inputValue("#text"));
await page.screenshot({ path: SP + "/shot120_attach.png" });
await page.click("#send"); await idle(); await page.waitForTimeout(700);
let last = sent().slice(-1)[0];
ok("guest got the text and the PDF in one message", last.includes(R) && last.endsWith("|" + FN) && await page.locator("#attach-bar").isHidden(), last);
ok("chat shows the document", (await page.locator(".ib-m.out").last().locator(".ib-m__doc").innerText()).includes(FN));
// cancel the file: only the text goes
await page.fill("#text", "/" + R); await page.locator("#text").dispatchEvent("input"); await page.waitForSelector("#cmd-box [data-cmd]"); await page.locator("#cmd-box [data-cmd]").first().dispatchEvent("mousedown");
await page.waitForSelector("#attach-bar:not(.hidden)"); await page.click("#attach-cancel"); await page.click("#send"); await idle(); await page.waitForTimeout(500);
last = sent().slice(-1)[0]; ok("file cancelled → text only", last.includes(R) && !last.includes("|"), last);
// long text: text first, then the file
let n0 = sent().length;
await page.fill("#text", "/" + R + "long"); await page.locator("#text").dispatchEvent("input"); await page.waitForSelector("#cmd-box [data-cmd]"); await page.locator("#cmd-box [data-cmd]").first().dispatchEvent("mousedown");
await page.waitForSelector("#attach-bar:not(.hidden)"); await page.click("#send"); await idle(); await page.waitForTimeout(1500);
let got = sent().slice(n0);
ok("long text: two messages, text then file", got.length === 2 && !got[0].includes("|") && got[1] === "|" + FN, got.join(" // "));
ok("both are in the chat", (await page.locator(".ib-m.out .ib-m__t", { hasText: "Длинная инструкция " + R }).count()) === 1 && (await page.locator(".ib-m.out").last().locator(".ib-m__doc").count()) === 1);
// no connection: the reply with the file waits and is sent once
n0 = sent().length;
await page.route("**/api/inbox/chats/*/send", (r) => r.abort("internetdisconnected"));
await page.fill("#text", "/" + R); await page.locator("#text").dispatchEvent("input"); await page.waitForSelector("#cmd-box [data-cmd]"); await page.locator("#cmd-box [data-cmd]").first().dispatchEvent("mousedown");
await page.waitForSelector("#attach-bar:not(.hidden)"); await page.click("#send"); await page.waitForSelector(".ib-m__wait", { timeout: 8000 });
ok("waiting bubble shows the file", (await page.locator(".ib-m.queued .ib-m__doc").innerText()).includes(FN));
await page.screenshot({ path: SP + "/shot120_queued.png" });
await page.unroute("**/api/inbox/chats/*/send"); await idle(); await page.waitForTimeout(800);
got = sent().slice(n0); ok("sent once after the connection is back", got.length === 1 && got[0].endsWith("|" + FN), got.join(" // "));
// templates window: attach to an existing template, make a new one with a file
await page.click("#tpl-btn"); await page.waitForSelector("[data-tpl-file]");
ok("templates window shows files", (await page.locator(".ib-tpl__file").count()) >= 2);
await page.fill("#tpl-cmd", R + "n"); await page.fill("#tpl-title", "Новый " + R); await page.fill("#tpl-text", "Текст нового шаблона " + R);
await page.setInputFiles("#tpl-new-file", PDF); await page.waitForTimeout(200);
await page.screenshot({ path: SP + "/shot120_tplmodal.png" });
await page.click("[data-tpl-add]"); await page.waitForSelector(`.ib-tpl:has-text("Новый ${R}") .ib-tpl__file`, { timeout: 8000 });
ok("new template with a file from the chat window", true);
await page.locator(`.ib-tpl:has-text("Новый ${R}") [data-tpl-unfile]`).click(); await page.waitForTimeout(700);
ok("file taken off a template", (await page.locator(`.ib-tpl:has-text("Новый ${R}") .ib-tpl__file`).count()) === 0);
const [chooser] = await Promise.all([page.waitForEvent("filechooser"), page.locator(`.ib-tpl:has-text("Новый ${R}") [data-tpl-file]`).click()]);
await chooser.setFiles(PDF); await page.waitForSelector(`.ib-tpl:has-text("Новый ${R}") .ib-tpl__file`, { timeout: 8000 });
ok("file attached to an existing template", true);
n0 = sent().length;
await page.locator(`.ib-tpl:has-text("Новый ${R}") [data-tpl-use]`).click(); await page.waitForSelector("#attach-bar:not(.hidden)"); await page.click("#send"); await idle(); await page.waitForTimeout(600);
got = sent().slice(n0); ok("template picked in the window → text + file", got.length === 1 && got[0].includes("Текст нового шаблона") && got[0].endsWith("|" + FN), got.join(" // "));
// the paperclip as before: any file from the phone
n0 = sent().length;
await page.setInputFiles("#file", PDF); await page.waitForTimeout(1500);
got = sent().slice(n0); ok("paperclip still sends a file", got.length === 1 && got[0].endsWith("|" + FN), got.join(" // "));
ok("no page errors", errors.length === 0, errors.join("; "));
await browser.close();
