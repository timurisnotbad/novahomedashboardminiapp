/* Stat cards (2x2) + finance grouped list. */
(function () {
  const NH = (window.NH = window.NH || {});
  const { esc, usd, icon } = NH.ui;

  function statCard(iconName, color, value, label, extra) {
    return `
      <div class="stat" data-metric="${label}">
        <div class="stat__top">
          <div class="stat__ic" style="background:${color}">${icon(iconName)}</div>
          <div class="stat__lab">${esc(label)}</div>
          ${extra && extra.pct != null ? `<span class="pct">${extra.pct}%</span>` : ""}
        </div>
        <div class="stat__num">${value}</div>
        ${extra && extra.bar != null ? `<div class="bar"><i style="width:${extra.bar}%"></i></div>` : ""}
      </div>`;
  }

  function metricsRow(stats) {
    const pct = Math.round(stats.occupancy_pct || 0);
    return `
      <div class="stats">
        ${statCard("key", "var(--tint)", stats.checkins_today, "Заезды")}
        ${statCard("door", "var(--orange)", stats.checkouts_today, "Выезды")}
        ${statCard("clean", "var(--purple)", stats.cleanings_today, "Уборки")}
        ${statCard(
          "chart", "var(--green)",
          `${stats.occupied}<small> / ${stats.total}</small>`,
          "Занятость", { pct, bar: pct }
        )}
      </div>`;
  }

  function financeBlock(finance) {
    return `
      <div class="group owner-only">
        <div class="group__h">${icon("wallet")} Финансы</div>
        <div class="list">
          <div class="li">
            <div class="li__main"><div class="li__t">Ожидается сегодня</div></div>
            <div class="li__v v-green">${usd(finance.expected_today_usd)}</div>
          </div>
          <div class="li li--tap" data-metric="debtors">
            <div class="li__main"><div class="li__t">Должники</div><div class="li__s">${finance.debtors_count} гостей</div></div>
            <div class="li__v v-red">${usd(finance.debtors_total_usd)}</div>
            <div class="li__chev">${icon("chev")}</div>
          </div>
        </div>
      </div>`;
  }

  NH.metrics = { metricsRow, financeBlock };
})();
