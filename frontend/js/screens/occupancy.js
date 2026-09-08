/* "Week" screen — occupancy matrix. */
(function () {
  const NH = (window.NH = window.NH || {});
  const { esc } = NH.ui;

  function skeleton() {
    return `<div class="skeleton" style="height:32px"></div>${NH.ui.skeletonList(6)}`;
  }

  function render(data) {
    const cols = data.dates.length;

    let header = `<div class="occ-row occ-row--header" style="--cols:${cols}"><div class="occ-cell occ-name"></div>`;
    data.day_numbers.forEach((num, i) => {
      header += `<div class="occ-cell"><div>${esc(data.weekdays[i])}</div><div class="occ-dow">${num}</div></div>`;
    });
    header += `</div>`;

    // per-day occupancy under the dates: "45%" for each column
    const perDay = data.per_day || [];
    let pctRow = "";
    if (perDay.length === cols) {
      pctRow = `<div class="occ-row occ-row--pct" style="--cols:${cols}"><div class="occ-cell occ-name">%</div>`;
      perDay.forEach((p) => {
        const cls = p.pct >= 70 ? "hi" : p.pct >= 40 ? "mid" : "lo";
        pctRow += `<div class="occ-cell"><span class="occ-pct ${cls}">${p.pct}</span></div>`;
      });
      pctRow += `</div>`;
    }

    const t = data.today;
    const todayTile = t
      ? `<div class="occ-tile">
           <div class="occ-tile__l">Сегодня</div>
           <div class="occ-tile__v">${t.pct}%</div>
           <div class="occ-tile__s">${t.occupied} из ${t.total} квартир</div>
         </div>`
      : "";

    let rows = "";
    data.apartments.forEach((apt) => {
      const days = apt.days;
      let cells = "";
      let i = 0;
      while (i < cols) {
        const d = days[i];
        if (d.status === "occupied") {
          // merge consecutive days of the SAME stay into one island
          let j = i + 1;
          while (j < cols && days[j].status === "occupied" &&
                 days[j].checkin === d.checkin && days[j].checkout === d.checkout) j++;
          const len = j - i;
          const payload = encodeURIComponent(JSON.stringify({ ...d, apartment: apt.name }));
          cells += `<div class="occ-seg" style="grid-column:${2 + i}/span ${len}" data-cell="${payload}"></div>`;
          i = j;
        } else {
          cells += `<div class="occ-free" style="grid-column:${2 + i}"></div>`;
          i++;
        }
      }
      rows += `<div class="occ-row" style="--cols:${cols}"><div class="occ-cell occ-name">${esc(apt.name)}</div>${cells}</div>`;
    });

    return `
      <div class="occ-tiles">
        ${todayTile}
        <div class="occ-tile occ-tile--week">
          <div class="occ-tile__l">Неделя</div>
          <div class="occ-tile__v">${data.occupancy_pct}%</div>
          <div class="occ-tile__s">среднее за ${cols} дн.</div>
        </div>
      </div>
      <div class="occ-scroll"><div class="occ-grid">${header}${pctRow}${rows}</div></div>
      <div class="occ-legend">
        <span><i class="occ-swatch occ-swatch--on"></i> Занято</span>
        <span><i class="occ-swatch occ-swatch--off"></i> Свободно</span>
      </div>`;
  }

  NH.screens = NH.screens || {};
  NH.screens.occupancy = { render, skeleton };
})();
