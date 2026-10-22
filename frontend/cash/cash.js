/* «Оплаты» — cash receipts page. Opens from the bot (/оплата, personal link),
   from Telegram (WebApp signature) and inside the CRM (iframe, cookie). */
(function () {
  "use strict";
  const API = (window.NH_API_BASE || "") + "/api/cash";
  const tg = window.Telegram && window.Telegram.WebApp;
  const $ = (id) => document.getElementById(id);
  const esc = NH.common.esc;
  const EMBED = /[?&]embed=1/.test(location.search);

  // ---- access: personal link key (?k=), Telegram signature, owner key -------
  function stored(name) { try { return localStorage.getItem(name) || ""; } catch (e) { return ""; } }
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
    if (changed) { const q = params.toString(); history.replaceState(null, "", location.pathname + (q ? "?" + q : "") + location.hash); }
  })();
  const initData = () => { try { return (tg && tg.initData) || ""; } catch (e) { return ""; } };
  const headers = () => ({
    "X-Telegram-Init-Data": initData(),
    "X-Inbox-Key": stored("nh_ikey") || window.__nh_ikey || "",
    "X-Owner-Key": stored("nh_okey") || window.__nh_okey || "",
  });
  async function req(path, opts) {
    opts = Object.assign({}, opts || {});
    opts.headers = Object.assign({}, headers(), opts.headers || {});
    try { return await NH.common.request(API + path, opts); } catch (e) { if (e.status === 403 && !S.meta) showDenied(e.message); throw e; }
  }
  const get = (p) => req(p);
  const post = (p, json) => req(p, { method: "POST", json: json || {} });
  const patch = (p, json) => req(p, { method: "PATCH", json: json || {} });
  function showDenied(msg) { if (msg) $("denied-text").textContent = msg; $("denied").classList.remove("hidden"); }

  // ---- state -----------------------------------------------------------------
  const S = { meta: null, tab: "new", form: null, editing: null, today: [], month: NH.common.todayIso().slice(0, 7), stats: null };
  const haptic = (kind) => { try { if (tg && tg.HapticFeedback) (kind === "ok" ? tg.HapticFeedback.notificationOccurred("success") : tg.HapticFeedback.impactOccurred(kind || "light")); } catch (e) { /* ignore */ } };
  let toastT = null;
  function toast(msg) { const t = $("toast"); t.textContent = msg; t.classList.remove("hidden"); clearTimeout(toastT); toastT = setTimeout(() => t.classList.add("hidden"), 2800); }

  // ---- helpers ------------------------------------------------------------------
  const SIGN = { USD: "$", UZS: "сум", EUR: "€", RUB: "₽" };
  const fmtN = (n, cur) => { n = Number(n || 0); const s = cur === "USD" || cur === "EUR" ? (Math.round(n * 100) / 100).toLocaleString("ru-RU", { maximumFractionDigits: 2 }) : Math.round(n).toLocaleString("ru-RU"); return s.replace(/ /g, " "); };
  const money = (n, cur) => `${fmtN(n, cur)} ${SIGN[cur] || cur}`;
  const aptNum = (name) => parseInt(String(name || "").replace(/\D/g, ""), 10) || name;
  const methodCur = (m) => { const x = (S.meta.methods || []).find((y) => y.name === m); return x ? x.currency : "USD"; };
  const toUsd = (amount, cur) => cur === "USD" ? amount : (S.meta.rates[cur] ? amount / S.meta.rates[cur] : 0);
  const parseAmt = (s) => { const v = parseFloat(String(s || "").replace(/\s| /g, "").replace(",", ".")); return isFinite(v) && v > 0 ? v : 0; };
  const groupDigits = (s) => { const [i, f] = String(s).replace(/\s| /g, "").replace(",", ".").split("."); const g = (i || "").replace(/\D/g, "").replace(/\B(?=(\d{3})+(?!\d))/g, " "); return f !== undefined ? g + "." + f.replace(/\D/g, "").slice(0, 2) : g; };
  const shortMethod = (m) => m.replace("Карта ", "").replace(" Biz", "").replace("Нал ", "Нал ");
  const timeOf = (iso) => (iso || "").slice(11, 16);
  const dateRu = (iso) => { const d = new Date(iso); return `${d.getDate()} ${NH.common.MONTHS_GEN[d.getMonth()]}`; };
  const who = (p) => p.who_name || "—";
  const sheetBadge = (p) => p.status === "cancelled" ? `<span class="badge cancelled">отменено</span>` : p.status === "review" ? `<span class="badge review">на проверке</span>` : p.sheet === "ok" ? `<span class="badge ok">в таблице</span>` : p.sheet === "error" ? `<span class="badge error">не записано</span>` : `<span class="badge pending">ждёт таблицу</span>`;

  // ---- tabs ------------------------------------------------------------------------
  function setTab(name) {
    S.tab = name;
    document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("is-on", b.dataset.tab === name));
    try { localStorage.setItem("nh_cash_tab", name); } catch (e) { /* ignore */ }
    ({ new: renderNew, today: renderToday, stats: renderStats }[name] || renderNew)();
  }
  $("tabs").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) { haptic("light"); setTab(b.dataset.tab); } });

  // ---- «Записать» ---------------------------------------------------------------
  function blankForm() {
    return { apartments: [], receiver: "", lines: [{ amount: "", method: S.meta.quick[0] || (S.meta.methods[0] || {}).name || "" }], stay: "", note: "", day: NH.common.todayIso(), showNote: false, showDay: false };
  }
  function renderNew() {
    const m = S.meta;
    if (!S.form) S.form = blankForm();
    const f = S.form;
    const main = $("main");
    main.innerHTML = `<div class="view" id="v-new">
      ${S.editing ? `<div class="card" style="background:var(--orange-soft);border-color:#FDE4C4"><b>Исправление оплаты №${S.editing}</b> <span class="muted small">— изменения попадут в те же строки таблицы и в журнал</span> <button class="link" id="edit-off" style="float:right">отмена</button></div>` : ""}
      <section class="card">
        <div class="card__h"><h3>Квартира</h3><span class="muted" id="apt-hint">${f.apartments.length > 1 ? "несколько — платёж совместный" : "можно выбрать несколько"}</span></div>
        <div class="apts" id="apts">${m.apartments.map((a) => `<button class="apt ${f.apartments.includes(a.name) ? "is-on" : ""}" data-apt="${esc(a.name)}"><small>${esc(a.name[0])}</small>${a.label}</button>`).join("")}</div>
        <div id="receiver"></div>
      </section>
      <section class="card">
        <div class="card__h"><h3>Сумма</h3><span class="muted">${esc(m.article)} · ${m.source === "sheet" ? "курс из таблицы" : "курс по умолчанию"}</span></div>
        <div class="lines" id="lines"></div>
        <button class="add-line" id="add-line">+ ещё одна сумма (другая валюта)</button>
      </section>
      <section class="card">
        <div class="card__h"><h3>Проживание</h3><span class="muted">как в таблице</span></div>
        <input class="field" id="stay" inputmode="numeric" placeholder="19.10-23.10" value="${esc(f.stay)}" autocomplete="off" />
        <div class="hint" id="stay-hint"></div>
        <div class="row" style="margin-top:6px;flex-wrap:wrap">
          <button class="link" id="note-btn">${f.showNote || f.note ? "" : "+ комментарий"}</button>
          <button class="link" id="day-btn">${f.showDay || f.day !== NH.common.todayIso() ? "" : "дата: сегодня · изменить"}</button>
        </div>
        <input class="field ${f.showNote || f.note ? "" : "hidden"}" id="note" placeholder="Комментарий (необязательно)" value="${esc(f.note)}" maxlength="300" style="margin-top:6px" />
        <input class="field ${f.showDay || f.day !== NH.common.todayIso() ? "" : "hidden"}" id="day" type="date" value="${esc(f.day)}" max="${NH.common.todayIso()}" style="margin-top:6px" />
      </section>
      <button class="go" id="go"><span id="go-text">${S.editing ? "Сохранить" : "Записать"}</span></button>
    </div>`;
    renderLines();
    renderReceiver();
    updateGo();
    $("apts").addEventListener("click", (e) => {
      const b = e.target.closest("[data-apt]");
      if (!b) return;
      const a = b.dataset.apt;
      const i = f.apartments.indexOf(a);
      if (i >= 0) f.apartments.splice(i, 1); else f.apartments.push(a);
      b.classList.toggle("is-on", i < 0);
      haptic("light");
      if (!f.apartments.includes(f.receiver)) f.receiver = f.apartments[0] || "";
      $("apt-hint").textContent = f.apartments.length > 1 ? "несколько — платёж совместный" : "можно выбрать несколько";
      renderReceiver(); updateGo();
    });
    $("add-line").addEventListener("click", () => { f.lines.push({ amount: "", method: S.meta.quick[1] || S.meta.quick[0] }); renderLines(); haptic("light"); const inputs = document.querySelectorAll("#lines input"); inputs[inputs.length - 1].focus(); });
    $("stay").addEventListener("input", (e) => { f.stay = e.target.value; stayHint(); updateGo(); });
    stayHint();
    $("note-btn").addEventListener("click", () => { f.showNote = true; $("note-btn").textContent = ""; $("note").classList.remove("hidden"); $("note").focus(); });
    $("note").addEventListener("input", (e) => { f.note = e.target.value; });
    $("day-btn").addEventListener("click", () => { f.showDay = true; $("day-btn").textContent = ""; $("day").classList.remove("hidden"); $("day").focus(); });
    $("day").addEventListener("change", (e) => { f.day = e.target.value || NH.common.todayIso(); });
    $("go").addEventListener("click", submit);
    if (S.editing) $("edit-off").addEventListener("click", () => { S.editing = null; S.form = null; renderNew(); });
  }
  function stayHint() {
    const v = ($("stay").value || "").trim();
    const h = $("stay-hint");
    if (!v) { h.textContent = "можно оставить пустым"; $("stay").classList.remove("bad"); return; }
    const ok = /^\d{1,2}([./]\d{1,2})?\s*[-–—]\s*\d{1,2}[./]\d{1,2}([./]\d{2,4})?$/.test(v);
    h.textContent = ok ? "в комментарий таблицы попадёт как «19-23.10»" : "формат: 19.10-23.10 или 19-23.10";
    $("stay").classList.toggle("bad", !ok);
  }
  function renderReceiver() {
    const f = S.form;
    const box = $("receiver");
    if (f.apartments.length < 2) { box.innerHTML = ""; return; }
    box.innerHTML = `<div class="receiver"><div class="small"><b>Деньги лежат в кассе квартиры:</b> <span class="muted">в таблицу попадёт только она, остальные — на проверку владельцу</span></div>
      <div class="chips">${f.apartments.map((a) => `<button class="chip ${a === f.receiver ? "is-on" : ""}" data-rc="${esc(a)}">${aptNum(a)}</button>`).join("")}</div></div>`;
    box.querySelectorAll("[data-rc]").forEach((b) => b.addEventListener("click", () => { f.receiver = b.dataset.rc; box.querySelectorAll(".chip").forEach((c) => c.classList.toggle("is-on", c.dataset.rc === f.receiver)); haptic("light"); }));
  }
  function renderLines() {
    const f = S.form, m = S.meta;
    const box = $("lines");
    box.innerHTML = f.lines.map((ln, i) => {
      const cur = methodCur(ln.method);
      const quick = m.quick.slice(0, 3);
      const other = !quick.includes(ln.method);
      return `<div class="line" data-i="${i}">
        <div class="line__amt"><input inputmode="decimal" placeholder="0" value="${esc(ln.amount ? groupDigits(ln.amount) : "")}" data-amt="${i}" autocomplete="off" /><span class="line__cur" data-cur="${i}">${esc(SIGN[cur] || cur)}</span>${f.lines.length > 1 ? `<button class="line__del" data-del="${i}" title="Убрать">×</button>` : ""}</div>
        <div class="methods">${quick.map((q) => `<button class="${ln.method === q ? "is-on" : ""}" data-m="${esc(q)}" data-i="${i}">${esc(shortMethod(q))}</button>`).join("")}<button class="more ${other ? "is-on" : ""}" data-more="${i}">${other ? esc(shortMethod(ln.method)) : "ещё"}</button></div>
        <div class="line__usd" data-usd="${i}"></div>
      </div>`;
    }).join("");
    f.lines.forEach((_, i) => lineUsd(i));
    box.querySelectorAll("[data-amt]").forEach((inp) => {
      inp.addEventListener("input", (e) => {
        const i = +e.target.dataset.amt;
        const caretEnd = e.target.selectionEnd === e.target.value.length;
        const v = groupDigits(e.target.value);
        f.lines[i].amount = v.replace(/\s/g, "");
        if (e.target.value !== v) { e.target.value = v; if (caretEnd) e.target.setSelectionRange(v.length, v.length); }
        lineUsd(i); updateGo();
      });
      inp.addEventListener("focus", () => { setTimeout(() => { try { inp.scrollIntoView({ block: "center", behavior: "smooth" }); } catch (e) { /* ignore */ } }, 250); });
    });
    box.querySelectorAll("[data-m]").forEach((b) => b.addEventListener("click", () => { const i = +b.dataset.i; f.lines[i].method = b.dataset.m; haptic("light"); renderLines(); updateGo(); }));
    box.querySelectorAll("[data-more]").forEach((b) => b.addEventListener("click", () => pickMethod(+b.dataset.more)));
    box.querySelectorAll("[data-del]").forEach((b) => b.addEventListener("click", () => { const i = +b.dataset.del; const el = b.closest(".line"); el.classList.add("out"); setTimeout(() => { f.lines.splice(i, 1); renderLines(); updateGo(); }, 170); }));
  }
  function lineUsd(i) {
    const ln = S.form.lines[i];
    const el = document.querySelector(`[data-usd="${i}"]`);
    if (!el) return;
    const cur = methodCur(ln.method);
    const a = parseAmt(ln.amount);
    el.textContent = !a ? "" : cur === "USD" ? `${esc(ln.method)}` : `${esc(ln.method)} · ≈ ${fmtN(toUsd(a, cur), "USD")} $ по курсу ${fmtN(S.meta.rates[cur], cur)}`;
  }
  function pickMethod(i) {
    const f = S.form;
    openSheet(`<h3>Куда поступили деньги</h3><div class="opts">${S.meta.methods.map((m) => `<button class="${f.lines[i].method === m.name ? "is-on" : ""}" data-pick="${esc(m.name)}">${esc(m.name)}<span>${esc(m.currency)}</span></button>`).join("")}</div>`,
      (card) => card.querySelectorAll("[data-pick]").forEach((b) => b.addEventListener("click", () => { f.lines[i].method = b.dataset.pick; closeSheet(); renderLines(); updateGo(); haptic("light"); })));
  }
  function totalUsd() { return S.form.lines.reduce((s, ln) => s + toUsd(parseAmt(ln.amount), methodCur(ln.method)), 0); }
  function formOk() {
    const f = S.form;
    if (!f.apartments.length) return "выберите квартиру";
    if (!f.lines.some((ln) => parseAmt(ln.amount))) return "введите сумму";
    if (f.lines.some((ln) => parseAmt(ln.amount) && !ln.method)) return "выберите счёт";
    const v = (f.stay || "").trim();
    if (v && !/^\d{1,2}([./]\d{1,2})?\s*[-–—]\s*\d{1,2}[./]\d{1,2}([./]\d{2,4})?$/.test(v)) return "даты: 19.10-23.10";
    return "";
  }
  function updateGo() {
    const go = $("go"), t = $("go-text");
    if (!go) return;
    const why = formOk();
    go.disabled = !!why;
    const total = totalUsd();
    t.innerHTML = why ? `${S.editing ? "Сохранить" : "Записать"} <small>· ${esc(why)}</small>` : `${S.editing ? "Сохранить" : "Записать"} <small>· ${S.form.apartments.map(aptNum).join(", ")} · ${fmtN(total, "USD")} $</small>`;
  }
  async function submit() {
    const f = S.form;
    if (formOk()) return;
    const go = $("go");
    go.classList.add("busy"); go.innerHTML = `<span class="spin"></span><span>${S.editing ? "Сохраняю…" : "Записываю…"}</span>`;
    const body = { apartments: f.apartments, receiver: f.receiver || f.apartments[0], lines: f.lines.filter((ln) => parseAmt(ln.amount)).map((ln) => ({ amount: parseAmt(ln.amount), method: ln.method })), stay: (f.stay || "").trim(), note: (f.note || "").trim(), day: f.day };
    try {
      const p = S.editing ? await patch(`/payments/${S.editing}`, body) : await post("/payments", body);
      haptic("ok");
      S.form = null; S.editing = null;
      showDone(p);
      refreshToday();
    } catch (e) {
      haptic("heavy");
      toast(e.message);
      go.classList.remove("busy"); go.innerHTML = `<span id="go-text"></span>`; updateGo();
    }
  }
  function showDone(p) {
    const d = $("done");
    const rows = p.lines.filter((l) => l.sheet_row && !l.void).map((l) => l.sheet_row);
    const sheetLine = p.status === "review" ? `Совместный платёж за кв. ${p.apartments.map(aptNum).join(", ")}: в таблицу записана касса кв. ${aptNum(p.receiver)}, владелец увидит его в списке на проверку.` :
      p.sheet === "ok" ? `В таблице «Операции»: строк${rows.length > 1 ? "и" : "а"} ${rows.join(", ")}.` : `Таблица пока не ответила — запишу автоматически через несколько минут, ничего делать не нужно.`;
    d.innerHTML = `<svg class="done__mark" viewBox="0 0 100 100"><circle cx="50" cy="50" r="46"/><path d="M30 52 l14 14 l28 -30"/></svg>
      <h2>${p.status === "cancelled" ? "Отменено" : "Записано"}</h2>
      <p><b>кв. ${esc(p.apartments.map(aptNum).join(", "))}</b> · ${p.lines.filter((l) => !l.void).map((l) => esc(money(l.amount, l.currency)) + " <span class='muted'>" + esc(l.method) + "</span>").join(" + ")}${p.stay_label ? " · " + esc(p.stay_label) : ""}</p>
      <p>${sheetLine}</p>
      <div class="btns"><button class="btn" id="done-fix">Исправить</button><button class="btn primary" id="done-more">Ещё одна</button></div>`;
    d.classList.remove("hidden");
    $("done-more").addEventListener("click", () => { d.classList.add("hidden"); S.form = null; setTab("new"); });
    $("done-fix").addEventListener("click", () => { d.classList.add("hidden"); startEdit(p); });
  }
  function startEdit(p) {
    S.editing = p.id;
    S.form = { apartments: p.apartments.slice(), receiver: p.receiver, lines: p.lines.filter((l) => !l.void).map((l) => ({ amount: String(l.amount), method: l.method })), stay: p.stay || "", note: p.note || "", day: p.day, showNote: !!p.note, showDay: p.day !== NH.common.todayIso() };
    setTab("new");
    window.scrollTo(0, 0);
  }

  // ---- «Сегодня» --------------------------------------------------------------------
  const todaySig = (rows) => rows.map((p) => `${p.id}:${p.status}:${p.sheet}:${p.updated_at}`).join("|");
  async function refreshToday() {
    try {
      const r = await get("/payments?day=" + NH.common.todayIso());
      const changed = todaySig(r.payments) !== todaySig(S.today);
      S.today = r.payments;
      const live = S.today.filter((p) => p.status !== "cancelled").length;
      $("today-n").textContent = live; $("today-n").classList.toggle("hidden", !live);
      if (S.tab === "today" && changed) renderToday(true);  // only when something really changed: no flicker, no loop
    } catch (e) { /* shown on the tab */ }
  }
  function payRow(p, i) {
    const live = p.lines.filter((l) => !l.void);
    return `<div class="pay ${p.status}" data-open="${p.id}" style="animation-delay:${Math.min(i * 30, 240)}ms">
      <div class="pay__apt ${p.joint ? "joint" : ""}">${esc(p.apartments.map(aptNum).join("+"))}</div>
      <div class="pay__body"><div class="pay__sum">${live.map((l) => esc(money(l.amount, l.currency))).join(" + ")}</div><div class="pay__sub">${live.map((l) => esc(shortMethod(l.method))).join(", ")}${p.stay_label ? " · " + esc(p.stay_label) : ""}${p.note ? " · " + esc(p.note) : ""}</div></div>
      <div class="pay__right"><span>${esc(timeOf(p.created_at))} · ${esc(who(p))}</span>${sheetBadge(p)}</div>
    </div>`;
  }
  function renderToday(fresh) {
    const main = $("main");
    const rows = S.today;
    const total = rows.filter((p) => p.status !== "cancelled").reduce((s, p) => s + p.total_usd, 0);
    main.innerHTML = `<div class="view">
      <div class="stats"><div class="stat"><b>${fmtN(total, "USD")} $</b><span>принято сегодня</span></div><div class="stat"><b>${rows.filter((p) => p.status !== "cancelled").length}</b><span>оплат · ${esc(dateRu(NH.common.todayIso()))}</span></div></div>
      ${rows.length ? rows.map(payRow).join("") : `<div class="empty">Сегодня оплат ещё не было.<br><button class="link" id="go-new">Записать первую →</button></div>`}
    </div>`;
    main.querySelectorAll("[data-open]").forEach((el) => el.addEventListener("click", () => openPayment(+el.dataset.open)));
    const gn = $("go-new"); if (gn) gn.addEventListener("click", () => setTab("new"));
    if (!fresh) refreshToday();
  }
  async function openPayment(id) {
    let p;
    try { p = await get(`/payments/${id}`); } catch (e) { toast(e.message); return; }
    const live = p.lines.filter((l) => !l.void);
    openSheet(`<h3>Оплата №${p.id} ${sheetBadge(p)}</h3>
      <dl class="kv">
        <dt>Квартира</dt><dd>${esc(p.apartments.map(aptNum).join(", "))}${p.joint ? ` <span class="muted">· касса кв. ${aptNum(p.receiver)}</span>` : ""}</dd>
        <dt>Сумма</dt><dd>${live.map((l) => `${esc(money(l.amount, l.currency))} <span class="muted">${esc(l.method)}${l.currency !== "USD" ? " ≈ " + fmtN(l.usd, "USD") + " $" : ""}${l.sheet_row ? " · строка " + l.sheet_row : ""}</span>`).join("<br>")}</dd>
        ${p.joint ? `<dt>Доли</dt><dd>${Object.entries(p.shares || {}).map(([a, v]) => `кв. ${aptNum(a)}: ${fmtN(v, "USD")} $`).join(", ")}</dd>` : ""}
        ${p.stay_label ? `<dt>Проживание</dt><dd>${esc(p.stay_label)}</dd>` : ""}
        ${p.note ? `<dt>Комментарий</dt><dd>${esc(p.note)}</dd>` : ""}
        <dt>Кто</dt><dd>${esc(who(p))} · ${esc(p.day.split("-").reverse().join("."))} ${esc(timeOf(p.created_at))}</dd>
        ${p.cancel_reason ? `<dt>Отмена</dt><dd class="bad">${esc(p.cancel_reason)}</dd>` : ""}
        ${p.sheet === "error" ? `<dt>Таблица</dt><dd class="bad">${esc((p.lines.find((l) => l.sheet_error) || {}).sheet_error || "ошибка")} <button class="btn sm" id="p-retry">Повторить</button></dd>` : ""}
      </dl>
      ${p.audit && p.audit.length > 1 ? `<div><div class="small muted" style="margin-bottom:4px">История изменений</div><div class="audit">${p.audit.map(auditRow).join("")}</div></div>` : ""}
      ${p.status !== "cancelled" ? (p.can_edit ? `<div class="row"><button class="btn danger" id="p-cancel">Отменить</button><button class="btn primary" id="p-edit" style="flex:1">Исправить</button></div>` : `<div class="muted small">Исправить можно ${S.meta.edit_minutes} минут после записи; позже — только владелец.</div>`) : ""}`,
      (card) => {
        const ed = card.querySelector("#p-edit"); if (ed) ed.addEventListener("click", () => { closeSheet(); startEdit(p); });
        const rt = card.querySelector("#p-retry"); if (rt) rt.addEventListener("click", async () => { rt.disabled = true; try { const r = await post(`/payments/${p.id}/retry`); toast(r.sheet === "ok" ? "Записано в таблицу" : "Таблица снова не ответила"); closeSheet(); refreshToday(); } catch (e) { toast(e.message); rt.disabled = false; } });
        const cn = card.querySelector("#p-cancel"); if (cn) cn.addEventListener("click", () => askCancel(p));
      });
  }
  function askCancel(p) {
    openSheet(`<h3>Отменить оплату №${p.id}?</h3><p class="muted small">Ничего не удаляется: в таблице сумма станет 0 с пометкой «ОТМЕНА», в журнале останется кто и почему.</p>
      <input class="field" id="c-reason" placeholder="Причина (обязательно)" maxlength="300" />
      <div class="row"><button class="btn" id="c-no">Назад</button><button class="btn danger" id="c-yes" style="flex:1">Отменить оплату</button></div>`,
      (card) => {
        card.querySelector("#c-no").addEventListener("click", () => openPayment(p.id));
        card.querySelector("#c-yes").addEventListener("click", async (e) => {
          const reason = card.querySelector("#c-reason").value.trim();
          if (reason.length < 3) { card.querySelector("#c-reason").classList.add("bad"); card.querySelector("#c-reason").focus(); return; }
          e.currentTarget.disabled = true;
          try { const r = await post(`/payments/${p.id}/cancel`, { reason }); closeSheet(); haptic("ok"); toast("Отменено"); refreshToday(); if (S.tab === "stats") renderStats(); void r; } catch (err) { toast(err.message); e.currentTarget.disabled = false; }
        });
        setTimeout(() => card.querySelector("#c-reason").focus(), 250);
      });
  }
  function auditRow(a) {
    const d = a.details || {};
    const names = { add: "записал", edit: "исправил", cancel: "отменил", debt: "долг между квартирами", reviewed: "проверил" };
    let what = "";
    if (a.action === "add") what = `кв. ${(d.apartments || []).map(aptNum).join(", ")} · ${(d.lines || []).map((l) => `${fmtN(l.amount, l.currency)} ${esc(l.method)}`).join(" + ")}${d.stay ? " · " + esc(d.stay) : ""}`;
    else if (a.action === "edit") what = `было: ${(d.before && d.before.lines || []).map((l) => `${fmtN(l.amount)} ${esc(l.method)}`).join(" + ")} · кв. ${(d.before && d.before.apartments || []).map(aptNum).join(", ")}\nстало: ${(d.after && d.after.lines || []).map((l) => `${fmtN(l.amount)} ${esc(l.method)}`).join(" + ")} · кв. ${(d.after && d.after.apartments || []).map(aptNum).join(", ")}`;
    else if (a.action === "cancel") what = `причина: ${esc(d.reason || "")}`;
    else if (a.action === "debt") what = (d.rows || []).map((r) => `${r[1]} → ${r[2]}: ${fmtN(r[3], "USD")} $`).join(", ");
    return `<div class="audit__row"><div><span class="audit__act ${esc(a.action)}">${names[a.action] || esc(a.action)}</span><div class="muted">${esc(a.who_name || "")}<br>${esc((a.at || "").slice(5, 16).replace("T", " "))}</div></div><div class="audit__what">${a.payment_id ? `№${a.payment_id} · ` : ""}${what}</div></div>`;
  }

  // ---- «Итоги» (owner) ---------------------------------------------------------------
  async function renderStats() {
    const main = $("main");
    main.innerHTML = `<div class="view"><div class="empty">Загрузка…</div></div>`;
    let st, rv, au, list;
    try { [st, rv, au, list] = await Promise.all([get("/stats?month=" + S.month), get("/review"), get("/audit?limit=60"), get("/payments?month=" + S.month)]); }
    catch (e) { main.innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
    const [y, mo] = S.month.split("-").map(Number);
    $("review-n").textContent = rv.payments.length; $("review-n").classList.toggle("hidden", !rv.payments.length);
    const byDay = Object.entries(st.by_day || {}).sort((a, b) => b[0].localeCompare(a[0]));
    main.innerHTML = `<div class="view">
      <div class="month"><button id="m-prev">‹</button><b>${NH.common.MONTHS_NOM[mo - 1]} ${y}</b><button id="m-next" ${S.month >= NH.common.todayIso().slice(0, 7) ? "disabled" : ""}>›</button></div>
      <div class="stats">
        <div class="stat"><b>${fmtN(st.total_usd, "USD")} $</b><span>принято за месяц</span></div>
        <div class="stat"><b>${st.count}</b><span>оплат · отменено ${st.cancelled}</span></div>
        <div class="stat ${st.pending ? "bad" : ""}"><b>${st.pending}</b><span>не попало в таблицу</span></div>
        <div class="stat ${st.review ? "warn" : ""}"><b>${st.review}</b><span>на проверке</span></div>
      </div>
      ${rv.payments.length ? `<section class="card"><div class="card__h"><h3>На проверку · совместные платежи</h3></div>
        ${rv.payments.map((p) => `<div style="padding:10px 0;border-bottom:1px solid var(--sep)">
          <div><b>№${p.id} · кв. ${esc(p.apartments.map(aptNum).join(" + "))}</b> · ${p.lines.filter((l) => !l.void).map((l) => esc(money(l.amount, l.currency))).join(" + ")}${p.stay_label ? " · " + esc(p.stay_label) : ""} <span class="muted small">· ${esc(who(p))}, ${esc(p.day.split("-").reverse().join("."))}</span></div>
          <div class="small muted" style="margin:4px 0 8px">Деньги в кассе кв. <b>${aptNum(p.receiver)}</b> (строка в «Операции» уже есть). Доли: ${Object.entries(p.shares || {}).map(([a, v]) => `кв. ${aptNum(a)} — ${fmtN(v, "USD")} $`).join(", ")}</div>
          <div class="row wrap"><button class="btn sm" data-rv="${p.id}" data-debt="0">Оставить как есть</button><button class="btn sm primary" data-rv="${p.id}" data-debt="1">Записать как долг между квартирами</button></div>
        </div>`).join("")}</section>` : ""}
      <section class="card"><div class="card__h"><h3>По счетам</h3><span class="muted">${esc(NH.common.MONTHS_NOM[mo - 1])}</span></div>
        ${st.by_method.length ? `<table class="tbl">${st.by_method.map((m) => `<tr><td>${esc(m.method)} <span class="muted small">· ${m.n}</span></td><td>${esc(money(m.amount, m.currency))}${m.currency !== "USD" ? `<div class="muted small">≈ ${fmtN(m.usd, "USD")} $</div>` : ""}</td></tr>`).join("")}</table>` : `<div class="muted">Пока пусто</div>`}
      </section>
      <section class="card"><div class="card__h"><h3>По дням</h3></div>
        ${byDay.length ? `<table class="tbl">${byDay.map(([d, v]) => `<tr><td>${esc(dateRu(d))}</td><td>${fmtN(v, "USD")} $</td></tr>`).join("")}</table>` : `<div class="muted">Пока пусто</div>`}
      </section>
      <section class="card"><div class="card__h"><h3>Все оплаты</h3><span class="muted">${list.payments.length}</span></div>
        <div style="display:flex;flex-direction:column;gap:8px">${list.payments.length ? list.payments.map((p, i) => payRow(p, i).replace('class="pay ', `class="pay `)).join("") : `<div class="muted">Пока пусто</div>`}</div>
      </section>
      <section class="card"><div class="card__h"><h3>Журнал изменений</h3><span class="muted">кто · когда · что</span></div>
        ${au.audit.length ? `<div class="audit">${au.audit.map(auditRow).join("")}</div>` : `<div class="muted">Пока пусто</div>`}
      </section>
    </div>`;
    $("m-prev").addEventListener("click", () => { S.month = shiftMonth(S.month, -1); renderStats(); });
    $("m-next").addEventListener("click", () => { S.month = shiftMonth(S.month, 1); renderStats(); });
    main.querySelectorAll("[data-open]").forEach((el) => el.addEventListener("click", () => openPayment(+el.dataset.open)));
    main.querySelectorAll("[data-rv]").forEach((b) => b.addEventListener("click", async () => {
      b.disabled = true;
      try { await post(`/payments/${b.dataset.rv}/review`, { as_debt: b.dataset.debt === "1" }); haptic("ok"); toast(b.dataset.debt === "1" ? "Записано в «Долг между Аппартами»" : "Отмечено как проверенное"); renderStats(); }
      catch (e) { toast(e.message); b.disabled = false; }
    }));
  }
  const shiftMonth = (m, d) => { const [y, mo] = m.split("-").map(Number); const x = new Date(y, mo - 1 + d, 1); return `${x.getFullYear()}-${NH.common.pad(x.getMonth() + 1)}`; };

  // ---- bottom sheet -----------------------------------------------------------------
  function openSheet(html, wire) {
    const s = $("sheet"), c = $("sheet-card");
    s.classList.remove("closing"); c.innerHTML = html; s.classList.remove("hidden");
    if (wire) wire(c);
  }
  function closeSheet() { const s = $("sheet"); if (s.classList.contains("hidden")) return; s.classList.add("closing"); setTimeout(() => { s.classList.add("hidden"); s.classList.remove("closing"); }, 170); }
  $("sheet").addEventListener("click", (e) => { if (e.target.hasAttribute("data-close")) closeSheet(); });

  // ---- boot ---------------------------------------------------------------------------
  if (EMBED && NH.keyboard) NH.keyboard.embed(() => { const a = document.activeElement; if (a && /^(INPUT|TEXTAREA)$/.test(a.tagName)) { try { a.scrollIntoView({ block: "center" }); } catch (e) { /* ignore */ } } });
  async function boot() {
    if (tg && !EMBED) { try { tg.ready(); tg.expand(); if (tg.setHeaderColor) tg.setHeaderColor("#F5F6F8"); if (tg.setBackgroundColor) tg.setBackgroundColor("#F5F6F8"); } catch (e) { /* ignore */ } }
    try { S.meta = await get("/meta"); } catch (e) { if (e.status !== 403) $("main").innerHTML = `<div class="empty">${esc(e.message)}<div style="margin-top:12px"><button class="btn" id="retry">Повторить</button></div></div>`; const r = $("retry"); if (r) r.addEventListener("click", boot); return; }
    $("me-name").textContent = S.meta.me.name || "";
    if (S.meta.me.owner) document.querySelectorAll(".owner-only").forEach((el) => el.classList.remove("hidden"));
    if (!S.meta.sheet) toast("Таблица не подключена (SHEET_API_URL) — оплаты копятся в CRM");
    let tab = "new";
    try { tab = localStorage.getItem("nh_cash_tab") || "new"; } catch (e) { /* ignore */ }
    if (location.hash === "#stats" && S.meta.me.owner) tab = "stats";
    if (tab === "stats" && !S.meta.me.owner) tab = "new";
    setTab(tab);
    refreshToday();
    if (S.meta.me.owner) { try { const rv = await get("/review"); $("review-n").textContent = rv.payments.length; $("review-n").classList.toggle("hidden", !rv.payments.length); } catch (e) { /* ignore */ } }
  }
  boot();
})();
