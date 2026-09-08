/* "Цены" screen — Booking.com price monitor (our object vs competitors). */
(function () {
  const NH = (window.NH = window.NH || {});
  const { esc, icon } = NH.ui;

  function isoPlus(days) {
    const d = new Date();
    d.setDate(d.getDate() + days);
    return NH.ui.localIso(d);
  }

  // internal state persists while navigating between tabs
  const state = {
    loading: false,
    error: null,
    data: null,
    checkin: isoPlus(1),
    checkout: isoPlus(2),
  };

  function skeleton() {
    return `<div class="skeleton" style="height:150px;border-radius:16px"></div>`;
  }

  function nightsWord(n) {
    n = Math.abs(n);
    if (n % 10 === 1 && n % 100 !== 11) return n + " ночь";
    if (n % 10 >= 2 && n % 10 <= 4 && !(n % 100 >= 12 && n % 100 <= 14)) return n + " ночи";
    return n + " ночей";
  }

  function form() {
    return `
      <div class="group"><div class="group__h">${icon("cal")} Даты проверки</div>
        <div class="prices-form">
          <div class="prices-dates">
            <button type="button" class="pk-field" id="price-checkin" data-pick="date" data-value="${esc(state.checkin)}">
              ${icon("cal")}<span class="pk-field__v">${NH.ui.shortDate(state.checkin)}</span>
            </button>
            <span class="prices-arrow">${icon("chev")}</span>
            <button type="button" class="pk-field" id="price-checkout" data-pick="date" data-value="${esc(state.checkout)}">
              ${icon("cal")}<span class="pk-field__v">${NH.ui.shortDate(state.checkout)}</span>
            </button>
          </div>
          <button class="btn-primary prices-go" data-prices-fetch ${state.loading ? "disabled" : ""}>
            ${state.loading ? "⏳ Собираю цены…" : "Проверить цены на Booking.com"}
          </button>
        </div>
      </div>`;
  }

  function priceRow(name, price, badge, cmp, delta) {
    const left = badge
      ? `<span class="aptbadge${NH.ui.badgeCls(name)}">${esc(name)}</span>`
      : `<span class="li__main"><div class="li__t">${esc(name)}</div></span>`;
    let cmpHtml = "";
    if (cmp === "low") cmpHtml = `<span class="price-cmp low">−${Math.abs(delta)}$</span>`;
    else if (cmp === "high") cmpHtml = `<span class="price-cmp high">+${delta}$</span>`;
    else if (cmp === "mid") cmpHtml = `<span class="price-cmp mid">≈</span>`;
    const cls = cmp ? ` price-${cmp}` : "";
    return `<div class="li">${left}${cmpHtml}<span class="price-val${cls}">${esc(price)}</span></div>`;
  }

  function results() {
    if (state.loading) {
      return `<div class="prices-loading">
        <div class="ptr-spinner" style="margin:0 auto 12px"></div>
        Собираю цены с Booking.com…<br><span class="muted">Это занимает 30–90 секунд</span>
      </div>`;
    }
    if (state.error) {
      if (state.error === "playwright_not_installed") {
        return NH.ui.empty("Модуль цен не установлен на сервере. Выполните: pip install playwright && playwright install chromium");
      }
      return NH.ui.empty("Не удалось получить цены. Попробуйте ещё раз через минуту.");
    }
    const d = state.data;
    if (!d) {
      return `<div class="prices-hint">${icon("chart")}<div>Выберите даты и нажмите «Проверить цены».<br>Сравним наши цены с конкурентами в Nest One.</div></div>`;
    }

    let html = `<div class="prices-range">${NH.ui.shortDate(d.checkin)} → ${NH.ui.shortDate(d.checkout)} · ${nightsWord(d.nights)}${d.cached ? " · из кэша" : ""}</div>`;

    // summary vs cheapest competitor
    if (d.comp_min) {
      html += `<div class="prices-summary">
        <div class="prices-summary__row"><span>Самый дешёвый конкурент</span><b>US$${d.comp_min}</b></div>
        <div class="prices-summary__row"><span>Наших дешевле него</span><b>${d.our_below} из ${d.our_count}</b></div>
        <div class="prices-summary__legend">
          <span class="lg low">дешевле</span><span class="lg mid">наравне</span><span class="lg high">дороже</span>
        </div>
      </div>`;
    }

    // ours
    html += `<div class="group"><div class="group__h">${icon("home")} Nova Home (наши)</div><div class="list">`;
    if (d.ours && d.ours.length) {
      html += d.ours.map((r) => priceRow(r.name, r.price, true, r.cmp, r.delta)).join("");
    } else {
      html += `<div class="li"><span class="li__main"><div class="li__t muted">Цены не найдены</div></span></div>`;
    }
    html += `</div></div>`;

    // competitors
    (d.competitors || []).forEach((c) => {
      html += `<div class="group"><div class="group__h">🏢 ${esc(c.name)}</div><div class="list">`;
      if (c.error) {
        html += `<div class="li"><span class="li__main"><div class="li__t muted">нет данных (ошибка загрузки)</div></span></div>`;
      } else if (c.rooms && c.rooms.length) {
        html += c.rooms.map((r) => priceRow(r.name, r.price, false)).join("");
      } else {
        html += `<div class="li"><span class="li__main"><div class="li__t muted">нет доступных номеров</div></span></div>`;
      }
      html += `</div></div>`;
    });
    return html;
  }

  function render() {
    return form() + results();
  }

  function setDates(ci, co) {
    if (ci) state.checkin = ci;
    if (co) state.checkout = co;
    if (state.checkout <= state.checkin) {
      const d = new Date(state.checkin + "T00:00:00");
      d.setDate(d.getDate() + 1);
      state.checkout = NH.ui.localIso(d);
    }
  }

  // sets loading synchronously, then resolves after the scrape
  function fetchNow(ci, co) {
    setDates(ci, co);
    state.loading = true;
    state.error = null;
    return NH.api
      .getPrices(state.checkin, state.checkout, 1)
      .then((data) => {
        if (data && data.ok) state.data = data;
        else state.error = (data && data.error) || "error";
      })
      .catch(() => {
        state.error = "error";
      })
      .finally(() => {
        state.loading = false;
      });
  }

  NH.screens = NH.screens || {};
  NH.screens.prices = { render, skeleton, state, setDates, fetchNow, load: () => state };
})();
