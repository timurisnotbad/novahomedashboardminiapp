/* "Guests" screen — owner only. Debtors + all current guests. */
(function () {
  const NH = (window.NH = window.NH || {});
  const { empty, icon, skeletonList } = NH.ui;

  function skeleton() {
    return `<div class="group"><div class="group__h">Должники</div></div>${skeletonList(4)}`;
  }

  function render(data) {
    const { debtorRow, guestRow } = NH.bookingCard;

    let html = `<div class="group"><div class="group__h">${icon("users")} Должники <span class="count">${data.debtors.length}</span></div>`;
    html += data.debtors.length
      ? `<div class="list">${data.debtors.map(debtorRow).join("")}</div>`
      : empty("Должников нет");
    html += `</div>`;

    html += `<div class="group"><div class="group__h">${icon("home")} Гости сейчас <span class="count">${data.current_count}</span></div>`;
    html += data.guests.length
      ? `<div class="list">${data.guests.map(guestRow).join("")}</div>`
      : empty("Сейчас нет гостей");
    html += `</div>`;

    return html;
  }

  NH.screens = NH.screens || {};
  NH.screens.guests = { render, skeleton };
})();
