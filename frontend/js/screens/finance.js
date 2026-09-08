/* "Касса" screen — cash balances per apartment + fines/bonuses log (owner). */
(function () {
  const NH = (window.NH = window.NH || {});
  const { esc, usd, icon } = NH.ui;

  // sub-view: "balance" | "pen" | "pay"; data cached after first load
  const pstate = { view: "balance", data: null, loading: false };
  // payroll (табель): off = month offset from current (0 = this month)
  const pay = { off: 0, data: null, loading: false, editing: null };

  function skeleton() {
    return `<div class="skeleton" style="height:96px;border-radius:16px"></div>
      <div style="height:12px"></div>${NH.ui.skeletonList(6)}`;
  }

  function segments() {
    return `<div class="fin-seg">
      <button class="fin-seg__btn ${pstate.view === "balance" ? "is-on" : ""}" data-fin-view="balance">Баланс</button>
      <button class="fin-seg__btn ${pstate.view === "pen" ? "is-on" : ""}" data-fin-view="pen">Штрафы</button>
      <button class="fin-seg__btn ${pstate.view === "pay" ? "is-on" : ""}" data-fin-view="pay">Зарплата</button>
    </div>`;
  }

  // ---- Зарплата (табель) ---------------------------------------------------
  function payMonthYM(off) {
    const d = new Date();
    d.setDate(1);
    d.setMonth(d.getMonth() + (off || 0));
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
  }

  function payForms(names) {
    const opts = (names || []).map((s) => `<option value="${esc(s)}"></option>`).join("");
    const ed = pay.editing;
    const banner = ed
      ? `<div class="pay-banner">✏️ Редактирование выплаты — измените поля и нажмите «Сохранить»
           <button class="pay-banner__x" data-pay-cancel>Отмена</button></div>`
      : "";
    return `
      <datalist id="pay-staff-list">${opts}</datalist>
      <div class="form-card">
        <div class="form-card__title">💸 ${ed ? "Изменить выплату" : "Записать выплату"}</div>
        <div class="form-card__sub">Аванс или часть зарплаты — попадёт в список ниже и в «Выплачено»</div>
        ${banner}
        <input id="pp-staff" class="form-input" type="text" maxlength="60" list="pay-staff-list"
               placeholder="Кому (сотрудник)" value="${ed ? esc(ed.staff) : ""}" />
        <div class="form-row2">
          <input id="pp-amount" class="form-input" type="number" inputmode="decimal" min="0" step="any"
                 placeholder="Сумма, сум" value="${ed ? esc(String(ed.amount)) : ""}" />
          <input id="pp-note" class="form-input" type="text" maxlength="100"
                 placeholder="Примечание (аванс…)" value="${ed ? esc(ed.note || "") : ""}" />
        </div>
        <button class="btn-primary" data-pay-add>${ed ? "Сохранить изменения" : "Записать выплату"}</button>
      </div>
      <details class="pay-terms" ${pay.termsOpen ? "open" : ""}>
        <summary>⚙️ Оклад и даты выдачи сотрудника</summary>
        <div class="form-card">
          <div class="form-card__sub">У каждого свои условия — оклад в месяц и когда выдаёте (текстом)</div>
          <input id="pt-staff" class="form-input" type="text" maxlength="60" list="pay-staff-list" placeholder="Сотрудник" />
          <div class="form-row2">
            <input id="pt-salary" class="form-input" type="number" inputmode="decimal" min="0" step="any" placeholder="Оклад в месяц, сум" />
            <input id="pt-note" class="form-input" type="text" maxlength="120" placeholder="Даты выдачи (5 и 20 числа…)" />
          </div>
          <button class="btn-primary" data-pay-terms>Сохранить условия</button>
        </div>
      </details>`;
  }

  function payRow(label, value, cls) {
    return `<div class="li"><span class="li__main"><div class="task__t">${label}</div></span>
      <span class="li__v ${cls || ""}">${value}</span></div>`;
  }

  function payCards(rows) {
    if (!rows.length) return NH.ui.empty("Пока пусто. Задайте оклад сотруднику (⚙️ выше) или запишите первую выплату.");
    return rows
      .map((r) => {
        const hasTerms = r.salary > 0;
        let items = "";
        items += payRow("Оклад", hasTerms ? fmtAmount(r.salary) : "не задан ⚙️");
        if (r.bonuses) items += payRow("Премии", "+" + fmtAmount(r.bonuses), "pen-green");
        if (r.fines) items += payRow("Штрафы", "−" + fmtAmount(r.fines), "pen-red");
        if (hasTerms || r.bonuses || r.fines) {
          items += payRow("К выплате", fmtAmount(r.due));
        }
        items += payRow("Выплачено", fmtAmount(r.paid), r.paid ? "pen-green" : "");
        if (hasTerms || r.bonuses || r.fines) {
          const bal = r.balance;
          items += payRow(bal >= 0 ? "Осталось выплатить" : "Переплата",
                          fmtAmount(Math.abs(bal)), bal > 0 ? "pen-red" : "pen-green");
        }
        const meta = [];
        if (r.pay_note) meta.push("📅 " + esc(r.pay_note));
        if (r.days) meta.push(`выходов: ${r.days}` + (r.lates ? ` · опозданий: ${r.lates}` : ""));
        const metaHtml = meta.length ? `<div class="pay-meta">${meta.join(" · ")}</div>` : "";
        const btns = `<div class="pay-cardbtns">
          <button class="pay-cardbtn" data-pay-fill data-staff="${esc(r.staff)}">➕ Выплата</button>
          <button class="pay-cardbtn" data-terms-fill data-staff="${esc(r.staff)}"
                  data-salary="${r.salary || ""}" data-note="${esc(r.pay_note || "")}">⚙️ Условия</button>
        </div>`;
        return `<div class="group"><div class="group__h">${icon("users")} ${esc(r.staff)}</div>
          <div class="list">${items}</div>${metaHtml}${btns}</div>`;
      })
      .join("");
  }

  function payPayments(payments) {
    if (!payments.length) return "";
    const rows = payments
      .map((p) => {
        const d = new Date(p.at);
        const when = isNaN(d) ? "" :
          `${String(d.getDate()).padStart(2, "0")}.${String(d.getMonth() + 1).padStart(2, "0")}`;
        return `<div class="li">
          <span class="li__main"><div class="task__t">${esc(p.staff)} — <b>${fmtAmount(p.amount)}</b></div>
          <div class="task__meta"><span class="due">${when}${p.note ? " · " + esc(p.note) : ""}</span></div></span>
          <button class="task-del" data-pay-edit data-id="${p.id}" data-staff="${esc(p.staff)}"
                  data-amount="${p.amount}" data-note="${esc(p.note || "")}" aria-label="Изменить">✏️</button>
          <button class="task-del" data-pay-del data-id="${p.id}" aria-label="Удалить">${icon("x")}</button>
        </div>`;
      })
      .join("");
    return `<div class="group"><div class="group__h">${icon("wallet")} Выплаты за месяц</div>
      <div class="list">${rows}</div></div>`;
  }

  // a failed request shows the reason and a retry, never an endless skeleton
  function failedView(st, view) {
    if (st.loading || !st.error) return null;
    return NH.ui.empty(st.error) +
      `<div class="empty"><button class="btn" data-fin-view="${view}">Повторить</button></div>`;
  }

  function payView() {
    const f = failedView(pay, "pay");
    if (f) return f;
    if (pay.loading || !pay.data) return NH.ui.skeletonList(4);
    const d = pay.data;
    const [y, m] = d.month.split("-");
    const label = `${MONTHS_RU[parseInt(m, 10) - 1]} ${y}`;
    let html = `<div class="pay-mnav">
      <button class="pay-mnav__btn" data-pay-month="-1">‹</button>
      <span class="pay-mnav__label">${esc(label)}</span>
      <button class="pay-mnav__btn" data-pay-month="1" ${pay.off >= 0 ? "disabled" : ""}>›</button>
    </div>`;
    html += payForms(d.names);
    html += payCards(d.rows || []);
    html += payPayments(d.payments || []);
    return html;
  }

  const MONTHS_RU = ["январь","февраль","март","апрель","май","июнь",
    "июль","август","сентябрь","октябрь","ноябрь","декабрь"];

  function fmtAmount(n) {
    return (Math.round(n * 100) / 100).toLocaleString("ru-RU");
  }

  function penForm(staffNames) {
    const opts = (staffNames || []).map((s) => `<option value="${esc(s)}"></option>`).join("");
    return `
      <div class="addfield">
        <div class="addfield__top">
          <button class="plus" data-pen-add aria-label="Добавить">${icon("plus")}</button>
          <input id="pen-staff" class="title" type="text" maxlength="60" list="pen-staff-list" placeholder="Сотрудник…" />
          <datalist id="pen-staff-list">${opts}</datalist>
        </div>
        <div class="addfield__row2">
          <select id="pen-kind" class="pen-kind">
            <option value="fine">🔻 Штраф</option>
            <option value="bonus">🔺 Премия</option>
          </select>
          <input id="pen-amount" type="number" inputmode="decimal" min="0" step="any" placeholder="Сумма" class="pen-amount" />
        </div>
        <div class="addfield__row">
          <input id="pen-reason" type="text" maxlength="300" placeholder="Причина / описание…" class="pen-reason" />
        </div>
      </div>`;
  }

  function penRows(records) {
    let html = "";
    let curKey = "";
    records.forEach((r) => {
      const d = new Date(r.at);
      const key = isNaN(d) ? "—" : `${MONTHS_RU[d.getMonth()]} ${d.getFullYear()}`;
      if (key !== curKey) {
        if (curKey) html += `</div></div>`;
        curKey = key;
        html += `<div class="group"><div class="group__h">${icon("cal")} ${esc(key)}</div><div class="list">`;
      }
      const when = isNaN(d) ? "" :
        `${String(d.getDate()).padStart(2, "0")}.${String(d.getMonth() + 1).padStart(2, "0")} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
      const fine = r.kind === "fine";
      html += `
        <div class="li">
          <span class="pen-mark ${fine ? "fine" : "bonus"}">${fine ? "🔻" : "🔺"}</span>
          <span class="li__main">
            <div class="task__t">${esc(r.staff)} — <b class="${fine ? "pen-red" : "pen-green"}">${fmtAmount(r.amount)}</b></div>
            <div class="task__meta"><span class="due">${esc(when)}${r.reason ? " · " + esc(r.reason) : ""}</span></div>
          </span>
          <button class="task-del" data-pen-del data-id="${r.id}" aria-label="Удалить">${icon("x")}</button>
        </div>`;
    });
    if (curKey) html += `</div></div>`;
    return html;
  }

  function penSummary(records) {
    const now = new Date();
    const per = {};
    records.forEach((r) => {
      const d = new Date(r.at);
      if (isNaN(d) || d.getMonth() !== now.getMonth() || d.getFullYear() !== now.getFullYear()) return;
      per[r.staff] = per[r.staff] || { fine: 0, bonus: 0 };
      per[r.staff][r.kind === "fine" ? "fine" : "bonus"] += r.amount || 0;
    });
    const names = Object.keys(per);
    if (!names.length) return "";
    const rows = names
      .map((n) => `
        <div class="li">
          <span class="li__main"><div class="task__t">${esc(n)}</div></span>
          <span class="pen-red">−${fmtAmount(per[n].fine)}</span>
          <span class="pen-green" style="margin-left:10px">+${fmtAmount(per[n].bonus)}</span>
        </div>`)
      .join("");
    return `<div class="group"><div class="group__h">${icon("chart")} Итог за ${MONTHS_RU[now.getMonth()]}</div>
      <div class="list">${rows}</div></div>`;
  }

  function penView() {
    const f = failedView(pstate, "pen");
    if (f) return f;
    if (pstate.loading || !pstate.data) return NH.ui.skeletonList(4);
    const recs = pstate.data.records || [];
    let html = penForm(pstate.data.staff);
    if (!recs.length) html += NH.ui.empty("Записей пока нет — добавьте первую сверху");
    else html += penSummary(recs) + penRows(recs);
    return html;
  }

  let lastBal = null; // keep balances so the toggle re-renders without refetch

  function render(data) {
    if (data) lastBal = data;
    else data = lastBal;
    if (pstate.view === "pen") return segments() + penView();
    if (pstate.view === "pay") return segments() + payView();

    if (!data || data.configured === false) {
      return segments() + NH.ui.empty("Таблица не подключена. Добавьте SHEET_API_URL и SHEET_API_TOKEN в .env.");
    }
    if (!data.ok) {
      return segments() + NH.ui.empty("Не удалось получить данные из таблицы. Проверьте ссылку и доступ «Все».");
    }

  function accountsHtml(apt) {
    const accounts = (apt.accounts || []).filter((a) => Math.round(a.usd || 0) !== 0);
    if (!accounts.length) return `<div class="cash-acc"><span class="cash-acc__name">Нет средств на счетах</span></div>`;
    return accounts
      .map(
        (a) => `
        <div class="cash-acc">
          <span class="cash-acc__name">${esc(a.name)}</span>
          <span class="cash-acc__native">${esc(a.native || "")}</span>
          <span class="cash-acc__usd">${usd(a.usd)}</span>
        </div>`
      )
      .join("");
  }

    const apts = (data.apartments || []).slice();
    const max = apts.reduce((m, a) => Math.max(m, a.usd || 0), 0) || 1;
    // units the app knows (RealtyCalendar) that the sheet did not report
    const missing = (data.missing || []);
    const missingHtml = missing.length
      ? `<div class="group"><div class="group__h">⚠️ Нет в ответе таблицы <span class="count">${missing.length}</span></div>
          <div class="list"><div class="li"><span class="li__main">
            <div class="task__t">${missing.map(esc).join(", ")}</div>
            <div class="task__meta"><span class="due">Эти квартиры есть в RealtyCalendar, но лист «${esc(data.sheet || "Счета")}» (или скрипт таблицы) их не отдаёт. Добавьте строки в лист — приложение показывает то, что возвращает таблица.</span></div>
          </span></div></div></div>`
      : "";

    const items = apts
      .map((a) => {
        const pct = Math.max(4, Math.round(((a.usd || 0) / max) * 100));
        const has = (a.accounts || []).length > 0;
        return `
          <div class="cash-item">
            <div class="li li--tap" ${has ? "data-cash-toggle" : ""}>
              <span class="aptbadge${NH.ui.badgeCls(a.code)}">${esc(a.code)}</span>
              <span class="cash-row__bar"><i style="width:${pct}%"></i></span>
              <span class="li__v">${usd(a.usd)}</span>
              ${has ? `<span class="li__chev">${icon("chev")}</span>` : ""}
            </div>
            <div class="cash-accounts">${accountsHtml(a)}</div>
          </div>`;
      })
      .join("");

    return segments() + `
      <div class="cash-total">
        <div class="cash-total__label">Всего денег по всем квартирам</div>
        <div class="cash-total__value">${usd(data.total_usd)}</div>
        <div class="cash-total__sub">Лист «${esc(data.sheet || "Счета")}» · ${apts.length} квартир · нажмите для деталей</div>
      </div>
      <div class="group"><div class="group__h">${icon("wallet")} Баланс по квартирам</div>
        <div class="list">${items}</div>
      </div>${missingHtml}`;
  }

  NH.screens = NH.screens || {};
  NH.screens.finance = { render, skeleton, pstate, pay, payMonthYM };
})();
