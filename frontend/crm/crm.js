/* Nova Home CRM — single-page office app. Hash routing, cookie session. */
(function () {
  "use strict";
  const API = (window.NH_API_BASE || "") + "/api/crm";
  const $ = (id) => document.getElementById(id);
  const S = { me: null, users: [], pipelines: [], fields: [], route: "home", cache: {} };

  // ---- helpers --------------------------------------------------------------
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const MONTHS = ["янв.", "февр.", "мар.", "апр.", "мая", "июн.", "июл.", "авг.", "сент.", "окт.", "нояб.", "дек."];
  const MONTHS_FULL = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"];
  const WD = ["вс", "пн", "вт", "ср", "чт", "пт", "сб"];
  const pad = (n) => String(n).padStart(2, "0");
  const iso = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const todayIso = () => iso(new Date());
  function dShort(s) { if (!s) return ""; const d = new Date(s.slice(0, 10) + "T00:00"); return `${d.getDate()} ${MONTHS[d.getMonth()]}`; }
  function dLong(s) { if (!s) return ""; const d = new Date(s.slice(0, 10) + "T00:00"); return `${d.getDate()} ${MONTHS_FULL[d.getMonth()]} ${d.getFullYear()}, ${WD[d.getDay()]}`; }
  function dtShort(s) {
    if (!s) return "";
    const d = new Date(s);
    const t = s.length > 10 ? `, ${pad(d.getHours())}:${pad(d.getMinutes())}` : "";
    return `${d.getDate()} ${MONTHS[d.getMonth()]}${t}`;
  }
  function money(v) { if (v == null || v === "") return ""; return Number(v).toLocaleString("ru-RU", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + " $"; }
  function toast(t) { const el = $("toast"); el.textContent = t; el.classList.remove("hidden"); clearTimeout(toast.t); toast.t = setTimeout(() => el.classList.add("hidden"), 3200); }
  function nights(a, b) { if (!a || !b) return ""; return Math.round((new Date(b) - new Date(a)) / 86400000); }
  function plural(n, one, few, many) { const m = n % 10, h = n % 100; return n + " " + (h > 10 && h < 20 ? many : m === 1 ? one : m > 1 && m < 5 ? few : many); }

  async function api(path, opts) {
    opts = opts || {};
    opts.credentials = "same-origin";
    opts.headers = Object.assign({}, opts.headers || {});
    if (opts.json !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(opts.json); delete opts.json; }
    const res = await fetch(path.startsWith("/../") ? (window.NH_API_BASE || "") + "/api" + path.slice(3) : API + path, opts);
    let data = null;
    try { data = await res.json(); } catch (e) { /* empty */ }
    if (res.status === 401 && S.me) { S.me = null; showAuth(false); }
    if (!res.ok) { const err = new Error((data && data.detail) || "Ошибка " + res.status); err.status = res.status; throw err; }
    return data;
  }
  const get = (p) => api(p);
  const post = (p, json) => api(p, { method: "POST", json });
  const put = (p, json) => api(p, { method: "PUT", json });
  const patch = (p, json) => api(p, { method: "PATCH", json });
  const del = (p) => api(p, { method: "DELETE" });

  // one click handler per rendered page (the <main> element itself survives renders)
  function onMain(fn) { $("main").onclick = fn; }
  function keepFocus(id) { const el = $(id); if (el && el.value) { el.focus(); el.setSelectionRange(el.value.length, el.value.length); } }
  function modal(html, wide) {
    $("modal-card").innerHTML = html;
    $("modal-card").classList.toggle("wide", !!wide);
    $("modal").classList.remove("hidden");
    const f = $("modal-card").querySelector("input, textarea, select");
    if (f) setTimeout(() => f.focus(), 50);
  }
  function closeModal() { $("modal").classList.add("hidden"); }
  function val(id) { const el = $(id); return el ? el.value.trim() : ""; }
  function userOptions(selected) {
    return S.users.filter((u) => u.active || u.id === selected).map((u) => `<option value="${u.id}" ${u.id === selected ? "selected" : ""}>${esc(u.name)}</option>`).join("");
  }
  function clientOptions(selected) {
    const list = S.cache.clients || [];
    return `<option value="">— без клиента —</option>` + list.map((c) => `<option value="${c.id}" ${c.id === selected ? "selected" : ""}>${esc(c.name)}${c.phone ? " · +" + esc(c.phone) : ""}</option>`).join("");
  }
  async function loadClients() { S.cache.clients = await get("/clients"); return S.cache.clients; }
  async function loadFields() { if (!S.fields.length) S.fields = await get("/fields"); return S.fields; }
  const fieldsFor = (entity) => S.fields.filter((f) => (f.entity || "client") === entity);
  function fieldInputs(entity, values) {
    values = values || {};
    return fieldsFor(entity).map((f) => {
      const v = values[f.id];
      let inp;
      if (f.type === "select") inp = `<select class="field" data-field="${f.id}"><option value="">—</option>${f.options.map((o) => `<option ${o === v ? "selected" : ""}>${esc(o)}</option>`).join("")}</select>`;
      else if (f.type === "checkbox") inp = `<label><input type="checkbox" data-field="${f.id}" ${v ? "checked" : ""} /> да</label>`;
      else inp = `<input class="field" data-field="${f.id}" type="${f.type === "number" ? "number" : f.type === "date" ? "date" : "text"}" value="${esc(v || "")}" />`;
      return `<div><label class="lbl">${esc(f.name)}</label>${inp}</div>`;
    }).join("");
  }
  function readFields(root) { const out = {}; root.querySelectorAll("[data-field]").forEach((el) => { out[el.dataset.field] = el.type === "checkbox" ? el.checked : el.value; }); return out; }
  function fieldRows(entity, values) {
    values = values || {};
    return fieldsFor(entity).map((f) => `<dt>${esc(f.name)}</dt><dd>${f.type === "checkbox" ? (values[f.id] ? "да" : "нет") : esc(values[f.id] || "—")}</dd>`).join("");
  }

  // ---- auth ---------------------------------------------------------------
  let setupMode = false;
  function showAuth(setup) {
    setupMode = !!setup;
    if (inboxFrame) { inboxFrame.remove(); inboxFrame = null; } // no chats behind the login form
    $("tabbar").classList.add("hidden");
    $("app").classList.add("hidden");
    $("auth").classList.remove("hidden");
    $("auth-sub").textContent = setup ? "Первый запуск: создайте администратора" : "Войдите, чтобы продолжить";
    $("auth-name-row").classList.toggle("hidden", !setup);
    $("auth-btn").textContent = setup ? "Создать и войти" : "Войти";
    $("auth-pass").autocomplete = setup ? "new-password" : "current-password";
    $("auth-err").classList.add("hidden");
  }
  $("auth-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    $("auth-btn").disabled = true;
    try {
      const body = { email: val("auth-email"), password: $("auth-pass").value };
      if (setupMode) body.name = val("auth-name");
      const d = await post(setupMode ? "/setup" : "/login", body);
      S.me = d.user;
      await boot();
    } catch (err) {
      $("auth-err").textContent = err.message;
      $("auth-err").classList.remove("hidden");
    } finally { $("auth-btn").disabled = false; }
  });
  $("logout").addEventListener("click", async (e) => { e.preventDefault(); await post("/logout"); S.me = null; showAuth(false); });
  $("me-pass").addEventListener("click", (e) => {
    e.preventDefault();
    modal(`<h2>Сменить пароль</h2><label class="lbl">Новый пароль</label><input class="field" id="np" type="password" minlength="6" />
      <div class="row-actions"><button class="btn" data-close>Отмена</button><button class="btn primary" id="np-go">Сохранить</button></div>`);
    $("np-go").addEventListener("click", async () => {
      try { await post("/me/password", { password: $("np").value }); closeModal(); toast("Пароль изменён"); } catch (err) { toast(err.message); }
    });
  });

  // ---- routing --------------------------------------------------------------
  const ROUTES = {};
  function navigate() {
    const h = (location.hash || "#home").slice(1);
    const [name, arg] = h.split("/");
    const r = ROUTES[name] ? name : "home";
    S.route = r;
    document.querySelectorAll("#nav a, #tabbar a").forEach((a) => a.classList.toggle("is-on", a.dataset.r === r));
    document.querySelector(".side").classList.remove("open");
    const main = $("main");
    main.classList.remove("hidden");
    if (inboxFrame && r !== "messages") inboxFrame.classList.add("hidden");
    main.onclick = null;
    main.innerHTML = `<div class="empty">Загрузка…</div>`;
    Promise.resolve(ROUTES[r](arg)).catch((err) => { main.innerHTML = `<div class="empty">${esc(err.message)}</div>`; });
  }
  window.addEventListener("hashchange", navigate);
  $("burger").addEventListener("click", () => document.querySelector(".side").classList.toggle("open"));
  $("modal").addEventListener("click", (e) => { if (e.target.closest("[data-close]")) closeModal(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("modal").classList.contains("hidden")) closeModal(); });

  // ---- home -----------------------------------------------------------------
  ROUTES.home = async () => {
    const d = await get("/home");
    const open = d.stages.filter((s) => s.kind === "open");
    $("main").innerHTML = `
      <div class="page-head"><h1>Главная</h1><span class="muted">${esc(dLong(d.date))}</span></div>
      <div class="grid c4" style="margin-bottom:16px">
        <div class="stat" onclick="location.hash='#bookings'"><b>${d.arrivals}</b><span>заездов сегодня</span></div>
        <div class="stat" onclick="location.hash='#bookings'"><b>${d.departures}</b><span>выездов сегодня · живут ${d.staying}</span></div>
        <div class="stat" onclick="location.hash='#messages'"><b>${d.unread}</b><span>непрочитанных сообщений</span></div>
        <div class="stat ${d.overdue_tasks ? "warn" : ""}" onclick="location.hash='#tasks'"><b>${d.my_open_tasks}</b><span>моих задач${d.overdue_tasks ? " · просрочено " + d.overdue_tasks : ""}</span></div>
      </div>
      <div class="grid c2">
        <div class="card"><div class="card__title">Воронка</div>
          ${open.length ? open.map((s) => `<div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--sep)">
            <span><i class="kcol__dot" style="background:${esc(s.color || "#999")}"></i>${esc(s.name)} <small class="muted">${esc(s.pipeline)}</small></span>
            <span><b>${s.n}</b> ${s.amount ? `<span class="muted small">· ${money(s.amount)}</span>` : ""}</span></div>`).join("") : `<div class="muted">Сделок пока нет</div>`}
          <div style="margin-top:10px"><a href="#deals">Открыть сделки →</a></div>
        </div>
        <div class="card"><div class="card__title">Задачи на сегодня</div>
          ${d.today_tasks.length ? d.today_tasks.map((t) => `<div style="padding:6px 0;border-bottom:1px solid var(--sep)">${esc(t.title)}<div class="muted small">${esc(dtShort(t.due))} · ${esc(t.assignee_name || "")}</div></div>`).join("") : `<div class="muted">На сегодня задач нет</div>`}
          <div style="margin-top:10px"><a href="#tasks">Все задачи →</a></div>
        </div>
        <div class="card"><div class="card__title">Последние чаты</div>
          ${d.recent_chats.length ? d.recent_chats.map((c) => `<div style="display:flex;justify-content:space-between;gap:8px;padding:6px 0;border-bottom:1px solid var(--sep)">
            <a href="#messages/${c.id}" style="min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"><b style="color:var(--label)">${esc(c.name || c.push_name || "+" + c.phone)}</b> <span class="muted small">${esc(c.last_text || "")}</span></a>
            ${c.unread ? `<b class="pill">${c.unread}</b>` : ""}</div>`).join("") : `<div class="muted">WhatsApp ${d.wa === "connected" ? "подключён, сообщений пока нет" : "не подключён — раздел «Каналы»"}</div>`}
        </div>
        <div class="card"><div class="card__title">Система</div>
          <dl class="kv"><dt>WhatsApp</dt><dd class="${d.wa === "connected" ? "ok" : "bad"}">${d.wa === "connected" ? "подключён" : d.wa === "offline" ? "мост не запущен" : d.wa || "—"}</dd>
          <dt>RealtyCalendar</dt><dd>${d.last_sync ? "синхронизация " + esc(dtShort(d.last_sync)) : "ещё не синхронизировано"}</dd>
          <dt>Клиентов</dt><dd>${d.clients}</dd></dl>
          <div style="margin-top:10px"><a href="#revenue">Выручка по каналам →</a> · <a href="#auto">Автосообщения →</a></div>
        </div>
      </div>`;
  };

  // ---- messages (embedded inbox) ----------------------------------------
  // the chats iframe is created once and kept alive (no reload, no lost
  // draft when switching sections); other screens render into #main
  let inboxFrame = null;
  ROUTES.messages = (chatId) => {
    if (!inboxFrame) {
      inboxFrame = document.createElement("iframe");
      inboxFrame.className = "inbox-frame";
      inboxFrame.title = "Чаты";
      inboxFrame.src = "/inbox/?v=41&embed=1" + (chatId ? "#chat=" + chatId : "");
      document.getElementById("app").appendChild(inboxFrame);
    } else if (chatId) {
      try { inboxFrame.contentWindow.postMessage({ nh: "open", chat: parseInt(chatId, 10) }, location.origin); } catch (e) { /* ignore */ }
    }
    $("main").innerHTML = "";
    $("main").classList.add("hidden");
    inboxFrame.classList.remove("hidden");
  };

  // ---- deals ------------------------------------------------------------------
  ROUTES.deals = async () => {
    S.pipelines = await get("/pipelines");
    const pid = parseInt(sessionStorage.getItem("crm_pipeline") || S.pipelines[0].id, 10);
    const p = S.pipelines.find((x) => x.id === pid) || S.pipelines[0];
    sessionStorage.setItem("crm_pipeline", p.id);
    const q = S.cache.dealQ || "";
    const deals = await get(`/deals?pipeline=${p.id}&q=${encodeURIComponent(q)}`);
    $("main").innerHTML = `
      <div class="page-head"><h1>Сделки</h1><button class="btn primary" id="deal-add">Новая сделка</button></div>
      <div class="toolbar">
        ${S.pipelines.length > 1 ? `<select class="field" id="pipe-sel" style="width:auto">${S.pipelines.map((x) => `<option value="${x.id}" ${x.id === p.id ? "selected" : ""}>${esc(x.name)}</option>`).join("")}</select>` : ""}
        <input class="field grow" id="deal-q" placeholder="Поиск: название, клиент, объект" value="${esc(q)}" />
        <span class="muted small">${plural(deals.length, "сделка", "сделки", "сделок")}</span>
      </div>
      <div class="kanban" id="kanban">${p.stages.map((s) => {
        const items = deals.filter((d) => d.stage_id === s.id);
        const sum = items.reduce((a, d) => a + (Number(d.amount) || 0), 0);
        return `<div class="kcol" data-stage="${s.id}"><div class="kcol__head"><span><i class="kcol__dot" style="background:${esc(s.color || "#999")}"></i>${esc(s.name)}</span><small>${items.length}${sum ? " · " + money(sum) : ""}</small></div>
          ${items.map((d) => `<div class="kcard" draggable="true" data-deal="${d.id}"><b>${esc(d.title)}</b>
            <div class="muted small">${esc(d.client_name || "")}${d.apartment ? " · " + esc(d.apartment) : ""}${d.checkin ? " · " + esc(dShort(d.checkin)) + (d.checkout ? "–" + esc(dShort(d.checkout)) : "") : ""}</div>
            <div class="meta"><span>${esc(d.owner_name || "")}</span><span class="amt">${money(d.amount)}</span></div></div>`).join("")}</div>`;
      }).join("")}</div>`;
    keepFocus("deal-q");
    const sel = $("pipe-sel");
    if (sel) sel.addEventListener("change", () => { sessionStorage.setItem("crm_pipeline", sel.value); navigate(); });
    let t;
    $("deal-q").addEventListener("input", (e) => { clearTimeout(t); t = setTimeout(() => { S.cache.dealQ = e.target.value.trim(); navigate(); }, 300); });
    $("deal-add").addEventListener("click", () => dealForm(null, { pipeline_id: p.id }));
    const kb = $("kanban");
    kb.addEventListener("click", (e) => { const c = e.target.closest("[data-deal]"); if (c) openDeal(parseInt(c.dataset.deal, 10)); });
    let dragId = null;
    kb.addEventListener("dragstart", (e) => { const c = e.target.closest("[data-deal]"); if (c) { dragId = c.dataset.deal; e.dataTransfer.effectAllowed = "move"; } });
    kb.addEventListener("dragover", (e) => { const col = e.target.closest(".kcol"); if (col && dragId) { e.preventDefault(); col.classList.add("over"); } });
    kb.addEventListener("dragleave", (e) => { const col = e.target.closest(".kcol"); if (col) col.classList.remove("over"); });
    kb.addEventListener("drop", async (e) => {
      const col = e.target.closest(".kcol");
      if (!col || !dragId) return;
      e.preventDefault();
      col.classList.remove("over");
      try { await patch(`/deals/${dragId}`, { stage_id: parseInt(col.dataset.stage, 10) }); navigate(); } catch (err) { toast(err.message); }
      dragId = null;
    });
  };

  async function openDeal(id) {
    const d = await get(`/deals/${id}`);
    await loadFields();
    const p = S.pipelines.find((x) => x.id === d.pipeline_id) || S.pipelines[0];
    modal(`<h2>${esc(d.title)}</h2>
      <div class="muted small" style="margin-bottom:12px">${esc(d.owner_name || "")} · создана ${esc(dtShort(d.created_at))}</div>
      <dl class="kv">
        <dt>Этап</dt><dd><select class="field" id="d-stage" style="width:auto">${p.stages.map((s) => `<option value="${s.id}" ${s.id === d.stage_id ? "selected" : ""}>${esc(s.name)}</option>`).join("")}</select></dd>
        <dt>Клиент</dt><dd>${d.client_id ? `<a href="#clients/${d.client_id}" data-close>${esc(d.client_name)}</a>${d.client_phone ? " · +" + esc(d.client_phone) : ""}` : "—"}</dd>
        <dt>Объект</dt><dd>${esc(d.apartment || "—")}</dd>
        <dt>Даты</dt><dd>${d.checkin ? esc(dShort(d.checkin)) + " – " + esc(dShort(d.checkout)) + (nights(d.checkin, d.checkout) ? " · " + plural(nights(d.checkin, d.checkout), "ночь", "ночи", "ночей") : "") : "—"}${d.guests ? " · гостей " + d.guests : ""}</dd>
        <dt>Сумма</dt><dd>${money(d.amount) || "—"}</dd>
        ${d.booking_id ? `<dt>Бронь</dt><dd><a href="#" data-bopen="${d.booking_id}">RealtyCalendar #${d.booking_id}</a></dd>` : ""}
        ${fieldRows("deal", d.fields)}
        ${d.notes ? `<dt>Заметки</dt><dd style="white-space:pre-wrap">${esc(d.notes)}</dd>` : ""}
        ${d.chats && d.chats.length ? `<dt>Чаты</dt><dd>${d.chats.map((c) => `<a href="#messages/${c.id}" data-close>${esc(c.channel_name)} · ${esc(c.title)}</a>`).join(" · ")}</dd>` : ""}
      </dl>
      ${d.log && d.log.length ? `<div class="muted small" style="margin-top:8px">⚡ ${d.log.map((l) => `${esc(l.rule_name)}: ${esc((l.text || "").slice(0, 50))} — <span class="${l.status === "sent" ? "ok" : l.status === "failed" ? "bad" : ""}">${l.status === "sent" ? "выполнено" : esc(l.error || l.status)}</span>`).join("<br>")}</div>` : ""}
      <div class="card" style="margin-top:14px;padding:12px 14px"><div class="card__title">Задачи</div>
        ${d.tasks.length ? d.tasks.map((t) => `<div class="small ${t.status === "done" ? "muted" : ""}">${t.status === "done" ? "✓ " : "○ "}${esc(t.title)} <span class="muted">${esc(dtShort(t.due))}</span></div>`).join("") : `<div class="muted small">Нет задач</div>`}
        <button class="btn sm" id="d-task" style="margin-top:8px">+ Задача</button></div>
      <div class="row-actions"><button class="btn danger" id="d-del">Удалить</button><span style="flex:1"></span>
        ${d.client_id ? `<button class="btn" id="d-chat">Написать в WhatsApp</button>` : ""}
        <button class="btn" id="d-edit">Изменить</button><button class="btn primary" data-close>Закрыть</button></div>`);
    $("d-stage").addEventListener("change", async (e) => { try { await patch(`/deals/${id}`, { stage_id: parseInt(e.target.value, 10) }); toast("Этап изменён"); if (S.route === "deals") navigate(); } catch (err) { toast(err.message); } });
    $("d-edit").addEventListener("click", () => dealForm(d));
    $("d-task").addEventListener("click", () => taskForm({ deal_id: d.id, client_id: d.client_id, title: "" }, () => openDeal(id)));
    $("d-del").addEventListener("click", async () => { if (!confirm("Удалить сделку?")) return; await del(`/deals/${id}`); closeModal(); navigate(); });
    const ch = $("d-chat");
    if (ch) ch.addEventListener("click", () => openClientChat(d.client_id));
    $("modal-card").querySelectorAll("[data-bopen]").forEach((a) => a.addEventListener("click", (e) => { e.preventDefault(); openBooking(parseInt(a.dataset.bopen, 10)); }));
  }

  async function dealForm(d, defaults) {
    d = d || Object.assign({ title: "", amount: "", apartment: "", checkin: "", checkout: "", guests: "", notes: "", client_id: null }, defaults || {});
    await loadClients();
    await loadFields();
    const pid = d.pipeline_id || S.pipelines[0].id;
    const p = S.pipelines.find((x) => x.id === pid) || S.pipelines[0];
    modal(`<h2>${d.id ? "Сделка" : "Новая сделка"}</h2>
      <label class="lbl">Название <span class="muted">(пусто — из клиента и объекта)</span></label><input class="field" id="f-title" value="${esc(d.title)}" />
      <div class="form-row"><div><label class="lbl">Клиент</label><select class="field" id="f-client">${clientOptions(d.client_id)}</select></div>
        <div><label class="lbl">Этап</label><select class="field" id="f-stage">${p.stages.map((s) => `<option value="${s.id}" ${s.id === d.stage_id ? "selected" : ""}>${esc(s.name)}</option>`).join("")}</select></div></div>
      <div class="form-row c3"><div><label class="lbl">Объект</label><input class="field" id="f-apt" value="${esc(d.apartment || "")}" placeholder="A-066" /></div>
        <div><label class="lbl">Заезд</label><input class="field" id="f-in" type="date" value="${esc(d.checkin || "")}" /></div>
        <div><label class="lbl">Выезд</label><input class="field" id="f-out" type="date" value="${esc(d.checkout || "")}" /></div></div>
      <div class="form-row c3"><div><label class="lbl">Сумма, $</label><input class="field" id="f-amt" type="number" step="any" value="${esc(d.amount ?? "")}" /></div>
        <div><label class="lbl">Гостей</label><input class="field" id="f-guests" type="number" value="${esc(d.guests ?? "")}" /></div>
        <div><label class="lbl">Ответственный</label><select class="field" id="f-owner">${userOptions(d.owner_uid || S.me.id)}</select></div></div>
      ${fieldsFor("deal").length ? `<div class="form-row">${fieldInputs("deal", d.fields)}</div>` : ""}
      <label class="lbl">Заметки</label><textarea class="field" id="f-notes">${esc(d.notes || "")}</textarea>
      <div class="row-actions"><button class="btn" data-close>Отмена</button><button class="btn primary" id="f-go">Сохранить</button></div>`);
    $("f-go").addEventListener("click", async () => {
      const body = { title: val("f-title"), client_id: val("f-client") ? parseInt(val("f-client"), 10) : null, pipeline_id: p.id,
        stage_id: parseInt(val("f-stage"), 10), apartment: val("f-apt"), checkin: val("f-in"), checkout: val("f-out"),
        amount: val("f-amt"), guests: val("f-guests"), owner_uid: parseInt(val("f-owner"), 10), notes: val("f-notes"), fields: readFields($("modal-card")) };
      try {
        const r = d.id ? await patch(`/deals/${d.id}`, body) : await post("/deals", body);
        closeModal(); toast("Сохранено");
        if (S.route === "deals") navigate(); else openDeal(r.id);
      } catch (err) { toast(err.message); }
    });
  }

  // ---- clients ----------------------------------------------------------------
  ROUTES.clients = async (id) => {
    if (id) return openClient(parseInt(id, 10), true);
    const q = S.cache.clientQ || "";
    const list = await get(`/clients?history=1&q=${encodeURIComponent(q)}`);
    S.cache.clients = list;
    $("main").innerHTML = `
      <div class="page-head"><h1>Клиенты</h1><div style="display:flex;gap:8px;flex-wrap:wrap"><a class="btn" href="${API}/clients/export.xlsx" download>⬇ Excel</a><button class="btn" id="c-import" title="Создать карточки для всех, кто есть в бронях и чатах">Подтянуть из броней и чатов</button><button class="btn primary" id="c-add">Добавить</button></div></div>
      <div class="toolbar"><input class="field grow" id="c-q" placeholder="Поиск: имя, телефон, email, комментарий" value="${esc(q)}" /><span class="muted small">${plural(list.length, "клиент", "клиента", "клиентов")}</span></div>
      ${list.length ? `<div class="table-wrap"><table class="table"><thead><tr><th>Имя</th><th>Телефон</th><th>Визитов</th><th>Сумма</th><th>Последний выезд</th><th>Источник</th><th>Комментарий</th></tr></thead><tbody>
        ${list.map((c) => `<tr class="click" data-id="${c.id}"><td><b>${esc(c.name)}</b></td><td>${c.phone ? "+" + esc(c.phone) : "—"}</td><td>${c.visits || 0}${c.nights ? ` <span class="muted small">· ${c.nights} н.</span>` : ""}</td><td>${c.amount ? money(c.amount) : ""}</td><td class="muted small">${esc(dShort(c.last_stay))}</td><td>${esc(c.source || "")}</td><td class="muted small" style="max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(c.notes || "")}</td></tr>`).join("")}
      </tbody></table></div>` : `<div class="card empty">${q ? "Ничего не найдено" : "Клиентов пока нет. Нажмите «Подтянуть из броней и чатов» — карточки создадутся по номерам телефонов."}</div>`}`;
    keepFocus("c-q");
    let t;
    $("c-q").addEventListener("input", (e) => { clearTimeout(t); t = setTimeout(() => { S.cache.clientQ = e.target.value.trim(); navigate(); }, 300); });
    $("c-add").addEventListener("click", () => clientForm(null));
    $("c-import").addEventListener("click", async (e) => { e.target.disabled = true; try { const r = await post("/clients/import"); toast(`Добавлено клиентов: ${r.added}`); navigate(); } catch (err) { toast(err.message); e.target.disabled = false; } });
    onMain((e) => { const r = e.target.closest("tr[data-id]"); if (r) openClient(parseInt(r.dataset.id, 10)); });
  };

  async function openClient(id, asPage) {
    const c = await get(`/clients/${id}`);
    await loadFields();
    const fieldRowsHtml = fieldRows("client", c.fields);
    const html = `<h2>${esc(c.name)}</h2>
      <div class="muted small" style="margin-bottom:12px">${c.phone ? `<a href="tel:+${esc(c.phone)}">+${esc(c.phone)}</a>` : ""}${c.email ? " · " + esc(c.email) : ""}${c.source ? " · " + esc(c.source) : ""}</div>
      <div class="grid c2">
        <div><dl class="kv">${fieldRowsHtml}${c.notes ? `<dt>Заметки</dt><dd style="white-space:pre-wrap">${esc(c.notes)}</dd>` : ""}</dl>
          ${!fieldRowsHtml && !c.notes ? `<div class="muted small">Дополнительных полей нет — настраиваются в «Поля карточек»</div>` : ""}</div>
        <div>
          <div class="card__title">Брони</div>
          ${c.bookings.length ? c.bookings.slice(0, 6).map((b) => `<div class="small" style="padding:4px 0;border-bottom:1px solid var(--sep)"><b>${esc(b.apartment)}</b> · ${esc(dShort(b.checkin))}–${esc(dShort(b.checkout))} · ${money(b.amount)} <span class="muted">${esc(b.source)}</span></div>`).join("") : `<div class="muted small">Броней не найдено</div>`}
          <div class="card__title" style="margin-top:12px">Сделки</div>
          ${c.deals.length ? c.deals.map((d) => `<div class="small" style="padding:4px 0;border-bottom:1px solid var(--sep)"><a href="#" data-deal="${d.id}">${esc(d.title)}</a> ${money(d.amount)}</div>`).join("") : `<div class="muted small">Нет сделок</div>`}
          <div class="card__title" style="margin-top:12px">Задачи</div>
          ${c.tasks.length ? c.tasks.slice(0, 6).map((t) => `<div class="small ${t.status === "done" ? "muted" : ""}">${t.status === "done" ? "✓" : "○"} ${esc(t.title)} <span class="muted">${esc(dtShort(t.due))}</span></div>`).join("") : `<div class="muted small">Нет задач</div>`}
        </div></div>
      <div class="row-actions">${S.me.role === "admin" ? `<button class="btn danger" id="c-del">Удалить</button>` : ""}<span style="flex:1"></span>
        <button class="btn" id="c-task">+ Задача</button><button class="btn" id="c-deal">+ Сделка</button>
        <button class="btn" id="c-chat">${c.chat_id ? "Открыть чат" : "Написать в WhatsApp"}</button>
        <button class="btn" id="c-edit">Изменить</button>${asPage ? `<a class="btn primary" href="#clients">К списку</a>` : `<button class="btn primary" data-close>Закрыть</button>`}</div>`;
    if (asPage) { $("main").innerHTML = `<div class="card">${html}</div>`; } else modal(html, true);
    const root = asPage ? $("main") : $("modal-card");
    root.querySelector("#c-edit").addEventListener("click", () => clientForm(c));
    root.querySelector("#c-task").addEventListener("click", () => taskForm({ client_id: c.id, title: "" }, () => openClient(id, asPage)));
    root.querySelector("#c-deal").addEventListener("click", async () => { S.pipelines = S.pipelines.length ? S.pipelines : await get("/pipelines"); dealForm(null, { client_id: c.id, title: "" }); });
    root.querySelector("#c-chat").addEventListener("click", () => openClientChat(c.id));
    root.querySelectorAll("[data-deal]").forEach((a) => a.addEventListener("click", async (e) => { e.preventDefault(); S.pipelines = S.pipelines.length ? S.pipelines : await get("/pipelines"); openDeal(parseInt(a.dataset.deal, 10)); }));
    const dl = root.querySelector("#c-del");
    if (dl) dl.addEventListener("click", async () => { if (!confirm("Удалить клиента?")) return; await del(`/clients/${id}`); closeModal(); location.hash = "#clients"; navigate(); });
  }

  async function openClientChat(cid) {
    try { const r = await post(`/clients/${cid}/chat`); closeModal(); location.hash = `#messages/${r.chat_id}`; } catch (err) { toast(err.message); }
  }

  async function clientForm(c) {
    c = c || { name: "", phone: "", email: "", source: "", notes: "", fields: {} };
    await loadFields();
    const fieldInputsHtml = fieldInputs("client", c.fields);
    modal(`<h2>${c.id ? "Клиент" : "Новый клиент"}</h2>
      <div class="form-row"><div><label class="lbl">Имя</label><input class="field" id="cf-name" value="${esc(c.name)}" /></div>
        <div><label class="lbl">Телефон</label><input class="field" id="cf-phone" value="${c.phone ? "+" + esc(c.phone) : ""}" placeholder="+998 90 123 45 67" /></div></div>
      <div class="form-row"><div><label class="lbl">Email</label><input class="field" id="cf-email" value="${esc(c.email || "")}" /></div>
        <div><label class="lbl">Источник</label><input class="field" id="cf-source" value="${esc(c.source || "")}" placeholder="Booking.com, Airbnb, WhatsApp…" list="src-list" /><datalist id="src-list"><option>Booking.com</option><option>Airbnb</option><option>WhatsApp</option><option>Telegram</option><option>Instagram</option><option>Рекомендация</option></datalist></div></div>
      ${fieldInputsHtml ? `<div class="form-row">${fieldInputsHtml}</div>` : ""}
      <label class="lbl">Заметки</label><textarea class="field" id="cf-notes">${esc(c.notes || "")}</textarea>
      <div class="row-actions"><button class="btn" data-close>Отмена</button><button class="btn primary" id="cf-go">Сохранить</button></div>`);
    $("cf-go").addEventListener("click", async () => {
      const body = { name: val("cf-name"), phone: val("cf-phone"), email: val("cf-email"), source: val("cf-source"), notes: val("cf-notes"), fields: readFields($("modal-card")) };
      try {
        const r = c.id ? await put(`/clients/${c.id}`, body) : await post("/clients", body);
        closeModal(); toast("Сохранено");
        if (S.route === "clients" && !location.hash.includes("/")) navigate(); else openClient(r.id, S.route === "clients");
      } catch (err) { toast(err.message); }
    });
  }

  // ---- tasks ------------------------------------------------------------------
  ROUTES.tasks = async () => {
    const f = S.cache.taskF || { mine: 1, status: "open" };
    S.cache.taskF = f;
    const list = await get(`/tasks?mine=${f.mine}&status=${f.status}`);
    const now = new Date().toISOString().slice(0, 16);
    const today = todayIso();
    const groups = [["Просрочено", (t) => t.due && t.due < now && t.status === "open"], ["Сегодня", (t) => t.due && t.due.slice(0, 10) === today && !(t.due < now)],
      ["В работе", (t) => t.status === "open" && (!t.due || t.due.slice(0, 10) > today)], ["Выполнено", (t) => t.status === "done"]];
    const row = (t) => `<div class="task ${t.status}" data-id="${t.id}"><input type="checkbox" ${t.status === "done" ? "checked" : ""} data-done="${t.id}" />
      <div class="task__body"><div class="task__title">${esc(t.title)}</div>
        <div class="task__meta">${t.due ? `<span class="${t.status === "open" && t.due < now ? "late" : ""}">${esc(dtShort(t.due))}</span>` : ""}<span>${esc(t.assignee_name || "—")}</span>${t.client_id ? `<a href="#clients/${t.client_id}">${esc(t.client_name || "клиент")}</a>` : ""}${t.deal_id ? `<a href="#" data-deal="${t.deal_id}">сделка</a>` : ""}</div></div>
      <button class="task__del" data-edit="${t.id}" title="Изменить">✎</button><button class="task__del" data-del="${t.id}" title="Удалить">✕</button></div>`;
    $("main").innerHTML = `
      <div class="page-head"><h1>Задачи</h1></div>
      <div class="toolbar"><div class="seg"><button data-mine="1" class="${f.mine ? "is-on" : ""}">Мои</button><button data-mine="0" class="${!f.mine ? "is-on" : ""}">Все</button></div>
        <div class="seg"><button data-status="open" class="${f.status === "open" ? "is-on" : ""}">Открытые</button><button data-status="done" class="${f.status === "done" ? "is-on" : ""}">Выполненные</button></div></div>
      <div class="card"><div class="card__title">Новая задача</div>
        <input class="field" id="t-title" placeholder="Новая задача, например «Отправить адрес и код домофона»" />
        <div style="display:flex;gap:8px;margin-top:8px;flex-wrap:wrap"><input class="field" id="t-due" type="datetime-local" style="width:auto" />
          <select class="field" id="t-who" style="width:auto">${userOptions(S.me.id)}</select><button class="btn" id="t-add">Добавить</button></div></div>
      ${groups.map(([name, fn]) => { const items = list.filter(fn); return items.length ? `<div class="card"><div class="card__title">${name} · ${items.length}</div>${items.map(row).join("")}</div>` : ""; }).join("") || `<div class="card empty">Задач нет</div>`}`;
    onMain(async (e) => {
      const m = e.target.closest("[data-mine]"); if (m) { f.mine = parseInt(m.dataset.mine, 10); navigate(); return; }
      const st = e.target.closest("[data-status]"); if (st) { f.status = st.dataset.status; navigate(); return; }
      if (e.target.id === "t-add") { addTask(); return; }
      const dn = e.target.closest("[data-done]"); if (dn) { try { await patch(`/tasks/${dn.dataset.done}`, { status: dn.checked ? "done" : "open" }); navigate(); } catch (err) { toast(err.message); } return; }
      const dl = e.target.closest("[data-del]"); if (dl) { if (confirm("Удалить задачу?")) { await del(`/tasks/${dl.dataset.del}`); navigate(); } return; }
      const ed = e.target.closest("[data-edit]"); if (ed) { taskForm(list.find((t) => t.id === parseInt(ed.dataset.edit, 10)), navigate); return; }
      const dd = e.target.closest("[data-deal]"); if (dd) { e.preventDefault(); S.pipelines = S.pipelines.length ? S.pipelines : await get("/pipelines"); openDeal(parseInt(dd.dataset.deal, 10)); }
    });
    $("t-title").addEventListener("keydown", (e) => { if (e.key === "Enter") addTask(); });
    async function addTask() {
      const title = val("t-title");
      if (!title) { toast("Введите текст задачи"); return; }
      try { await post("/tasks", { title, due: val("t-due") || null, assignee_uid: parseInt(val("t-who"), 10) }); toast("Задача добавлена"); navigate(); } catch (err) { toast(err.message); }
    }
  };

  async function taskForm(t, after) {
    await loadClients();
    modal(`<h2>${t.id ? "Задача" : "Новая задача"}</h2>
      <label class="lbl">Текст</label><input class="field" id="tf-title" value="${esc(t.title || "")}" />
      <div class="form-row"><div><label class="lbl">Срок</label><input class="field" id="tf-due" type="datetime-local" value="${esc((t.due || "").slice(0, 16))}" /></div>
        <div><label class="lbl">Исполнитель</label><select class="field" id="tf-who">${userOptions(t.assignee_uid || S.me.id)}</select></div></div>
      <label class="lbl">Клиент</label><select class="field" id="tf-client">${clientOptions(t.client_id)}</select>
      <div class="row-actions"><button class="btn" data-close>Отмена</button><button class="btn primary" id="tf-go">Сохранить</button></div>`);
    $("tf-go").addEventListener("click", async () => {
      const body = { title: val("tf-title"), due: val("tf-due") || null, assignee_uid: parseInt(val("tf-who"), 10),
        client_id: val("tf-client") ? parseInt(val("tf-client"), 10) : null, deal_id: t.deal_id || null };
      try { if (t.id) await patch(`/tasks/${t.id}`, body); else await post("/tasks", body); closeModal(); toast("Сохранено"); if (after) after(); } catch (err) { toast(err.message); }
    });
  }

  // ---- bookings ---------------------------------------------------------------
  ROUTES.bookings = async () => {
    const st = S.cache.bk || { view: "day", date: todayIso(), start: todayIso() };
    S.cache.bk = st;
    const head = `<div class="page-head"><h1>Брони</h1></div><div class="page-sub">Из RealtyCalendar. Создавать и менять брони — в RealtyCalendar, CRM обновится сама.</div>
      <div class="toolbar"><div class="seg"><button data-view="day" class="${st.view === "day" ? "is-on" : ""}">День</button><button data-view="chess" class="${st.view === "chess" ? "is-on" : ""}">Шахматка</button></div>
        <button class="btn" data-step="-1">←</button><input class="field" type="date" id="bk-date" value="${st.view === "day" ? st.date : st.start}" style="width:auto" /><button class="btn" data-step="1">→</button><button class="btn link" id="bk-today">Сегодня</button>
        <span style="flex:1"></span><button class="btn sm" id="bk-sync">Обновить из RC</button></div>`;
    const PAY = { paid: ["Оплачено", "green"], prepaid: ["Предоплата", "orange"], unpaid: ["Не оплачено", "red"], unconfirmed: ["Не подтверждена", ""] };
    const CONTACT = { contacted: ["✓ связь есть", "green"], incoming: ["✉ гость писал, без ответа", "orange"], none: ["○ связи не было", "red"] };
    const payTag = (b) => { const p = PAY[b.pay] || PAY.unpaid; return `<span class="tag ${p[1]}">${p[0]}</span>`; };
    const contactTag = (b) => { const c = CONTACT[b.contact] || CONTACT.none; return `<span class="tag ${c[1]}" style="font-weight:500">${c[0]}</span>`; };
    const bkCard = (b) => `<div class="bk pay-${esc(b.pay || "unpaid")} contact-${esc(b.contact || "none")}"><div class="bk__top"><span class="bk__apt">${esc(b.apartment)}</span><span class="bk__amt">${money(b.amount)}</span></div>
      <div class="bk__sub">${esc(dShort(b.checkin))} – ${esc(dShort(b.checkout))} · ${plural(b.nights || nights(b.checkin, b.checkout) || 0, "ночь", "ночи", "ночей")}${b.arrival_time || b.departure_time ? ` · ${esc(b.arrival_time || "")}${b.arrival_time && b.departure_time ? "/" : ""}${esc(b.departure_time || "")}` : ""}</div>
      <div class="bk__guest">${b.client_id ? `<a href="#clients/${b.client_id}">${esc(b.guest || "Гость")}</a>` : `<b>${esc(b.guest || "Гость")}</b>`}${b.phone ? ` · +${esc(b.phone)}` : ""} <span class="muted small">· ${esc(b.source)}</span></div>
      <div class="bk__tags">${payTag(b)}${Number(b.debt) > 0 ? `<span class="tag red">долг ${money(b.debt)}</span>` : ""}${contactTag(b)}</div>
      ${b.notes ? `<div class="bk__notes">${esc(b.notes)}</div>` : ""}
      <div class="bk__links"><a href="#" data-bopen="${b.id}">Открыть бронь</a><a href="#" data-bdeal="${b.id}">${b.deal_id ? "Сделка" : "+ Сделка"}</a></div></div>`;
    if (st.view === "day") {
      const d = await get(`/bookings/day?date=${st.date}`);
      $("main").innerHTML = head + `<div class="muted" style="margin:-8px 0 14px">${esc(dLong(st.date))} · живут ${d.staying.length}</div><div class="bk-cols">
        <div class="card"><div class="card__title">Заезды · ${d.arrivals.length}</div>${d.arrivals.map(bkCard).join("") || `<div class="muted">Нет заездов</div>`}</div>
        <div class="card"><div class="card__title">Выезды · ${d.departures.length}</div>${d.departures.map(bkCard).join("") || `<div class="muted">Нет выездов</div>`}</div></div>`;
    } else {
      const g = await get(`/bookings/grid?start=${st.start}&days=30`);
      const d0 = new Date(st.start + "T00:00");
      const days = []; for (let i = 0; i < g.days; i++) { const d = new Date(d0); d.setDate(d0.getDate() + i); days.push(d); }
      const today = todayIso();
      const byApt = {}; g.bookings.forEach((b) => (byApt[b.apartment] = byApt[b.apartment] || []).push(b));
      const apts = g.apartments.slice(); Object.keys(byApt).forEach((a) => { if (!apts.includes(a)) apts.push(a); });
      const src = (b) => b.source === "Airbnb" ? "airbnb" : b.source === "Booking.com" ? "booking" : "direct";
      const rows = apts.map((a) => {
        const cells = days.map((d, i) => {
          const di = iso(d);
          const b = (byApt[a] || []).find((x) => x.checkin === di && x.checkout > di) || (i === 0 ? (byApt[a] || []).find((x) => x.checkin < di && x.checkout > di) : null);
          let bar = "";
          if (b) {
            const startIdx = Math.max(0, Math.round((new Date(b.checkin + "T00:00") - d0) / 86400000));
            const endIdx = Math.min(g.days, Math.round((new Date(b.checkout + "T00:00") - d0) / 86400000));
            const span = Math.max(1, endIdx - startIdx) - (startIdx === i ? 0 : 0);
            const w = `calc(${span * 100}% - 4px)`;
            const left = startIdx === i ? "50%" : "2px";
            bar = `<span class="bar pay-${esc(b.pay || "unpaid")} contact-${esc(b.contact || "none")}" style="left:${left};width:${w}" data-bk="${b.id}" title="${esc(b.guest || "")} · ${esc(dShort(b.checkin))}–${esc(dShort(b.checkout))} · ${money(b.amount)} · ${esc(b.source)} · ${(PAY[b.pay] || PAY.unpaid)[0]} · ${(CONTACT[b.contact] || CONTACT.none)[0]}">${b.contact === "none" ? "○ " : b.contact === "incoming" ? "✉ " : ""}${esc(b.guest || "Гость")}</span>`;
          }
          return `<td class="${d.getDay() % 6 === 0 ? "we" : ""} ${di === today ? "today" : ""}">${bar}</td>`;
        }).join("");
        return `<tr><td class="apt">${esc(a)}</td>${cells}</tr>`;
      }).join("");
      $("main").innerHTML = head + `<div class="chess"><table><thead><tr><th class="apt">Объект</th>${days.map((d) => `<th class="${d.getDay() % 6 === 0 ? "we" : ""} ${iso(d) === today ? "today" : ""}">${d.getDate()}<br><small>${WD[d.getDay()]}</small></th>`).join("")}</tr></thead><tbody>${rows}</tbody></table></div>
        <div class="muted small" style="margin-top:8px">Цвет — оплата как в календаре: <span class="tag" style="background:#22C55E;color:#fff">оплачено</span> <span class="tag" style="background:#F59E0B;color:#fff">предоплата</span> <span class="tag" style="background:#EF4444;color:#fff">не оплачено</span> <span class="tag" style="background:#9CA3AF;color:#fff">не подтверждена</span> · Связь с гостем: сплошная — писали ему, <b>✉ пунктир</b> — гость писал, без ответа, <b>○ бледная</b> — связи не было. Источник — в подсказке при наведении.</div>`;
      $("main").querySelector(".chess").addEventListener("click", (e) => {
        const bar = e.target.closest("[data-bk]");
        if (!bar) return;
        openBooking(parseInt(bar.dataset.bk, 10));
      });
    }
    onMain(async (e) => {
      const v = e.target.closest("[data-view]"); if (v) { st.view = v.dataset.view; navigate(); return; }
      const s = e.target.closest("[data-step]");
      if (s) { const k = st.view === "day" ? "date" : "start"; const d = new Date(st[k] + "T00:00"); d.setDate(d.getDate() + parseInt(s.dataset.step, 10) * (st.view === "day" ? 1 : 7)); st[k] = iso(d); navigate(); return; }
      if (e.target.id === "bk-today") { st.date = st.start = todayIso(); navigate(); return; }
      if (e.target.id === "bk-sync") { e.target.disabled = true; try { const r = await post("/integrations/sync"); toast(`Обновлено: ${r.bookings} броней`); navigate(); } catch (err) { toast(err.message); e.target.disabled = false; } return; }
      const bd = e.target.closest("[data-bdeal]"); if (bd) { e.preventDefault(); try { S.pipelines = S.pipelines.length ? S.pipelines : await get("/pipelines"); const d = await post(`/bookings/${bd.dataset.bdeal}/deal`); openDeal(d.id); } catch (err) { toast(err.message); } return; }
      const bo = e.target.closest("[data-bopen]"); if (bo) { e.preventDefault(); openBooking(parseInt(bo.dataset.bopen, 10)); }
    });
    $("bk-date").addEventListener("change", (e) => { if (e.target.value) { st[st.view === "day" ? "date" : "start"] = e.target.value; navigate(); } });
  };

  // ---- channels ---------------------------------------------------------------
  let chTimer = null;
  let tgStep = null; // telegram login: null | "code" | "password"
  ROUTES.channels = async () => {
    clearInterval(chTimer);
    const admin = S.me.role === "admin";
    const render = async () => {
      if (!$("modal").classList.contains("hidden")) return; // don't repaint under an open dialog
      const d = await get("/channels");
      if (S.route !== "channels") { clearInterval(chTimer); return; }
      const wz = d.wazzup;
      const w = d.whatsapp, wc = d.whatsapp_cloud, ig = d.instagram, tg = d.telegram, gb = d.telegram_guest_bot, mw = d.meta_webhook;
      const ok = w.status === "connected";
      const num = w.me ? "+" + (w.me.id || "").split("@")[0].split(":")[0] : "";
      const state = (good, text) => `<div class="small ${good === true ? "ok" : good === false ? "bad" : "muted"}">${text}</div>`;
      const cfgRow = (name, hint) => `<div class="muted small">${name}: <span class="mono">${esc(hint)}</span></div>`;
      const tgForm = !tg.configured ? "" : tg.authorized ? "" : !admin ? `<div class="muted small">Вход выполняет администратор.</div>` :
        tgStep === "code" || tg.pending_phone && tgStep !== "password" ? `<div class="form-row" style="margin-top:10px;align-items:end"><div><label class="lbl">Код из Telegram (${esc(tg.pending_phone || "")})</label><input class="field" id="tg-code" inputmode="numeric" placeholder="12345" /></div><div><button class="btn primary" id="tg-sign">Войти</button> <button class="btn link" id="tg-again">другой номер</button></div></div>`
        : tgStep === "password" ? `<div class="form-row" style="margin-top:10px;align-items:end"><div><label class="lbl">Пароль двухэтапной защиты</label><input class="field" id="tg-pass" type="password" /></div><div><button class="btn primary" id="tg-sign-pass">Войти</button></div></div>`
        : `<div class="form-row" style="margin-top:10px;align-items:end"><div><label class="lbl">Номер телефона аккаунта</label><input class="field" id="tg-phone" type="tel" placeholder="+998 90 123 45 67" /></div><div><button class="btn primary" id="tg-send">Получить код</button></div></div>`;
      const wzChan = (c) => `<span class="tag ${c.state === "active" ? "green" : "orange"}">${esc(c.transport || "")}${c.plainId ? " · " + esc(c.plainId) : ""}${c.state && c.state !== "active" ? " · " + esc(c.state) : ""}</span>`;
      $("main").innerHTML = `<div class="page-head"><h1>Каналы</h1></div><div class="page-sub">Откуда приходят сообщения в «Сообщения». Все каналы попадают в один список чатов, карточка клиента создаётся сама.</div>
        <div class="card"><div style="display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap">
          <div><h3>Wazzup — WhatsApp по API</h3>${state(wz.configured ? (wz.ok && wz.webhook_ok !== false) : null, !wz.configured ? "Не настроен" : !wz.ok ? "Ошибка: " + esc(wz.error || "") : wz.webhook_ok === false ? "Ключ работает, вебхук не подключён: " + esc(wz.webhook_error || "") : wz.webhook_ok ? "Подключён · вебхук зарегистрирован" : "Подключён · проверяю вебхук…")}
            <div class="muted small">Основной канал WhatsApp: номер живёт в Wazzup, CRM получает входящие и отвечает через их API. Чатов: ${wz.chats || 0}.</div>
            ${wz.channels && wz.channels.length ? `<div style="margin-top:6px">${wz.channels.map(wzChan).join(" ")}</div>` : ""}</div>
          ${wz.configured && admin ? `<button class="btn" id="wz-hook">Подключить вебхук заново</button>` : ""}</div>
          ${wz.configured ? `<div class="muted small" style="margin-top:8px">Адрес вебхука: <span class="mono">${esc(wz.webhook_url || "")}</span></div>` : `<details class="small" style="margin-top:8px"><summary>Как подключить</summary><ol class="muted" style="margin:6px 0 0 18px"><li>Wazzup → Настройки → Интеграция с CRM → скопировать <b>Ключ API</b>.</li><li>В <code>.env</code>: <span class="mono">WAZZUP_API_KEY=…</span>, затем <code>restart_all.bat</code>.</li><li>Вебхук зарегистрируется сам (нужен https-адрес WEBAPP_URL).</li></ol></details>`}
        </div>
        <div class="card"><div style="display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap">
          <div><h3>WhatsApp — привязка по QR ${num ? `<span class="muted">${esc(num)}</span>` : ""}</h3>${state(ok ? true : false, ok ? "Подключён" : w.status === "offline" ? "Мост не запущен (окно «Nova WhatsApp»)" : w.status === "qr" || w.status === "logged_out" ? "Не привязан — отсканируйте QR-код" : "Подключение… " + esc(w.error || ""))}
            <div class="muted small">Как WhatsApp Web: бесплатно, телефон работает как обычно. Чатов: ${w.chats}</div></div>
          ${ok && admin ? `<button class="btn danger" id="wa-off">Отключить</button>` : ""}</div>
          ${w.qr && admin ? `<ol class="muted small" style="margin:14px 0 0 18px"><li>WhatsApp на рабочем телефоне → Настройки → Связанные устройства</li><li>Привязка устройства → навести камеру на код</li></ol><img class="qr" src="${esc(w.qr)}" alt="QR" /><div class="muted small" style="text-align:center">Код обновляется сам</div>` : ""}
          ${!ok && w.status !== "offline" && !admin ? `<div class="muted small" style="margin-top:10px">QR-код видит администратор.</div>` : ""}
          ${w.status === "offline" ? `<div class="muted small" style="margin-top:10px">На компьютере с ботом запустите <b>restart_all.bat</b>. Нужен Node.js (README, раздел «Чаты»).</div>` : ""}
        </div>
        <div class="card"><h3>WhatsApp Business — официальный Cloud API ${wc.phone ? `<span class="muted">${esc(wc.phone)}</span>` : ""}</h3>
          ${state(wc.configured ? wc.ok : null, !wc.configured ? "Не настроен" : wc.ok ? `Подключён${wc.name ? " · " + esc(wc.name) : ""}${wc.quality ? " · качество " + esc(wc.quality) : ""}` : "Ошибка: " + esc(wc.error || ""))}
          <div class="muted small">Прямое подключение к Meta без телефона и QR: номер не заблокируют, а первые 1000 диалогов в месяц бесплатны (дальше по тарифу Meta за диалог). Чатов: ${wc.chats}.</div>
          ${wc.configured ? "" : `<details class="small" style="margin-top:8px"><summary>Как подключить</summary><ol class="muted" style="margin:6px 0 0 18px"><li>developers.facebook.com → создать приложение (Business) → добавить WhatsApp.</li><li>Привязать номер (не тот, что на QR-бридже — у номера может быть только один способ).</li><li>Скопировать <b>Phone number ID</b> и постоянный токен (System User → Generate token, права whatsapp_business_messaging).</li><li>В <code>.env</code>: ${esc("WA_CLOUD_TOKEN=…, WA_CLOUD_PHONE_ID=…, META_VERIFY_TOKEN=любое слово, META_APP_SECRET=App secret")}.</li><li>Webhooks → WhatsApp → Callback URL: <span class="mono">${esc(mw.url || "")}</span>, Verify token — как в .env, подписаться на <b>messages</b>.</li><li><code>restart_all.bat</code>.</li></ol><div class="muted" style="margin-top:6px">Ограничение Meta: писать гостю первым или спустя 24 часа после его последнего сообщения можно только утверждённым шаблоном.</div></details>`}
        </div>
        <div class="card"><h3>Instagram Direct ${ig.username ? `<span class="muted">@${esc(ig.username)}</span>` : ""}</h3>
          ${state(ig.configured ? ig.ok : null, !ig.configured ? "Не настроен" : ig.ok ? `Подключён${ig.page ? " · страница " + esc(ig.page) : ""}` : "Ошибка: " + esc(ig.error || ""))}
          <div class="muted small">Сообщения из Direct бизнес-аккаунта попадают в «Сообщения», ответы уходят обратно. Чатов: ${ig.chats}.</div>
          ${ig.configured ? "" : `<details class="small" style="margin-top:8px"><summary>Как подключить</summary><ol class="muted" style="margin:6px 0 0 18px"><li>Instagram должен быть профессиональным (Business) и привязан к Facebook-странице.</li><li>В том же приложении Meta добавить продукт <b>Messenger</b> → Instagram settings; права pages_messaging, instagram_basic, instagram_manage_messages (для чужих аккаунтов нужна проверка приложения — свой аккаунт работает в режиме разработки).</li><li>Сгенерировать <b>Page access token</b> страницы и вписать в <code>.env</code>: <span class="mono">IG_PAGE_TOKEN=…</span>.</li><li>Webhooks → Instagram → Callback URL: <span class="mono">${esc(mw.url || "")}</span>, verify token как в .env, подписаться на <b>messages</b>.</li><li>В настройках Instagram: Сообщения → Разрешить доступ к сообщениям (подключённые инструменты).</li></ol></details>`}
        </div>
        <div class="card"><h3>Telegram — аккаунт компании ${tg.me ? `<span class="muted">${esc(tg.me.phone ? "+" + tg.me.phone : tg.me.name || "")}</span>` : ""}</h3>
          ${state(tg.configured ? (tg.authorized && !tg.error ? true : false) : null, !tg.configured ? "Не настроен" : tg.authorized ? "Подключён" + (tg.error ? " · " + esc(tg.error) : "") : tg.error ? "Ошибка: " + esc(tg.error) : "Не выполнен вход")}
          <div class="muted small">Как в Wazzup: личные чаты аккаунта (например, рабочего номера) видны всей команде. Чатов: ${tg.chats}.</div>
          ${tg.configured ? tgForm : `<details class="small" style="margin-top:8px"><summary>Как подключить</summary><ol class="muted" style="margin:6px 0 0 18px"><li>Зайти на <b>my.telegram.org</b> с номера аккаунта → API development tools → создать приложение.</li><li>В <code>.env</code>: <span class="mono">TG_API_ID=…</span>, <span class="mono">TG_API_HASH=…</span>, затем <code>restart_all.bat</code>.</li><li>Здесь появится форма входа: номер → код из Telegram → (пароль, если включена двухэтапная защита).</li></ol></details>`}
          ${tg.authorized && admin ? `<div class="row-actions" style="justify-content:flex-start"><button class="btn danger" id="tg-off">Выйти из аккаунта</button></div>` : ""}
        </div>
        <div class="card"><h3>Telegram-бот для гостей ${gb.me && gb.me.username ? `<span class="muted">@${esc(gb.me.username)}</span>` : ""}</h3>
          ${state(gb.configured ? gb.ok : null, !gb.configured ? "Не настроен" : gb.ok ? "Работает" : "Ошибка: " + esc(gb.error || "запуск…"))}
          <div class="muted small">Отдельный бот, которому пишут гости (ссылку можно давать в объявлениях). Чатов: ${gb.chats}.${gb.configured ? "" : " Создать в @BotFather и вписать <span class=\"mono\">TG_GUEST_BOT_TOKEN</span> в .env (не тот же токен, что у рабочего бота)."}</div></div>
        <div class="card"><h3>Рабочий Telegram-бот</h3>${state(d.telegram_bot.configured, d.telegram_bot.configured ? "Настроен" : "BOT_TOKEN не задан")}
          <div class="muted small">Уведомления о новых сообщениях из всех каналов (${d.telegram_bot.notify_targets.length ? "чатов: " + d.telegram_bot.notify_targets.length : "выключены"}); ответ на уведомление уходит гостю.</div></div>
        <div class="card"><h3>Booking.com · Airbnb</h3><div class="muted small">Брони приходят через RealtyCalendar («Интеграции»). Сообщения гостей — в приложениях площадок.</div></div>`;
      const on = (id, fn) => { const el = $(id); if (el) el.addEventListener("click", fn); };
      on("wz-hook", async (e) => { e.target.disabled = true; try { const r = await post("/channels/wazzup/webhook"); toast("Вебхук подключён: " + r.url); render(); } catch (err) { toast(err.message); e.target.disabled = false; } });
      on("wa-off", async () => { if (!confirm("Отключить WhatsApp? Для возврата нужно будет снова сканировать QR.")) return; try { await post("/channels/whatsapp/logout"); toast("Отключено"); render(); } catch (err) { toast(err.message); } });
      on("tg-send", async (e) => { e.target.disabled = true; try { await post("/channels/telegram/send_code", { phone: val("tg-phone") }); tgStep = "code"; toast("Код отправлен в Telegram"); render(); } catch (err) { toast(err.message); e.target.disabled = false; } });
      on("tg-again", () => { tgStep = "phone"; render(); });
      on("tg-sign", async (e) => { e.target.disabled = true; try { const r = await post("/channels/telegram/sign_in", { code: val("tg-code") }); if (r.password_needed) { tgStep = "password"; toast("Нужен пароль двухэтапной защиты"); } else { tgStep = null; toast("Telegram подключён"); } render(); } catch (err) { toast(err.message); e.target.disabled = false; } });
      on("tg-sign-pass", async (e) => { e.target.disabled = true; try { await post("/channels/telegram/sign_in", { password: $("tg-pass").value }); tgStep = null; toast("Telegram подключён"); render(); } catch (err) { toast(err.message); e.target.disabled = false; } });
      on("tg-off", async () => { if (!confirm("Выйти из Telegram-аккаунта? Чаты останутся, новые сообщения приходить перестанут.")) return; await post("/channels/telegram/logout"); tgStep = null; render(); });
    };
    await render();
    chTimer = setInterval(() => { if (!document.activeElement || !/^tg-/.test(document.activeElement.id)) render(); }, 5000);
  };

  // ---- integrations -------------------------------------------------------------
  ROUTES.integrations = async () => {
    const d = await get("/integrations");
    const rc = d.realtycalendar;
    $("main").innerHTML = `<div class="page-head"><h1>Интеграции</h1></div>
      <div class="card"><div style="display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;align-items:center">
        <div><h3>RealtyCalendar</h3><div class="small ${rc.configured ? "ok" : "warn"}">${rc.configured ? "Подключён" : "RC_TOKEN не задан — демо-данные"}</div>
          <div class="muted small">Броней: ${rc.bookings} · объектов: ${rc.apartments} · каждые ${rc.interval_min} мин · последняя синхронизация: ${rc.last_sync ? esc(dtShort(rc.last_sync)) : "—"}</div></div>
        <button class="btn" id="i-sync">Синхронизировать сейчас</button></div>
        <div class="muted small" style="margin-top:10px">Запись в календарь: из карточки брони («Изменить бронь») уходят гость, телефон, сумма, даты, время, заметка. ${rc.push_log && rc.push_log.length ? `Последние записи:` : "Записей пока не было."}</div>
        ${rc.push_log && rc.push_log.length ? `<table class="table" style="margin-top:6px"><thead><tr><th>Когда</th><th>Бронь</th><th>Что</th><th>Результат</th></tr></thead><tbody>${rc.push_log.map((l) => `<tr><td class="muted small">${esc(dtShort(l.at))}</td><td>#${l.booking_id}</td><td class="small">${esc(l.changes)}</td><td class="${l.status === "ok" ? "ok" : "bad"} small">${l.status === "ok" ? "принято" : esc((l.response || "").slice(0, 160))}</td></tr>`).join("")}</tbody></table>` : ""}</div>
      <div class="card"><h3>Задачи из броней</h3><label style="display:flex;gap:8px;align-items:center;margin-top:6px"><input type="checkbox" id="i-auto" ${d.settings.auto_tasks ? "checked" : ""} ${S.me.role !== "admin" ? "disabled" : ""} /> Создавать задачи «Заезд» и «Выезд» на день заезда/выезда (исполнитель — администратор)</label></div>
      <div class="card"><h3>Telegram Mini App</h3><div class="small ${d.telegram.configured ? "ok" : "bad"}">${d.telegram.configured ? "Бот настроен" : "BOT_TOKEN не задан"}</div><div class="muted small">${esc(d.telegram.webapp_url || "")}</div></div>
      <div class="card"><h3>Healthchecks.io</h3><div class="small ${d.healthchecks.configured ? "ok" : "muted"}">${d.healthchecks.configured ? "Подключён" : "Не настроен — уведомления о выключенном компьютере не придут"}</div></div>`;
    $("i-sync").addEventListener("click", async (e) => { e.target.disabled = true; e.target.textContent = "Синхронизация…"; try { const r = await post("/integrations/sync"); toast(`Обновлено: ${r.bookings} броней`); navigate(); } catch (err) { toast(err.message); navigate(); } });
    $("i-auto").addEventListener("change", async (e) => { try { await post("/integrations/settings", { auto_tasks: e.target.checked }); toast("Сохранено"); } catch (err) { toast(err.message); } });
  };

  // ---- quick commands ------------------------------------------------------------
  ROUTES.commands = async () => {
    const list = await get("/commands");
    const q = (S.cache.cmdQ || "").toLowerCase();
    const shown = list.filter((t) => !q || (t.command || "").includes(q.replace(/^\//, "")) || (t.title || "").toLowerCase().includes(q) || (t.text || "").toLowerCase().includes(q));
    $("main").innerHTML = `<div class="page-head"><h1>Быстрые команды</h1></div>
      <div class="page-sub">В чате наберите / и начало команды — например /wifi — выберите стрелками и нажмите Enter. Готовый текст подставится в сообщение.</div>
      <div class="grid" style="grid-template-columns:1fr 240px;align-items:start">
        <div><div class="toolbar"><input class="field grow" id="cmd-q" placeholder="Найти команду: /wifi, пароль, адрес…" value="${esc(S.cache.cmdQ || "")}" /><button class="btn primary" id="cmd-add">Новая команда</button></div>
          <div class="list">${shown.map((t) => `<div class="list__row"><span class="mono">${t.command ? "/" + esc(t.command) : "—"}</span><div><b>${esc(t.title)}</b><small>${esc(t.text)}</small></div><div class="actions"><button data-edit="${t.id}">Изменить</button><button data-del="${t.id}" class="muted">Удалить</button></div></div>`).join("") || `<div class="empty">Команд нет</div>`}</div></div>
        <div class="card"><div class="card__title">Переменные</div><div class="muted small">Пишите их в тексте — при вставке заменятся на данные гостя из брони:</div>
          <div style="margin-top:10px"><span class="chip">{имя}</span><div class="muted small">имя гостя</div></div>
          <div style="margin-top:8px"><span class="chip">{объект}</span><div class="muted small">квартира из текущей или ближайшей брони</div></div>
          <div style="margin-top:8px"><span class="chip">{заезд}</span><div class="muted small">дата заезда, например «20 октября»</div></div>
          <div style="margin-top:8px"><span class="chip">{выезд}</span><div class="muted small">дата выезда</div></div>
          <div class="muted small" style="margin-top:10px">Если брони нет, переменная останется в тексте — допишите вручную перед отправкой.</div></div></div>`;
    keepFocus("cmd-q");
    let tm;
    $("cmd-q").addEventListener("input", (e) => { clearTimeout(tm); tm = setTimeout(() => { S.cache.cmdQ = e.target.value.trim(); navigate(); }, 250); });
    $("cmd-add").addEventListener("click", () => cmdForm(null));
    onMain(async (e) => {
      const ed = e.target.closest("[data-edit]"); if (ed) { cmdForm(list.find((t) => t.id === parseInt(ed.dataset.edit, 10))); return; }
      const dl = e.target.closest("[data-del]"); if (dl && confirm("Удалить команду?")) { await del(`/commands/${dl.dataset.del}`); navigate(); }
    });
  };
  function cmdForm(t) {
    t = t || { command: "", title: "", text: "" };
    modal(`<h2>${t.id ? "Команда" : "Новая команда"}</h2>
      <div class="form-row"><div><label class="lbl">Команда <span class="muted">(без /, латиница или кириллица)</span></label><input class="field" id="cm-cmd" value="${esc(t.command || "")}" placeholder="wifi" /></div>
        <div><label class="lbl">Название</label><input class="field" id="cm-title" value="${esc(t.title || "")}" placeholder="Пароль от Wi-Fi" /></div></div>
      <label class="lbl">Текст сообщения</label><textarea class="field" id="cm-text" rows="5">${esc(t.text || "")}</textarea>
      <div class="muted small" style="margin-top:6px">Переменные: {имя} {объект} {заезд} {выезд}</div>
      <div class="row-actions"><button class="btn" data-close>Отмена</button><button class="btn primary" id="cm-go">Сохранить</button></div>`);
    $("cm-go").addEventListener("click", async () => {
      const body = { command: val("cm-cmd"), title: val("cm-title"), text: $("cm-text").value.trim() };
      try { if (t.id) await put(`/commands/${t.id}`, body); else await post("/commands", body); closeModal(); navigate(); } catch (err) { toast(err.message); }
    });
  }

  // ---- card fields ---------------------------------------------------------------
  const TYPES = { text: "Текст", number: "Число", date: "Дата", select: "Список", checkbox: "Галочка" };
  ROUTES.fields = async () => {
    S.fields = await get("/fields");
    const group = (entity, title, hint) => `<div class="card__title" style="margin:${entity === "client" ? 0 : 18}px 0 8px">${title}</div><div class="list">${fieldsFor(entity).map((f, i) => `<div class="list__row"><span class="tag">${TYPES[f.type] || f.type}</span><div><b>${esc(f.name)}</b>${f.type === "select" ? `<small>${esc(f.options.join(", "))}</small>` : ""}</div>
        <div class="actions">${i > 0 ? `<button data-up="${f.id}">↑</button>` : ""}<button data-edit="${f.id}">Изменить</button><button data-del="${f.id}" class="muted">Удалить</button></div></div>`).join("") || `<div class="empty">${hint}</div>`}</div>`;
    $("main").innerHTML = `<div class="page-head"><h1>Поля карточек</h1><button class="btn primary" id="fl-add">Добавить поле</button></div>
      <div class="page-sub">Свои поля в карточке контакта и сделки — как в amoCRM. Стандартные (имя, телефон, источник, сумма, даты…) есть всегда.</div>
      ${group("client", "Контакт", "Полей контакта нет. Примеры: паспорт, язык, откуда узнал, предпочтения.")}
      ${group("deal", "Сделка", "Полей сделки нет. Примеры: цель поездки, количество детей, откуда запрос, депозит внесён.")}`;
    $("fl-add").addEventListener("click", () => fieldForm(null));
    onMain(async (e) => {
      const ed = e.target.closest("[data-edit]"); if (ed) { fieldForm(S.fields.find((f) => f.id === parseInt(ed.dataset.edit, 10))); return; }
      const dl = e.target.closest("[data-del]"); if (dl && confirm("Удалить поле? Значения в карточках пропадут.")) { await del(`/fields/${dl.dataset.del}`); navigate(); return; }
      const up = e.target.closest("[data-up]");
      if (up) { const f = S.fields.find((x) => x.id === parseInt(up.dataset.up, 10)); const same = fieldsFor(f.entity || "client").map((x) => x.id); const i = same.indexOf(f.id); [same[i - 1], same[i]] = [same[i], same[i - 1]]; const other = S.fields.filter((x) => (x.entity || "client") !== (f.entity || "client")).map((x) => x.id); await post("/fields/order", { ids: same.concat(other) }); navigate(); }
    });
  };
  function fieldForm(f) {
    f = f || { name: "", type: "text", options: [], entity: "client" };
    modal(`<h2>${f.id ? "Поле" : "Новое поле"}</h2>
      <div class="form-row"><div><label class="lbl">Где</label><select class="field" id="ff-entity"><option value="client" ${(f.entity || "client") === "client" ? "selected" : ""}>Контакт</option><option value="deal" ${f.entity === "deal" ? "selected" : ""}>Сделка</option></select></div>
        <div><label class="lbl">Тип</label><select class="field" id="ff-type">${Object.entries(TYPES).map(([k, v]) => `<option value="${k}" ${k === f.type ? "selected" : ""}>${v}</option>`).join("")}</select></div></div>
      <label class="lbl">Название</label><input class="field" id="ff-name" value="${esc(f.name)}" />
      <div id="ff-opts-row" class="${f.type === "select" ? "" : "hidden"}"><label class="lbl">Варианты (по одному в строке)</label><textarea class="field" id="ff-opts">${esc(f.options.join("\n"))}</textarea></div>
      <div class="row-actions"><button class="btn" data-close>Отмена</button><button class="btn primary" id="ff-go">Сохранить</button></div>`);
    $("ff-type").addEventListener("change", (e) => $("ff-opts-row").classList.toggle("hidden", e.target.value !== "select"));
    $("ff-go").addEventListener("click", async () => {
      const body = { name: val("ff-name"), type: val("ff-type"), options: $("ff-opts").value.split("\n"), entity: val("ff-entity") };
      try { if (f.id) await put(`/fields/${f.id}`, body); else await post("/fields", body); closeModal(); navigate(); } catch (err) { toast(err.message); }
    });
  }

  // ---- pipelines -------------------------------------------------------------------
  ROUTES.pipelines = async () => {
    S.pipelines = await get("/pipelines");
    $("main").innerHTML = `<div class="page-head"><h1>Воронки</h1><button class="btn primary" id="pp-add">Новая воронка</button></div>
      <div class="page-sub">Этапы, по которым движется сделка. Этап с типом «успех» или «отказ» закрывает сделку.</div>
      ${S.pipelines.map((p) => `<div class="card"><div style="display:flex;justify-content:space-between;align-items:center;gap:10px"><h3>${esc(p.name)}</h3><div><button class="btn sm" data-edit="${p.id}">Изменить</button> ${S.pipelines.length > 1 ? `<button class="btn sm danger" data-del="${p.id}">Удалить</button>` : ""}</div></div>
        <div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:10px">${p.stages.map((s) => `<span class="tag" style="background:${esc(s.color || "#eee")}22;color:${esc(s.color || "#555")}">${esc(s.name)}${s.kind !== "open" ? " · " + (s.kind === "won" ? "успех" : "отказ") : ""}${(s.actions || []).length ? ` ⚡${s.actions.length}` : ""}</span>`).join("")}</div>
        <div class="muted small" style="margin-top:8px">⚡ — автодействия при переходе сделки на этап: сообщение гостю, задача ответственному, уведомление в Telegram.</div></div>`).join("")}`;
    $("pp-add").addEventListener("click", () => pipelineForm(null));
    onMain(async (e) => {
      const ed = e.target.closest("[data-edit]"); if (ed) { pipelineForm(S.pipelines.find((p) => p.id === parseInt(ed.dataset.edit, 10))); return; }
      const dl = e.target.closest("[data-del]"); if (dl && confirm("Удалить воронку?")) { try { await del(`/pipelines/${dl.dataset.del}`); navigate(); } catch (err) { toast(err.message); } }
    });
  };
  function pipelineForm(p) {
    p = p || { name: "", stages: [{ name: "Новый запрос", color: "#5B8DEF", kind: "open" }, { name: "В работе", color: "#E08A2B", kind: "open" }, { name: "Успех", color: "#2EAD6B", kind: "won" }, { name: "Отказ", color: "#D9534F", kind: "lost" }] };
    const KINDS = { message: "Сообщение гостю", task: "Задача ответственному", notify: "Уведомление в Telegram" };
    const actRow = (a, i, j) => `<div class="act-row" data-j="${j}"><select class="field" data-a="kind">${Object.entries(KINDS).map(([k, v]) => `<option value="${k}" ${a.kind === k ? "selected" : ""}>${v}</option>`).join("")}</select>
      <input class="field" data-a="text" value="${esc(a.text || "")}" placeholder="${a.kind === "task" ? "Текст задачи, напр. «Отправить реквизиты»" : "Текст ({имя} {объект} {заезд} {выезд} {сумма})"}" />
      <span class="act-task ${a.kind === "task" ? "" : "hidden"}">через <input class="field" data-a="days" type="number" min="0" value="${a.days || 0}" style="width:60px" /> дн. в <input class="field" data-a="at_time" type="time" value="${esc(a.at_time || "10:00")}" style="width:110px" /></span>
      <button data-rma="${i}:${j}" title="Убрать">✕</button></div>`;
    const stageRow = (s, i) => `<div class="stage-row" data-i="${i}"><span class="muted">${i + 1}</span><input class="field" value="${esc(s.name)}" data-k="name" data-id="${s.id || ""}" /><select class="field" data-k="kind"><option value="open" ${s.kind === "open" ? "selected" : ""}>обычный</option><option value="won" ${s.kind === "won" ? "selected" : ""}>успех</option><option value="lost" ${s.kind === "lost" ? "selected" : ""}>отказ</option></select><input type="color" value="${esc(s.color || "#5B8DEF")}" data-k="color" /><button data-rm="${i}" title="Убрать">✕</button>
      <div class="stage-acts">${(s.actions || []).map((a, j) => actRow(a, i, j)).join("")}<button class="btn link sm" data-adda="${i}">⚡ + автодействие</button></div></div>`;
    modal(`<h2>${p.id ? "Воронка" : "Новая воронка"}</h2><label class="lbl">Название</label><input class="field" id="pp-name" value="${esc(p.name)}" />
      <label class="lbl">Этапы</label><div id="pp-stages">${p.stages.map(stageRow).join("")}</div><button class="btn sm" id="pp-more">+ Этап</button>
      <div class="row-actions"><button class="btn" data-close>Отмена</button><button class="btn primary" id="pp-go">Сохранить</button></div>`, true);
    const read = () => Array.from($("pp-stages").querySelectorAll(".stage-row")).map((r) => ({ id: r.querySelector("[data-k=name]").dataset.id ? parseInt(r.querySelector("[data-k=name]").dataset.id, 10) : null, name: r.querySelector("[data-k=name]").value, kind: r.querySelector("[data-k=kind]").value, color: r.querySelector("[data-k=color]").value,
      actions: Array.from(r.querySelectorAll(".act-row")).map((a) => ({ kind: a.querySelector("[data-a=kind]").value, text: a.querySelector("[data-a=text]").value, days: a.querySelector("[data-a=days]").value, at_time: a.querySelector("[data-a=at_time]").value })) }));
    const rerender = (st) => { $("pp-stages").innerHTML = st.map(stageRow).join(""); };
    $("pp-more").addEventListener("click", () => { const st = read(); st.push({ name: "", color: "#8E6CC6", kind: "open", actions: [] }); rerender(st); });
    $("pp-stages").addEventListener("click", (e) => {
      const rm = e.target.closest("[data-rm]"); if (rm) { const st = read(); st.splice(parseInt(rm.dataset.rm, 10), 1); rerender(st); return; }
      const ad = e.target.closest("[data-adda]"); if (ad) { const st = read(); st[parseInt(ad.dataset.adda, 10)].actions.push({ kind: "message", text: "", days: 0, at_time: "10:00" }); rerender(st); return; }
      const ra = e.target.closest("[data-rma]"); if (ra) { const [i, j] = ra.dataset.rma.split(":").map(Number); const st = read(); st[i].actions.splice(j, 1); rerender(st); }
    });
    $("pp-stages").addEventListener("change", (e) => { if (e.target.dataset.a === "kind") { const row = e.target.closest(".act-row"); row.querySelector(".act-task").classList.toggle("hidden", e.target.value !== "task"); row.querySelector("[data-a=text]").placeholder = e.target.value === "task" ? "Текст задачи, напр. «Отправить реквизиты»" : "Текст ({имя} {объект} {заезд} {выезд} {сумма})"; } });
    $("pp-go").addEventListener("click", async () => {
      const body = { name: val("pp-name"), stages: read() };
      try { if (p.id) await put(`/pipelines/${p.id}`, body); else await post("/pipelines", body); closeModal(); navigate(); } catch (err) { toast(err.message); }
    });
  }

  // ---- users ------------------------------------------------------------------------
  ROUTES.users = async () => {
    S.users = await get("/users");
    $("main").innerHTML = `<div class="page-head"><h1>Пользователи</h1><button class="btn primary" id="u-add">Добавить</button></div>
      <div class="table-wrap"><table class="table"><thead><tr><th>Имя</th><th>Email</th><th>Роль</th><th>Статус</th><th></th></tr></thead><tbody>
      ${S.users.map((u) => `<tr><td><b>${esc(u.name)}</b></td><td>${esc(u.email)}</td><td>${u.role === "admin" ? "Администратор" : "Менеджер"}</td><td class="${u.active ? "ok" : "bad"}">${u.active ? "Активен" : "Заблокирован"}</td>
        <td class="actions"><button data-edit="${u.id}">Изменить</button>${u.id !== S.me.id ? `<button data-block="${u.id}" data-to="${u.active ? 0 : 1}">${u.active ? "Заблокировать" : "Разблокировать"}</button>` : ""}</td></tr>`).join("")}</tbody></table></div>`;
    $("u-add").addEventListener("click", () => userForm(null));
    onMain(async (e) => {
      const ed = e.target.closest("[data-edit]"); if (ed) { userForm(S.users.find((u) => u.id === parseInt(ed.dataset.edit, 10))); return; }
      const bl = e.target.closest("[data-block]"); if (bl) { try { await patch(`/users/${bl.dataset.block}`, { active: bl.dataset.to === "1" }); navigate(); } catch (err) { toast(err.message); } }
    });
  };
  function userForm(u) {
    u = u || { name: "", email: "", role: "manager" };
    modal(`<h2>${u.id ? "Пользователь" : "Новый пользователь"}</h2>
      <div class="form-row"><div><label class="lbl">Имя</label><input class="field" id="uf-name" value="${esc(u.name)}" /></div><div><label class="lbl">Email</label><input class="field" id="uf-email" type="email" value="${esc(u.email)}" /></div></div>
      <div class="form-row"><div><label class="lbl">${u.id ? "Новый пароль (пусто — не менять)" : "Пароль"}</label><input class="field" id="uf-pass" type="password" autocomplete="new-password" /></div>
        <div><label class="lbl">Роль</label><select class="field" id="uf-role" ${u.id === S.me.id ? "disabled" : ""}><option value="manager" ${u.role === "manager" ? "selected" : ""}>Менеджер</option><option value="admin" ${u.role === "admin" ? "selected" : ""}>Администратор</option></select></div></div>
      <div class="muted small" style="margin-top:8px">Менеджер видит всё, кроме настроек (пользователи, воронки, поля). Администратор — всё.</div>
      <div class="row-actions"><button class="btn" data-close>Отмена</button><button class="btn primary" id="uf-go">Сохранить</button></div>`);
    $("uf-go").addEventListener("click", async () => {
      const body = { name: val("uf-name"), email: val("uf-email"), role: val("uf-role") };
      if ($("uf-pass").value) body.password = $("uf-pass").value;
      try { if (u.id) await patch(`/users/${u.id}`, body); else await post("/users", body); closeModal(); navigate(); } catch (err) { toast(err.message); }
    });
  }


  // ---- booking hub: contacts, linked chats, quick message -------------------------
  async function openBooking(id) {
    let b;
    try { b = await get(`/bookings/${id}/card`); } catch (err) { toast(err.message); return; }
    const h = b.history;
    const chatPill = (c) => `<span class="chat-pill"><i class="ch ${esc(c.channel)}"></i><a href="#messages/${c.id}" data-close>${esc(c.title)}</a><span class="muted">${esc(c.channel_name)}${c.pinned ? "" : " · по номеру"}</span>${c.pinned ? `<button data-unlink="${c.id}" title="Отвязать">✕</button>` : `<button data-pin="${c.id}" title="Закрепить за бронью">📌</button>`}</span>`;
    modal(`<h2>${esc(b.apartment)} · ${esc(dShort(b.checkin))} – ${esc(dShort(b.checkout))}</h2>
      <div style="margin-bottom:6px">${({ paid: '<span class="tag green">Оплачено</span>', prepaid: '<span class="tag orange">Предоплата</span>', unpaid: '<span class="tag red">Не оплачено</span>', unconfirmed: '<span class="tag">Не подтверждена</span>' })[b.pay] || ""} ${({ contacted: '<span class="tag green">✓ связь есть</span>', incoming: '<span class="tag orange">✉ гость писал, без ответа</span>', none: '<span class="tag red">○ связи не было</span>' })[b.contact] || ""}</div>
      <div class="muted small" style="margin-bottom:10px">${plural(b.nights || nights(b.checkin, b.checkout) || 0, "ночь", "ночи", "ночей")} · ${esc(b.source)} · ${money(b.amount)}${Number(b.debt) > 0 ? ` · <span class="bad">долг ${money(b.debt)}</span>` : ""}${b.arrival_time ? " · заезд " + esc(b.arrival_time) : ""}${b.departure_time ? " · выезд " + esc(b.departure_time) : ""}${b.notes ? `<div>${esc(b.notes)}</div>` : ""}</div>
      <div class="grid c2">
        <div>
          <div class="card__title">Гость</div>
          <div><b>${esc(b.guest || "Гость")}</b>${b.phone ? ` · <a href="tel:+${esc(b.phone)}">+${esc(b.phone)}</a>` : ""}${b.client ? ` · <a href="#clients/${b.client.id}" data-close>карточка</a>` : ""}</div>
          ${h ? `<div class="hist"><div><b>${h.visits}</b><span>визитов</span></div><div><b>${h.nights}</b><span>ночей</span></div><div><b>${money(h.amount)}</b><span>всего</span></div><div><b>${h.upcoming}</b><span>будущих</span></div></div><div class="muted small">Квартиры: ${esc(h.apartments.join(", "))}${h.first ? " · с " + esc(dShort(h.first)) : ""}</div>` : `<div class="muted small">Истории нет${b.phone ? "" : " — у брони нет телефона"}</div>`}
          <label class="lbl">Особенности гостя (видны в чате и в карточке)</label>
          <textarea class="field" id="bk-notes" rows="3" ${b.client ? "" : "disabled placeholder='Нет телефона — карточка не создана'"}>${esc(b.client ? b.client.notes || "" : "")}</textarea>
          <div style="margin-top:6px"><button class="btn sm" id="bk-notes-save" ${b.client ? "" : "disabled"}>Сохранить</button> <button class="btn sm" id="bk-edit">Изменить бронь</button> ${b.deal ? `<button class="btn sm" data-dealopen="${b.deal.id}">Сделка: ${esc(b.deal.title)}</button>` : `<button class="btn sm" id="bk-deal">+ Сделка</button>`}</div>
          <div class="muted small" style="margin-top:4px">«Изменить бронь» — гость, телефон, сумма, даты, время, заметка уходят в RealtyCalendar. Особенности гостя и свои поля хранятся только в CRM.</div>
        </div>
        <div>
          <div class="card__title">Чаты по этой брони</div>
          <div id="bk-chats">${b.chats.length ? b.chats.map(chatPill).join("") : `<div class="muted small">Чатов нет</div>`}</div>
          <div style="display:flex;gap:6px;margin-top:6px"><input class="field" id="bk-find" placeholder="Привязать чат: имя или номер" /><button class="btn sm" id="bk-find-go">Найти</button></div>
          <div id="bk-found"></div>
          ${b.phone ? `<button class="btn sm" id="bk-wa" style="margin-top:6px">Открыть WhatsApp по номеру</button>` : ""}
          <div class="card__title" style="margin-top:14px">Написать гостю</div>
          <select class="field" id="bk-to">${b.chats.map((c) => `<option value="${c.id}">${esc(c.channel_name)} · ${esc(c.title)}</option>`).join("") || `<option value="">— сначала привяжите чат —</option>`}</select>
          <textarea class="field" id="bk-msg" rows="3" placeholder="Текст (переменные {имя} {объект} {заезд} {выезд} подставятся)" style="margin-top:6px"></textarea>
          <div style="display:flex;gap:6px;margin-top:6px;align-items:center"><select class="field" id="bk-tpl" style="width:auto"><option value="">Шаблон…</option></select><span style="flex:1"></span><button class="btn primary sm" id="bk-send">Отправить</button></div>
          ${b.auto_log.length ? `<div class="card__title" style="margin-top:14px">Автосообщения</div>${b.auto_log.map((l) => `<div class="small"><span class="${l.status === "sent" ? "ok" : l.status === "failed" ? "bad" : "muted"}">${l.status === "sent" ? "✓" : l.status === "failed" ? "✕" : "–"}</span> ${esc(l.rule_name)} · ${esc(dtShort(l.at))}${l.error ? ` <span class="muted">${esc(l.error)}</span>` : ""}</div>`).join("")}` : ""}
        </div>
      </div>
      <div class="row-actions"><button class="btn primary" data-close>Закрыть</button></div>`, true);
    const fillVars = (t) => { const d = (s) => s ? `${new Date(s + "T00:00").getDate()} ${MONTHS_FULL[new Date(s + "T00:00").getMonth()]}` : ""; const nm = (b.guest || "").split(" ")[0]; return t.replace(/\{(имя|объект|заезд|выезд)\}/g, (m, k) => ({ "имя": nm, "объект": b.apartment || "", "заезд": d(b.checkin), "выезд": d(b.checkout) }[k] || m)); };
    get("/commands").then((list) => { $("bk-tpl").innerHTML += list.map((t) => `<option value="${t.id}">${esc(t.command ? "/" + t.command + " " : "")}${esc(t.title)}</option>`).join(""); $("bk-tpl").dataset.list = JSON.stringify(list); }).catch(() => {});
    $("bk-tpl").addEventListener("change", (e) => { const t = JSON.parse(e.target.dataset.list || "[]").find((x) => String(x.id) === e.target.value); if (t) $("bk-msg").value = fillVars(t.text); });
    $("bk-send").addEventListener("click", async (e) => {
      const to = $("bk-to").value, text = $("bk-msg").value.trim();
      if (!to) { toast("Привяжите чат"); return; } if (!text) { toast("Введите текст"); return; }
      e.target.disabled = true;
      try { await api(`/../inbox/chats/${to}/send`, { method: "POST", json: { text } }); toast("Отправлено"); $("bk-msg").value = ""; } catch (err) { toast(err.message); } finally { e.target.disabled = false; }
    });
    $("bk-notes-save").addEventListener("click", async () => { try { await put(`/clients/${b.client.id}`, { name: b.client.name, phone: b.client.phone, email: b.client.email, source: b.client.source, fields: b.client.fields, notes: $("bk-notes").value }); toast("Сохранено"); } catch (err) { toast(err.message); } });
    $("bk-edit").addEventListener("click", () => bookingForm(b));
    const bd = $("bk-deal"); if (bd) bd.addEventListener("click", async () => { S.pipelines = S.pipelines.length ? S.pipelines : await get("/pipelines"); try { const d = await post(`/bookings/${id}/deal`); openDeal(d.id); } catch (err) { toast(err.message); } });
    const wa = $("bk-wa"); if (wa) wa.addEventListener("click", async () => { if (!b.client) return; await openClientChat(b.client.id); });
    const find = async () => {
      const q = $("bk-find").value.trim(); if (!q) return;
      const list = await get(`/chats/search?q=${encodeURIComponent(q)}`);
      $("bk-found").innerHTML = list.length ? list.filter((c) => !b.chats.some((x) => x.id === c.id)).map((c) => `<div class="small" style="padding:4px 0"><i class="ch chat-pill" style="padding:2px 8px"><span class="ch ${esc(c.channel)}"></span>${esc(c.channel_name)}</i> ${esc(c.title)} <span class="muted">${esc((c.last_text || "").slice(0, 40))}</span> <button class="btn sm" data-pin="${c.id}">Закрепить</button></div>`).join("") || `<div class="muted small">Все найденные уже привязаны</div>` : `<div class="muted small">Не найдено</div>`;
    };
    $("bk-find-go").addEventListener("click", find);
    $("bk-find").addEventListener("keydown", (e) => { if (e.key === "Enter") find(); });
    $("modal-card").addEventListener("click", async (e) => {
      const pin = e.target.closest("[data-pin]"); if (pin) { try { await post(`/bookings/${id}/chats`, { chat_id: parseInt(pin.dataset.pin, 10) }); toast("Чат закреплён"); openBooking(id); } catch (err) { toast(err.message); } return; }
      const un = e.target.closest("[data-unlink]"); if (un) { await del(`/bookings/${id}/chats/${un.dataset.unlink}`); openBooking(id); return; }
      const dl = e.target.closest("[data-dealopen]"); if (dl) { S.pipelines = S.pipelines.length ? S.pipelines : await get("/pipelines"); openDeal(parseInt(dl.dataset.dealopen, 10)); }
    });
  }

  function bookingForm(b) {
    modal(`<h2>Изменить бронь · ${esc(b.apartment)}</h2>
      <p class="muted small">Изменения отправляются в RealtyCalendar и после подтверждения появляются везде: в CRM, дашборде и у горничных.</p>
      <div class="form-row"><div><label class="lbl">Гость</label><input class="field" id="be-guest" value="${esc(b.guest || "")}" /></div>
        <div><label class="lbl">Телефон</label><input class="field" id="be-phone" value="${b.phone ? "+" + esc(b.phone) : ""}" /></div></div>
      <div class="form-row c3"><div><label class="lbl">Заезд</label><input class="field" id="be-in" type="date" value="${esc(b.checkin)}" /></div>
        <div><label class="lbl">Выезд</label><input class="field" id="be-out" type="date" value="${esc(b.checkout)}" /></div>
        <div><label class="lbl">Сумма, $</label><input class="field" id="be-amt" type="number" step="any" value="${esc(b.amount ?? "")}" /></div></div>
      <div class="form-row"><div><label class="lbl">Статус оплаты (как в календаре)</label><select class="field" id="be-status">${[["booked", "Бронь, не оплачена"], ["prepaid", "Предоплата внесена"], ["paid", "Оплачено"], ["confirmed", "Подтверждена"], ["not_confirmed", "Не подтверждена"]].map(([k, v]) => `<option value="${k}" ${b.status === k ? "selected" : ""}>${v}</option>`).join("")}</select></div>
        <div><label class="lbl">Предоплата, $</label><input class="field" id="be-prep" type="number" step="any" value="${esc(b.prepayment ?? "")}" /></div></div>
      <div class="form-row"><div><label class="lbl">Время заезда</label><input class="field" id="be-at" value="${esc(b.arrival_time || "")}" placeholder="15:00" /></div>
        <div><label class="lbl">Время выезда</label><input class="field" id="be-dt" value="${esc(b.departure_time || "")}" placeholder="12:00" /></div></div>
      <label class="lbl">Заметка брони (видна в RealtyCalendar)</label><textarea class="field" id="be-notes" rows="3">${esc(b.notes || "")}</textarea>
      <div class="row-actions"><button class="btn" data-close>Отмена</button><button class="btn primary" id="be-go">Сохранить в календарь</button></div>`);
    $("be-go").addEventListener("click", async (e) => {
      const cur = { guest: b.guest || "", phone: b.phone || "", checkin: b.checkin, checkout: b.checkout, amount: b.amount == null ? "" : String(b.amount), arrival_time: b.arrival_time || "", departure_time: b.departure_time || "", notes: b.notes || "", status: b.status || "", prepayment: b.prepayment == null ? "" : String(b.prepayment) };
      const now = { guest: val("be-guest"), phone: val("be-phone").replace(/\D/g, ""), checkin: val("be-in"), checkout: val("be-out"), amount: val("be-amt"), arrival_time: val("be-at"), departure_time: val("be-dt"), notes: val("be-notes"), status: val("be-status"), prepayment: val("be-prep") };
      const changes = {};
      Object.keys(now).forEach((k) => { if (String(now[k]) !== String(cur[k])) changes[k] = now[k]; });
      if (!Object.keys(changes).length) { toast("Ничего не изменилось"); return; }
      e.target.disabled = true; e.target.textContent = "Отправляю в RealtyCalendar…";
      try { await patch(`/bookings/${b.id}`, changes); toast("Календарь обновлён"); closeModal(); if (S.route === "bookings") navigate(); openBooking(b.id); }
      catch (err) { toast(err.message); e.target.disabled = false; e.target.textContent = "Сохранить в календарь"; }
    });
  }

  // ---- revenue by channel (RealtyCalendar mirror) ----------------------------------
  ROUTES.revenue = async () => {
    const st = S.cache.rev || { month: todayIso().slice(0, 7) };
    S.cache.rev = st;
    const d = await get(`/revenue?month=${st.month}`);
    const t = d.total;
    const max = Math.max(1, ...d.by_source.map((x) => x.amount));
    const maxT = Math.max(1, ...d.trend.map((x) => x.amount));
    const [y, m] = st.month.split("-").map(Number);
    const title = `${MONTHS_FULL[m - 1].replace(/я$/, "ь").replace(/а$/, "")} ${y}`.replace("мая", "май");
    $("main").innerHTML = `<div class="page-head"><h1>Выручка</h1><span class="muted small">по данным RealtyCalendar · обновлено ${esc(dtShort(d.last_sync))}</span></div>
      <div class="toolbar"><button class="btn" data-m="-1">←</button><input class="field" type="month" id="rev-m" value="${st.month}" style="width:auto" /><button class="btn" data-m="1">→</button><span class="muted small">Брони по дате заезда · ${title}</span></div>
      <div class="grid c4" style="margin-bottom:16px">
        <div class="stat"><b>${money(t.amount)}</b><span>выручка за месяц${t.debt ? ` · долг ${money(t.debt)}` : ""}</span></div>
        <div class="stat"><b>${t.bookings}</b><span>броней · ${t.nights} ночей</span></div>
        <div class="stat"><b>${t.occupancy}%</b><span>загрузка (${d.apartments} объектов)</span></div>
        <div class="stat"><b>${money(t.adr)}</b><span>средняя цена ночи</span></div></div>
      <div class="grid c2">
        <div class="card"><div class="card__title">По каналам</div>${d.by_source.map((s) => `<div class="bar-row"><span>${esc(s.source)} <small class="muted">${s.bookings} · ${s.nights} н.</small></span><div class="bar" style="width:${Math.round(100 * s.amount / max)}%"></div><span class="amt">${money(s.amount)}</span></div>`).join("") || `<div class="muted">Нет броней в этом месяце</div>`}</div>
        <div class="card"><div class="card__title">По объектам</div><div style="max-height:320px;overflow:auto">${d.by_apartment.map((a) => `<div class="bar-row"><span>${esc(a.apartment)} <small class="muted">${a.bookings} · ${a.nights} н.</small></span><div class="bar" style="width:${Math.round(100 * a.amount / Math.max(1, d.by_apartment[0].amount))}%;background:#60A5FA"></div><span class="amt">${money(a.amount)}</span></div>`).join("") || `<div class="muted">—</div>`}</div></div>
      </div>
      <div class="card" style="margin-top:16px"><div class="card__title">12 месяцев</div><div class="trend">${d.trend.map((x) => `<div class="trend__col ${x.month === st.month ? "cur" : ""}" title="${esc(Object.entries(x.by_source).map(([k, v]) => k + ": " + money(v)).join("\n"))}"><b>${Math.round(x.amount / 1000) >= 1 ? Math.round(x.amount / 100) / 10 + "k" : Math.round(x.amount)}</b><div class="trend__bar" style="height:${Math.max(3, Math.round(130 * x.amount / maxT))}px"></div>${MONTHS[parseInt(x.month.slice(5), 10) - 1]}</div>`).join("")}</div></div>`;
    onMain((e) => { const b = e.target.closest("[data-m]"); if (b) { const dd = new Date(y, m - 1 + parseInt(b.dataset.m, 10), 1); st.month = `${dd.getFullYear()}-${pad(dd.getMonth() + 1)}`; navigate(); } });
    $("rev-m").addEventListener("change", (e) => { if (e.target.value) { st.month = e.target.value; navigate(); } });
  };

  // ---- auto-message rules ------------------------------------------------------------
  const CHANNEL_NAMES = { auto: "любой доступный", wa: "WhatsApp (QR)", wac: "WhatsApp Cloud API", tg: "Telegram-аккаунт", tgbot: "Telegram-бот", ig: "Instagram" };
  ROUTES.auto = async () => {
    const d = await get("/auto");
    const admin = S.me.role === "admin";
    const when = (r) => r.trigger === "before_checkin" ? `за ${r.offset_days} дн. до заезда в ${r.at_time}` : r.trigger === "after_checkout" ? `через ${r.offset_days} дн. после выезда в ${r.at_time}` : r.trigger === "checkin_day" || r.trigger === "checkout_day" ? `${r.trigger_name.toLowerCase()} в ${r.at_time}` : r.trigger === "off_hours" ? `вне ${r.hours_from || "09:00"}–${r.hours_to || "22:00"}` : r.trigger_name.toLowerCase();
    $("main").innerHTML = `<div class="page-head"><h1>Автосообщения</h1><div style="display:flex;gap:8px"><button class="btn" id="au-preview">Что отправится сейчас</button>${admin ? `<button class="btn primary" id="au-add">Новое правило</button>` : ""}</div></div>
      <div class="page-sub">Правила проверяются каждые 5 минут. Каждое срабатывает один раз на бронь (или на чат). Канал «любой доступный»: закреплённый за бронью чат → чат по номеру телефона → новый WhatsApp-чат, если разрешено.</div>
      <div class="list">${d.rules.map((r) => `<div class="rule"><div class="switch ${r.enabled ? "on" : ""}" data-toggle="${r.id}" data-on="${r.enabled ? 0 : 1}" title="${admin ? "Включить/выключить" : ""}"></div>
        <div><b>${esc(r.name)} <span class="tag ${r.enabled ? "green" : ""}">${r.enabled ? "включено" : "выключено"}</span></b><small>${esc(when(r))} · канал: ${CHANNEL_NAMES[r.channel] || r.channel}${r.sources ? " · только " + esc(r.sources) : ""}${r.only_if_chat ? "" : " · может создать WhatsApp-чат"} · отправлено: ${r.sent}</small><div class="txt">${esc(r.text)}</div></div>
        <div class="actions">${admin ? `<button data-edit="${r.id}">Изменить</button><button data-del="${r.id}" class="muted">Удалить</button>` : ""}</div></div>`).join("") || `<div class="empty">Правил нет</div>`}</div>
      <div class="card" style="margin-top:16px"><div class="card__title">Журнал · последние 100</div>
        ${d.log.length ? `<div class="table-wrap"><table class="table"><thead><tr><th>Когда</th><th>Правило</th><th>Кому</th><th>Статус</th><th>Текст</th></tr></thead><tbody>${d.log.map((l) => `<tr><td class="muted small">${esc(dtShort(l.at))}</td><td>${esc(l.rule_name)}</td><td>${l.chat_id ? `<a href="#messages/${l.chat_id}">${esc(l.chat_title || "чат")}</a>` : "—"}${l.booking_id ? ` <a href="#" data-bopen="${l.booking_id}" class="muted small">бронь</a>` : ""}</td><td class="${l.status === "sent" ? "ok" : l.status === "failed" ? "bad" : "muted"}">${l.status === "sent" ? "отправлено" : l.status === "failed" ? "ошибка" : "пропущено"}${l.error ? `<div class="muted small">${esc(l.error)}</div>` : ""}</td><td class="muted small" style="max-width:320px">${esc((l.text || "").slice(0, 120))}</td></tr>`).join("")}</tbody></table></div>` : `<div class="muted">Пока ничего не отправлялось</div>`}</div>`;
    onMain(async (e) => {
      const tg = e.target.closest("[data-toggle]"); if (tg && admin) { await patch(`/auto/${tg.dataset.toggle}`, { enabled: tg.dataset.on === "1" }); navigate(); return; }
      const ed = e.target.closest("[data-edit]"); if (ed) { ruleForm(d.rules.find((r) => r.id === parseInt(ed.dataset.edit, 10)), d.triggers); return; }
      const dl = e.target.closest("[data-del]"); if (dl && confirm("Удалить правило? Журнал останется.")) { await del(`/auto/${dl.dataset.del}`); navigate(); return; }
      const bo = e.target.closest("[data-bopen]"); if (bo) { e.preventDefault(); openBooking(parseInt(bo.dataset.bopen, 10)); return; }
      if (e.target.id === "au-add") { ruleForm(null, d.triggers); return; }
      if (e.target.id === "au-preview") {
        e.target.disabled = true;
        try {
          const list = await post("/auto/preview");
          modal(`<h2>Что отправится сейчас</h2><p class="muted small">Проверка по включённым правилам и броням ±15 дней. Ничего не отправлено.</p>
            ${list.length ? list.map((x) => `<div style="padding:8px 0;border-bottom:1px solid var(--sep)"><b>${esc(x.rule)}</b> → ${esc(x.guest || "гость")} · ${esc(x.apartment || "")} ${x.chat ? `· <span class="ok">${esc(x.channel)}: ${esc(x.chat)}</span>` : `· <span class="warn">${esc(x.note || "нет чата")}</span>`}<div class="small muted" style="white-space:pre-wrap">${esc(x.text)}</div></div>`).join("") : `<div class="muted">Сейчас отправлять нечего — все сроки прошли или уже отправлено.</div>`}
            <div class="row-actions">${admin && list.length ? `<button class="btn" id="au-run">Отправить сейчас</button>` : ""}<button class="btn primary" data-close>Закрыть</button></div>`, true);
          const run = $("au-run"); if (run) run.addEventListener("click", async () => { run.disabled = true; const r = await post("/auto/run"); toast(`Отправлено: ${r.filter((x) => x.status === "sent").length}, пропущено: ${r.filter((x) => x.status !== "sent").length}`); closeModal(); navigate(); });
        } catch (err) { toast(err.message); } finally { e.target.disabled = false; }
      }
    });
  };
  function ruleForm(r, triggers) {
    r = r || { name: "", enabled: false, trigger: "before_checkin", offset_days: 1, at_time: "10:00", hours_from: "09:00", hours_to: "22:00", channel: "auto", sources: "", only_if_chat: true, text: "" };
    modal(`<h2>${r.id ? "Правило" : "Новое правило"}</h2>
      <label class="lbl">Название</label><input class="field" id="r-name" value="${esc(r.name)}" />
      <div class="form-row"><div><label class="lbl">Событие</label><select class="field" id="r-trig">${Object.entries(triggers).map(([k, v]) => `<option value="${k}" ${k === r.trigger ? "selected" : ""}>${esc(v)}</option>`).join("")}</select></div>
        <div><label class="lbl">Канал</label><select class="field" id="r-ch">${Object.entries(CHANNEL_NAMES).map(([k, v]) => `<option value="${k}" ${k === r.channel ? "selected" : ""}>${v}</option>`).join("")}</select></div></div>
      <div class="form-row c3" id="r-timing"><div><label class="lbl">Дней (до/после)</label><input class="field" id="r-off" type="number" min="0" value="${r.offset_days}" /></div><div><label class="lbl">Время отправки</label><input class="field" id="r-at" type="time" value="${esc(r.at_time || "10:00")}" /></div><div><label class="lbl">Только источники <span class="muted">(через запятую)</span></label><input class="field" id="r-src" value="${esc(r.sources || "")}" placeholder="Booking.com, Airbnb, Прямое" /></div></div>
      <div class="form-row" id="r-hours"><div><label class="lbl">Рабочее время с</label><input class="field" id="r-from" type="time" value="${esc(r.hours_from || "09:00")}" /></div><div><label class="lbl">до</label><input class="field" id="r-to" type="time" value="${esc(r.hours_to || "22:00")}" /></div></div>
      <label class="lbl">Текст <span class="muted">{имя} {объект} {заезд} {выезд} {ночей} {сумма} {долг}</span></label><textarea class="field" id="r-text" rows="5">${esc(r.text)}</textarea>
      <label style="display:flex;gap:8px;align-items:center;margin-top:10px"><input type="checkbox" id="r-only" ${r.only_if_chat ? "checked" : ""} /> Писать только в существующий чат (не создавать новый WhatsApp-чат по номеру)</label>
      <label style="display:flex;gap:8px;align-items:center;margin-top:6px"><input type="checkbox" id="r-on" ${r.enabled ? "checked" : ""} /> Включено</label>
      <div class="row-actions"><button class="btn" data-close>Отмена</button><button class="btn primary" id="r-go">Сохранить</button></div>`, true);
    const upd = () => { const t = $("r-trig").value; $("r-timing").classList.toggle("hidden", t === "first_message" || t === "off_hours"); $("r-hours").classList.toggle("hidden", t !== "off_hours"); $("r-off").disabled = !(t === "before_checkin" || t === "after_checkout"); };
    $("r-trig").addEventListener("change", upd); upd();
    $("r-go").addEventListener("click", async () => {
      const body = { name: val("r-name"), trigger: val("r-trig"), channel: val("r-ch"), offset_days: val("r-off"), at_time: val("r-at"), sources: val("r-src"), hours_from: val("r-from"), hours_to: val("r-to"), text: $("r-text").value.trim(), only_if_chat: $("r-only").checked, enabled: $("r-on").checked };
      try { if (r.id) await put(`/auto/${r.id}`, body); else await post("/auto", body); closeModal(); navigate(); } catch (err) { toast(err.message); }
    });
  }

  // ---- notifications: sound + popup + browser notification on new incoming -----------
  let lastSeenMsg = null;
  function chime() {
    try {
      if (!$("notif-sound").checked) return;
      const ctx = chime.ctx || (chime.ctx = new (window.AudioContext || window.webkitAudioContext)());
      [[880, 0], [1174, 0.12]].forEach(([f, t]) => { const o = ctx.createOscillator(); const g = ctx.createGain(); o.frequency.value = f; g.gain.setValueAtTime(0.0001, ctx.currentTime + t); g.gain.exponentialRampToValueAtTime(0.2, ctx.currentTime + t + 0.02); g.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + t + 0.4); o.connect(g).connect(ctx.destination); o.start(ctx.currentTime + t); o.stop(ctx.currentTime + t + 0.45); });
    } catch (e) { /* no audio */ }
  }
  function showPopup(m) {
    const el = $("popup");
    $("popup-title").textContent = `${m.channel} · ${m.title}`;
    $("popup-text").textContent = m.text || "";
    $("popup-sub").textContent = "Нажмите, чтобы открыть чат";
    el.classList.remove("hidden");
    el.onclick = () => { el.classList.add("hidden"); location.hash = `#messages/${m.chat_id}`; };
    clearTimeout(showPopup.t);
    showPopup.t = setTimeout(() => el.classList.add("hidden"), 8000);
    if ("Notification" in window && Notification.permission === "granted" && document.visibilityState !== "visible") {
      try { const n = new Notification(`${m.channel} · ${m.title}`, { body: m.text || "", tag: "nh-" + m.chat_id }); n.onclick = () => { window.focus(); location.hash = `#messages/${m.chat_id}`; n.close(); }; } catch (e) { /* ignore */ }
    }
  }
  $("notif-allow").addEventListener("click", (e) => {
    e.preventDefault();
    if (!("Notification" in window)) { toast("Браузер не поддерживает уведомления"); return; }
    Notification.requestPermission().then((p) => toast(p === "granted" ? "Всплывающие уведомления включены" : "Уведомления запрещены в браузере"));
  });
  try { $("notif-sound").checked = localStorage.getItem("nh_sound") !== "0"; } catch (e) { /* ignore */ }
  $("notif-sound").addEventListener("change", (e) => { try { localStorage.setItem("nh_sound", e.target.checked ? "1" : "0"); } catch (err) { /* ignore */ } });
  // ---- boot --------------------------------------------------------------------------
  async function badges() {
    try {
      const d = await get("/badges");
      $("nav-unread").textContent = d.unread || "";
      $("nav-unread").classList.toggle("hidden", !d.unread);
      $("nav-tasks").textContent = d.my_open_tasks || "";
      $("nav-tasks").classList.toggle("hidden", !d.my_open_tasks);
      $("tab-unread").textContent = d.unread || ""; $("tab-unread").classList.toggle("hidden", !d.unread);
      $("tab-tasks").textContent = d.my_open_tasks || ""; $("tab-tasks").classList.toggle("hidden", !d.my_open_tasks);
      if (d.latest) {
        if (lastSeenMsg !== null && d.latest.id > lastSeenMsg) { chime(); showPopup(d.latest); }
        lastSeenMsg = Math.max(lastSeenMsg || 0, d.latest.id);
      } else if (lastSeenMsg === null) lastSeenMsg = 0;
    } catch (e) { /* ignore */ }
    clearTimeout(boot.t);
    boot.t = setTimeout(badges, document.visibilityState === "visible" ? 5000 : 15000);
  }
  async function boot() {
    $("auth").classList.add("hidden");
    $("app").classList.remove("hidden");
    $("tabbar").classList.toggle("hidden", window.innerWidth > 760);
    window.addEventListener("resize", () => $("tabbar").classList.toggle("hidden", window.innerWidth > 760));
    document.body.classList.toggle("is-admin", S.me.role === "admin");
    $("me-name").textContent = S.me.name;
    $("me-mail").textContent = S.me.email;
    S.users = await get("/users").catch(() => []);
    navigate();
    badges();
  }
  (async () => {
    try {
      const s = await get("/session");
      if (s.setup_needed) return showAuth(true);
      if (!s.user) return showAuth(false);
      S.me = s.user;
      await boot();
    } catch (e) {
      $("auth").classList.remove("hidden");
      $("auth-err").textContent = "Сервер недоступен: " + e.message;
      $("auth-err").classList.remove("hidden");
    }
  })();
})();
