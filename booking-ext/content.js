/* Content script in the Booking.com extranet (Сообщения). Reads the open
   conversation and the inbox list, answers "scan" with what it found and "send"
   by typing a reply into the composer. Selectors come from the CRM (/ext/config)
   and can be adjusted there without reinstalling. */
(() => {
  let SEL = null, selAt = 0;
  const txt = (el) => (el ? (el.innerText || el.textContent || "") : "").replace(/\s+/g, " ").trim();
  const isVisible = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const rx = (s) => new RegExp(s, "i");

  async function selectors() {
    if (SEL && Date.now() - selAt < 60000) return SEL;
    try { const r = await chrome.runtime.sendMessage({ type: "config" }); if (r && r.selectors) { SEL = r.selectors; selAt = Date.now(); } } catch (e) { /* keep old */ }
    return SEL || {};
  }
  function qsa(sel, root) { try { return Array.from((root || document).querySelectorAll(sel)); } catch (e) { return []; } }
  // ---- reservation panel: label → value by geometry (value sits right under or right of its label)
  const LABELS = "Номер бронирования|Booking number|Reservation number|Имя гостя|Guest name|Заезд|Check-in|Отъезд|Выезд|Check-out|Итого|Total|Предпочитаемый язык|Preferred language|Количество гостей|Number of guests|номер:|Room|Unit";
  function leaves(root) { return qsa("*", root || document).filter((n) => n.children.length === 0 && isVisible(n) && txt(n)); }
  function labelValue(labelRe) {
    const re = rx("^\\s*(" + labelRe + ")\\s*:?\\s*"), all = leaves(document);
    const label = all.find((n) => re.test(txt(n)) && txt(n).length < 60);
    if (!label) return "";
    const own = txt(label).replace(re, "").trim();
    if (own) return own;
    const lr = label.getBoundingClientRect(), isLabel = rx("^(" + LABELS + ")\\s*:?$");
    let best = null, bestD = 1e9;
    for (const n of all) {
      if (n === label || isLabel.test(txt(n))) continue;
      const r = n.getBoundingClientRect();
      const below = r.top >= lr.bottom - 2 && r.top - lr.bottom < 40 && Math.abs(r.left - lr.left) < 24;
      const right = Math.abs(r.top - lr.top) < 10 && r.left >= lr.right - 2 && r.left - lr.right < 120;
      if (!below && !right) continue;
      const d = below ? (r.top - lr.bottom) + Math.abs(r.left - lr.left) : (r.left - lr.right);
      if (d < bestD) { bestD = d; best = n; }
    }
    return best ? txt(best) : "";
  }
  const RU_M = { "янв": 1, "фев": 2, "мар": 3, "апр": 4, "мая": 5, "май": 5, "июн": 6, "июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12 };
  function isoDate(s) {
    const m = (s || "").toLowerCase().match(/(\d{1,2})\s+([а-яa-z]{3})[а-яa-z]*\.?\s+(\d{4})/);
    if (!m || !RU_M[m[2]]) return "";
    return `${m[3]}-${String(RU_M[m[2]]).padStart(2, "0")}-${m[1].padStart(2, "0")}`;
  }
  function panel() {
    return { reservation: (labelValue("Номер бронирования|Booking number|Reservation number").match(/\d{6,}/) || [""])[0],
      guest: labelValue("Имя гостя|Guest name"), checkin: isoDate(labelValue("Заезд|Check-in")), checkout: isoDate(labelValue("Отъезд|Выезд|Check-out")),
      total: labelValue("Итого|Total"), lang: labelValue("Предпочитаемый язык|Preferred language"), guests: labelValue("Количество гостей|Number of guests"),
      room: labelValue("\\d+ номер|\\d+ номера|Room|Unit") };
  }
  // ---- messages: bubbles are the coloured blocks in the middle column; day separators give the dates
  const STATUS = /^(Доставлено|Delivered|Прочитано|Read|Отправлено|Sent|Ответ не требуется|No reply needed|Ответить|Reply|Сегодня|Today|Вчера|Yesterday)$/i;
  const SEP = /^(Сегодня|Today|Вчера|Yesterday|\d{1,2}\s+[а-яa-z]{3,}\.?\s+\d{4})$/i;
  function bubbleOf(leaf, colL, colR) {
    let el = leaf;
    for (let i = 0; i < 7 && el && el !== document.body; i++, el = el.parentElement) {
      const cs = getComputedStyle(el), r = el.getBoundingClientRect();
      const bg = cs.backgroundColor || "";
      const painted = bg && !/rgba\(0, 0, 0, 0\)|transparent/.test(bg);
      if (painted && r.width < (colR - colL) * 0.92 && r.width > 40) return el;
    }
    return null;
  }
  function conversation(S) {
    const composer = qsa(S.composer).find(isVisible);
    const P = panel();
    if (!composer) return null;
    const cr = composer.getBoundingClientRect(); const colL = cr.left - 30, colR = cr.right + 30, colMid = (colL + colR) / 2;
    const all = leaves(document).filter((n) => { const r = n.getBoundingClientRect(); return r.left >= colL && r.right <= colR && r.bottom < cr.top; });
    const msgs = []; const seen = new Set(); let day = new Date().toISOString().slice(0, 10); let lastTime = "";
    for (const n of all) {
      const t = txt(n);
      if (SEP.test(t)) { const d = /сегодня|today/i.test(t) ? new Date() : /вчера|yesterday/i.test(t) ? new Date(Date.now() - 864e5) : null; day = d ? d.toISOString().slice(0, 10) : (isoDate(t) || day); continue; }
      const tm = t.match(/^(\d{1,2}:\d{2})$/); if (tm) { lastTime = tm[1]; if (msgs.length && !msgs[msgs.length - 1].time) msgs[msgs.length - 1].time = tm[1]; continue; }
      if (STATUS.test(t) || t.length < 1) continue;
      const b = bubbleOf(n, colL, colR); if (!b || seen.has(b)) continue;
      if (b.querySelector("a, button, input, textarea") || /Защитите свой аккаунт|Protect your account/i.test(txt(b))) continue;
      seen.add(b);
      const text = txt(b).replace(/\s*\b\d{1,2}:\d{2}\b\s*$/, "").replace(/\s*(Доставлено|Delivered|Прочитано|Read|Отправлено|Sent)\s*$/i, "").trim();
      if (!text || STATUS.test(text)) continue;
      const r = b.getBoundingClientRect();
      msgs.push({ dir: (r.left + r.right) / 2 > colMid ? "out" : "in", text, day, time: "" });
    }
    if (!msgs.length && !P.guest) return null;
    return Object.assign(P, { messages: msgs.map((m) => ({ dir: m.dir, text: m.text, at: `${m.day}T${(m.time || "00:00").padStart(5, "0")}:00` })) });
  }
  function inboxList(S) {
    const search = qsa(S.search).find(isVisible); let unread = 0; const previews = [];
    if (search) {
      const sr = search.getBoundingClientRect();
      const col = leaves(document).filter((n) => { const r = n.getBoundingClientRect(); return r.left >= sr.left - 10 && r.right <= sr.right + 40 && r.top > sr.bottom; });
      for (const n of col) { const t = txt(n); if (/^\d{1,2}\s+[а-яa-z]{3,}\.?\s+\d{4}$/i.test(t)) continue; }
      unread = qsa(S.listUnread).filter(isVisible).filter((n) => { const r = n.getBoundingClientRect(); return r.left >= sr.left - 10 && r.right <= sr.right + 40 && r.width < 16; }).length;
    }
    return { unread, previews };
  }
  async function scan() {
    const S = await selectors(); if (!S || !S.composer) return { page: location.pathname, unread: 0 };
    const out = { page: location.pathname.slice(0, 80), unread: 0, conversations: [] };
    try { const li = inboxList(S); out.unread = li.unread; } catch (e) { /* ignore */ }
    try { const c = conversation(S); if (c && c.messages.length) out.conversations.push(c); } catch (e) { out.error = String(e.message || e); }
    return out;
  }
  async function openConversation(item, S) {
    const search = qsa(S.search).find(isVisible); if (!search) return false;
    const q = item.reservation && /^\d{6,}$/.test(item.reservation) ? item.reservation : item.guest;
    if (!q) return false;
    setNativeValue(search, q); search.dispatchEvent(new Event("input", { bubbles: true }));
    await sleep(1500);
    const it = qsa(S.listItem).filter(isVisible).find((n) => rx(q.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).test(txt(n))) || qsa(S.listItem).filter(isVisible)[0];
    if (!it) return false;
    it.click(); await sleep(1500);
    const guest = labelValue(S.guestLabel), res = labelValue(S.reservationLabel);
    return (item.reservation && res && res.replace(/\D/g, "") === item.reservation.replace(/\D/g, "")) || (item.guest && guest && guest.toLowerCase() === item.guest.toLowerCase()) || (!item.reservation && !!guest);
  }
  function setNativeValue(el, value) {
    const d = Object.getOwnPropertyDescriptor(el.__proto__, "value") || Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value") || Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value");
    if (d && d.set) d.set.call(el, value); else el.value = value;
  }
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  async function send(item) {
    const S = await selectors();
    const cur = { reservation: labelValue(S.reservationLabel), guest: labelValue(S.guestLabel) };
    const same = (item.reservation && cur.reservation && cur.reservation.replace(/\D/g, "") === String(item.reservation).replace(/\D/g, "")) || (!item.reservation && item.guest && cur.guest && cur.guest.toLowerCase() === item.guest.toLowerCase());
    if (!same && !(await openConversation(item, S))) return { ok: false, error: "не нашёл разговор с гостем в экстранете" };
    const composer = qsa(S.composer).find(isVisible); if (!composer) return { ok: false, error: "нет поля ввода" };
    composer.focus();
    if (composer.isContentEditable) { composer.textContent = item.text; composer.dispatchEvent(new InputEvent("input", { bubbles: true, data: item.text, inputType: "insertText" })); }
    else { setNativeValue(composer, item.text); composer.dispatchEvent(new Event("input", { bubbles: true })); composer.dispatchEvent(new Event("change", { bubbles: true })); }
    await sleep(400);
    const btn = qsa(S.sendButton).filter(isVisible).find((b) => !b.disabled && /отправить|send/i.test(txt(b) + (b.getAttribute("aria-label") || ""))) || qsa(S.sendButton).filter(isVisible).find((b) => !b.disabled);
    if (!btn) return { ok: false, error: "кнопка «Отправить» не найдена" };
    btn.click(); await sleep(1500);
    return { ok: true };
  }
  function snapshot() {
    const html = "<!doctype html>\n" + document.documentElement.outerHTML.replace(/<script[\s\S]*?<\/script>/gi, "");
    const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([html], { type: "text/html" })); a.download = "booking-extranet-snapshot.html"; a.click();
  }
  chrome.runtime.onMessage.addListener((m, s, reply) => {
    if (m.type === "scan") { scan().then(reply); return true; }
    if (m.type === "send") { send(m.item).then(reply).catch((e) => reply({ ok: false, error: String(e.message || e) })); return true; }
    if (m.type === "snapshot") { snapshot(); reply({ ok: true }); }
  });
})();
