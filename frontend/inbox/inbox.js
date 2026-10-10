/* «Чаты» — shared WhatsApp inbox page.
   Polls /api/inbox/poll?since=<rev> and patches only what changed. */
(function () {
  "use strict";
  const API = (window.NH_API_BASE || "") + "/api/inbox";
  const tg = window.Telegram && window.Telegram.WebApp;
  const $ = (id) => document.getElementById(id);

  // ---- access: personal link key (?k=), Telegram signature, owner key -------
  function stored(name) {
    try { return localStorage.getItem(name) || ""; } catch (e) { return ""; }
  }
  (function takeKeys() {
    const params = new URLSearchParams(location.search);
    let changed = false;
    [["k", "nh_ikey"], ["okey", "nh_okey"]].forEach(([p, ls]) => {
      const v = params.get(p);
      if (!v) return;
      try { localStorage.setItem(ls, v); } catch (e) { window["__" + ls] = v; }
      params.delete(p);
      changed = true;
    });
    if (changed) {
      const q = params.toString();
      history.replaceState(null, "", location.pathname + (q ? "?" + q : "") + location.hash);
    }
  })();
  const initData = () => { try { return (tg && tg.initData) || ""; } catch (e) { return ""; } };
  const headers = () => ({
    "X-Telegram-Init-Data": initData(),
    "X-Inbox-Key": stored("nh_ikey") || window.__nh_ikey || "",
    "X-Owner-Key": stored("nh_okey") || window.__nh_okey || "",
  });

  async function req(path, opts) {
    opts = opts || {};
    opts.headers = Object.assign({}, headers(), opts.headers || {});
    if (opts.json !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(opts.json);
      delete opts.json;
    }
    const res = await fetch(API + path, opts);
    let data = null;
    try { data = await res.json(); } catch (e) { /* empty */ }
    if (!res.ok) {
      const err = new Error((data && data.detail) || "Ошибка " + res.status);
      err.status = res.status;
      throw err;
    }
    return data;
  }

  // ---- state -------------------------------------------------------------
  const S = {
    me: null, agents: [], rev: 0, chats: new Map(), only: "all", q: "",
    open: null, msgs: [], reply: null, wa: "…", loadingOlder: false, noOlder: false,
  };

  // ---- helpers -------------------------------------------------------------
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  function linkify(s) {
    return esc(s).replace(/(https?:\/\/[^\s<]+)/g, (u) => `<a href="${u}" target="_blank" rel="noopener">${u}</a>`)
      .replace(/\*([^*\n]+)\*/g, "<b>$1</b>").replace(/_([^_\n]+)_/g, "<i>$1</i>");
  }
  function toast(t) {
    const el = $("toast");
    el.textContent = t;
    el.classList.remove("hidden");
    clearTimeout(toast.t);
    toast.t = setTimeout(() => el.classList.add("hidden"), 3200);
  }
  const pad = (n) => String(n).padStart(2, "0");
  const hm = (iso) => (iso ? iso.slice(11, 16) : "");
  function shortWhen(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    const now = new Date();
    if (d.toDateString() === now.toDateString()) return hm(iso);
    const y = new Date(now); y.setDate(now.getDate() - 1);
    if (d.toDateString() === y.toDateString()) return "вчера";
    return `${pad(d.getDate())}.${pad(d.getMonth() + 1)}${d.getFullYear() !== now.getFullYear() ? "." + String(d.getFullYear()).slice(2) : ""}`;
  }
  const MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"];
  function dayLabel(iso) {
    const d = new Date(iso);
    const now = new Date();
    if (d.toDateString() === now.toDateString()) return "Сегодня";
    const y = new Date(now); y.setDate(now.getDate() - 1);
    if (d.toDateString() === y.toDateString()) return "Вчера";
    return `${d.getDate()} ${MONTHS[d.getMonth()]}${d.getFullYear() !== now.getFullYear() ? " " + d.getFullYear() : ""}`;
  }
  const dm = (s) => (s ? `${s.slice(8, 10)}.${s.slice(5, 7)}` : "");
  const COLORS = ["#2563EB", "#5B8DEF", "#2EAD6B", "#D9534F", "#8E6CC6", "#E08A2B", "#2A9DA8", "#C2577F"];
  function avatar(c) {
    const t = (c.title || "?").replace(/^\+/, "");
    const letters = /\d/.test(t[0]) ? "#" : t.split(/\s+/).map((w) => w[0]).join("").slice(0, 2).toUpperCase();
    const color = COLORS[(c.id || 0) % COLORS.length];
    return `<div class="ib-av ch-${esc(c.channel || "wa")}" style="background:${color}" title="${esc(c.channel_name || "WhatsApp")}">${esc(letters)}</div>`;
  }
  const TICK1 = '<svg viewBox="0 0 24 24"><path d="M5 12.5l4.4 4.4L19 7.3"/></svg>';
  const TICK2 = '<svg viewBox="0 0 24 24"><path d="M2 12.5l4.4 4.4L16 7.3"/><path d="M10 15l1.9 1.9L21.5 7.3"/></svg>';
  const CLOCK = '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="7"/><path d="M12 8.5V12l2.3 1.4"/></svg>';
  const WARN = '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8"/><path d="M12 8v5M12 16h.01"/></svg>';
  function tick(status) {
    if (!status) return "";
    const ic = status === "pending" ? CLOCK : status === "failed" ? WARN : status === "sent" ? TICK1 : TICK2;
    return `<span class="ib-tick ${esc(status)}">${ic}</span>`;
  }
  function bookingText(b, long) {
    if (!b) return "";
    const when = b.when === "now" ? "живёт сейчас" : b.when === "next" ? "заезд" : "был";
    return long
      ? `🏠 <a href="#" data-bk-open="${b.id || ""}"><b>${esc(b.apartment)}</b> · ${esc(dm(b.begin))}–${esc(dm(b.end))}</a> · ${when}${b.guest ? " · " + esc(b.guest) : ""}${b.pinned ? "" : ' <span class="ib-muted">· по номеру</span>'}`
      : `${esc(b.apartment)}`;
  }
  let statuses = null;
  function loadStatuses() { if (statuses) return; req("/client-statuses").then((l) => { statuses = l || []; renderHead(); }).catch(() => { statuses = []; }); }

  // ---- chat list --------------------------------------------------------------
  function visibleChats() {
    let list = Array.from(S.chats.values());
    if (S.only === "unread") list = list.filter((c) => c.unread > 0 || (S.open && c.id === S.open.id));
    if (S.only === "mine") list = list.filter((c) => c.assignee === S.me.name);
    if (S.q) {
      const q = S.q.toLowerCase();
      const qd = S.q.replace(/\D/g, "");
      list = list.filter((c) => (c.title || "").toLowerCase().includes(q)
        || (qd && (c.phone || "").includes(qd))
        || (c.booking && ((c.booking.apartment || "").toLowerCase().includes(q) || (c.booking.guest || "").toLowerCase().includes(q))));
    }
    return list.sort((a, b) => (b.last_at || "").localeCompare(a.last_at || ""));
  }

  function renderList() {
    const list = visibleChats();
    const el = $("chat-list");
    if (!list.length) {
      el.innerHTML = `<div class="ib-list__empty">${S.q ? "Ничего не найдено" : S.only !== "all" ? "Пусто" : "Чатов пока нет — они появятся, как только WhatsApp будет подключён"}</div>`;
    } else {
      el.innerHTML = list.map((c) => {
        const last = (c.last_dir === "out" ? tick(c.last_status) : "") + esc(c.last_text || "");
        const apt = c.booking ? `<span class="ib-apt ${c.booking.when === "now" ? "now" : ""}">${bookingText(c.booking)}</span>` : "";
        return `<div class="ib-chat ${S.open && S.open.id === c.id ? "is-on" : ""} ${c.unread ? "unread" : ""}" data-chat="${c.id}">
          ${avatar(c)}
          <div class="ib-chat__body">
            <div class="ib-chat__top"><span class="ib-chat__name">${esc(c.title)}</span>${apt}<span class="ib-chat__time">${esc(shortWhen(c.last_at))}</span></div>
            <div class="ib-chat__bot"><span class="ib-chat__last">${last}</span>${c.unread ? `<span class="ib-badge">${c.unread}</span>` : ""}</div>
            ${c.assignee ? `<div class="ib-who">👤 ${esc(c.assignee)}</div>` : ""}
          </div>
        </div>`;
      }).join("");
    }
    let total = 0;
    S.chats.forEach((c) => { total += c.unread || 0; });
    $("unread-total").textContent = total ? String(total) : "";
    document.title = (total ? `(${total}) ` : "") + "Чаты · Nova Home";
  }

  // ---- conversation -----------------------------------------------------------
  function renderHead() {
    const c = S.open && S.chats.get(S.open.id) || S.open;
    if (!c) return;
    $("conv-title").textContent = c.title;
    const phone = c.phone ? `<a href="tel:+${esc(c.phone)}">+${esc(c.phone)}</a>` : "";
    const ch = c.channel && c.channel !== "wa" ? `<span class="ib-chan ch-${esc(c.channel)}">${esc(c.channel_name || "")}</span>` : "";
    $("conv-sub").innerHTML = [ch, phone, c.push_name && c.push_name !== c.title ? "~" + esc(c.push_name) : ""].filter(Boolean).join(" · ");
    const b = $("conv-booking");
    const stay = !c.booking && c.bk_stay ? c.bk_stay : null;
    const stayDates = stay && stay.checkin && stay.checkout ? `${esc(dm(stay.checkin))}–${esc(dm(stay.checkout))}` : "";
    const stayText = stay ? `<span class="ib-stay">Booking.com${stayDates ? ": " + stayDates : ""}${stay.room ? " · " + esc(stay.room) : ""}</span> <span class="ib-muted">· бронь в календаре не выбрана</span>` : "";
    const bkBtn = c.channel === "bk" ? `<button class="ib-link ib-bkinfo" id="bk-info" title="Все данные гостя и брони из экстранета">ℹ️ Данные Booking</button>` : "";
    const receipt = c.receipt ? `<span class="ib-rcpt ${c.receipt_pending ? "pending" : ""}" title="Что гость видит в своём мессенджере">${c.receipt_pending ? "👁" : "✓✓"} ${esc(c.receipt)}</span>` : "";
    b.innerHTML = `<div class="ib-booking__row"><span>${c.booking ? bookingText(c.booking, true) : stayText || '<span class="ib-muted">Бронь не привязана</span>'}</span><span class="ib-booking__btns">${bkBtn}<button class="ib-link ${stay ? "attn" : ""}" id="bk-pick">${c.booking && c.booking.pinned ? "Изменить" : stay ? "Выбрать бронь" : "Привязать бронь"}</button></span></div>` + (receipt ? `<div class="ib-booking__row ib-booking__rcpt">${receipt}</div>` : "") + (c.client_notes ? `<div class="ib-notes">📝 ${esc(c.client_notes)}</div>` : "");
    b.classList.remove("hidden");
    $("receipt").classList.toggle("hidden", !c.receipt_pending);
    const st = $("cl-status");
    loadStatuses();
    const stNames = (statuses || []).map((s) => s.name);
    if (c.client_status && !stNames.includes(c.client_status)) stNames.push(c.client_status);
    st.innerHTML = `<option value="">— статус —</option>` + stNames.map((n) => `<option value="${esc(n)}" ${n === c.client_status ? "selected" : ""}>${esc(n)}</option>`).join("");
    const col = ((statuses || []).find((s) => s.name === c.client_status) || {}).color;
    st.style.background = col ? col + "22" : ""; st.style.color = col || "";
    const sel = $("assignee");
    const names = Array.from(new Set(S.agents.concat(c.assignee ? [c.assignee] : [])));
    sel.innerHTML = `<option value="">— ничей —</option>` + names.map((n) =>
      `<option value="${esc(n)}" ${n === c.assignee ? "selected" : ""}>👤 ${esc(n)}</option>`).join("");
  }

  function msgHtml(m) {
    const out = m.direction === "out";
    let body = "";
    if (m.quoted_wa_id) {
      const q = S.msgs.find((x) => x.wa_id === m.quoted_wa_id);
      body += `<div class="ib-m__q">${esc(q ? (q.text || kindLabel(q.kind)) : "Сообщение")}</div>`;
    }
    const url = m.media_url ? esc(m.media_url) : "";
    if (m.kind === "image" || m.kind === "sticker") {
      body += url ? `<img class="ib-m__img" src="${url}" loading="lazy" alt="" data-zoom="${url}" ${m.kind === "sticker" ? 'style="max-width:160px"' : ""}/>` : `<div class="ib-m__doc">📷 <span>Фото</span></div>`;
    } else if (m.kind === "video") {
      body += url ? `<video class="ib-m__vid" src="${url}" controls preload="metadata"></video>` : `<div class="ib-m__doc">🎬 <span>Видео</span></div>`;
    } else if (m.kind === "audio") {
      body += url ? `<audio src="${url}" controls preload="none"></audio>` : `<div class="ib-m__doc">🎤 <span>Голосовое</span></div>`;
    } else if (m.kind === "document") {
      const nm = m.file_name || "Файл";
      body += url ? `<a class="ib-m__doc" href="${url}" download="${esc(nm)}" target="_blank">📄 <span>${esc(nm)}</span></a>` : `<div class="ib-m__doc">📄 <span>${esc(nm)}</span></div>`;
    } else if (m.kind === "location" && m.lat != null) {
      body += `<a class="ib-m__doc" href="https://maps.google.com/?q=${encodeURIComponent(m.lat + "," + m.lng)}" target="_blank" rel="noopener">📍 <span>Локация на карте</span></a>`;
    } else if (m.kind === "contact") {
      body += `<div class="ib-m__doc">👤 <span>Контакт</span></div>`;
    }
    const caption = m.kind === "document" && m.text === m.file_name ? "" : m.text;
    if (caption) body += `<div class="ib-m__t">${linkify(caption)}</div>`;
    if (!body) body = `<div class="ib-m__t" style="color:var(--label-2)">${esc(kindLabel(m.kind))}</div>`;
    const author = out ? `<div class="ib-m__author">${esc(m.author || "")}</div>` : "";
    const err = m.status === "failed"
      ? `<div class="ib-m__err">Не отправлено${m.error ? ": " + esc(m.error) : ""} · <button data-retry="${m.id}">повторить</button></div>` : "";
    return `${author}<div class="ib-m ${out ? "out" : "in"} ${m.reaction ? "has-react" : ""}" data-mid="${m.id}">
      <div class="ib-m__b">${body}<div class="ib-m__meta">${esc(hm(m.at))}${out ? tick(m.status) : ""}</div></div>
      ${m.reaction ? `<span class="ib-m__react">${esc(m.reaction)}</span>` : ""}
      <div class="ib-m__act"><button data-reply="${m.id}" title="Ответить"><svg viewBox="0 0 24 24"><path d="M10 8L4 13l6 5"/><path d="M4 13h10a6 6 0 0 1 6 6"/></svg></button></div>
    </div>${err}`;
  }
  function kindLabel(k) {
    return { image: "📷 Фото", video: "🎬 Видео", audio: "🎤 Голосовое", document: "📄 Файл", sticker: "Стикер", location: "📍 Локация", contact: "👤 Контакт" }[k] || "Сообщение";
  }

  function renderMsgs(keepScroll) {
    const el = $("msgs");
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
    const prevH = el.scrollHeight;
    const prevTop = el.scrollTop;
    let html = S.noOlder ? "" : `<button class="ib-more" id="older">Загрузить раньше</button>`;
    let day = "";
    let lastAuthor = null;
    S.msgs.forEach((m) => {
      const d = (m.at || "").slice(0, 10);
      if (d !== day) {
        day = d;
        lastAuthor = null;
        html += `<div class="ib-day">${esc(dayLabel(m.at))}</div>`;
      }
      // the author label once per run of replies by the same person
      const key = m.direction === "out" ? "o:" + (m.author || "") : "in";
      const mm = m.direction === "out" && key === lastAuthor ? Object.assign({}, m, { author: "" }) : m;
      if (m.direction === "out" && !mm.author) html += msgHtml(mm).replace('<div class="ib-m__author"></div>', "");
      else html += msgHtml(mm);
      lastAuthor = key;
    });
    el.innerHTML = html || `<div class="ib-day">Сообщений пока нет</div>`;
    if (keepScroll === "older") el.scrollTop = el.scrollHeight - prevH + prevTop;
    else if (keepScroll === "bottom" || atBottom) el.scrollTop = el.scrollHeight;
    else el.scrollTop = prevTop;
  }

  async function openChat(id) {
    const c = S.chats.get(id);
    S.open = c || { id };
    S.msgs = [];
    S.noOlder = false;
    setReply(null);
    $("empty").classList.add("hidden");
    $("conv").classList.remove("hidden");
    $("ib").classList.add("show-conv");
    renderHead();
    renderList();
    $("msgs").innerHTML = "";
    try { history.replaceState(null, "", "#chat=" + id); } catch (e) { /* ignore */ }
    try {
      const d = await req(`/chats/${id}`);
      if (!S.open || S.open.id !== id) return;
      S.chats.set(id, d.chat);
      S.open = d.chat;
      S.msgs = d.messages;
      S.noOlder = d.messages.length < 60;
      renderHead();
      renderMsgs("bottom");
      if (d.chat.unread) markRead(id);
      repoll();  // the waiting poll does not know about this chat yet
    } catch (e) {
      toast(e.message);
    }
    if (window.innerWidth > 760) $("text").focus();
  }

  function markRead(id) {
    const c = S.chats.get(id);
    if (c) { c.unread = 0; renderList(); }
    req(`/chats/${id}/read`, { method: "POST" }).catch(() => {});
  }

  async function loadOlder() {
    if (!S.open || S.loadingOlder || S.noOlder || !S.msgs.length) return;
    S.loadingOlder = true;
    try {
      const d = await req(`/chats/${S.open.id}?before=${S.msgs[0].id}`);
      const have = new Set(S.msgs.map((m) => m.id));
      const older = d.messages.filter((m) => !have.has(m.id));
      S.noOlder = d.messages.length < 60;
      S.msgs = older.concat(S.msgs);
      renderMsgs("older");
    } catch (e) {
      toast(e.message);
    } finally {
      S.loadingOlder = false;
    }
  }

  function closeChat() {
    S.open = null;
    $("ib").classList.remove("show-conv");
    $("conv").classList.add("hidden");
    $("empty").classList.remove("hidden");
    try { history.replaceState(null, "", location.pathname + location.search); } catch (e) { /* ignore */ }
    renderList();
  }

  function mergeMsgs(list) {
    let changed = false;
    list.forEach((m) => {
      const i = S.msgs.findIndex((x) => x.id === m.id);
      if (i >= 0) S.msgs[i] = m;
      else S.msgs.push(m);
      changed = true;
    });
    if (changed) S.msgs.sort((a, b) => (a.at || "").localeCompare(b.at || "") || a.id - b.id);
    return changed;
  }

  // ---- sending -------------------------------------------------------------------
  function setReply(m) {
    S.reply = m;
    $("reply-bar").classList.toggle("hidden", !m);
    if (m) {
      $("reply-text").textContent = m.text || kindLabel(m.kind);
      $("text").focus();
    }
  }

  let sending = false;
  async function sendText() {
    const ta = $("text");
    const text = ta.value.trim();
    if (!text || !S.open || sending) return;
    sending = true;
    $("send").disabled = true;
    const quoted = S.reply && S.reply.wa_id && !S.reply.wa_id.startsWith("local-") ? S.reply.wa_id : null;
    ta.value = "";
    autosize();
    setReply(null);
    try {
      const m = await req(`/chats/${S.open.id}/send`, { method: "POST", json: { text, quoted_id: quoted } });
      if (m && S.open) { mergeMsgs([m]); renderMsgs("bottom"); }
    } catch (e) {
      toast(e.message);
      pollNow();
    } finally {
      sending = false;
      $("send").disabled = false;
    }
  }

  async function sendFile(file) {
    if (!file || !S.open) return;
    if (file.size > 30 * 1024 * 1024) { toast("Файл больше 30 МБ"); return; }
    const caption = $("text").value.trim();
    $("text").value = "";
    autosize();
    toast("Отправляю файл…");
    try {
      const m = await req(`/chats/${S.open.id}/file`, {
        method: "POST",
        headers: {
          "Content-Type": file.type || "application/octet-stream",
          "X-File-Name": encodeURIComponent(file.name || "file"),
          "X-Caption": encodeURIComponent(caption),
        },
        body: file,
      });
      if (m && S.open) { mergeMsgs([m]); renderMsgs("bottom"); }
      toast("Отправлено");
    } catch (e) {
      toast(e.message);
      pollNow();
    }
  }

  // ---- quick commands: «/wifi» in the composer → template with variables ----
  const MONTHS_GEN = MONTHS;
  function fillVars(text) {
    const c = S.open && (S.chats.get(S.open.id) || S.open) || {};
    const b = c.booking || {};
    const fmt = (iso) => { if (!iso) return ""; const d = new Date(iso); return `${d.getDate()} ${MONTHS_GEN[d.getMonth()]}`; };
    const name = (b.guest || c.title || "").replace(/^\+\d+$/, "").split(" ")[0];
    const dt = b.default_times || ["14:00", "11:00"]; const tIn = b.arrival_time || dt[0], tOut = b.departure_time || dt[1];
    const map = { "имя": name, "объект": b.apartment || "", "заезд": fmt(b.begin), "выезд": fmt(b.end), "дата заезда": fmt(b.begin), "дата выезда": fmt(b.end),
      "время заезда": b.begin ? tIn : "", "время выезда": b.end ? tOut : "", "заезд время": b.begin ? `${fmt(b.begin)} в ${tIn}` : "", "выезд время": b.end ? `${fmt(b.end)} до ${tOut}` : "",
      "ночей": b.nights ? String(b.nights) : "", "сумма": b.amount != null ? String(b.amount) : "", "телефон": c.phone ? "+" + c.phone : "" };
    return text.replace(/\{(имя|объект|заезд|выезд|время заезда|время выезда|заезд время|выезд время|дата заезда|дата выезда|ночей|сумма|долг|оплачено|телефон)\}/g, (m, k) => map[k] || m);
  }
  let tpls = [];
  let cmdSel = 0;
  function loadTpls() {
    req("/templates").then((l) => { tpls = l || []; }).catch(() => {});
  }
  function cmdMatches() {
    const v = $("text").value;
    if (!v.startsWith("/") || /\s/.test(v)) return [];
    const q = v.slice(1).toLowerCase();
    return tpls.filter((t) => (t.command && t.command.toLowerCase().startsWith(q)) || (!q && t.command)
      || (q && (t.title || "").toLowerCase().includes(q))).slice(0, 8);
  }
  function renderCmd() {
    let box = $("cmd-box");
    const list = cmdMatches();
    if (!list.length) { if (box) box.remove(); return; }
    if (!box) {
      box = document.createElement("div");
      box.id = "cmd-box";
      box.className = "ib-cmd";
      $("conv").insertBefore(box, document.querySelector(".ib-compose"));
      box.addEventListener("mousedown", (e) => {
        const it = e.target.closest("[data-cmd]");
        if (it) { e.preventDefault(); useCmd(parseInt(it.dataset.cmd, 10)); }
      });
    }
    cmdSel = Math.min(cmdSel, list.length - 1);
    box.innerHTML = list.map((t, i) => `<div class="ib-cmd__it ${i === cmdSel ? "is-on" : ""}" data-cmd="${t.id}">
      <b>${t.command ? "/" + esc(t.command) : "—"}</b><span>${esc(t.title || "")}</span><small>${esc((t.text || "").slice(0, 90))}</small></div>`).join("");
  }
  function useCmd(id) {
    const t = tpls.find((x) => x.id === id);
    if (!t) return;
    const ta = $("text");
    ta.value = fillVars(t.text);
    const box = $("cmd-box");
    if (box) box.remove();
    autosize();
    ta.focus();
    ta.setSelectionRange(ta.value.length, ta.value.length);
  }

  function autosize() {
    const ta = $("text");
    ta.style.height = "auto";
    ta.style.height = Math.min(ta.scrollHeight, 160) + "px";
  }

  // ---- polling ---------------------------------------------------------------------
  let pollTimer = null;
  let polling = false;
  let pollCtl = null;  // the long-poll in flight: aborted when another chat is opened
  let lastIncoming = null;
  function repoll() { if (pollCtl) { try { pollCtl.abort(); } catch (e) { /* ignore */ } } else pollNow(); }
  async function pollNow() {
    if (polling) return;
    polling = true;
    clearTimeout(pollTimer);
    const since = S.rev, reqChat = S.open ? S.open.id : null;
    pollCtl = typeof AbortController !== "undefined" ? new AbortController() : null;
    try {
      // long-poll: the server answers at once when something changed, otherwise
      // holds the request up to 25 s — far fewer round trips over the tunnel
      const d = await req(`/poll?since=${since}${reqChat ? "&chat=" + reqChat : ""}${since && document.visibilityState === "visible" ? "&wait=25" : ""}`, pollCtl ? { signal: pollCtl.signal } : undefined);
      if (S.open && S.open.id !== reqChat && d.chats.some((c) => c.id === S.open.id)) {
        // another chat was opened while this request waited: its new messages are
        // not in this answer — keep `since` so the next poll (with the chat) brings them
        d.rev = since;
      }
      if (d.rev < S.rev) {  // DB was reset — start over
        S.rev = 0;
        S.chats.clear();
      }
      let ping = false;
      d.chats.forEach((c) => {
        const old = S.chats.get(c.id);
        if (c.last_dir === "in" && c.unread && (!old || old.last_at !== c.last_at) && S.rev) ping = true;
        S.chats.set(c.id, c);
      });
      S.rev = d.rev;
      if (S.open && d.messages.length) {
        mergeMsgs(d.messages);
        renderMsgs();
        const c = S.chats.get(S.open.id);
        if (c && c.unread && document.visibilityState === "visible") markRead(S.open.id);
      }
      if (d.chats.length) {
        renderList();
        if (S.open && d.chats.some((c) => c.id === S.open.id)) renderHead();
      }
      if (ping) beep();
      pollFailed = false;
    } catch (e) {
      pollFailed = !(e && e.name === "AbortError");
      if (e.status === 403) { $("denied").classList.remove("hidden"); return; }
    } finally {
      pollCtl = null;
      polling = false;
      pollTimer = setTimeout(pollNow, pollFailed ? 4000 : document.visibilityState === "visible" ? 300 : 10000);
    }
  }
  let pollFailed = false;

  const EMBED = /[?&]embed=1/.test(location.search);
  function beep() {
    if (EMBED) return; // the CRM shell plays the sound and shows the popup
    const now = Date.now();
    if (lastIncoming && now - lastIncoming < 3000) return;
    lastIncoming = now;
    try {
      const ctx = beep.ctx || (beep.ctx = new (window.AudioContext || window.webkitAudioContext)());
      const o = ctx.createOscillator();
      const g = ctx.createGain();
      o.frequency.value = 880;
      g.gain.setValueAtTime(0.0001, ctx.currentTime);
      g.gain.exponentialRampToValueAtTime(0.15, ctx.currentTime + 0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.35);
      o.connect(g).connect(ctx.destination);
      o.start();
      o.stop(ctx.currentTime + 0.4);
    } catch (e) { /* no audio */ }
  }

  // ---- WhatsApp connection ------------------------------------------------------------
  async function refreshWa() {
    try {
      const st = await req("/status");
      S.wa = st.status;
      const el = $("wa-state");
      const ok = st.status === "connected";
      el.className = "wa-state " + (ok ? "ok" : "bad");
      el.querySelector("span").textContent = ok ? "WhatsApp" : st.status === "qr" || st.status === "logged_out" ? "Нужен QR" : st.status === "offline" ? "Мост выключен" : "Подключение…";
      el.title = ok && st.me ? "Подключён: +" + (st.me.id || "").split("@")[0].split(":")[0] : (st.error || "");
      if (connModalOpen) renderConn(st);
    } catch (e) { /* poll errors are shown by pollNow */ }
  }

  let connModalOpen = false;
  function renderConn(st) {
    const ok = st.status === "connected";
    let html = `<h3>Подключение WhatsApp</h3>`;
    if (ok) {
      const num = st.me ? "+" + (st.me.id || "").split("@")[0].split(":")[0] : "";
      html += `<p>✅ Подключён ${esc(num)}. Все сотрудники видят и отвечают в этих чатах; телефон продолжает работать как обычно.</p>`;
      if (S.me.owner) html += `<div class="ib-row"><button class="ib-btn danger" data-logout>Отключить WhatsApp</button><button class="ib-btn" data-close>Закрыть</button></div>`;
      else html += `<div class="ib-row"><button class="ib-btn" data-close>Закрыть</button></div>`;
    } else if (st.status === "offline") {
      html += `<p>Программа-мост WhatsApp не запущена на компьютере. Запустите <b>restart_all.bat</b> (или <b>start_wa.bat</b>) — должно открыться окно «Nova WhatsApp».</p><div class="ib-row"><button class="ib-btn" data-close>Закрыть</button></div>`;
    } else if (st.qr && S.me.owner) {
      html += `<ol><li>Откройте WhatsApp на рабочем телефоне</li><li>Настройки → <b>Связанные устройства</b> → Привязка устройства</li><li>Наведите камеру на этот код</li></ol>
        <img class="ib-qr" src="${esc(st.qr)}" alt="QR" /><p style="text-align:center">Код обновляется сам каждые ~20 секунд</p>
        <div class="ib-row"><button class="ib-btn" data-close>Закрыть</button></div>`;
    } else if (st.status === "qr" || st.status === "logged_out") {
      html += `<p>WhatsApp ещё не привязан. Попросите владельца открыть «Чаты» и отсканировать QR-код.</p><div class="ib-row"><button class="ib-btn" data-close>Закрыть</button></div>`;
    } else {
      html += `<p>Соединяюсь с WhatsApp… ${st.error ? "<br><small>" + esc(st.error) + "</small>" : ""}</p><div class="ib-row"><button class="ib-btn" data-close>Закрыть</button></div>`;
    }
    $("modal-card").innerHTML = html;
  }
  let connTimer = null;
  function openConn() {
    connModalOpen = true;
    $("modal-card").innerHTML = "<p>Загрузка…</p>";
    $("modal").classList.remove("hidden");
    refreshWa();
    clearInterval(connTimer);
    connTimer = setInterval(refreshWa, 3000);
  }
  function closeModal() {
    $("modal").classList.add("hidden");
    $("modal-card").onclick = null;
    connModalOpen = false;
    clearInterval(connTimer);
  }

  // ---- templates -------------------------------------------------------------------
  async function openTemplates() {
    $("modal").classList.remove("hidden");
    $("modal-card").innerHTML = "<p>Загрузка…</p>";
    let list = [];
    try { list = await req("/templates"); tpls = list; } catch (e) { toast(e.message); }
    $("modal-card").innerHTML = `<h3>Шаблоны ответов</h3>
      <p>Нажмите на шаблон, чтобы вставить его. В поле сообщения можно набрать «/» и начало команды. Переменные {имя} {объект} {заезд} {выезд} подставляются из брони гостя.</p>
      <div>${list.map((t) => `<div class="ib-tpl"><div class="ib-tpl__body" data-tpl-use="${t.id}"><b>${t.command ? '<i class="ib-cmdtag">/' + esc(t.command) + '</i> ' : ""}${esc(t.title)}</b><span>${esc(t.text)}</span></div>
        <button class="ib-icon" data-tpl-del="${t.id}" title="Удалить"><svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6 6 18"/></svg></button></div>`).join("") || '<p>Пока нет шаблонов.</p>'}</div>
      <h3 style="margin-top:14px;font-size:16px">Новый шаблон</h3>
      <input class="ib-field" id="tpl-cmd" placeholder="Команда, например wifi (необязательно)" maxlength="30" />
      <input class="ib-field" id="tpl-title" placeholder="Название (например: Инструкция заезда)" maxlength="60" />
      <textarea class="ib-field" id="tpl-text" placeholder="Текст сообщения"></textarea>
      <div class="ib-row"><button class="ib-btn" data-close>Закрыть</button><button class="ib-btn primary" data-tpl-add>Сохранить</button></div>`;
    $("modal-card").dataset.tpls = JSON.stringify(list);
  }

  // ---- guest card right from the chat (pencil in the header) -------------------------
  async function openGuestEdit() {
    if (!S.open) return;
    const id = S.open.id;
    $("modal").classList.remove("hidden");
    $("modal-card").innerHTML = "<p>Загрузка…</p>";
    let g;
    try { g = await req(`/chats/${id}/client`); } catch (e) { toast(e.message); closeModal(); return; }
    loadStatuses();
    const f = (k, label, ph, type) => `<div><label class="ib-lbl">${label}</label><input class="ib-field" id="ge-${k}" type="${type || "text"}" value="${esc(g[k] || "")}" placeholder="${ph || ""}" /></div>`;
    $("modal-card").innerHTML = `<h3>Гость</h3>
      <p>Карточка гостя в CRM. Всё, что вы впишете здесь, видно в «Клиентах», в карточке брони и в этом чате.</p>
      <div class="ib-2col">${f("name", "Имя", "Имя Фамилия")}<div><label class="ib-lbl">Статус</label><select class="ib-field" id="ge-status"><option value="">— без статуса —</option>${(statuses || []).map((s) => `<option value="${esc(s.name)}" ${s.name === g.status ? "selected" : ""}>${esc(s.name)}</option>`).join("")}</select></div></div>
      <div class="ib-2col">${f("phone", "Телефон", "+998 90 123 45 67", "tel")}${f("phone2", "Доп. телефон", "", "tel")}</div>
      <div class="ib-2col">${f("email", "Email", "guest@example.com", "email")}${f("instagram", "Instagram", "@username")}</div>
      <div class="ib-2col">${f("telegram", "Telegram", "@username")}${f("city", "Город / страна", "")}</div>
      ${f("lang", "Язык общения", "RU / EN / UZ")}
      <label class="ib-lbl">Особенности гостя</label><textarea class="ib-field" id="ge-notes" placeholder="Аллергии, предпочтения, во сколько обычно заезжает…">${esc(g.notes || "")}</textarea>
      <div class="ib-row"><button class="ib-btn" data-close>Отмена</button><button class="ib-btn primary" id="ge-save">Сохранить</button></div>`;
    $("ge-save").addEventListener("click", async (e) => {
      e.target.disabled = true;
      const body = {};
      ["name", "phone", "phone2", "email", "instagram", "telegram", "city", "lang", "notes", "status"].forEach((k) => { body[k] = $("ge-" + k).value.trim(); });
      try {
        const c = await req(`/chats/${id}/client`, { method: "PATCH", json: body });
        S.chats.set(c.id, c); if (S.open && S.open.id === c.id) S.open = c;
        renderHead(); renderList(); closeModal(); toast("Карточка гостя сохранена");
      } catch (err) { toast(err.message); e.target.disabled = false; }
    });
  }

  // ---- link the chat to a booking from the calendar ------------------------------
  async function openBookingPick() {
    if (!S.open) return;
    const id = S.open.id;
    $("modal").classList.remove("hidden");
    $("modal-card").innerHTML = "<p>Загрузка…</p>";
    const when = (b) => b.when === "now" ? "живёт сейчас" : b.when === "next" ? "заезд" : "прошлая";
    const row = (b, pinned) => `<div class="ib-tpl ${b.strong ? "strong" : ""}"><div class="ib-tpl__body"><b>${esc(b.apartment)} · ${esc(dm(b.checkin))}–${esc(dm(b.checkout))}</b><span>${esc(b.guest || "гость")}${b.phone ? " · +" + esc(b.phone) : ""} · ${when(b)} · ${esc(b.source || "")}</span>${b.reason ? `<span class="ib-reason">${b.strong ? "✓" : "~"} ${esc(b.reason)}</span>` : ""}</div>
      ${pinned ? `<button class="ib-btn danger" data-bk-unlink="${b.id}">Отвязать</button>` : `<button class="ib-btn primary" data-bk-link="${b.id}">Привязать</button>`}</div>`;
    let d;
    try { d = await req(`/chats/${id}/bookings`); } catch (e) { toast(e.message); closeModal(); return; }
    $("modal-card").innerHTML = `<h3>Бронь гостя</h3>
      <p>Бронь из RealtyCalendar, к которой относится этот чат. Данные брони подтягиваются из календаря и обновляются сами. В карточке брони чат появится в списке «Чаты по этой брони».</p>
      ${d.stay ? `<div class="ib-stay-box">Booking.com говорит: <b>${d.stay.checkin ? esc(dm(d.stay.checkin)) : "?"}–${d.stay.checkout ? esc(dm(d.stay.checkout)) : "?"}</b>${d.stay.room ? " · " + esc(d.stay.room) : ""}${d.stay.reservation ? " · № " + esc(d.stay.reservation) : ""}<br><span class="ib-muted">Номер и даты в Booking могут отличаться от календаря — сверьте и выберите бронь вручную.</span></div>` : ""}
      ${d.pinned.length ? `<div class="ib-sec">Привязано</div>${d.pinned.map((b) => row(b, true)).join("")}` : ""}
      ${(d.candidates || []).length ? `<div class="ib-sec">Похоже на эту бронь</div>${d.candidates.map((b) => row(b, false)).join("")}` : d.stay ? `<p class="ib-muted">В календаре нет броней с заездом ${d.stay.checkin ? esc(dm(d.stay.checkin)) : "в этот день"} — найдите вручную ниже.</p>` : ""}
      ${d.suggested.length ? `<div class="ib-sec">По номеру телефона</div>${d.suggested.map((b) => row(b, false)).join("")}` : ""}
      <div class="ib-sec">Найти в календаре</div>
      <input class="ib-field" id="bk-q" placeholder="Гость, телефон, квартира или дата (12.10)" autocomplete="off" />
      <div id="bk-res"><p class="ib-muted">Введите имя гостя, номер или квартиру</p></div>
      <div class="ib-row"><button class="ib-btn" data-close>Закрыть</button></div>`;
    let t;
    const search = async () => {
      const q = $("bk-q").value.trim();
      if (!q) { $("bk-res").innerHTML = ""; return; }
      try {
        const list = await req(`/bookings/search?q=${encodeURIComponent(q)}`);
        const ids = new Set(d.pinned.map((b) => b.id));
        $("bk-res").innerHTML = list.filter((b) => !ids.has(b.id)).map((b) => row(b, false)).join("") || "<p>Ничего не найдено</p>";
      } catch (e) { toast(e.message); }
    };
    $("bk-q").addEventListener("input", () => { clearTimeout(t); t = setTimeout(search, 300); });
    $("bk-q").focus();
    $("modal-card").onclick = async (e) => {
      const l = e.target.closest("[data-bk-link]"), u = e.target.closest("[data-bk-unlink]");
      if (!l && !u) return;
      try {
        const c = l ? await req(`/chats/${id}/bookings`, { method: "POST", json: { booking_id: parseInt(l.dataset.bkLink, 10) } })
          : await req(`/chats/${id}/bookings/${u.dataset.bkUnlink}`, { method: "DELETE" });
        S.chats.set(c.id, c); if (S.open && S.open.id === c.id) S.open = c;
        renderHead(); renderList(); toast(l ? "Бронь привязана" : "Бронь отвязана");
        openBookingPick();
      } catch (err) { toast(err.message); }
    };
  }

  // ---- everything Booking.com told us about the stay --------------------------------
  async function openBookingInfo() {
    if (!S.open) return;
    const id = S.open.id;
    $("modal").classList.remove("hidden");
    $("modal-card").innerHTML = "<p>Загрузка…</p>";
    let d;
    try { d = await req(`/chats/${id}/bookings`); } catch (e) { toast(e.message); closeModal(); return; }
    const st = d.stay || {};
    const rowsDef = [["Номер брони", st.reservation], ["Гость", st.guest], ["Заезд", st.checkin ? dm(st.checkin) + "." + st.checkin.slice(0, 4) : ""], ["Выезд", st.checkout ? dm(st.checkout) + "." + st.checkout.slice(0, 4) : ""],
      ["Номер / объект", st.room], ["Гостей", st.guests], ["Сумма", st.total], ["Язык", st.lang], ["Обновлено", st.updated_at ? st.updated_at.replace("T", " ").slice(0, 16) : ""]];
    const rows = rowsDef.filter((r) => r[1]).map((r) => `<div class="ib-kv"><span>${esc(r[0])}</span><b>${esc(r[1])}</b></div>`).join("");
    $("modal-card").innerHTML = `<h3>Данные Booking.com</h3>
      <p class="ib-muted" style="font-size:13px">Что расширение прочитало в экстранете. Если чего-то не хватает или поле разобрано неверно — ниже весь текст панели брони как есть.</p>
      ${rows || '<p class="ib-muted">Расширение пока не передало данные брони — откройте этот разговор в экстранете, оно дочитает их за минуту.</p>'}
      ${st.url ? `<p style="font-size:13px"><a href="${esc(st.url)}" target="_blank" rel="noopener">Открыть в экстранете ↗</a></p>` : ""}
      ${st.raw ? `<details class="ib-raw"><summary>Весь текст панели брони</summary><pre>${esc(st.raw)}</pre></details>` : ""}
      <div class="ib-row"><button class="ib-btn primary" id="bk-info-pick">${d.pinned.length ? "Изменить бронь в календаре" : "Выбрать бронь в календаре"}</button><button class="ib-btn" data-close>Закрыть</button></div>`;
    $("bk-info-pick").onclick = () => openBookingPick();
  }

  async function openNewChat() {
    $("modal").classList.remove("hidden");
    $("modal-card").innerHTML = "<p>Загрузка…</p>";
    let chans = [];
    try { chans = await req("/new/channels"); } catch (e) { chans = [{ code: "wa", name: "WhatsApp", ready: true, check: true }]; }
    const first = (chans.find((c) => c.ready) || chans[0] || {}).code;
    $("modal-card").innerHTML = `<h3>Новый чат</h3>
      <p>Написать гостю первым. Номер — с кодом страны. Выберите канал: гость может быть в WhatsApp, но не в Telegram, и наоборот — кнопка «Проверить» покажет, есть ли он там.</p>
      <div class="ib-chans">${chans.map((c) => `<label class="ib-chan-opt ${c.ready ? "" : "off"}"><input type="radio" name="nc-ch" value="${c.code}" ${c.code === first ? "checked" : ""} ${c.ready ? "" : "disabled"} /><span class="ch-dot ch-${c.code === "wz" ? "wa" : c.code}"></span>${esc(c.name)}${c.hint ? `<small>${esc(c.hint)}</small>` : ""}</label>`).join("")}</div>
      <div class="ib-check"><input class="ib-field" id="nc-phone" type="tel" placeholder="+998 90 123 45 67" style="margin:0" /><button class="ib-btn" id="nc-check">Проверить</button></div>
      <div id="nc-res" class="ib-muted" style="font-size:13px;margin:4px 0 8px"></div>
      <input class="ib-field" id="nc-name" placeholder="Имя (необязательно)" maxlength="80" />
      <textarea class="ib-field" id="nc-text" placeholder="Сообщение (необязательно)"></textarea>
      <div class="ib-row"><button class="ib-btn" data-close>Отмена</button><button class="ib-btn primary" data-nc-go>Открыть чат</button></div>`;
    const check = async () => {
      const phone = $("nc-phone").value.trim(), ch = (document.querySelector("input[name=nc-ch]:checked") || {}).value;
      if (!phone) { toast("Введите номер"); return; }
      $("nc-res").textContent = "Проверяю…"; $("nc-res").className = "ib-muted";
      try {
        const r = await req(`/new/check?phone=${encodeURIComponent(phone)}&channel=${ch}`);
        $("nc-res").textContent = (r.exists === true ? "✅ " : r.exists === false ? "❌ " : "ℹ️ ") + r.note;
        $("nc-res").className = r.exists === true ? "ib-ok" : r.exists === false ? "ib-bad" : "ib-muted";
        if (r.name && !$("nc-name").value) $("nc-name").value = r.name;
      } catch (e) { $("nc-res").textContent = "✕ " + e.message; $("nc-res").className = "ib-bad"; }
    };
    $("nc-check").addEventListener("click", check);
    $("nc-phone").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); check(); } });
    document.querySelectorAll("input[name=nc-ch]").forEach((r) => r.addEventListener("change", () => { $("nc-res").textContent = ""; }));
    setTimeout(() => $("nc-phone").focus(), 50);
  }

  // ---- events ----------------------------------------------------------------------
  function bind() {
    $("chat-list").addEventListener("click", (e) => {
      const it = e.target.closest("[data-chat]");
      if (it) openChat(parseInt(it.dataset.chat, 10));
    });
    $("filters").addEventListener("click", (e) => {
      const b = e.target.closest("[data-only]");
      if (!b) return;
      S.only = b.dataset.only;
      document.querySelectorAll("#filters button").forEach((x) => x.classList.toggle("is-on", x === b));
      renderList();
    });
    $("search").addEventListener("input", (e) => { S.q = e.target.value.trim(); renderList(); });
    $("back").addEventListener("click", closeChat);
    $("send").addEventListener("click", sendText);
    $("text").addEventListener("input", () => { autosize(); renderCmd(); });
    $("text").addEventListener("blur", () => setTimeout(() => { const b = $("cmd-box"); if (b) b.remove(); }, 150));
    $("text").addEventListener("keydown", (e) => {
      const list = cmdMatches();
      if (list.length && $("cmd-box")) {
        if (e.key === "ArrowDown") { e.preventDefault(); cmdSel = (cmdSel + 1) % list.length; renderCmd(); return; }
        if (e.key === "ArrowUp") { e.preventDefault(); cmdSel = (cmdSel - 1 + list.length) % list.length; renderCmd(); return; }
        if (e.key === "Enter" || e.key === "Tab") { e.preventDefault(); useCmd(list[cmdSel].id); return; }
        if (e.key === "Escape") { $("cmd-box").remove(); return; }
      }
      // Enter sends on a computer; on phones Enter is a new line (send button)
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing && window.innerWidth > 760) {
        e.preventDefault();
        sendText();
      }
      if (e.key === "Escape") setReply(null);
    });
    $("text").addEventListener("paste", (e) => {
      const f = e.clipboardData && e.clipboardData.files && e.clipboardData.files[0];
      if (f) { e.preventDefault(); sendFile(f); }
    });
    $("attach").addEventListener("click", () => $("file").click());
    $("file").addEventListener("change", (e) => { sendFile(e.target.files[0]); e.target.value = ""; });
    $("tpl-btn").addEventListener("click", openTemplates);
    $("reply-cancel").addEventListener("click", () => setReply(null));
    $("wa-state").addEventListener("click", openConn);
    $("new-chat").addEventListener("click", openNewChat);
    $("assignee").addEventListener("change", async (e) => {
      if (!S.open) return;
      try {
        const c = await req(`/chats/${S.open.id}`, { method: "PATCH", json: { assignee: e.target.value || null } });
        S.chats.set(c.id, c);
        renderList();
        toast(c.assignee ? "Ответственный: " + c.assignee : "Ответственный снят");
      } catch (err) { toast(err.message); }
    });
    $("cl-status").addEventListener("change", async (e) => {
      if (!S.open) return;
      try {
        const c = await req(`/chats/${S.open.id}/client`, { method: "PATCH", json: { status: e.target.value } });
        S.chats.set(c.id, c); S.open = c; renderHead();
        toast(c.client_status ? "Статус: " + c.client_status : "Статус снят");
      } catch (err) { toast(err.message); }
    });
    $("conv-booking").addEventListener("click", (e) => {
      if (e.target.id === "bk-pick") { openBookingPick(); return; }
      if (e.target.id === "bk-info") { openBookingInfo(); return; }
      const a = e.target.closest("[data-bk-open]");
      if (a) {
        e.preventDefault();
        const id = parseInt(a.dataset.bkOpen, 10);
        if (!id) return;
        if (EMBED) { try { window.parent.postMessage({ nh: "booking", id }, location.origin); } catch (err) { /* ignore */ } }
        else window.open(`/crm/#bookings/${id}`, "_blank");
      }
    });
    $("rename").addEventListener("click", () => openGuestEdit());
    $("receipt").addEventListener("click", async () => {
      if (!S.open) return;
      try {
        const r = await req(`/chats/${S.open.id}/receipt`, { method: "POST" });
        if (r.chat) { S.chats.set(r.chat.id, r.chat); S.open = r.chat; renderHead(); renderList(); }
        toast(r.sent ? "Гость увидит, что сообщения прочитаны" : r.note ? "Уже " + r.note : "Нечего отмечать: новых сообщений от гостя нет");
      } catch (err) { toast(err.message); }
    });

    $("msgs").addEventListener("click", (e) => {
      if (e.target.id === "older") { loadOlder(); return; }
      const r = e.target.closest("[data-reply]");
      if (r) { setReply(S.msgs.find((m) => m.id === parseInt(r.dataset.reply, 10))); return; }
      const rt = e.target.closest("[data-retry]");
      if (rt) {
        req(`/messages/${rt.dataset.retry}/retry`, { method: "POST" })
          .then(() => { S.msgs = S.msgs.filter((m) => m.id !== parseInt(rt.dataset.retry, 10)); pollNow(); })
          .catch((err) => { toast(err.message); pollNow(); });
        return;
      }
      const z = e.target.closest("[data-zoom]");
      if (z) {
        const lb = document.createElement("div");
        lb.className = "ib-lightbox";
        lb.innerHTML = `<img src="${esc(z.dataset.zoom)}" alt="" />`;
        lb.addEventListener("click", () => lb.remove());
        document.body.appendChild(lb);
      }
    });
    // double tap / double click on a message = reply to it (phones have no hover)
    $("msgs").addEventListener("dblclick", (e) => {
      const b = e.target.closest("[data-mid]");
      if (b) setReply(S.msgs.find((m) => m.id === parseInt(b.dataset.mid, 10)));
    });
    $("msgs").addEventListener("scroll", (e) => { if (e.target.scrollTop < 40) loadOlder(); });

    $("modal").addEventListener("click", async (e) => {
      if (e.target.closest("[data-close]")) { closeModal(); return; }
      if (e.target.closest("[data-logout]")) {
        if (!confirm("Отключить WhatsApp от «Чатов»? Чтобы вернуть, нужно будет снова сканировать QR.")) return;
        try { await req("/logout", { method: "POST" }); toast("Отключено"); refreshWa(); } catch (err) { toast(err.message); }
        return;
      }
      const use = e.target.closest("[data-tpl-use]");
      if (use) {
        const list = JSON.parse($("modal-card").dataset.tpls || "[]");
        const t = list.find((x) => String(x.id) === use.dataset.tplUse);
        if (t) {
          const ta = $("text");
          const txt = fillVars(t.text);
          ta.value = ta.value ? ta.value + "\n" + txt : txt;
          autosize();
          closeModal();
          ta.focus();
        }
        return;
      }
      const del = e.target.closest("[data-tpl-del]");
      if (del) {
        if (!confirm("Удалить шаблон?")) return;
        try { await req(`/templates/${del.dataset.tplDel}`, { method: "DELETE" }); openTemplates(); } catch (err) { toast(err.message); }
        return;
      }
      if (e.target.closest("[data-tpl-add]")) {
        const text = $("tpl-text").value.trim();
        if (!text) { toast("Введите текст шаблона"); return; }
        try {
          await req("/templates", { method: "POST", json: { title: $("tpl-title").value.trim(), text, command: $("tpl-cmd").value.trim() } });
          openTemplates();
        } catch (err) { toast(err.message); }
        return;
      }
      if (e.target.closest("[data-nc-go]")) {
        const phone = $("nc-phone").value.trim();
        if (!phone) { toast("Введите номер"); return; }
        e.target.disabled = true;
        try {
          const channel = (document.querySelector("input[name=nc-ch]:checked") || {}).value || "wa";
          const c = await req("/new", { method: "POST", json: { phone, name: $("nc-name").value, text: $("nc-text").value, channel } });
          S.chats.set(c.id, c);
          closeModal();
          openChat(c.id);
        } catch (err) {
          toast(err.message);
          e.target.disabled = false;
        }
      }
    });

    // the CRM shell asks to open a chat (the iframe is kept alive between sections)
    window.addEventListener("message", (e) => {
      if (e.origin === location.origin && e.data && e.data.nh === "open" && e.data.chat) openChat(e.data.chat);
    });
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") {
        pollNow();
        if (S.open && (S.chats.get(S.open.id) || {}).unread) markRead(S.open.id);
      }
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !$("modal").classList.contains("hidden")) closeModal();
    });
  }

  // ---- start -----------------------------------------------------------------------
  async function start() {
    try {
      if (tg) { tg.ready(); tg.expand(); if (tg.setHeaderColor) tg.setHeaderColor("#FFFFFF"); }
    } catch (e) { /* not in Telegram */ }
    bind();
    try {
      const me = await req("/me");
      S.me = me.user;
      S.agents = me.agents || [];
      $("me-name").textContent = "Вы: " + S.me.name;
    } catch (e) {
      if (e.status === 403) $("denied").classList.remove("hidden");
      else toast("Сервер недоступен: " + e.message);
      return;
    }
    try {
      const d = await req("/chats");
      d.chats.forEach((c) => S.chats.set(c.id, c));
      S.rev = d.rev;
    } catch (e) { toast(e.message); }
    renderList();
    refreshWa();
    loadTpls();
    setInterval(refreshWa, 30000);
    const m = /chat=(\d+)/.exec(location.hash);
    if (m) openChat(parseInt(m[1], 10));
    pollTimer = setTimeout(pollNow, 2500);
  }
  start();
})();
