/* "Cleaning" screen — today + upcoming, available to everyone. */
(function () {
  const NH = (window.NH = window.NH || {});
  const { empty, icon, shortDate, skeletonList } = NH.ui;

  function skeleton() {
    return `<div class="group"><div class="group__h">Сегодня</div></div>${skeletonList(2)}`;
  }

  function render(data) {
    const { cleaningCard } = NH.cleaningCard;
    const cleanings = data.cleanings || [];
    const todayStr = NH.ui.localIso();

    const todays = cleanings.filter((c) => c.cleaning_date === todayStr);
    const rest = cleanings.filter((c) => c.cleaning_date !== todayStr);

    let html = `<div class="group"><div class="group__h">${icon("clean")} Сегодня <span class="count">${todays.length}</span></div>`;
    html += todays.length ? todays.map((c) => cleaningCard(c, true)).join("") : empty("На сегодня уборок нет");
    html += `</div>`;

    const byDate = {};
    rest.forEach((c) => (byDate[c.cleaning_date] = byDate[c.cleaning_date] || []).push(c));
    Object.keys(byDate)
      .sort()
      .forEach((date) => {
        html += `<div class="group"><div class="group__h">${icon("cal")} ${shortDate(date)} <span class="count">${byDate[date].length}</span></div>`;
        html += byDate[date].map((c) => cleaningCard(c, false)).join(""); // future → read-only
        html += `</div>`;
      });

    return html;
  }

  NH.screens = NH.screens || {};
  NH.screens.cleaning = { render, skeleton };
})();
