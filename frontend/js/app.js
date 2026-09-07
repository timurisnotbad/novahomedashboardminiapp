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
        container.innerHTML = ui.empty("Не удалось загрузить данные. Проверьте соединение.");
        return;
      }
    }
    container.innerHTML = mod.render(cache[name]);
    applyRole();
    setHeader(name, cache[name]);
  }

  function navigate(name, force) {
    if (!SCREENS[name]) return;
    if (name === "payments") {
      if (!(isOwner() || canPayments)) name = "today";
      else loadRecon();
    } else if ((name === "guests" || name === "finance" || name === "prices" || name === "control") && !isOwner()) {
      name = "today";
    } else if (name === "control") {
      loadControl();
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
  async function doSync() {
    const btn = document.getElementById("sync-btn");
    btn.classList.add("spinning");
    try {
      const res = await api.sync();
      Object.keys(cache).forEach((k) => delete cache[k]);
      await loadScreen(current, true);
      ui.toast(res.success ? `Обновлено: ${res.bookings_synced} броней` : "Ошибка синхронизации");
      ui.haptic("success");
    } catch (e) {
      ui.toast("Ошибка синхронизации");
    } finally {
      btn.classList.remove("spinning");
    }
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
    renderFinance();
    api.getPayroll(NH.screens.finance.payMonthYM(pv.off))
      .then((d) => { pv.data = d; })
      .catch(() => {})
      .finally(() => { pv.loading = false; renderFinance(); });
  }

  function renderReconScreen() {
    const el = document.getElementById("screen-payments");
    if (el) el.innerHTML = NH.screens.payrecon.render();
    applyRole();
  }

  function loadRecon() {
    const rv = NH.screens.payrecon.state;
    rv.loading = true;
    renderReconScreen();
    api.getPayRecon(NH.screens.payrecon.ym(rv.off))
      .then((d) => { rv.data = d; })
      .catch(() => {})
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
    renderControl();
    const month = NH.screens.control.ym(cs.off);
    const call = v === "clean" ? api.getCleaningStats(month)
      : v === "buy" ? api.getSupplies()
      : api.getAttendanceStats(month);
    call
      .then((d) => { cs[v] = d; })
      .catch(() => ui.toast("Не удалось загрузить"))
      .finally(() => { cs.loading = false; renderControl(); });
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

  async function addTaskFromForm() {
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
    await mutateTask(() =>
      api.addTask({
        title,
        apartment: aptEl ? aptEl.value || null : null,
        deadline: dueEl ? dueEl.dataset.value || null : null,
        deadline_time: timeEl ? timeEl.dataset.value || null : null,
      })
    );
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
        if (ps.view === "pen" && !ps.data && !ps.loading) {
          ps.loading = true;
          api.getPenalties()
            .then((d) => { ps.data = d; })
            .catch(() => {})
            .finally(() => { ps.loading = false; renderFinance(); });
        }
        if (ps.view === "pay" && !NH.screens.finance.pay.data && !NH.screens.finance.pay.loading) {
          loadPayroll();
        }
        renderFinance();
        return;
      }
      // контроль: sub-view / month / shopping list
      const cv = e.target.closest("[data-ctl-view]");
      if (cv) {
        NH.screens.control.state.view = cv.dataset.ctlView;
        loadControl();
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
      if (e.target.closest("[data-sup-add]")) {
        const item = ((document.getElementById("sup-item") || {}).value || "").trim();
        const qty = parseInt((document.getElementById("sup-qty") || {}).value || "1", 10) || 1;
        const apt = ((document.getElementById("sup-apt") || {}).value || "").trim();
        if (!item) { ui.toast("Напишите, что купить"); return; }
        ui.haptic("light");
        api.addSupply({ item, qty, apartment: apt || null })
          .then(() => loadControl(true))
          .catch(() => ui.toast("Не удалось сохранить"));
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
        const rv = NH.screens.finance.recon;
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
      if (e.target.closest("[data-pay-terms]")) {
        const staff = (document.getElementById("pt-staff") || {}).value || "";
        const salary = parseFloat((document.getElementById("pt-salary") || {}).value || "0") || 0;
        const note = (document.getElementById("pt-note") || {}).value || "";
        if (!staff.trim()) { ui.toast("Укажите сотрудника"); return; }
        ui.haptic("light");
        api.setPayTerms({ staff: staff.trim(), salary, pay_note: note.trim() })
          .then(loadPayroll)
          .catch(() => ui.toast("Не удалось сохранить"));
        return;
      }
      if (e.target.closest("[data-pay-add]")) {
        const staff = (document.getElementById("pp-staff") || {}).value || "";
        const amount = parseFloat((document.getElementById("pp-amount") || {}).value || "");
        const note = (document.getElementById("pp-note") || {}).value || "";
        if (!staff.trim() || !(amount > 0)) { ui.toast("Укажите сотрудника и сумму"); return; }
        ui.haptic("light");
        const editing = NH.screens.finance.pay.editing;
        const call = editing
          ? api.patchPayPayment(editing.id, { staff: staff.trim(), amount, note: note.trim() })
          : api.addPayPayment({ staff: staff.trim(), amount, note: note.trim() });
        call
          .then(() => { NH.screens.finance.pay.editing = null; loadPayroll(); })
          .catch(() => ui.toast("Не удалось сохранить"));
        return;
      }
      const payDel = e.target.closest("[data-pay-del]");
      if (payDel) {
        api.delPayPayment(payDel.dataset.id).then(loadPayroll).catch(() => {});
        return;
      }
      if (e.target.closest("[data-pen-add]")) {
        const staff = (document.getElementById("pen-staff") || {}).value || "";
        const kind = (document.getElementById("pen-kind") || {}).value || "fine";
        const amount = parseFloat((document.getElementById("pen-amount") || {}).value || "");
        const reason = (document.getElementById("pen-reason") || {}).value || "";
        if (!staff.trim() || !(amount > 0)) {
          ui.toast("Укажите сотрудника и сумму");
          return;
        }
        ui.haptic("light");
        api.addPenalty({ kind, staff: staff.trim(), amount, reason: reason.trim() })
          .then(() => api.getPenalties())
          .then((d) => { NH.screens.finance.pstate.data = d; renderFinance(); })
          .catch(() => ui.toast("Не удалось сохранить"));
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
        api.patchPayRecon({
          chat_id: parseInt(prSave.dataset.chat, 10),
          msg_id: parseInt(prSave.dataset.msg, 10),
          amount: amount > 0 ? amount : null,
          method: method.trim() || null,
        })
          .then(() => { closeSheet(); loadRecon(); ui.toast("Дополнено"); })
          .catch(() => ui.toast("Не удалось сохранить"));
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
    try {
      const me = await api.getMe();
      role = me && me.role === "owner" ? "owner" : "staff";
      canPayments = !!(me && me.can_payments);
    } catch (e) {
      role = "staff"; // if /me fails, stay locked down (never leak Касса)
      canPayments = false;
    }
    applyRole();
    navigate("today", true);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
