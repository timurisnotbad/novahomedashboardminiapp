/* Custom date & time pickers — brand-styled bottom sheets (no native inputs). */
(function () {
  const NH = (window.NH = window.NH || {});
  const MONTHS = ["Январь","Февраль","Март","Апрель","Май","Июнь","Июль","Август","Сентябрь","Октябрь","Ноябрь","Декабрь"];
  const WD = ["Пн","Вт","Ср","Чт","Пт","Сб","Вс"];
  const pad = (n) => String(n).padStart(2, "0");

  function overlay(inner) {
    const o = document.createElement("div");
    o.className = "pk-overlay";
    o.innerHTML = `<div class="pk-backdrop"></div><div class="pk-card">${inner}</div>`;
    document.body.appendChild(o);
    requestAnimationFrame(() => o.classList.add("show"));
    return o;
  }
  function close(o) {
    o.classList.remove("show");
    setTimeout(() => o.remove(), 200);
  }
  function haptic() {
    try { NH.ui && NH.ui.haptic && NH.ui.haptic("light"); } catch (e) {}
  }

  function openDate(initial, onPick) {
    const view = initial ? new Date(initial + "T00:00:00") : new Date();
    view.setDate(1);
    const o = overlay('<div class="pk"></div>');
    const pk = o.querySelector(".pk");
    const todayIso = NH.ui.localIso();

    function render() {
      const y = view.getFullYear(), m = view.getMonth();
      const first = (new Date(y, m, 1).getDay() + 6) % 7; // Mon = 0
      const days = new Date(y, m + 1, 0).getDate();
      let cells = "";
      for (let i = 0; i < first; i++) cells += '<span class="pk-day pk-empty"></span>';
      for (let d = 1; d <= days; d++) {
        const iso = `${y}-${pad(m + 1)}-${pad(d)}`;
        cells += `<button class="pk-day ${initial === iso ? "is-sel" : ""} ${iso === todayIso ? "is-today" : ""}" data-iso="${iso}">${d}</button>`;
      }
      pk.innerHTML = `
        <div class="pk-grab"></div>
        <div class="pk-head">
          <button class="pk-nav" data-prev aria-label="Назад">‹</button>
          <div class="pk-title">${MONTHS[m]} ${y}</div>
          <button class="pk-nav" data-next aria-label="Вперёд">›</button>
        </div>
        <div class="pk-wd">${WD.map((w) => `<span>${w}</span>`).join("")}</div>
        <div class="pk-grid">${cells}</div>
        <div class="pk-actions">
          <button class="pk-btn" data-clear>Очистить</button>
          <button class="pk-btn pk-btn--p" data-today>Сегодня</button>
        </div>`;
    }
    render();

    o.addEventListener("click", (e) => {
      if (e.target.closest(".pk-backdrop")) return close(o);
      if (e.target.closest("[data-prev]")) { view.setMonth(view.getMonth() - 1); return render(); }
      if (e.target.closest("[data-next]")) { view.setMonth(view.getMonth() + 1); return render(); }
      if (e.target.closest("[data-today]")) { haptic(); onPick(todayIso); return close(o); }
      if (e.target.closest("[data-clear]")) { onPick(""); return close(o); }
      const day = e.target.closest(".pk-day[data-iso]");
      if (day) { haptic(); onPick(day.dataset.iso); close(o); }
    });
  }

  function openTime(initial, onPick) {
    let hh = initial ? parseInt(initial.split(":")[0], 10) : 9;
    let mm = initial ? parseInt(initial.split(":")[1], 10) : 0;
    if (isNaN(hh)) hh = 9;
    if (isNaN(mm)) mm = 0;
    mm = (Math.round(mm / 5) * 5) % 60;
    const o = overlay('<div class="pk pk--time"></div>');
    const pk = o.querySelector(".pk");

    const hours = Array.from({ length: 24 }, (_, i) => i);
    const mins = Array.from({ length: 12 }, (_, i) => i * 5);
    pk.innerHTML = `
      <div class="pk-grab"></div>
      <div class="pk-title center">Время</div>
      <div class="pk-time">
        <div class="pk-col" data-col="h">${hours.map((h) => `<button class="pk-cell ${h === hh ? "is-sel" : ""}" data-h="${h}">${pad(h)}</button>`).join("")}</div>
        <div class="pk-colon">:</div>
        <div class="pk-col" data-col="m">${mins.map((x) => `<button class="pk-cell ${x === mm ? "is-sel" : ""}" data-m="${x}">${pad(x)}</button>`).join("")}</div>
      </div>
      <div class="pk-actions">
        <button class="pk-btn" data-clear>Убрать</button>
        <button class="pk-btn pk-btn--p" data-set>Готово</button>
      </div>`;

    function centerSel() {
      pk.querySelectorAll(".pk-col").forEach((col) => {
        const s = col.querySelector(".is-sel");
        if (s) col.scrollTop = s.offsetTop - col.clientHeight / 2 + s.offsetHeight / 2;
      });
    }
    requestAnimationFrame(centerSel);

    o.addEventListener("click", (e) => {
      if (e.target.closest(".pk-backdrop")) return close(o);
      const h = e.target.closest("[data-h]");
      if (h) { hh = +h.dataset.h; mark(); return; }
      const m = e.target.closest("[data-m]");
      if (m) { mm = +m.dataset.m; mark(); return; }
      if (e.target.closest("[data-clear]")) { onPick(""); return close(o); }
      if (e.target.closest("[data-set]")) { onPick(`${pad(hh)}:${pad(mm)}`); return close(o); }
    });
    function mark() {
      haptic();
      pk.querySelectorAll("[data-h]").forEach((b) => b.classList.toggle("is-sel", +b.dataset.h === hh));
      pk.querySelectorAll("[data-m]").forEach((b) => b.classList.toggle("is-sel", +b.dataset.m === mm));
    }
  }

  NH.picker = { openDate, openTime };
})();
