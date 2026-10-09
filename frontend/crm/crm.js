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
    const res = await fetch(API + path, opts);
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

  // ---- auth ---------------------------------------------------------------
  let setupMode = false;
  function showAuth(setup) {
    setupMode = !!setup;
    if (inboxFrame) { inboxFrame.remove(); inboxFrame = null; } // no chats behind the login form
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
    document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("is-on", a.dataset.r === r));
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
      inboxFrame.src = "/inbox/?v=35&embed=1" + (chatId ? "#chat=" + chatId : "");
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
    const p = S.pipelines.find((x) => x.id === d.pipeline_id) || S.pipelines[0];
    modal(`<h2>${esc(d.title)}</h2>
      <div class="muted small" style="margin-bottom:12px">${esc(d.owner_name || "")} · создана ${esc(dtShort(d.created_at))}</div>
      <dl class="kv">
        <dt>Этап</dt><dd><select class="field" id="d-stage" style="width:auto">${p.stages.map((s) => `<option value="${s.id}" ${s.id === d.stage_id ? "selected" : ""}>${esc(s.name)}</option>`).join("")}</select></dd>
        <dt>Клиент</dt><dd>${d.client_id ? `<a href="#clients/${d.client_id}" data-close>${esc(d.client_name)}</a>${d.client_phone ? " · +" + esc(d.client_phone) : ""}` : "—"}</dd>
        <dt>Объект</dt><dd>${esc(d.apartment || "—")}</dd>
        <dt>Даты</dt><dd>${d.checkin ? esc(dShort(d.checkin)) + " – " + esc(dShort(d.checkout)) + (nights(d.checkin, d.checkout) ? " · " + plural(nights(d.checkin, d.checkout), "ночь", "ночи", "ночей") : "") : "—"}${d.guests ? " · гостей " + d.guests : ""}</dd>
        <dt>Сумма</dt><dd>${money(d.amount) || "—"}</dd>
        ${d.booking_id ? `<dt>Бронь</dt><dd>RealtyCalendar #${d.booking_id}</dd>` : ""}
        ${d.notes ? `<dt>Заметки</dt><dd style="white-space:pre-wrap">${esc(d.notes)}</dd>` : ""}
      </dl>
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
  }

  async function dealForm(d, defaults) {
    d = d || Object.assign({ title: "", amount: "", apartment: "", checkin: "", checkout: "", guests: "", notes: "", client_id: null }, defaults || {});
    await loadClients();
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
      <label class="lbl">Заметки</label><textarea class="field" id="f-notes">${esc(d.notes || "")}</textarea>
      <div class="row-actions"><button class="btn" data-close>Отмена</button><button class="btn primary" id="f-go">Сохранить</button></div>`);
    $("f-go").addEventListener("click", async () => {
      const body = { title: val("f-title"), client_id: val("f-client") ? parseInt(val("f-client"), 10) : null, pipeline_id: p.id,
        stage_id: parseInt(val("f-stage"), 10), apartment: val("f-apt"), checkin: val("f-in"), checkout: val("f-out"),
        amount: val("f-amt"), guests: val("f-guests"), owner_uid: parseInt(val("f-owner"), 10), notes: val("f-notes") };
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
    const list = await get(`/clients?q=${encodeURIComponent(q)}`);
    S.cache.clients = list;
    $("main").innerHTML = `
      <div class="page-head"><h1>Клиенты</h1><div style="display:flex;gap:8px"><button class="btn" id="c-import" title="Создать карточки для всех, кто есть в бронях и чатах">Подтянуть из броней и чатов</button><button class="btn primary" id="c-add">Добавить</button></div></div>
      <div class="toolbar"><input class="field grow" id="c-q" placeholder="Поиск: имя, телефон, email" value="${esc(q)}" /><span class="muted small">${plural(list.length, "клиент", "клиента", "клиентов")}</span></div>
      ${list.length ? `<div class="table-wrap"><table class="table"><thead><tr><th>Имя</th><th>Телефон</th><th>Источник</th><th>Email</th><th>Обновлён</th></tr></thead><tbody>
        ${list.map((c) => `<tr class="click" data-id="${c.id}"><td><b>${esc(c.name)}</b></td><td>${c.phone ? "+" + esc(c.phone) : "—"}</td><td>${esc(c.source || "")}</td><td>${esc(c.email || "")}</td><td class="muted small">${esc(dtShort(c.updated_at))}</td></tr>`).join("")}
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
    if (!S.fields.length) S.fields = await get("/fields");
    const fieldRows = S.fields.map((f) => `<dt>${esc(f.name)}</dt><dd>${f.type === "checkbox" ? (c.fields[f.id] ? "да" : "нет") : esc(c.fields[f.id] || "—")}</dd>`).join("");
    const html = `<h2>${esc(c.name)}</h2>
      <div class="muted small" style="margin-bottom:12px">${c.phone ? `<a href="tel:+${esc(c.phone)}">+${esc(c.phone)}</a>` : ""}${c.email ? " · " + esc(c.email) : ""}${c.source ? " · " + esc(c.source) : ""}</div>
      <div class="grid c2">
        <div><dl class="kv">${fieldRows}${c.notes ? `<dt>Заметки</dt><dd style="white-space:pre-wrap">${esc(c.notes)}</dd>` : ""}</dl>
          ${!fieldRows && !c.notes ? `<div class="muted small">Дополнительных полей нет — настраиваются в «Поля карточек»</div>` : ""}</div>
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
    if (!S.fields.length) S.fields = await get("/fields");
    const fieldInputs = S.fields.map((f) => {
      const v = c.fields[f.id];
      let inp;
      if (f.type === "select") inp = `<select class="field" data-field="${f.id}"><option value="">—</option>${f.options.map((o) => `<option ${o === v ? "selected" : ""}>${esc(o)}</option>`).join("")}</select>`;
      else if (f.type === "checkbox") inp = `<label><input type="checkbox" data-field="${f.id}" ${v ? "checked" : ""} /> да</label>`;
      else inp = `<input class="field" data-field="${f.id}" type="${f.type === "number" ? "number" : f.type === "date" ? "date" : "text"}" value="${esc(v || "")}" />`;
      return `<div><label class="lbl">${esc(f.name)}</label>${inp}</div>`;
    }).join("");
    modal(`<h2>${c.id ? "Клиент" : "Новый клиент"}</h2>
      <div class="form-row"><div><label class="lbl">Имя</label><input class="field" id="cf-name" value="${esc(c.name)}" /></div>
        <div><label class="lbl">Телефон</label><input class="field" id="cf-phone" value="${c.phone ? "+" + esc(c.phone) : ""}" placeholder="+998 90 123 45 67" /></div></div>
      <div class="form-row"><div><label class="lbl">Email</label><input class="field" id="cf-email" value="${esc(c.email || "")}" /></div>
        <div><label class="lbl">Источник</label><input class="field" id="cf-source" value="${esc(c.source || "")}" placeholder="Booking.com, Airbnb, WhatsApp…" list="src-list" /><datalist id="src-list"><option>Booking.com</option><option>Airbnb</option><option>WhatsApp</option><option>Telegram</option><option>Instagram</option><option>Рекомендация</option></datalist></div></div>
      ${fieldInputs ? `<div class="form-row">${fieldInputs}</div>` : ""}
      <label class="lbl">Заметки</label><textarea class="field" id="cf-notes">${esc(c.notes || "")}</textarea>
      <div class="row-actions"><button class="btn" data-close>Отмена</button><button class="btn primary" id="cf-go">Сохранить</button></div>`);
    $("cf-go").addEventListener("click", async () => {
      const fields = {};
      $("modal-card").querySelectorAll("[data-field]").forEach((el) => { fields[el.dataset.field] = el.type === "checkbox" ? el.checked : el.value; });
      const body = { name: val("cf-name"), phone: val("cf-phone"), email: val("cf-email"), source: val("cf-source"), notes: val("cf-notes"), fields };
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
    const bkCard = (b) => `<div class="bk"><div class="bk__top"><span class="bk__apt">${esc(b.apartment)}</span><span class="bk__amt">${money(b.amount)}</span></div>
      <div class="bk__sub">${esc(dShort(b.checkin))} – ${esc(dShort(b.checkout))} · ${plural(b.nights || nights(b.checkin, b.checkout) || 0, "ночь", "ночи", "ночей")}${b.arrival_time || b.departure_time ? ` · ${esc(b.arrival_time || "")}${b.arrival_time && b.departure_time ? "/" : ""}${esc(b.departure_time || "")}` : ""}</div>
      <div class="bk__guest">${b.client_id ? `<a href="#clients/${b.client_id}">${esc(b.guest || "Гость")}</a>` : `<b>${esc(b.guest || "Гость")}</b>`}${b.phone ? ` · +${esc(b.phone)}` : ""} <span class="muted small">· ${esc(b.source)}</span>${Number(b.debt) > 0 ? ` <span class="tag red">долг ${money(b.debt)}</span>` : ""}</div>
      ${b.notes ? `<div class="bk__notes">${esc(b.notes)}</div>` : ""}
      <div class="bk__links"><a href="#" data-bdeal="${b.id}">${b.deal_id ? "Сделка" : "+ Сделка"}</a>${b.phone ? `<a href="#" data-bchat="${b.phone}">WhatsApp</a>` : ""}</div></div>`;
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
            bar = `<span class="bar ${src(b)}" style="left:${left};width:${w}" data-bk="${b.id}" title="${esc(b.guest || "")} · ${esc(dShort(b.checkin))}–${esc(dShort(b.checkout))} · ${money(b.amount)}">${esc(b.guest || "Гость")}</span>`;
          }
          return `<td class="${d.getDay() % 6 === 0 ? "we" : ""} ${di === today ? "today" : ""}">${bar}</td>`;
        }).join("");
        return `<tr><td class="apt">${esc(a)}</td>${cells}</tr>`;
      }).join("");
      $("main").innerHTML = head + `<div class="chess"><table><thead><tr><th class="apt">Объект</th>${days.map((d) => `<th class="${d.getDay() % 6 === 0 ? "we" : ""} ${iso(d) === today ? "today" : ""}">${d.getDate()}<br><small>${WD[d.getDay()]}</small></th>`).join("")}</tr></thead><tbody>${rows}</tbody></table></div>
        <div class="muted small" style="margin-top:8px">Полоска начинается в день заезда и заканчивается в день выезда. <span class="tag" style="background:#3B82F6;color:#fff">Booking</span> <span class="tag" style="background:#F472B6;color:#fff">Airbnb</span> <span class="tag" style="background:#34D399;color:#fff">Прямая</span></div>`;
      $("main").querySelector(".chess").addEventListener("click", (e) => {
        const bar = e.target.closest("[data-bk]");
        if (!bar) return;
        const b = g.bookings.find((x) => x.id === parseInt(bar.dataset.bk, 10));
        modal(`<h2>${esc(b.apartment)}</h2>${bkCard(b)}<div class="row-actions"><button class="btn primary" data-close>Закрыть</button></div>`);
      });
    }
    onMain(async (e) => {
      const v = e.target.closest("[data-view]"); if (v) { st.view = v.dataset.view; navigate(); return; }
      const s = e.target.closest("[data-step]");
      if (s) { const k = st.view === "day" ? "date" : "start"; const d = new Date(st[k] + "T00:00"); d.setDate(d.getDate() + parseInt(s.dataset.step, 10) * (st.view === "day" ? 1 : 7)); st[k] = iso(d); navigate(); return; }
      if (e.target.id === "bk-today") { st.date = st.start = todayIso(); navigate(); return; }
      if (e.target.id === "bk-sync") { e.target.disabled = true; try { const r = await post("/integrations/sync"); toast(`Обновлено: ${r.bookings} броней`); navigate(); } catch (err) { toast(err.message); e.target.disabled = false; } return; }
      const bd = e.target.closest("[data-bdeal]"); if (bd) { e.preventDefault(); try { S.pipelines = S.pipelines.length ? S.pipelines : await get("/pipelines"); const d = await post(`/bookings/${bd.dataset.bdeal}/deal`); openDeal(d.id); } catch (err) { toast(err.message); } return; }
      const bc = e.target.closest("[data-bchat]"); if (bc) { e.preventDefault(); try { const c = await post("/clients", { phone: bc.dataset.bchat }).catch(async (err) => { const m = /#(\d+)/.exec(err.message); if (m) return { id: parseInt(m[1], 10) }; throw err; }); openClientChat(c.id); } catch (err) { toast(err.message); } }
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
      const w = d.whatsapp, wc = d.whatsapp_cloud, ig = d.instagram, tg = d.telegram, gb = d.telegram_guest_bot, mw = d.meta_webhook;
      const ok = w.status === "connected";
      const num = w.me ? "+" + (w.me.id || "").split("@")[0].split(":")[0] : "";
      const state = (good, text) => `<div class="small ${good === true ? "ok" : good === false ? "bad" : "muted"}">${text}</div>`;
      const cfgRow = (name, hint) => `<div class="muted small">${name}: <span class="mono">${esc(hint)}</span></div>`;
      const tgForm = !tg.configured ? "" : tg.authorized ? "" : !admin ? `<div class="muted small">Вход выполняет администратор.</div>` :
        tgStep === "code" || tg.pending_phone && tgStep !== "password" ? `<div class="form-row" style="margin-top:10px;align-items:end"><div><label class="lbl">Код из Telegram (${esc(tg.pending_phone || "")})</label><input class="field" id="tg-code" inputmode="numeric" placeholder="12345" /></div><div><button class="btn primary" id="tg-sign">Войти</button> <button class="btn link" id="tg-again">другой номер</button></div></div>`
        : tgStep === "password" ? `<div class="form-row" style="margin-top:10px;align-items:end"><div><label class="lbl">Пароль двухэтапной защиты</label><input class="field" id="tg-pass" type="password" /></div><div><button class="btn primary" id="tg-sign-pass">Войти</button></div></div>`
        : `<div class="form-row" style="margin-top:10px;align-items:end"><div><label class="lbl">Номер телефона аккаунта</label><input class="field" id="tg-phone" type="tel" placeholder="+998 90 123 45 67" /></div><div><button class="btn primary" id="tg-send">Получить код</button></div></div>`;
      $("main").innerHTML = `<div class="page-head"><h1>Каналы</h1></div><div class="page-sub">Откуда приходят сообщения в «Сообщения». Все каналы попадают в один список чатов, карточка клиента создаётся сама.</div>
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
        <button class="btn" id="i-sync">Синхронизировать сейчас</button></div></div>
      <div class="card"><h3>Задачи из броней</h3><label style="display:flex;gap:8px;align-items:center;margin-top:6px"><input type="checkbox" id="i-auto" ${d.settings.auto_tasks ? "checked" : ""} ${S.me.role !== "admin" ? "disabled" : ""} /> Создавать задачи «Заезд» и «Выезд» на день заезда/выезда (исполнитель — администратор)</label></div>
      <div class="card"><h3>Google-таблица (касса)</h3><div class="small ${d.sheet.configured ? "ok" : "muted"}">${d.sheet.configured ? "Подключена" : "Не настроена (SHEET_API_URL в .env)"}</div></div>
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
    $("main").innerHTML = `<div class="page-head"><h1>Поля карточек</h1><button class="btn primary" id="fl-add">Добавить поле</button></div>
      <div class="page-sub">Дополнительные поля в карточке клиента: паспорт, язык, откуда узнал, предпочтения…</div>
      <div class="list">${S.fields.map((f, i) => `<div class="list__row"><span class="tag">${TYPES[f.type] || f.type}</span><div><b>${esc(f.name)}</b>${f.type === "select" ? `<small>${esc(f.options.join(", "))}</small>` : ""}</div>
        <div class="actions">${i > 0 ? `<button data-up="${f.id}">↑</button>` : ""}<button data-edit="${f.id}">Изменить</button><button data-del="${f.id}" class="muted">Удалить</button></div></div>`).join("") || `<div class="empty">Полей пока нет. Стандартные поля (имя, телефон, email, источник, заметки) есть всегда.</div>`}</div>`;
    $("fl-add").addEventListener("click", () => fieldForm(null));
    onMain(async (e) => {
      const ed = e.target.closest("[data-edit]"); if (ed) { fieldForm(S.fields.find((f) => f.id === parseInt(ed.dataset.edit, 10))); return; }
      const dl = e.target.closest("[data-del]"); if (dl && confirm("Удалить поле? Значения в карточках пропадут.")) { await del(`/fields/${dl.dataset.del}`); navigate(); return; }
      const up = e.target.closest("[data-up]");
      if (up) { const ids = S.fields.map((f) => f.id); const i = ids.indexOf(parseInt(up.dataset.up, 10)); [ids[i - 1], ids[i]] = [ids[i], ids[i - 1]]; await post("/fields/order", { ids }); navigate(); }
    });
  };
  function fieldForm(f) {
    f = f || { name: "", type: "text", options: [] };
    modal(`<h2>${f.id ? "Поле" : "Новое поле"}</h2>
      <div class="form-row"><div><label class="lbl">Название</label><input class="field" id="ff-name" value="${esc(f.name)}" /></div>
        <div><label class="lbl">Тип</label><select class="field" id="ff-type">${Object.entries(TYPES).map(([k, v]) => `<option value="${k}" ${k === f.type ? "selected" : ""}>${v}</option>`).join("")}</select></div></div>
      <div id="ff-opts-row" class="${f.type === "select" ? "" : "hidden"}"><label class="lbl">Варианты (по одному в строке)</label><textarea class="field" id="ff-opts">${esc(f.options.join("\n"))}</textarea></div>
      <div class="row-actions"><button class="btn" data-close>Отмена</button><button class="btn primary" id="ff-go">Сохранить</button></div>`);
    $("ff-type").addEventListener("change", (e) => $("ff-opts-row").classList.toggle("hidden", e.target.value !== "select"));
    $("ff-go").addEventListener("click", async () => {
      const body = { name: val("ff-name"), type: val("ff-type"), options: $("ff-opts").value.split("\n") };
      try { if (f.id) await put(`/fields/${f.id}`, body); else await post("/fields", body); closeModal(); navigate(); } catch (err) { toast(err.message); }
    });
  }

  // ---- pipelines -------------------------------------------------------------------
  ROUTES.pipelines = async () => {
    S.pipelines = await get("/pipelines");
    $("main").innerHTML = `<div class="page-head"><h1>Воронки</h1><button class="btn primary" id="pp-add">Новая воронка</button></div>
      <div class="page-sub">Этапы, по которым движется сделка. Этап с типом «успех» или «отказ» закрывает сделку.</div>
      ${S.pipelines.map((p) => `<div class="card"><div style="display:flex;justify-content:space-between;align-items:center;gap:10px"><h3>${esc(p.name)}</h3><div><button class="btn sm" data-edit="${p.id}">Изменить</button> ${S.pipelines.length > 1 ? `<button class="btn sm danger" data-del="${p.id}">Удалить</button>` : ""}</div></div>
        <div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:10px">${p.stages.map((s) => `<span class="tag" style="background:${esc(s.color || "#eee")}22;color:${esc(s.color || "#555")}">${esc(s.name)}${s.kind !== "open" ? " · " + (s.kind === "won" ? "успех" : "отказ") : ""}</span>`).join("")}</div></div>`).join("")}`;
    $("pp-add").addEventListener("click", () => pipelineForm(null));
    onMain(async (e) => {
      const ed = e.target.closest("[data-edit]"); if (ed) { pipelineForm(S.pipelines.find((p) => p.id === parseInt(ed.dataset.edit, 10))); return; }
      const dl = e.target.closest("[data-del]"); if (dl && confirm("Удалить воронку?")) { try { await del(`/pipelines/${dl.dataset.del}`); navigate(); } catch (err) { toast(err.message); } }
    });
  };
  function pipelineForm(p) {
    p = p || { name: "", stages: [{ name: "Новый запрос", color: "#5B8DEF", kind: "open" }, { name: "В работе", color: "#E08A2B", kind: "open" }, { name: "Успех", color: "#2EAD6B", kind: "won" }, { name: "Отказ", color: "#D9534F", kind: "lost" }] };
    const stageRow = (s, i) => `<div class="stage-row" data-i="${i}"><span class="muted">${i + 1}</span><input class="field" value="${esc(s.name)}" data-k="name" data-id="${s.id || ""}" /><select class="field" data-k="kind"><option value="open" ${s.kind === "open" ? "selected" : ""}>обычный</option><option value="won" ${s.kind === "won" ? "selected" : ""}>успех</option><option value="lost" ${s.kind === "lost" ? "selected" : ""}>отказ</option></select><input type="color" value="${esc(s.color || "#5B8DEF")}" data-k="color" /><button data-rm="${i}" title="Убрать">✕</button></div>`;
    modal(`<h2>${p.id ? "Воронка" : "Новая воронка"}</h2><label class="lbl">Название</label><input class="field" id="pp-name" value="${esc(p.name)}" />
      <label class="lbl">Этапы</label><div id="pp-stages">${p.stages.map(stageRow).join("")}</div><button class="btn sm" id="pp-more">+ Этап</button>
      <div class="row-actions"><button class="btn" data-close>Отмена</button><button class="btn primary" id="pp-go">Сохранить</button></div>`, true);
    const read = () => Array.from($("pp-stages").querySelectorAll(".stage-row")).map((r) => ({ id: r.querySelector("[data-k=name]").dataset.id ? parseInt(r.querySelector("[data-k=name]").dataset.id, 10) : null, name: r.querySelector("[data-k=name]").value, kind: r.querySelector("[data-k=kind]").value, color: r.querySelector("[data-k=color]").value }));
    $("pp-more").addEventListener("click", () => { const st = read(); st.push({ name: "", color: "#8E6CC6", kind: "open" }); $("pp-stages").innerHTML = st.map(stageRow).join(""); });
    $("pp-stages").addEventListener("click", (e) => { const rm = e.target.closest("[data-rm]"); if (rm) { const st = read(); st.splice(parseInt(rm.dataset.rm, 10), 1); $("pp-stages").innerHTML = st.map(stageRow).join(""); } });
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

  // ---- boot --------------------------------------------------------------------------
  async function badges() {
    try {
      const d = await get("/badges");
      $("nav-unread").textContent = d.unread || "";
      $("nav-unread").classList.toggle("hidden", !d.unread);
      $("nav-tasks").textContent = d.my_open_tasks || "";
      $("nav-tasks").classList.toggle("hidden", !d.my_open_tasks);
    } catch (e) { /* ignore */ }
  }
  async function boot() {
    $("auth").classList.add("hidden");
    $("app").classList.remove("hidden");
    document.body.classList.toggle("is-admin", S.me.role === "admin");
    $("me-name").textContent = S.me.name;
    $("me-mail").textContent = S.me.email;
    S.users = await get("/users").catch(() => []);
    navigate();
    badges();
    clearInterval(boot.t);
    boot.t = setInterval(badges, 20000);
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
