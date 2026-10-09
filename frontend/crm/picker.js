/* iOS-style wheel pickers for date / time / date+time / month inputs.
   Every <input type="date|time|datetime-local|month"> on the page is replaced by
   a button that opens a wheel picker; the original input stays in the DOM (hidden)
   and keeps its ISO value, so val(id), .value and "change" listeners keep working. */
(() => {
  const MONTHS = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"];
  const MONTHS_FULL = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"];
  const WD = ["вс", "пн", "вт", "ср", "чт", "пт", "сб"];
  const KINDS = { date: 1, time: 1, "datetime-local": 1, month: 1 };
  const pad = (n) => String(n).padStart(2, "0");
  const iso = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const ITEM = 36;

  function label(kind, v) {
    if (!v) return "";
    if (kind === "time") return v.slice(0, 5);
    if (kind === "month") { const [y, m] = v.split("-").map(Number); return `${MONTHS_FULL[m - 1]} ${y}`; }
    const d = new Date(v.length === 10 ? v + "T00:00" : v);
    if (isNaN(d)) return v;
    const ds = `${WD[d.getDay()]} ${d.getDate()} ${MONTHS[d.getMonth()]} ${d.getFullYear() === new Date().getFullYear() ? "" : d.getFullYear()}`.trim();
    return kind === "date" ? ds : `${ds}, ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }

  // ---- enhance inputs ---------------------------------------------------------
  function enhance(root) {
    (root.querySelectorAll ? root.querySelectorAll("input") : []).forEach((inp) => {
      const kind = inp.getAttribute("type");
      if (!KINDS[kind] || inp.dataset.pick) return;
      inp.dataset.pick = kind;
      inp.type = "hidden";
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = (inp.className || "field") + " pick";
      btn.setAttribute("style", inp.getAttribute("style") || "");
      btn.dataset.kind = kind;
      btn.title = inp.title || "";
      inp.after(btn);
      const paint = () => { const l = label(kind, inp.value); btn.innerHTML = `<span class="pick__ic">${kind === "time" ? "🕐" : "📅"}</span><span class="${l ? "" : "pick__ph"}">${l || (inp.placeholder || (kind === "time" ? "Время" : kind === "month" ? "Месяц" : kind === "date" ? "Дата" : "Дата и время"))}</span>`; };
      paint();
      inp.addEventListener("change", paint);
      inp.addEventListener("nh-paint", paint);
      btn.addEventListener("click", () => open(inp, btn));
    });
  }

  // ---- wheel --------------------------------------------------------------------
  function wheel(items, selectedIndex, onChange) {
    const el = document.createElement("div");
    el.className = "wheel";
    el.innerHTML = `<div class="wheel__pad"></div>${items.map((it, i) => `<div class="wheel__item" data-i="${i}">${it.label}</div>`).join("")}<div class="wheel__pad"></div>`;
    let idx = Math.max(0, Math.min(items.length - 1, selectedIndex));
    let t = null;
    const mark = () => el.querySelectorAll(".wheel__item").forEach((n, i) => n.classList.toggle("is-sel", i === idx));
    el.addEventListener("scroll", () => {
      clearTimeout(t);
      const i = Math.round(el.scrollTop / ITEM);
      if (i !== idx && i >= 0 && i < items.length) { idx = i; mark(); onChange(items[idx].value, idx); }
      t = setTimeout(() => { el.scrollTo({ top: idx * ITEM, behavior: "smooth" }); }, 90);
    });
    el.addEventListener("click", (e) => { const n = e.target.closest(".wheel__item"); if (n) el.scrollTo({ top: parseInt(n.dataset.i, 10) * ITEM, behavior: "smooth" }); });
    el.goto = (i, instant) => { idx = Math.max(0, Math.min(items.length - 1, i)); mark(); el.scrollTo({ top: idx * ITEM, behavior: instant ? "auto" : "smooth" }); };
    requestAnimationFrame(() => el.goto(idx, true));
    mark();
    return el;
  }

  let openEl = null;
  function close() { if (openEl) { openEl.remove(); openEl = null; document.removeEventListener("keydown", onKey); } }
  function onKey(e) { if (e.key === "Escape") close(); }

  function open(inp, btn) {
    close();
    const kind = inp.dataset.pick;
    const now = new Date();
    let cur = inp.value ? new Date(kind === "time" ? `2000-01-01T${inp.value}` : kind === "month" ? inp.value + "-01T00:00" : (inp.value.length === 10 ? inp.value + "T00:00" : inp.value)) : null;
    if (!cur || isNaN(cur)) cur = new Date(now.getFullYear(), now.getMonth(), now.getDate(), kind === "time" || kind === "datetime-local" ? now.getHours() : 0, kind === "time" || kind === "datetime-local" ? Math.round(now.getMinutes() / 5) * 5 : 0);
    const state = { y: cur.getFullYear(), m: cur.getMonth(), d: cur.getDate(), h: cur.getHours(), mi: cur.getMinutes() };

    const box = document.createElement("div");
    box.className = "pickpop";
    box.innerHTML = `<div class="pickpop__back"></div><div class="pickpop__card"><div class="pickpop__head"><button type="button" class="pickpop__btn" data-act="clear">Очистить</button><b class="pickpop__title"></b><button type="button" class="pickpop__btn primary" data-act="ok">Готово</button></div><div class="pickpop__wheels"><div class="pickpop__bar"></div></div><div class="pickpop__foot"><button type="button" class="pickpop__btn" data-act="now">${kind === "time" ? "Сейчас" : "Сегодня"}</button></div></div>`;
    const wheels = box.querySelector(".pickpop__wheels");
    const title = box.querySelector(".pickpop__title");
    const value = () => kind === "time" ? `${pad(state.h)}:${pad(state.mi)}` : kind === "month" ? `${state.y}-${pad(state.m + 1)}` : kind === "date" ? `${state.y}-${pad(state.m + 1)}-${pad(state.d)}` : `${state.y}-${pad(state.m + 1)}-${pad(state.d)}T${pad(state.h)}:${pad(state.mi)}`;
    const paintTitle = () => { title.textContent = label(kind, value()); };
    const dim = (y, m) => new Date(y, m + 1, 0).getDate();

    const cols = [];
    if (kind === "datetime-local") {
      // one combined day column (±1 year), like iOS "date and time"
      const base = new Date(now.getFullYear(), now.getMonth(), now.getDate());
      const days = []; for (let i = -400; i <= 400; i++) { const d = new Date(base); d.setDate(base.getDate() + i); days.push({ value: d, label: i === 0 ? "Сегодня" : `${WD[d.getDay()]} ${d.getDate()} ${MONTHS[d.getMonth()]}${d.getFullYear() !== now.getFullYear() ? " " + d.getFullYear() : ""}` }); }
      const curIdx = Math.round((new Date(state.y, state.m, state.d) - base) / 86400000) + 400;
      cols.push(wheel(days, curIdx, (d) => { state.y = d.getFullYear(); state.m = d.getMonth(); state.d = d.getDate(); paintTitle(); }));
    } else if (kind === "date") {
      const dayW = wheel(Array.from({ length: 31 }, (_, i) => ({ value: i + 1, label: String(i + 1) })), state.d - 1, (v) => { state.d = v; paintTitle(); });
      const monW = wheel(MONTHS_FULL.map((n, i) => ({ value: i, label: n })), state.m, (v) => { state.m = v; state.d = Math.min(state.d, dim(state.y, state.m)); paintTitle(); });
      const years = []; for (let y = now.getFullYear() - 3; y <= now.getFullYear() + 3; y++) years.push({ value: y, label: String(y) });
      const yrW = wheel(years, years.findIndex((x) => x.value === state.y), (v) => { state.y = v; paintTitle(); });
      cols.push(dayW, monW, yrW);
    } else if (kind === "month") {
      const monW = wheel(MONTHS_FULL.map((n, i) => ({ value: i, label: n })), state.m, (v) => { state.m = v; paintTitle(); });
      const years = []; for (let y = now.getFullYear() - 3; y <= now.getFullYear() + 3; y++) years.push({ value: y, label: String(y) });
      const yrW = wheel(years, years.findIndex((x) => x.value === state.y), (v) => { state.y = v; paintTitle(); });
      cols.push(monW, yrW);
    }
    if (kind === "time" || kind === "datetime-local") {
      cols.push(wheel(Array.from({ length: 24 }, (_, i) => ({ value: i, label: pad(i) })), state.h, (v) => { state.h = v; paintTitle(); }));
      const mins = []; for (let i = 0; i < 60; i += 5) mins.push({ value: i, label: pad(i) });
      if (state.mi % 5) mins.push({ value: state.mi, label: pad(state.mi) }), mins.sort((a, b) => a.value - b.value);
      cols.push(wheel(mins, mins.findIndex((x) => x.value === state.mi), (v) => { state.mi = v; paintTitle(); }));
    }
    cols.forEach((c) => wheels.appendChild(c));
    if (kind === "datetime-local") cols[0].classList.add("wide");
    paintTitle();

    const commit = (v) => { inp.value = v; inp.dispatchEvent(new Event("change", { bubbles: true })); inp.dispatchEvent(new Event("input", { bubbles: true })); close(); };
    box.addEventListener("click", (e) => {
      if (e.target.classList.contains("pickpop__back")) { close(); return; }
      const a = e.target.closest("[data-act]"); if (!a) return;
      if (a.dataset.act === "ok") commit(value());
      else if (a.dataset.act === "clear") commit("");
      else if (a.dataset.act === "now") { const n = new Date(); commit(kind === "time" ? `${pad(n.getHours())}:${pad(Math.round(n.getMinutes() / 5) * 5 % 60)}` : kind === "month" ? `${n.getFullYear()}-${pad(n.getMonth() + 1)}` : kind === "date" ? iso(n) : `${iso(n)}T${pad(n.getHours())}:${pad(n.getMinutes())}`); }
    });
    document.body.appendChild(box);
    openEl = box;
    document.addEventListener("keydown", onKey);
    // desktop: anchor under the button; phones: bottom sheet (CSS)
    const card = box.querySelector(".pickpop__card");
    if (window.innerWidth > 760) {
      const r = btn.getBoundingClientRect();
      const w = card.offsetWidth, h = card.offsetHeight;
      let left = Math.min(r.left, window.innerWidth - w - 12), top = r.bottom + 6;
      if (top + h > window.innerHeight - 8) top = Math.max(8, r.top - h - 6);
      card.style.left = Math.max(8, left) + "px"; card.style.top = top + "px";
    }
  }

  const mo = new MutationObserver((muts) => muts.forEach((m) => m.addedNodes.forEach((n) => { if (n.nodeType === 1) enhance(n); })));
  document.addEventListener("DOMContentLoaded", () => { enhance(document); mo.observe(document.body, { childList: true, subtree: true }); });
  if (document.readyState !== "loading") { enhance(document); mo.observe(document.body, { childList: true, subtree: true }); }
  window.NHPicker = { enhance, close, label };
})();
