/* Shared helpers of the CRM shell and the chats page (one copy instead of two):
   escaping, dates, the template-variable filler and the fetch wrapper. */
(function () {
  "use strict";
  const NH = (window.NH = window.NH || {});
  const C = (NH.common = NH.common || {});

  C.VERSION = window.NH_VERSION || NH.APP_VERSION || "";
  C.esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  C.pad = (n) => String(n).padStart(2, "0");
  C.iso = (d) => `${d.getFullYear()}-${C.pad(d.getMonth() + 1)}-${C.pad(d.getDate())}`;
  C.todayIso = () => C.iso(new Date());
  // local «now» as YYYY-MM-DDTHH:MM — task deadlines are local, toISOString() is UTC
  C.nowLocal = () => { const d = new Date(); return `${C.iso(d)}T${C.pad(d.getHours())}:${C.pad(d.getMinutes())}`; };
  C.MONTHS = ["янв.", "февр.", "мар.", "апр.", "мая", "июн.", "июл.", "авг.", "сент.", "окт.", "нояб.", "дек."];
  C.MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"];
  C.MONTHS_NOM = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"];
  C.MONTHS_EN = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
  C.WD = ["вс", "пн", "вт", "ср", "чт", "пт", "сб"];
  C.digits = (s) => String(s || "").replace(/\D/g, "");
  C.plural = (n, one, few, many) => { const m = n % 10, h = n % 100; return n + " " + (h > 10 && h < 20 ? many : m === 1 ? one : m > 1 && m < 5 ? few : many); };
  // «12 октября» from an ISO date; with the year when it is not this year
  C.dHuman = (s, lang) => {
    if (!s) return "";
    const d = new Date(String(s).slice(0, 10) + "T00:00");
    if (isNaN(d)) return String(s).slice(0, 10);
    const name = lang === "en" ? C.MONTHS_EN[d.getMonth()] : C.MONTHS_GEN[d.getMonth()];
    return `${d.getDate()} ${name}` + (d.getFullYear() !== new Date().getFullYear() ? ` ${d.getFullYear()}` : "");
  };

  // Template variables of guest messages — the same list and the same result in
  // the chats page, the booking card and the step editor preview. `ctx`:
  // { name, apartment, checkin, checkout, arrival_time, departure_time, nights,
  //   amount, debt, paid, phone, times: [checkin_default, checkout_default], lang }
  C.VARS = ["имя", "объект", "заезд", "выезд", "время заезда", "время выезда", "заезд время", "выезд время", "дата заезда", "дата выезда", "ночей", "сумма", "долг", "оплачено", "телефон", "блок", "адрес", "телефон компании"];
  C.fillVars = (text, ctx) => {
    ctx = ctx || {};
    const en = ctx.lang === "en";
    const times = ctx.times || ["14:00", "11:00"];
    const tIn = String(ctx.arrival_time || times[0]).slice(0, 5), tOut = String(ctx.departure_time || times[1]).slice(0, 5);
    const d = (s) => C.dHuman(s, en ? "en" : "ru").replace(/ \d{4}$/, "");
    const name = String(ctx.name || "").replace(/^\+?\d+$/, "").split(" ")[0];
    const apt = String(ctx.apartment || "").trim().toUpperCase();
    const n = (v) => (v == null || v === "" ? "" : String(v));
    const map = {
      "имя": name, "объект": ctx.apartment || "", "заезд": d(ctx.checkin), "выезд": d(ctx.checkout),
      "дата заезда": d(ctx.checkin), "дата выезда": d(ctx.checkout),
      "время заезда": ctx.checkin ? tIn : "", "время выезда": ctx.checkout ? tOut : "",
      "заезд время": ctx.checkin ? `${d(ctx.checkin)} ${en ? "at" : "в"} ${tIn}` : "",
      "выезд время": ctx.checkout ? `${d(ctx.checkout)} ${en ? "by" : "до"} ${tOut}` : "",
      "ночей": n(ctx.nights), "сумма": n(ctx.amount), "долг": n(ctx.debt), "оплачено": n(ctx.paid),
      "телефон": ctx.phone ? "+" + C.digits(ctx.phone) : "",
      "блок": /^[AB]/.test(apt) ? (en ? `Block ${apt[0]}` : `блок ${apt[0]}`) : "",
      "адрес": ctx.address || "", "телефон компании": ctx.company_phone || "",
    };
    const re = new RegExp("\\{(" + C.VARS.join("|") + ")\\}", "g");
    return String(text || "").replace(re, (m, k) => map[k] || m);
  };
  // tokens still unfilled after fillVars: «{имя}» or a «[код двери]» placeholder
  C.unresolved = (text) => {
    const out = [];
    String(text || "").replace(/\{[^{}\n]{1,40}\}|\[[^\[\]\n]{1,40}\]/g, (m) => { if (!out.includes(m)) out.push(m); return m; });
    return out;
  };

  // fetch with a timeout and one error shape: err.status (0 = network/timeout), err.message in Russian
  C.request = async (url, opts) => {
    opts = Object.assign({}, opts || {});
    const timeoutMs = opts.timeoutMs || (/[?&]wait=/.test(url) ? 40000 : 20000);
    delete opts.timeoutMs;
    opts.headers = Object.assign({}, opts.headers || {});
    if (opts.json !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(opts.json); delete opts.json; }
    const outer = opts.signal;
    const ctl = typeof AbortController !== "undefined" ? new AbortController() : null;
    if (ctl) {
      opts.signal = ctl.signal;
      if (outer) outer.addEventListener("abort", () => ctl.abort(), { once: true });
    }
    const timer = ctl ? setTimeout(() => ctl.abort(), timeoutMs) : null;
    let res;
    try {
      res = await fetch(url, opts);
    } catch (e) {
      const aborted = e && e.name === "AbortError";
      const err = new Error(aborted && outer && outer.aborted ? "aborted" : aborted ? "Сервер не ответил — проверьте связь" : "Нет связи с сервером");
      err.status = 0; err.name = aborted && outer && outer.aborted ? "AbortError" : "RequestError";
      throw err;
    } finally { if (timer) clearTimeout(timer); }
    let data = null;
    try { data = await res.json(); } catch (e) { /* empty body */ }
    if (!res.ok) {
      const gw = res.status === 502 || res.status === 504;
      const err = new Error((data && data.detail) || (gw ? "Сервер не ответил (ошибка " + res.status + "): окно Nova Backend закрыто или туннель оборван. Попробуйте ещё раз" : "Ошибка " + res.status));
      err.status = res.status; err.data = data;
      throw err;
    }
    return data;
  };
})();
