/* App orchestrator: Telegram init, navigation, data loading, interactions. */
(function () {
  const NH = window.NH;
  const { api, ui } = NH;
  const tg = window.Telegram && window.Telegram.WebApp;

  // ---- Owner / staff role ---------------------------------------------------
  // The role is decided server-side from the Telegram signature (/api/me).
  // Default to "staff" (least privilege) so the owner-only "Касса"/"Гости"
  // screens stay hidden until /api/me positively confirms the owner. This way
  // staff never see Касса, even for a single frame or if /api/me fails.
  let role = "staff";
  let canPayments = false;
  function isOwner() {
    return role === "owner";
  }

  // ---- Telegram init --------------------------------------------------------
  // The app commits to its own light brand theme, so we don't follow Telegram's
  // colour scheme — just expand to full height and set the header colour.
  function initTelegram() {
    document.documentElement.setAttribute("data-theme", "light");
    if (!tg) return;
    try {
      tg.ready();
      tg.expand();
      if (tg.setHeaderColor) tg.setHeaderColor("#F3EFE8");
      if (tg.setBackgroundColor) tg.setBackgroundColor("#F3EFE8");
    } catch (e) {}
  }

  const TITLES = {
    today: "Сегодня", tomorrow: "Завтра", cleaning: "Уборки", guests: "Гости",
    week: "Неделя", finance: "Касса", prices: "Цены", tasks: "Задачи",
    payments: "Оплаты", control: "Контроль",
  };

  function applyRole() {
    document.body.classList.toggle("staff-mode", !isOwner());
    // pay-viewer: staff who may see ONLY the Оплаты block inside Касса
    document.body.classList.toggle("pay-viewer", !isOwner() && canPayments);
    NH.caps = { owner: isOwner(), payments: isOwner() || canPayments };
  }

  // ---- Screen registry ------------------------------------------------------
  const SCREENS = {
    today: { el: "screen-today", mod: () => NH.screens.today, load: api.getToday },
    tomorrow: { el: "screen-tomorrow", mod: () => NH.screens.tomorrow, load: api.getTomorrow },
    cleaning: { el: "screen-cleaning", mod: () => NH.screens.cleaning, load: () => api.getCleaning(2) },
    guests: { el: "screen-guests", mod: () => NH.screens.guests, load: api.getGuests },
    week: { el: "screen-week", mod: () => NH.screens.occupancy, load: () => api.getOccupancy(7) },
    finance: { el: "screen-finance", mod: () => NH.screens.finance,
      load: () => (isOwner() ? api.getBalances() : Promise.resolve(null)) },
    prices: { el: "screen-prices", mod: () => NH.screens.prices, load: () => NH.screens.prices.state },
    tasks: { el: "screen-tasks", mod: () => NH.screens.tasks, load: api.getTasks },
    payments: { el: "screen-payments", mod: () => NH.screens.payrecon,
      load: () => NH.screens.payrecon.state },
    control: { el: "screen-control", mod: () => NH.screens.control,
      load: () => NH.screens.control.state },
  };

  let current = "today";
  const cache = {};

  let serverStale = false; // the running backend is older than these files

  function errorText(e) {
    const st = e && e.status;
    if (st === 403) return "Нет доступа. Откройте дашборд через кнопку «📊 Дашборд» в боте @novahomedashboardbot.";
    if (serverStale || st === 404) {
      return "Сервер работает на старой версии. На компьютере закройте окна «Nova Backend» и «Nova Bot» " +
        "и запустите restart_all.bat.";
    }
    if (e && e.message === "timeout") return "Сервер не отвечает (20 с). Потяните вниз, чтобы повторить.";
    if (st >= 500) return "Ошибка на сервере. Посмотрите окно «Nova Backend» на компьютере.";
    return "Не удалось загрузить данные. Проверьте соединение.";
  }

  // Compare the server's release number with ours. The frontend is static files
  // served from disk, so after an update it is always new — but if the old
  // server process survived restart_all.bat, every new API call 404s.
  async function checkServerVersion() {
    try {
      const h = await api.getHealth();
      const v = h && h.version ? String(h.version) : "old";
      if (v !== String(NH.APP_VERSION)) {
        serverStale = true;
        showBanner(`⚠️ Сервер не обновлён (версия ${v}, файлы ${NH.APP_VERSION}). ` +
          "Закройте окна «Nova Backend» и «Nova Bot» на компьютере и запустите restart_all.bat.");
      }
    } catch (e) { /* offline — the screens report it themselves */ }
  }

  function showBanner(text) {
    let el = document.getElementById("nh-banner");
    if (!el) {
      el = document.createElement("div");
      el.id = "nh-banner";
      el.className = "nh-banner";
      document.getElementById("app").prepend(el);
    }
    el.textContent = text;
  }

  function setActiveNav(name) {
    document.querySelectorAll(".nav-item").forEach((btn) => {
      btn.classList.toggle("is-active", btn.dataset.screen === name);
    });
    document.querySelectorAll(".screen").forEach((s) => s.classList.add("hidden"));
    document.getElementById(SCREENS[name].el).classList.remove("hidden");
  }

  async function loadScreen(name, force) {
    const cfg = SCREENS[name];
    const container = document.getElementById(cfg.el);
    const mod = cfg.mod();

    if (!cache[name] || force) {
      container.innerHTML = mod.skeleton ? mod.skeleton() : "";
      try {
        const data = await cfg.load();
        cache[name] = data;
      } catch (e) {
        container.innerHTML = ui.empty(errorText(e));
        return;
      }
    }
    container.innerHTML = mod.render(cache[name]);
    applyRole();
    if (current === name) setHeader(name, cache[name]); // a slow earlier screen must not retitle the current one
  }

  // One in-flight request per button: a second tap while the first one is
  // still travelling through the tunnel must not create a second record.
  function once(btn, fn) {
    if (!btn || btn.dataset.busy === "1") return;
    btn.dataset.busy = "1";
    btn.disabled = true;
    Promise.resolve().then(fn).catch(() => {}).finally(() => {
      btn.dataset.busy = "";
      btn.disabled = false;
    });
  }

  function navigate(name, force) {
    if (!SCREENS[name]) return;
    if (name === "payments") {
      if (!(isOwner() || canPayments)) name = "today";
      else loadRecon();
    } else if ((name === "guests" || name === "finance" || name === "prices" || name === "control") && !isOwner()) {
      name = "today";
    } else if (name === "control") {
      loadControl(true); // always fresh: the bot writes here all day
    }
    current = name;
    setActiveNav(name);
    setHeader(name, cache[name]);
    loadScreen(name, force);
    ui.haptic("light");
  }

  function setHeader(name, data) {
    document.getElementById("header-title").textContent = TITLES[name] || "Nova Home";
    const el = document.getElementById("header-date");
    let sub = "";
    if ((name === "today" || name === "tomorrow") && data && data.date_human) {
      sub = data.date_human;
    } else if (name === "tasks" && data && data.counts) {
      sub = `${data.counts.open} открытых · ${data.counts.due_soon} на сегодня`;
    } else if (cache.today && cache.today.date_human) {
      sub = cache.today.date_human;
    }
    el.textContent = sub;
  }

  // ---- Guest detail sheet ---------------------------------------------------
  function openGuestSheet(b) {
    const content = document.getElementById("sheet-content");
    const phoneRow = b.client_phone
      ? `<div class="sheet__field"><span>Телефон</span><b>${ui.esc(b.client_phone)}</b></div>`
      : "";
    const financeRows = isOwner()
      ? `<div class="sheet__field"><span>Источник</span><b>${ui.esc(b.source || "—")}</b></div>
         <div class="sheet__field"><span>Сумма</span><b>${ui.usd(b.amount_usd)}</b></div>
         <div class="sheet__field"><span>Долг</span><b>${
           (b.debt_usd || 0) > 0 ? ui.usd(b.debt_usd) : "оплачено"
         }</b></div>`
      : "";
    const notes = b.short_notes
      ? `<div class="sheet__field"><span>Заметки</span><b>${ui.esc(b.short_notes)}</b></div>`
      : "";
    const callBtn = b.client_phone
      ? `<a class="call-btn" href="tel:${ui.esc(b.client_phone)}">${ui.icon("phone")} Позвонить</a>`
      : "";

    content.innerHTML = `
      <div class="sheet__handle"></div>
      <h3>${ui.esc(b.apartment)} · ${ui.esc(b.client_name || "Гость")}</h3>
      <div class="sheet__sub">${ui.shortDate(b.checkin)} → ${ui.shortDate(b.checkout)}${
      b.days ? " · " + b.days + " ноч." : ""
    }</div>
      <div class="sheet__field"><span>Заезд</span><b>${ui.esc(b.arrival_time || "—")}</b></div>
      <div class="sheet__field"><span>Выезд</span><b>${ui.esc(b.departure_time || "—")}</b></div>
      ${phoneRow}${financeRows}${notes}
      <div class="sheet__actions">${callBtn}
        <button class="btn" data-close-sheet>Закрыть</button>
      </div>`;
    document.getElementById("sheet").classList.remove("hidden");
  }

  function closeSheet() {
    document.getElementById("sheet").classList.add("hidden");
  }

  function openPaySheet(p) {
    const content = document.getElementById("sheet-content");
    content.innerHTML = `
      <div class="sheet__handle"></div>
      <h3>Дополнить оплату</h3>
      <div class="sheet__sub">${ui.esc((p.raw || "").slice(0, 80))}</div>
      <input id="pr-amount" class="form-input" type="number" inputmode="decimal" min="0" step="any"
             placeholder="Сумма" value="${p.amount || ""}" style="margin-bottom:8px" />
      <input id="pr-method" class="form-input" type="text" maxlength="60"
             placeholder="Метод (нал / карта **4980 / uzcard…)" value="${ui.esc(p.method || "")}" />
      <div class="sheet__actions">
        <button class="btn-primary" data-precon-save data-chat="${p.chat_id}" data-msg="${p.msg_id}">Сохранить</button>
        <button class="btn" data-close-sheet>Закрыть</button>
      </div>`;
    document.getElementById("sheet").classList.remove("hidden");
  }

  // ---- Cleaning status ------------------------------------------------------
  async function setCleaning(apt, date, status, cardEl) {
    ui.haptic("success");
    try {
      await api.updateCleaningStatus(apt, status, date);
      // update caches so re-render reflects the change
      invalidateCleaningCaches(apt, date, status);
      const mod = SCREENS[current].mod();
      document.getElementById(SCREENS[current].el).innerHTML = mod.render(cache[current]);
      applyRole();
      ui.toast(status === "done" ? "Уборка готова" : "Уборка начата");
    } catch (e) {
      ui.toast("Ошибка обновления");
    }
  }

  function invalidateCleaningCaches(apt, date, status) {
    ["today", "tomorrow", "cleaning"].forEach((name) => {
      const d = cache[name];
      if (!d) return;
      (d.cleanings || []).forEach((c) => {
        if (c.apartment === apt && c.cleaning_date === date) c.status = status;
      });
      (d.checkouts || []).forEach((c) => {
        if (c.apartment === apt) c.cleaning_status = status;
      });
    });
  }

  // ---- Sync -----------------------------------------------------------------
  let syncing = false;
  let lastSyncAt = 0;
  async function doSync() {
    if (syncing) return;
    const btn = document.getElementById("sync-btn");
    btn.classList.add("spinning");
    syncing = true;
    let res = null;
    let failed = null;
    try {
      // the RC pull is slow and the tunnel is shared: at most once per 30 s,
      // otherwise just re-read what the server already has
      if (Date.now() - lastSyncAt > 30000) {
        lastSyncAt = Date.now(); // even if it times out, don't hammer RC again right away
        res = await api.sync();
      }
    } catch (e) {
      failed = e;
    }
    try {
      // re-read the screens in any case: the server may have finished the
      // sync after our request gave up
      Object.keys(cache).forEach((k) => delete cache[k]);
      const cs = NH.screens.control.state;
      cs.att = null; cs.clean = null; cs.buy = null;
      if (current === "control") loadControl(true);
      else if (current === "payments") loadRecon();
      await loadScreen(current, true);
    } finally {
      syncing = false;
      btn.classList.remove("spinning");
    }
    if (failed) ui.toast(failed.status === 403 ? "Нет доступа" : "Синхронизация не завершилась — показаны последние данные");
    else if (res) ui.toast(res.success ? `Обновлено: ${res.bookings_synced} броней` : "Ошибка синхронизации");
    else ui.toast("Обновлено");
    ui.haptic(failed ? "light" : "success");
  }

  // ---- Tasks ----------------------------------------------------------------
  function setPickField(el, v) {
    el.dataset.value = v || "";
    const label = el.querySelector(".pk-field__v");
    if (el.dataset.pick === "date") label.textContent = v ? ui.shortDate(v) : "Дата";
    else label.textContent = v || "Время";
    el.classList.toggle("has-value", !!v);
    ui.haptic("light");
  }

  function loadPayroll() {
    const pv = NH.screens.finance.pay;
    pv.loading = true;
    pv.error = null;
    renderFinance();
    api.getPayroll(NH.screens.finance.payMonthYM(pv.off))
      .then((d) => { pv.data = d; })
      .catch((e) => { pv.error = errorText(e); })
      .finally(() => { pv.loading = false; renderFinance(); });
  }

  function loadPenalties() {
    const ps = NH.screens.finance.pstate;
    ps.loading = true;
    ps.error = null;
    renderFinance();
    api.getPenalties()
      .then((d) => { ps.data = d; })
      .catch((e) => { ps.error = errorText(e); })
      .finally(() => { ps.loading = false; renderFinance(); });
  }

  function renderReconScreen() {
    const el = document.getElementById("screen-payments");
    if (el) el.innerHTML = NH.screens.payrecon.render();
    applyRole();
  }

  function loadRecon() {
    const rv = NH.screens.payrecon.state;
    rv.loading = true;
    rv.error = null;
    renderReconScreen();
    api.getPayRecon(NH.screens.payrecon.ym(rv.off))
      .then((d) => { rv.data = d; })
      .catch((e) => { rv.error = errorText(e); })
      .finally(() => { rv.loading = false; renderReconScreen(); });
  }

  function renderFinance() {
    const el = document.getElementById("screen-finance");
    if (el) el.innerHTML = NH.screens.finance.render(null);
    applyRole();
  }

  function renderControl() {
    const el = document.getElementById("screen-control");
    if (el) el.innerHTML = NH.screens.control.render();
    applyRole();
  }

  // «Контроль»: fetch the data of the active sub-view (явка / уборки / закупки)
  function loadControl(force) {
    const cs = NH.screens.control.state;
    const v = cs.view;
    if (!force && cs[v]) { renderControl(); return; }
    cs.loading = true;
    cs.error = null;
    const seq = (cs.seq = (cs.seq || 0) + 1); // ignore answers of superseded requests
    renderControl();
    const month = NH.screens.control.ym(cs.off);
    const call = v === "clean" ? api.getCleaningStats(month)
      : v === "buy" ? api.getSupplies()
      : api.getAttendanceStats(month);
    call
      .then((d) => { if (seq === cs.seq) cs[v] = d; })
      .catch((e) => { if (seq === cs.seq) cs.error = errorText(e); })
      .finally(() => { if (seq === cs.seq) { cs.loading = false; renderControl(); } });
  }

  function renderPrices() {
    const el = document.getElementById("screen-prices");
    if (el) el.innerHTML = NH.screens.prices.render();
    applyRole();
  }

  async function mutateTask(fn) {
    try {
      await fn();
      delete cache.tasks;
      await loadScreen("tasks", true);
    } catch (e) {
      ui.toast("Ошибка. Попробуйте ещё раз.");
    }
  }

  let addingTask = false; // Enter + tap on «+» within the same second = one task
  async function addTaskFromForm() {
    if (addingTask) return;
    const titleEl = document.getElementById("task-title");
    const aptEl = document.getElementById("task-apt");
    const dueEl = document.getElementById("task-deadline");
    const timeEl = document.getElementById("task-deadline-time");
    const title = (titleEl && titleEl.value || "").trim();
    if (!title) {
      ui.toast("Введите текст задачи");
      if (titleEl) titleEl.focus();
      return;
    }
    ui.haptic("success");
    addingTask = true;
    try {
      await mutateTask(() =>
        api.addTask({
          title,
          apartment: aptEl ? aptEl.value || null : null,
          deadline: dueEl ? dueEl.dataset.value || null : null,
          deadline_time: timeEl ? timeEl.dataset.value || null : null,
        })
      );
    } finally {
      addingTask = false;
    }
    ui.toast("Задача добавлена");
  }

  // ---- Event delegation -----------------------------------------------------
  function bindEvents() {
    // Enter in the task title field adds the task
    document.getElementById("app").addEventListener("keydown", (e) => {
      if (e.target && e.target.id === "task-title" && e.key === "Enter") {
        e.preventDefault();
        addTaskFromForm();
      }
    });

    document.querySelectorAll(".nav-item").forEach((btn) => {
      btn.addEventListener("click", () => navigate(btn.dataset.screen));
    });

    document.getElementById("sync-btn").addEventListener("click", doSync);

    document.getElementById("app").addEventListener("click", (e) => {
      // metric / debtors tap → jump to relevant screen
      const metric = e.target.closest("[data-metric]");
      if (metric) {
        const map = { "Выезды": "cleaning", "Уборки": "cleaning", "Занятость": "week", debtors: "guests" };
        const dest = map[metric.dataset.metric];
        if (dest) { navigate(dest); return; }
      }
      // custom date/time picker fields
      const pick = e.target.closest("[data-pick]");
      if (pick) {
        const cur = pick.dataset.value || "";
        const set = (v) => setPickField(pick, v);
        if (pick.dataset.pick === "date") NH.picker.openDate(cur, set);
        else NH.picker.openTime(cur, set);
        return;
      }
      // finance: balance <-> penalties <-> payroll toggle
      const finSeg = e.target.closest("[data-fin-view]");
      if (finSeg) {
        const ps = NH.screens.finance.pstate;
        ps.view = finSeg.dataset.finView;
        if (ps.view === "pen" && !ps.data && !ps.loading) loadPenalties();
        if (ps.view === "pay" && !NH.screens.finance.pay.data && !NH.screens.finance.pay.loading) {
          loadPayroll();
        }
        renderFinance();
        return;
      }
      if (e.target.closest("[data-recon-retry]")) {
        loadRecon();
        return;
      }
      // контроль: sub-view / month / shopping list
      const cv = e.target.closest("[data-ctl-view]");
      if (cv) {
        const cs = NH.screens.control.state;
        const retry = cs.view === cv.dataset.ctlView; // «Повторить» after an error
        cs.view = cv.dataset.ctlView;
        loadControl(retry);
        return;
      }
      const cmn = e.target.closest("[data-ctl-month]");
      if (cmn) {
        const cs = NH.screens.control.state;
        cs.off = Math.min(0, cs.off + parseInt(cmn.dataset.ctlMonth, 10));
        cs.att = null; cs.clean = null;
        loadControl(true);
        return;
      }
      const supAdd = e.target.closest("[data-sup-add]");
      if (supAdd) {
        const item = ((document.getElementById("sup-item") || {}).value || "").trim();
        const qty = parseInt((document.getElementById("sup-qty") || {}).value || "1", 10) || 1;
        const apt = ((document.getElementById("sup-apt") || {}).value || "").trim();
        if (!item) { ui.toast("Напишите, что купить"); return; }
        ui.haptic("light");
        once(supAdd, () => api.addSupply({ item, qty, apartment: apt || null })
          .then(() => loadControl(true))
          .catch(() => ui.toast("Не удалось сохранить")));
        return;
      }
      const st = e.target.closest("[data-sup-toggle]");
      if (st) {
        ui.haptic(st.dataset.bought === "1" ? "success" : "light");
        api.setSupplyBought(st.dataset.id, st.dataset.bought === "1")
          .then(() => loadControl(true))
          .catch(() => ui.toast("Ошибка"));
        return;
      }
      const sd = e.target.closest("[data-sup-del]");
      if (sd) {
        api.delSupply(sd.dataset.id).then(() => loadControl(true)).catch(() => {});
        return;
      }
      const rm = e.target.closest("[data-recon-month]");
      if (rm) {
        const rv = NH.screens.payrecon.state;
        rv.off = Math.min(0, rv.off + parseInt(rm.dataset.reconMonth, 10));
        loadRecon();
        return;
      }
      // payroll: month nav, terms, payments
      const pm = e.target.closest("[data-pay-month]");
      if (pm) {
        const pv = NH.screens.finance.pay;
        pv.off = Math.min(0, pv.off + parseInt(pm.dataset.payMonth, 10));
        loadPayroll();
        return;
      }
      const pFill = e.target.closest("[data-pay-fill]");
      if (pFill) {
        const el = document.getElementById("pp-staff");
        if (el) el.value = pFill.dataset.staff || "";
        const amt = document.getElementById("pp-amount");
        window.scrollTo({ top: 0, behavior: "smooth" });
        if (amt) amt.focus();
        return;
      }
      const tFill = e.target.closest("[data-terms-fill]");
      if (tFill) {
        const det = document.querySelector(".pay-terms");
        if (det) det.open = true;
        const st = document.getElementById("pt-staff");
        const sal = document.getElementById("pt-salary");
        const nt = document.getElementById("pt-note");
        if (st) st.value = tFill.dataset.staff || "";
        if (sal) sal.value = tFill.dataset.salary || "";
        if (nt) nt.value = tFill.dataset.note || "";
        window.scrollTo({ top: 0, behavior: "smooth" });
        if (sal) sal.focus();
        return;
      }
      const pEdit = e.target.closest("[data-pay-edit]");
      if (pEdit) {
        NH.screens.finance.pay.editing = {
          id: pEdit.dataset.id,
          staff: pEdit.dataset.staff || "",
          amount: pEdit.dataset.amount || "",
          note: pEdit.dataset.note || "",
        };
        renderFinance();
        window.scrollTo({ top: 0, behavior: "smooth" });
        return;
      }
      if (e.target.closest("[data-pay-cancel]")) {
        NH.screens.finance.pay.editing = null;
        renderFinance();
        return;
      }
      const payTerms = e.target.closest("[data-pay-terms]");
      if (payTerms) {
        const staff = (document.getElementById("pt-staff") || {}).value || "";
        const salary = parseFloat((document.getElementById("pt-salary") || {}).value || "0") || 0;
        const note = (document.getElementById("pt-note") || {}).value || "";
        if (!staff.trim()) { ui.toast("Укажите сотрудника"); return; }
        ui.haptic("light");
        once(payTerms, () => api.setPayTerms({ staff: staff.trim(), salary, pay_note: note.trim() })
          .then(loadPayroll)
          .catch(() => ui.toast("Не удалось сохранить")));
        return;
      }
      const payAdd = e.target.closest("[data-pay-add]");
      if (payAdd) {
        const staff = (document.getElementById("pp-staff") || {}).value || "";
        const amount = parseFloat((document.getElementById("pp-amount") || {}).value || "");
        const note = (document.getElementById("pp-note") || {}).value || "";
        if (!staff.trim() || !(amount > 0)) { ui.toast("Укажите сотрудника и сумму"); return; }
        ui.haptic("light");
        const editing = NH.screens.finance.pay.editing;
        once(payAdd, () => (editing
          ? api.patchPayPayment(editing.id, { staff: staff.trim(), amount, note: note.trim() })
          : api.addPayPayment({ staff: staff.trim(), amount, note: note.trim() }))
          .then((r) => {
            NH.screens.finance.pay.editing = null;
            if (r && r.duplicate) ui.toast("Эта выплата уже записана");
            loadPayroll();
          })
          .catch(() => ui.toast("Не удалось сохранить")));
        return;
      }
      const payDel = e.target.closest("[data-pay-del]");
      if (payDel) {
        api.delPayPayment(payDel.dataset.id).then(loadPayroll).catch(() => {});
        return;
      }
      const penAdd = e.target.closest("[data-pen-add]");
      if (penAdd) {
        const staff = (document.getElementById("pen-staff") || {}).value || "";
        const kind = (document.getElementById("pen-kind") || {}).value || "fine";
        const amount = parseFloat((document.getElementById("pen-amount") || {}).value || "");
        const reason = (document.getElementById("pen-reason") || {}).value || "";
        if (!staff.trim() || !(amount > 0)) {
          ui.toast("Укажите сотрудника и сумму");
          return;
        }
        ui.haptic("light");
        once(penAdd, () => api.addPenalty({ kind, staff: staff.trim(), amount, reason: reason.trim() })
          .then((r) => {
            if (r && r.duplicate) ui.toast("Эта запись уже есть");
            return api.getPenalties();
          })
          .then((d) => { NH.screens.finance.pstate.data = d; renderFinance(); })
          .catch(() => ui.toast("Не удалось сохранить")));
        return;
      }
      const penDel = e.target.closest("[data-pen-del]");
      if (penDel) {
        api.deletePenalty(penDel.dataset.id)
          .then(() => api.getPenalties())
          .then((d) => { NH.screens.finance.pstate.data = d; renderFinance(); })
          .catch(() => {});
        return;
      }
      // prices: fetch Booking.com prices for the chosen dates
      if (e.target.closest("[data-prices-fetch]")) {
        const ci = document.getElementById("price-checkin");
        const co = document.getElementById("price-checkout");
        ui.haptic("light");
        const p = NH.screens.prices.fetchNow(
          ci ? ci.dataset.value : "", co ? co.dataset.value : ""
        );
        renderPrices();
        p.then(renderPrices);
        return;
      }
      // tasks: add / toggle done / delete
      if (e.target.closest("[data-task-add]")) {
        addTaskFromForm();
        return;
      }
      const taskToggle = e.target.closest("[data-task-toggle]");
      if (taskToggle) {
        mutateTask(() => api.setTaskStatus(taskToggle.dataset.id, taskToggle.dataset.status));
        ui.haptic(taskToggle.dataset.status === "done" ? "success" : "light");
        return;
      }
      const taskDel = e.target.closest("[data-task-del]");
      if (taskDel) {
        mutateTask(() => api.deleteTask(taskDel.dataset.id));
        return;
      }

      // cash apartment row → expand/collapse account breakdown
      const cashToggle = e.target.closest("[data-cash-toggle]");
      if (cashToggle) {
        cashToggle.closest(".cash-item").classList.toggle("is-open");
        ui.haptic("light");
        return;
      }
      // call button
      const call = e.target.closest("[data-phone]");
      if (call) {
        window.location.href = "tel:" + call.dataset.phone;
        return;
      }
      // payment post edit → sheet
      const prEdit = e.target.closest("[data-precon-edit]");
      if (prEdit) {
        try { openPaySheet(JSON.parse(decodeURIComponent(prEdit.dataset.p))); } catch (err) {}
        return;
      }
      const prSave = e.target.closest("[data-precon-save]");
      if (prSave) {
        const amount = parseFloat((document.getElementById("pr-amount") || {}).value || "");
        const method = (document.getElementById("pr-method") || {}).value || "";
        ui.haptic("light");
        once(prSave, () => api.patchPayRecon({
          chat_id: parseInt(prSave.dataset.chat, 10),
          msg_id: parseInt(prSave.dataset.msg, 10),
          amount: amount > 0 ? amount : null,
          method: method.trim() || null,
        })
          .then(() => { closeSheet(); loadRecon(); ui.toast("Дополнено"); })
          .catch(() => ui.toast("Не удалось сохранить")));
        return;
      }
      // guest card tap → sheet
      const card = e.target.closest("[data-guest]");
      if (card) {
        try {
          openGuestSheet(JSON.parse(decodeURIComponent(card.dataset.guest)));
        } catch (err) {}
        return;
      }
      // occupancy cell tap
      const cell = e.target.closest(".occ-seg[data-cell]");
      if (cell) {
        try {
          const d = JSON.parse(decodeURIComponent(cell.dataset.cell));
          openGuestSheet({
            apartment: d.apartment,
            client_name: d.client,
            checkin: d.checkin,
            checkout: d.checkout,
            amount_usd: d.amount_usd,
            arrival_time: null,
            departure_time: null,
          });
        } catch (err) {}
      }
    });

    // sheet close
    const sheet = document.getElementById("sheet");
    sheet.addEventListener("click", (e) => {
      if (e.target.closest("[data-close-sheet]") || e.target.classList.contains("sheet__backdrop")) {
        closeSheet();
      }
    });

    bindPullToRefresh();
  }

  // ---- Pull-to-refresh ------------------------------------------------------
  function bindPullToRefresh() {
    let startY = 0;
    let pulling = false;
    const ptr = document.getElementById("ptr");
    const THRESHOLD = 70;

    window.addEventListener("touchstart", (e) => {
      if (window.scrollY <= 0) {
        startY = e.touches[0].clientY;
        pulling = true;
      }
    }, { passive: true });

    window.addEventListener("touchmove", (e) => {
      if (!pulling) return;
      const dy = e.touches[0].clientY - startY;
      if (dy > 10) ptr.classList.add("visible");
    }, { passive: true });

    window.addEventListener("touchend", (e) => {
      if (!pulling) return;
      const dy = (e.changedTouches[0].clientY || 0) - startY;
      ptr.classList.remove("visible");
      pulling = false;
      if (dy > THRESHOLD && window.scrollY <= 0) doSync();
    }, { passive: true });
  }

  // ---- Boot -----------------------------------------------------------------
  async function boot() {
    initTelegram();
    bindEvents();
    let noAccess = false;
    try {
      const me = await api.getMe();
      role = me && me.role === "owner" ? "owner" : "staff";
      canPayments = !!(me && me.can_payments);
      noAccess = !!(me && me.has_access === false);
      if (me && me.role !== "owner" && !canPayments) {
        // a leftover owner/pay key on a shared device must not survive a staff login
        try { localStorage.removeItem("nh_okey"); } catch (err) {}
      }
    } catch (e) {
      role = "staff"; // if /me fails, stay locked down (never leak Касса)
      canPayments = false;
    }
    applyRole();
    checkServerVersion(); // in parallel with the first screen
    if (noAccess) {
      // opened from a bare link (not through the bot): nothing will load
      document.getElementById("screen-today").innerHTML = ui.empty(
        "Откройте дашборд через кнопку «📊 Дашборд» в боте @novahomedashboardbot — " +
        "по прямой ссылке данные не показываются."
      );
      setActiveNav("today");
      return;
    }
    navigate("today", true);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
