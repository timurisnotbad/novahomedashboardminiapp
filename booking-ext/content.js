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
  function labelValue(labelRe, root) {
    // "Номер бронирования:" followed by the value in the next node
    const re = rx(labelRe);
    const nodes = qsa("*", root || document).filter((n) => n.children.length === 0 && re.test(txt(n)) && txt(n).length < 60);
    for (const n of nodes) {
      const own = txt(n);
      const after = own.replace(re, "").replace(/^[:\s]+/, "");
      if (after) return after;
      const sib = n.nextElementSibling || (n.parentElement && n.parentElement.nextElementSibling);
      if (sib && txt(sib)) return txt(sib).split("\n")[0];
    }
    return "";
  }
  function timeOf(el, S) {
    const t = el.querySelector(S.msgTime); const m = (txt(t) || txt(el)).match(/\b(\d{1,2}:\d{2})\b/);
    return m ? m[1] : "";
  }
  function isOutgoing(el, S) {
    const r = el.getBoundingClientRect(); const mid = window.innerWidth / 2;
    if (rx(S.outgoingHints).test(txt(el))) return true;
    const cs = getComputedStyle(el);
    if (/flex-end|right/.test(cs.alignSelf + cs.justifyContent + cs.textAlign + cs.marginLeft) && r.left > mid * 0.7) return true;
    return r.left + r.width / 2 > mid + 40;
  }
  function conversation(S) {
    const composer = qsa(S.composer).find(isVisible);
    const reservation = labelValue(S.reservationLabel), guest = labelValue(S.guestLabel);
    let bubbles = qsa(S.message).filter(isVisible);
    if (!bubbles.length && composer) {
      // heuristic: the scrollable column above the composer, leaf blocks with text + a HH:MM near them
      let col = composer; for (let i = 0; i < 8 && col && col.parentElement; i++) { col = col.parentElement; if (col.scrollHeight > col.clientHeight + 40 || col.querySelectorAll("*").length > 60) break; }
      bubbles = qsa("div, p, span", col).filter((n) => n.children.length <= 3 && txt(n).length > 0 && txt(n).length < 2000 && isVisible(n) && /\d{1,2}:\d{2}/.test(txt(n.parentElement || n)) && !/\d{1,2}:\d{2}$/.test(txt(n)) );
    }
    const msgs = [];
    for (const b of bubbles) {
      const text = txt(b).replace(/\s*(Доставлено|Delivered|Прочитано|Read|Ответ не требуется|Ответить)\s*$/i, "").replace(/\s*\b\d{1,2}:\d{2}\b\s*$/, "").trim();
      if (!text || text.length > 4000 || /^(Сегодня|Today|Вчера|Yesterday)$/i.test(text)) continue;
      if (msgs.length && msgs[msgs.length - 1].text === text) continue;
      msgs.push({ dir: isOutgoing(b, S) ? "out" : "in", text, time: timeOf(b, S) });
    }
    if (!msgs.length && !guest) return null;
    const today = new Date().toISOString().slice(0, 10);
    return { reservation, guest, checkin: labelValue(S.checkinLabel), checkout: labelValue(S.checkoutLabel), room: labelValue(S.roomLabel),
      messages: msgs.map((m) => ({ dir: m.dir, text: m.text, at: m.time ? `${today}T${m.time.padStart(5, "0")}:00` : undefined })) };
  }
  function inboxList(S) {
    const items = qsa(S.listItem).filter(isVisible);
    let unread = 0; const previews = [];
    for (const it of items) {
      const t = txt(it); if (!t) continue;
      const name = t.split(/\d{1,2} [а-яa-z]{3,4}\.? \d{4}|\n/i)[0].trim().slice(0, 80);
      const hasDot = !!it.querySelector(S.listUnread) || /•/.test(t);
      if (hasDot) unread++;
      previews.push({ name, unread: hasDot });
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
