/* "Today" screen (also reused by "Tomorrow"). */
(function () {
  const NH = (window.NH = window.NH || {});
  const { empty, esc, icon, skeletonList } = NH.ui;

  function skeleton() {
    return `<div class="skeleton-metrics"><div class="skeleton"></div><div class="skeleton"></div>
      <div class="skeleton"></div><div class="skeleton"></div></div>
      <div style="height:8px"></div>${skeletonList(3)}`;
  }

  function checkoutRow(c) {
    return `
      <div class="li">
        <span class="aptbadge">${esc(c.apartment)}</span>
        <div class="li__main"><div class="li__t">${esc(c.client_name || "—")}</div></div>
        <div class="li__s">${esc(c.departure_time || "")}</div>
      </div>`;
  }

  function render(data, opts) {
    opts = opts || {};
    const { metricsRow, financeBlock } = NH.metrics;
    const { checkinCard, debtorRow, newBookingRow } = NH.bookingCard;
    const { cleaningCard } = NH.cleaningCard;

    const when = opts.dayWord || "сегодня";
    const shortD = (data.date_human || "").split(",")[0].trim();
    const tag = shortD ? ` <span class="gdate">${esc(shortD)}</span>` : "";

    let html = metricsRow(data.stats);
    html += financeBlock(data.finance);

    // Check-ins
    html += `<div class="group"><div class="group__h">${icon("key")} Заезды ${when}${tag}</div>`;
    html += data.checkins.length
      ? `<div class="list">${data.checkins.map(checkinCard).join("")}</div>`
      : empty("Нет заездов");
    html += `</div>`;

    // Check-outs + cleaning
    html += `<div class="group"><div class="group__h">${icon("clean")} Выезды и уборки ${when}${tag}</div>`;
    if (data.cleanings && data.cleanings.length) {
      // "today" screen → today's cleanings are editable; "tomorrow" → read-only
      const editable = when !== "завтра";
      html += data.cleanings.map((c) => cleaningCard(c, editable)).join("");
    } else if (data.checkouts && data.checkouts.length) {
      html += `<div class="list">${data.checkouts.map(checkoutRow).join("")}</div>`;
    } else {
      html += empty("Нет выездов");
    }
    html += `</div>`;

    // Debtors (owner only, not on Tomorrow)
    if (!opts.hideDebtors) {
      html += `<div class="group owner-only"><div class="group__h">${icon("users")} Должники сейчас</div>`;
      html += data.debtors && data.debtors.length
        ? `<div class="list">${data.debtors.map(debtorRow).join("")}</div>`
        : empty("Должников нет");
      html += `</div>`;
    }

    // New bookings 24h
    if (data.new_bookings_24h && data.new_bookings_24h.length) {
      html += `<div class="group"><div class="group__h">${icon("cal")} Новые брони · 24 ч</div>`;
      html += `<div class="list">${data.new_bookings_24h.map(newBookingRow).join("")}</div></div>`;
    }

    return html;
  }

  NH.screens = NH.screens || {};
  NH.screens.today = { render, skeleton };
})();
