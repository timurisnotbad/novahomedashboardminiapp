/* Content script in the Booking.com extranet (Сообщения → Гость).
   Layout it reads (as of 10.2026):
     left   — search «Введите имя или номер бронирования», sort select, the list
              of conversations: guest name, date («10 окт. 2026»), preview, red dot;
     middle — conversation: day separators («Сегодня», «1 окт. 2026»), guest
              bubbles on the left, ours on the right («Доставлено 14:15»),
              Booking's own template cards («Шаблон «Hello» был отправлен…»),
              then the composer «Напишите сообщение здесь» / «Отправить»;
     right  — the reservation panel: «Имя гостя:» value, «Номер бронирования:»,
              «Заезд:», «Отъезд:», «Итого:», «Предпочитаемый язык:»,
              «Количество гостей:», «2 номера:» + one line per unit.
   Everything is read from visible text + geometry, never from class names
   (Booking renames those every release). The CRM can still override the
   few CSS selectors below via /ext/config. */
(() => {
  if (window.__novaBk) { try { chrome.runtime.onMessage.removeListener(window.__novaBk); } catch (e) { /* ignore */ } }
  let SEL = null, selAt = 0;
  const norm = (s) => (s || "").replace(/\s+/g, " ").trim();
  const txt = (el) => norm(el ? (el.innerText || el.textContent || "") : "");
  const rx = (s) => new RegExp(s, "i");
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const isVisible = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const DEF = { search: "input[placeholder*='номер бронирования' i], input[placeholder*='booking number' i]",
    composer: "textarea, [contenteditable='true']", sendButton: "button" };

  // the page is «busy» while a person uses it: we never click around then
  let lastInput = 0;
  for (const ev of ["mousemove", "mousedown", "keydown", "wheel", "touchstart"]) document.addEventListener(ev, () => { lastInput = Date.now(); }, { capture: true, passive: true });
  const busy = () => Date.now() - lastInput < 20000;

  async function selectors() {
    if (SEL && Date.now() - selAt < 60000) return SEL;
    try { const r = await chrome.runtime.sendMessage({ type: "config" }); if (r && r.selectors) { SEL = Object.assign({}, DEF, r.selectors); selAt = Date.now(); } } catch (e) { /* keep old */ }
    return SEL || DEF;
  }
  function qsa(sel, root) { try { return Array.from((root || document).querySelectorAll(sel)); } catch (e) { return []; } }

  // ---- text nodes with geometry: the only thing we trust -------------------------
  function nodes() {
    const out = [], w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let n;
    while ((n = w.nextNode())) {
      const t = norm(n.nodeValue); if (!t) continue;
      const p = n.parentElement; if (!p || /^(SCRIPT|STYLE|NOSCRIPT|OPTION)$/.test(p.tagName)) continue;
      const rg = document.createRange(); rg.selectNodeContents(n);
      const r = rg.getBoundingClientRect(); if (r.width < 1 || r.height < 1) continue;
      out.push({ t, r, el: p });
    }
    return out;
  }
  const RU_M = { "янв": 1, "фев": 2, "мар": 3, "апр": 4, "мая": 5, "май": 5, "июн": 6, "июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12 };
  function isoDate(s) {
    const m = (s || "").toLowerCase().match(/(\d{1,2})\s+([а-яa-z]{3})[а-яa-z]*\.?\s+(\d{4})/);
    if (!m || !RU_M[m[2]]) return "";
    return `${m[3]}-${String(RU_M[m[2]]).padStart(2, "0")}-${m[1].padStart(2, "0")}`;
  }
  const DATE = /^(?:[а-яa-z]{2,3},\s*)?\d{1,2}\s+[а-яa-z]{3,}\.?\s+\d{4}$/i;         // «10 окт. 2026», «чт, 22 окт. 2026»
  const SEP = /^(Сегодня|Today|Вчера|Yesterday|\d{1,2}\s+[а-яa-z]{3,}\.?\s+\d{4})$/i;
  const TIME = /^\d{1,2}:\d{2}$/;
  const STATUS = /^(Доставлено|Delivered|Прочитано|Read|Отправлено|Sent|Ответ не требуется|No reply needed|Ответить|Reply|Переведено|Translated|Показать оригинал|Show original|Перевести|Translate)$/i;
  const TEMPLATE = /^(Шаблон|Template)\s+«?["“]?/i;
  // banners and the composer sit below the last message: reading stops there
  const NOISE = /^(Защитите свой аккаунт|Protect your account|Ответы в этом чате включают|Replies in this chat|Напишите сообщение здесь|Write a message|Изображения|Images|Шаблоны|Templates|Помощь с ответом|Отправить|Send)$/i;

  // ---- the three columns, found by the search box and the composer ----------------
  function layout(S) {
    const search = qsa(S.search).find(isVisible), composer = qsa(S.composer).find(isVisible);
    if (!search || !composer) return null;
    const sr = search.getBoundingClientRect(), cr = composer.getBoundingClientRect();
    return { search, composer, left: { l: sr.left - 20, r: sr.right + 20, top: sr.bottom }, mid: { l: cr.left - 30, r: cr.right + 12, bottom: cr.top - 4 }, right: { l: cr.right + 6 } };
  }

  // ---- reservation panel: «Label:» then its value(s) right below ---------------------
  function panel(all, L) {
    const col = all.filter((n) => n.r.left >= L.right.l).sort((a, b) => a.r.top - b.r.top || a.r.left - b.r.left);
    const labelRe = /:\s*$/;
    const get = (re) => {
      const i = col.findIndex((n) => re.test(n.t) && n.t.includes(":") && n.t.length < 80);
      if (i < 0) return "";
      const own = (n) => n.t.replace(re, "").replace(/^[:\s]+/, "").trim();
      const lab = col[i];
      if (own(lab)) return own(lab);  // «Заезд: чт, 22 окт. 2026» in one node
      const vals = [];
      for (let k = i + 1; k < col.length && vals.length < 6; k++) {
        const n = col[k];
        if (n.r.top < lab.r.bottom - 4) continue;                                   // same row (the other sub-column's label)
        if (Math.abs(n.r.left - lab.r.left) > 60) continue;                         // the other sub-column
        if (labelRe.test(n.t) && n.t.length < 50) break;                            // next label in this sub-column
        if (n.r.top - (vals.length ? lab.r.bottom + 200 : lab.r.bottom) > 70) break;  // too far down
        if (/^(Посмотреть все детали|See all|Изменить|Edit)/i.test(n.t)) break;
        vals.push(n.t);
      }
      return vals.join("; ");
    };
    const rooms = get(/^\d+\s+(номер|номера|номеров|room|rooms|unit|units)\s*:?$/i);
    return { reservation: (get(/^(Номер бронирования|Booking number|Reservation number)/i).match(/\d{6,}/) || [""])[0],
      guest: get(/^(Имя гостя|Guest name)/i), checkin: isoDate(get(/^(Заезд|Check-in)/i)), checkout: isoDate(get(/^(Отъезд|Выезд|Check-out)/i)),
      total: get(/^(Итого|Total)/i), lang: get(/^(Предпочитаемый язык|Preferred language)/i), guests: get(/^(Количество гостей|Number of guests)/i),
      room: rooms, raw: col.map((n) => n.t).join("\n").slice(0, 6000), url: location.href.slice(0, 300) };
  }

  // the thread scrolls inside its own box: its rect bounds what is really visible
  function scrollBox(col) {
    const anchors = col.filter((n) => SEP.test(n.t) || TIME.test(n.t) || /\d{1,2}:\d{2}$/.test(n.t)).concat(col);
    for (const n of anchors.slice(0, 12)) {
      let el = n.el;
      for (let i = 0; i < 14 && el && el !== document.body; i++, el = el.parentElement) {
        const cs = getComputedStyle(el);
        if (/(auto|scroll)/.test(cs.overflowY) && el.scrollHeight > el.clientHeight + 4) return el.getBoundingClientRect();
      }
    }
    return null;
  }

  // ---- the open conversation: bubbles by position, time stamps close each message ----
  function conversation(all, L) {
    const P = panel(all, L);
    let col = all.filter((n) => n.r.left >= L.mid.l && n.r.right <= L.mid.r && n.r.bottom <= L.mid.bottom && n.r.top > 40)
      .sort((a, b) => a.r.top - b.r.top || a.r.left - b.r.left);
    // only the visible part of the scrolling thread: above the conversation title
    // sit the site's menus, below the last bubble sit banners and the composer
    const box = scrollBox(col);
    if (box) col = col.filter((n) => n.r.top >= box.top - 1 && n.r.bottom <= box.bottom + 1);
    else {
      const titleBottom = col.length ? col[0].r.bottom : 0;
      const noise = col.filter((n) => /^(Защитите свой аккаунт|Protect your account|Ответы в этом чате включают|Replies in this chat|Напишите сообщение здесь|Write a message)/i.test(n.t));
      const noiseTop = noise.length ? Math.min(...noise.map((n) => n.r.top)) : L.mid.bottom;
      col = col.filter((n) => n.r.top >= titleBottom - 1 && n.r.bottom <= noiseTop + 2);
    }
    const mid = (L.mid.l + L.mid.r) / 2;
    const msgs = []; let day = new Date().toISOString().slice(0, 10); let cur = null;
    const flush = (time) => {
      if (!cur) return;
      const text = cur.lines.join("\n").trim();
      if (text && !STATUS.test(text)) {
        const dl = cur.l - L.mid.l, dr = L.mid.r - cur.r;
        const out = cur.template || dr < dl;
        msgs.push({ dir: out ? "out" : "in", text: cur.template ? text + " (отправлено автоматически Booking.com)" : text, day, time: time || "" });
      }
      cur = null;
    };
    let header = true;  // the guest's name above the first separator / bubble
    for (const n of col) {
      let t = n.t.replace(/^[•·\s]+|[•·\s]+$/g, "");
      const tm = t.match(/^(.*?)\s*(\d{1,2}:\d{2})$/);  // «… 14:17», «Доставлено 14:15», «13:58 •»
      if (tm && !SEP.test(t)) {
        const body = tm[1].replace(/[•·\s.]+$/g, "").trim();
        if (body && !STATUS.test(body)) {
          if (!cur) cur = { lines: [], l: n.r.left, r: n.r.right, bottom: n.r.bottom, template: false };
          if (TEMPLATE.test(body)) cur.template = true;
          if (cur.lines.length && Math.abs(n.r.top - cur.lastTop) < 6) cur.lines[cur.lines.length - 1] += " " + body; else cur.lines.push(body);
        }
        header = false; flush(tm[2]); continue;
      }
      if (SEP.test(t)) { header = false; flush(""); const d = /сегодня|today/i.test(t) ? new Date() : /вчера|yesterday/i.test(t) ? new Date(Date.now() - 864e5) : null; day = d ? d.toISOString().slice(0, 10) : (isoDate(t) || day); continue; }
      if (NOISE.test(t) || /^(Защитите свой аккаунт|Protect your account|Ответы в этом чате включают)/i.test(t)) { flush(""); break; }
      if (STATUS.test(t)) continue;  // «Доставлено», «Ответить»… — not part of the text
      header = false;
      if (cur && n.r.top - cur.bottom > 150) flush("");  // a huge gap: a bubble without a time stamp (rare)
      if (!cur) cur = { lines: [], l: n.r.left, r: n.r.right, bottom: n.r.bottom, template: false };
      if (TEMPLATE.test(t)) cur.template = true;
      const same = cur.lines.length && Math.abs(n.r.top - cur.lastTop) < 6;
      if (same) cur.lines[cur.lines.length - 1] += " " + t; else cur.lines.push(t);
      cur.lastTop = n.r.top; cur.l = Math.min(cur.l, n.r.left); cur.r = Math.max(cur.r, n.r.right); cur.bottom = n.r.bottom;
    }
    flush("");
    if (!msgs.length && !P.guest) return null;
    return Object.assign(P, { messages: msgs.map((m) => ({ dir: m.dir, text: m.text.slice(0, 4000), at: `${m.day}T${(m.time || "00:00").padStart(5, "0")}:00` })) });
  }

  // ---- left list: {guest, date, preview, unread} per conversation ---------------------
  function list(all, L) {
    const col = all.filter((n) => n.r.left >= L.left.l && n.r.right <= L.left.r + 10 && n.r.top > L.left.top + 40).sort((a, b) => a.r.top - b.r.top || a.r.left - b.r.left);
    const items = [];
    const dots = [];  // the red «unanswered» dots: tiny painted elements in the list column
    for (const el of qsa("*")) {
      const r = el.getBoundingClientRect();
      if (r.width < 4 || r.width > 14 || r.height < 4 || r.height > 14 || r.left < L.left.l || r.right > L.left.r + 10 || r.top < L.left.top) continue;
      const bg = getComputedStyle(el).backgroundColor.match(/\d+/g);
      if (bg && +bg[0] > 150 && +bg[1] < 110 && +bg[2] < 110) dots.push(r);
    }
    for (let i = 0; i < col.length; i++) {
      const n = col[i]; if (!DATE.test(n.t)) continue;
      const name = col.slice(0, i).reverse().find((m) => Math.abs(m.r.top - n.r.top) < 8 && m.r.left < n.r.left && !DATE.test(m.t));
      const preview = col.slice(i + 1).find((m) => m.r.top > n.r.bottom - 2 && m.r.top - n.r.bottom < 40 && !DATE.test(m.t));
      if (!name) continue;
      const top = name.r.top - 6, bottom = (preview ? preview.r.bottom : n.r.bottom) + 6;
      const unread = dots.some((r) => r.top >= top && r.bottom <= bottom);
      items.push({ guest: name.t, date: isoDate(n.t) || n.t, preview: preview ? preview.t : "", unread, el: name.el });
    }
    return items;
  }

  function current(S) {
    const L = layout(S); if (!L) return null;
    const all = nodes();
    return { L, all, conv: conversation(all, L), list: list(all, L) };
  }

  async function scan() {
    const S = await selectors();
    const out = { page: location.pathname.slice(0, 80), unread: 0, conversations: [], list: [], busy: busy() };
    try {
      const c = current(S); if (!c) return out;
      out.list = c.list.map((i) => ({ guest: i.guest, date: i.date, preview: i.preview, unread: i.unread }));
      out.unread = c.list.filter((i) => i.unread).length;
      if (c.conv && c.conv.messages.length) out.conversations.push(c.conv);
      out.debug = c.all.filter((n) => n.r.left >= c.L.mid.l && n.r.right <= c.L.mid.r && n.r.bottom <= c.L.mid.bottom).slice(0, 120).map((n) => `${Math.round(n.r.left)},${Math.round(n.r.top)} ${n.t.slice(0, 80)}`).join("\n");
    } catch (e) { out.error = String(e.message || e); }
    return out;
  }

  // open a conversation from the list (by guest name / reservation number) and read it
  async function openConversation(item, S) {
    const c = current(S); if (!c) return false;
    let it = c.list.find((i) => item.guest && i.guest.toLowerCase() === String(item.guest).toLowerCase());
    if (!it) {  // not in the visible list: search for it
      const q = item.reservation && /^\d{6,}$/.test(String(item.reservation)) ? String(item.reservation) : item.guest;
      if (!q) return false;
      setNativeValue(c.L.search, q); c.L.search.dispatchEvent(new Event("input", { bubbles: true }));
      await sleep(1800);
      const c2 = current(S); if (!c2) return false;
      it = c2.list.find((i) => item.guest && i.guest.toLowerCase() === String(item.guest).toLowerCase()) || c2.list[0];
      if (!it) return false;
    }
    it.el.click(); await sleep(2200);
    const c3 = current(S);
    const P = c3 && c3.conv ? c3.conv : {};
    return (item.reservation && P.reservation && String(P.reservation) === String(item.reservation)) || (item.guest && P.guest && P.guest.toLowerCase() === String(item.guest).toLowerCase());
  }
  async function open(item) {
    if (busy()) return { ok: false, busy: true };
    const S = await selectors();
    const ok = await openConversation(item, S);
    const r = await scan();
    return Object.assign(r, { ok });
  }

  function setNativeValue(el, value) {
    const d = Object.getOwnPropertyDescriptor(el.__proto__, "value") || Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value") || Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value");
    if (d && d.set) d.set.call(el, value); else el.value = value;
  }
  async function send(item) {
    const S = await selectors();
    const c = current(S); if (!c) return { ok: false, error: "страница сообщений не открыта" };
    const P = c.conv || {};
    const same = (item.reservation && P.reservation && String(P.reservation) === String(item.reservation)) || (!item.reservation && item.guest && P.guest && P.guest.toLowerCase() === item.guest.toLowerCase());
    if (!same && !(await openConversation(item, S))) return { ok: false, error: "не нашёл разговор с гостем в экстранете" };
    const composer = qsa(S.composer).find(isVisible); if (!composer) return { ok: false, error: "нет поля ввода" };
    composer.focus();
    if (composer.isContentEditable) { composer.textContent = item.text; composer.dispatchEvent(new InputEvent("input", { bubbles: true, data: item.text, inputType: "insertText" })); }
    else { setNativeValue(composer, item.text); composer.dispatchEvent(new Event("input", { bubbles: true })); composer.dispatchEvent(new Event("change", { bubbles: true })); }
    await sleep(500);
    const cr = composer.getBoundingClientRect();
    const btn = qsa("button, [role='button']").filter(isVisible).filter((b) => b.getBoundingClientRect().top >= cr.bottom - 4 && b.getBoundingClientRect().top - cr.bottom < 120)
      .find((b) => !b.disabled && b.getAttribute("aria-disabled") !== "true" && /^(отправить|send)$/i.test(txt(b) + (b.getAttribute("aria-label") || "")));
    if (!btn) return { ok: false, error: "кнопка «Отправить» не найдена (или не активна)" };
    btn.click(); await sleep(1800);
    return { ok: true };
  }
  function snapshot() {
    const html = "<!doctype html>\n" + document.documentElement.outerHTML.replace(/<script[\s\S]*?<\/script>/gi, "");
    const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([html], { type: "text/html" })); a.download = "booking-extranet-snapshot.html"; a.click();
  }
  // watch the page: every 4 s (and on DOM changes) re-read; when the open thread or
  // the list changed, hand it to the background at once — no waiting for the alarm
  let lastSig = "", pushing = false, dirty = true;
  try { new MutationObserver(() => { dirty = true; }).observe(document.body, { childList: true, subtree: true, characterData: true }); } catch (e) { /* ignore */ }
  async function watch() {
    if (pushing || !dirty) return;
    dirty = false; pushing = true;
    try {
      const r = await scan();
      const sig = JSON.stringify([r.list, (r.conversations[0] || {}).reservation, (r.conversations[0] || {}).guest, ((r.conversations[0] || {}).messages || []).map((m) => m.dir + m.at + m.text.slice(-40))]);
      if (sig !== lastSig && (r.list.length || r.conversations.length)) { lastSig = sig; await chrome.runtime.sendMessage({ type: "push", scan: r }); }
    } catch (e) { /* background asleep: the alarm will catch up */ }
    finally { pushing = false; }
  }
  if (window.__novaBkTimer) clearInterval(window.__novaBkTimer);
  window.__novaBkTimer = setInterval(watch, 4000);
  setTimeout(watch, 1500);

  window.__novaBk = (m, s, reply) => {
    if (m.type === "scan") { scan().then(reply); return true; }
    if (m.type === "open") { open(m.item).then(reply).catch((e) => reply({ ok: false, error: String(e.message || e) })); return true; }
    if (m.type === "send") { send(m.item).then(reply).catch((e) => reply({ ok: false, error: String(e.message || e) })); return true; }
    if (m.type === "snapshot") { snapshot(); reply({ ok: true }); }
  };
  chrome.runtime.onMessage.addListener(window.__novaBk);
})();
