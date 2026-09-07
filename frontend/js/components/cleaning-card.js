/* Cleaning card with status actions (iOS style). */
(function () {
  const NH = (window.NH = window.NH || {});
  const { esc, shortDate } = NH.ui;

  function cleaningCard(c, editable) {
    const done = c.status === "done";
    const inProgress = c.status === "in_progress";
    const statusPill = done
      ? '<span class="status-pill sp-green">Готово</span>'
      : inProgress
      ? '<span class="status-pill sp-orange">Начата</span>'
      : '<span class="status-pill sp-muted">Ожидает</span>';

    const dep = c.departure_time ? `выезд ${esc(c.departure_time)}` : "выезд";
    let nextInfo = "нет заезда";
    if (c.next_checkin) {
      const sameDay = c.next_checkin === c.cleaning_date;
      nextInfo = sameDay
        ? `заезд ${esc(c.next_arrival_time || "—")}`
        : `заезд ${shortDate(c.next_checkin)}`;
    }
    const win = c.window_hours != null ? ` · окно ${c.window_hours} ч` : "";

    // who is cleaning / cleaned (from the до/после reports in the bot)
    let who = "";
    if (c.cleaner && inProgress && c.started_at) {
      who = `<div class="clean-line clean-who">🟡 убирает ${esc(c.cleaner)} с ${esc(c.started_at)}</div>`;
    } else if (c.cleaner && done) {
      const t = c.started_at && c.finished_at
        ? `${esc(c.started_at)}–${esc(c.finished_at)}${c.duration_min != null ? " · " + fmtDur(c.duration_min) : ""}`
        : (c.finished_at ? `в ${esc(c.finished_at)}` : "");
      who = `<div class="clean-line clean-who">✅ ${esc(c.cleaner)}${t ? " · " + t : ""}</div>`;
    }

    return `
      <div class="clean-card list" data-apt="${esc(c.apartment)}" data-date="${esc(c.cleaning_date)}">
        <div class="li">
          <span class="aptbadge">${esc(c.apartment)}</span>
          <div class="li__main"></div>
          ${statusPill}
        </div>
        <div class="clean-line">${dep} → ${nextInfo}${win}</div>${who}
      </div>`;
  }

  function fmtDur(m) {
    const h = Math.floor(m / 60), r = m % 60;
    return h ? `${h} ч ${String(r).padStart(2, "0")} мин` : `${r} мин`;
  }

  NH.cleaningCard = { cleaningCard };
})();
