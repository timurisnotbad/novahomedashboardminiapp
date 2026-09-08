/* «Контроль» screen (owner): attendance stats, cleaning-session stats,
   shopping list. */
(function () {
  const NH = (window.NH = window.NH || {});
  const { esc, icon, shortDate } = NH.ui;

  const MONTHS_RU = ["январь","февраль","март","апрель","май","июнь",
    "июль","август","сентябрь","октябрь","ноябрь","декабрь"];

  // view: "att" | "clean" | "buy"; off = month offset (att / clean)
  const state = { view: "att", off: 0, att: null, clean: null, buy: null, loading: false };

  function ym(off) {
    const d = new Date();
    d.setDate(1);
    d.setMonth(d.getMonth() + (off || 0));
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
  }

  function segments() {
    const b = (v, label) =>
      `<button class="fin-seg__btn ${state.view === v ? "is-on" : ""}" data-ctl-view="${v}">${label}</button>`;
    return `<div class="fin-seg">${b("att", "Явка")}${b("clean", "Уборки")}${b("buy", "Закупки")}</div>`;
  }

  function monthNav(month) {
    const [y, m] = (month || ym(state.off)).split("-");
    const label = `${MONTHS_RU[parseInt(m, 10) - 1]} ${y}`;
    const busy = state.loading ? "disabled" : "";
    return `<div class="pay-mnav">
      <button class="pay-mnav__btn" data-ctl-month="-1" ${busy}>‹</button>
      <span class="pay-mnav__label">${esc(label)}</span>
      <button class="pay-mnav__btn" data-ctl-month="1" ${state.off >= 0 || state.loading ? "disabled" : ""}>›</button>
    </div>`;
  }

  // a failed request must not leave the skeleton forever
  function failed(withNav) {
    if (state.loading || !state.error) return null;
    return (withNav ? monthNav() : "") + NH.ui.empty(state.error) +
      `<div class="empty"><button class="btn" data-ctl-view="${state.view}">Повторить</button></div>`;
  }

  function dur(m) {
    if (m == null) return "—";
    const h = Math.floor(m / 60), r = m % 60;
    return h ? `${h} ч ${String(r).padStart(2, "0")} мин` : `${r} мин`;
  }

  function groupByDate(rows, renderRow) {
    let html = "";
    let cur = "";
    rows.forEach((r) => {
      if (r.date !== cur) {
        if (cur) html += `</div></div>`;
        cur = r.date;
        html += `<div class="group"><div class="group__h">${icon("cal")} ${esc(shortDate(r.date))}</div><div class="list">`;
      }
      html += renderRow(r);
    });
    if (cur) html += `</div></div>`;
    return html;
  }

  // ---- Явка ---------------------------------------------------------------
  function attView() {
    const d = state.att;
    const f = failed(true);
    if (f) return f;
    if (state.loading || !d) return monthNav() + NH.ui.skeletonList(4);
    let html = monthNav(d.month);
    if (!d.rows.length) return html + NH.ui.empty("За этот месяц отметок нет");
    const cards = d.rows.map((r) => {
      const chips = [
        `<span class="ctl-chip ok">✅ вовремя ${r.ok}</span>`,
        `<span class="ctl-chip ${r.late ? "warn" : ""}">⚠️ опозданий ${r.late}${r.late_min ? " · " + r.late_min + " мин" : ""}</span>`,
        `<span class="ctl-chip ${r.absent ? "bad" : ""}">❌ неявок ${r.absent}</span>`,
      ].join("");
      const avg = r.avg_arrival ? `<div class="pay-meta">выходов: ${r.days} · средний приход ${esc(r.avg_arrival)}</div>` : "";
      return `<div class="ctl-card"><div class="ctl-card__t">${icon("users")} ${esc(r.staff)}</div>
        <div class="ctl-chips">${chips}</div>${avg}</div>`;
    }).join("");
    html += `<div class="group"><div class="group__h">${icon("chart")} Итог по сотрудникам</div>${cards}</div>`;
    html += groupByDate(d.log, (r) => {
      let st, cls = "";
      if (r.status === "absent") { st = "неявка"; cls = "pen-red"; }
      else if (r.status === "late") { st = `${esc(r.time)} · опоздание ${r.late_minutes} мин`; cls = "ctl-warn"; }
      else st = `${esc(r.time)} · вовремя`;
      const mark = r.status === "absent" ? "❌" : r.status === "late" ? "⚠️" : "✅";
      return `<div class="li"><span class="pen-mark">${mark}</span>
        <span class="li__main"><div class="task__t">${esc(r.staff)}</div>
        <div class="task__meta"><span class="due ${cls}">${st}</span></div></span></div>`;
    });
    return html;
  }

  // ---- Уборки -------------------------------------------------------------
  function cleanView() {
    const d = state.clean;
    const f = failed(true);
    if (f) return f;
    if (state.loading || !d) return monthNav() + NH.ui.skeletonList(4);
    let html = monthNav(d.month);
    if (!d.total) {
      return html + NH.ui.empty("Отчётов «до/после» за этот месяц пока нет. Горничная присылает кружок + «до 103», а после уборки кружок + «103».");
    }
    html += `<div class="ctl-total">
      <div class="ctl-total__n">${d.total}</div>
      <div class="ctl-total__l">уборок за месяц${d.avg != null ? ` · в среднем ${dur(d.avg)}` : ""}${d.max != null ? ` · дольше всего ${dur(d.max)}` : ""}</div>
    </div>`;
    const staff = d.staff.map((r) => {
      const chips = [
        `<span class="ctl-chip">🧹 ${r.count}</span>`,
        r.avg != null ? `<span class="ctl-chip">⏱ ср. ${dur(r.avg)}</span>` : "",
        r.travel_avg != null ? `<span class="ctl-chip ${r.travel_avg > 60 ? "warn" : ""}">🚶 переход ср. ${dur(r.travel_avg)}</span>` : "",
        r.no_before ? `<span class="ctl-chip warn">без «до»: ${r.no_before}</span>` : "",
        r.forced ? `<span class="ctl-chip bad">не закрыто: ${r.forced}</span>` : "",
      ].join("");
      const rng = r.min != null ? `<div class="pay-meta">от ${dur(r.min)} до ${dur(r.max)}${r.travel_max != null ? ` · самый долгий переход ${dur(r.travel_max)}` : ""}</div>` : "";
      return `<div class="ctl-card"><div class="ctl-card__t">${icon("users")} ${esc(r.staff)}</div>
        <div class="ctl-chips">${chips}</div>${rng}</div>`;
    }).join("");
    html += `<div class="group"><div class="group__h">${icon("users")} По горничным</div>${staff}</div>`;
    const apts = d.apartments.map((a) => `<div class="li">
        <span class="aptbadge${NH.ui.badgeCls(a.apartment)}">${esc(a.apartment)}</span>
        <span class="li__main"><div class="task__meta"><span class="due">${a.count} уб.${a.min != null ? ` · ${dur(a.min)} – ${dur(a.max)}` : ""}</span></div></span>
        <span class="li__v">${a.avg != null ? dur(a.avg) : "—"}</span></div>`).join("");
    html += `<div class="group"><div class="group__h">${icon("home")} По квартирам <span class="count">среднее время</span></div><div class="list">${apts}</div></div>`;
    html += groupByDate(d.log, (r) => {
      let time, cls = "";
      if (r.open) { time = `начата ${esc(r.start)} · ещё идёт`; cls = "orange"; }
      else if (r.forced) { time = `${esc(r.start || "—")} → не закрыта горничной`; cls = "red"; }
      else if (r.no_before) { time = `${esc(r.end)} · без отчёта «до»`; cls = "orange"; }
      else time = `${esc(r.start)}–${esc(r.end)} · ${dur(r.duration_min)}`;
      const trav = r.travel_min != null
        ? `<span class="due ${r.travel_min > 60 ? "orange" : ""}">🚶 ${dur(r.travel_min)}</span>` : "";
      return `<div class="li"><span class="aptbadge${NH.ui.badgeCls(r.apartment)}">${esc(r.apartment)}</span>
        <span class="li__main"><div class="task__t">${esc(r.staff)}</div>
        <div class="task__meta"><span class="due ${cls}">${time}</span>${trav}</div></span></div>`;
    });
    return html;
  }

  // ---- Закупки ------------------------------------------------------------
  function buyView() {
    const d = state.buy;
    const f = failed(false);
    if (f) return f;
    if (state.loading || !d) return NH.ui.skeletonList(4);
    let html = `<div class="form-card">
      <div class="form-card__title">🛒 Добавить в список</div>
      <div class="form-card__sub">Горничные добавляют через бота: «нужно 103 полотенца 2, шампунь»</div>
      <div class="form-row2">
        <input id="sup-item" class="form-input" type="text" maxlength="80" placeholder="Что купить" />
        <input id="sup-qty" class="form-input" type="number" inputmode="numeric" min="1" placeholder="Кол-во" />
      </div>
      <input id="sup-apt" class="form-input" type="text" maxlength="20" placeholder="Квартира (необязательно)" />
      <button class="btn-primary" data-sup-add>Добавить</button>
    </div>`;
    if (!d.open.length) {
      html += NH.ui.empty("Список закупок пуст 👍");
    } else {
      const sum = d.summary.map((a) => `<div class="li">
          <span class="li__main"><div class="task__t">${esc(a.item)}${a.qty > 1 ? ` <b>×${a.qty}</b>` : ""}</div>
          ${a.apartments.length ? `<div class="task__meta"><span class="tag-apt">${esc(a.apartments.join(", "))}</span></div>` : ""}</span>
        </div>`).join("");
      html += `<div class="group"><div class="group__h">${icon("tasks")} Итого купить <span class="count">${d.open.length}</span></div><div class="list">${sum}</div></div>`;
      const rows = d.open.map((r) => `<div class="li">
          <button class="circle" data-sup-toggle data-id="${r.id}" data-bought="1" aria-label="Куплено"></button>
          <span class="li__main"><div class="task__t">${esc(r.item)}${r.qty > 1 ? ` ×${r.qty}` : ""}</div>
          <div class="task__meta">${r.apartment ? `<span class="tag-apt">${esc(r.apartment)}</span>` : ""}<span class="due">${esc(r.staff_name || "")} · ${esc(shortDate((r.created_at || "").slice(0, 10)))}</span></div></span>
          <button class="task-del" data-sup-del data-id="${r.id}" aria-label="Удалить">${icon("x")}</button>
        </div>`).join("");
      html += `<div class="group"><div class="group__h">${icon("tag")} Заявки <span class="count">отметьте купленное</span></div><div class="list">${rows}</div></div>`;
    }
    if (d.bought.length) {
      const rows = d.bought.map((r) => `<div class="li">
          <button class="circle c-done" data-sup-toggle data-id="${r.id}" data-bought="0" aria-label="Вернуть">${icon("check")}</button>
          <span class="li__main"><div class="task__t done">${esc(r.item)}${r.qty > 1 ? ` ×${r.qty}` : ""}</div>
          <div class="task__meta">${r.apartment ? `<span class="tag-apt">${esc(r.apartment)}</span>` : ""}<span class="due">куплено ${esc(shortDate((r.bought_at || "").slice(0, 10)))}</span></div></span>
          <button class="task-del" data-sup-del data-id="${r.id}" aria-label="Удалить">${icon("x")}</button>
        </div>`).join("");
      html += `<details class="task-done-wrap"><summary class="group__h" style="padding-left:22px">${icon("check")} Куплено <span class="count">${d.bought.length}</span></summary>
        <div class="group" style="margin-top:6px"><div class="list">${rows}</div></div></details>`;
    }
    return html;
  }

  function skeleton() {
    return NH.ui.skeletonList(5);
  }

  function render() {
    let body;
    if (state.view === "clean") body = cleanView();
    else if (state.view === "buy") body = buyView();
    else body = attView();
    return segments() + body;
  }

  NH.screens = NH.screens || {};
  NH.screens.control = { render, skeleton, state, ym };
})();
