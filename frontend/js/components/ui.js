/* Small shared UI helpers. */
(function () {
  const NH = (window.NH = window.NH || {});

  function el(html) {
    const t = document.createElement("template");
    t.innerHTML = html.trim();
    return t.content.firstElementChild;
  }

  function esc(s) {
    if (s == null) return "";
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function usd(n) {
    const v = Math.round(Number(n) || 0);
    return "$" + v.toLocaleString("en-US");
  }

  function icon(name) {
    return `<svg><use href="#i-${name}"/></svg>`;
  }

  function sourceLabel(source) {
    return esc(source || "Прямое");
  }

  const RU_MONTHS = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"];
  function shortDate(iso) {
    if (!iso) return "";
    const d = new Date(iso + "T00:00:00");
    return d.getDate() + " " + RU_MONTHS[d.getMonth()];
  }
  function rangeDate(a, b) {
    return shortDate(a) + " – " + shortDate(b);
  }
  // YYYY-MM-DD in the device's LOCAL time. toISOString() is UTC and gives
  // yesterday's date until 05:00 Tashkent time.
  function localIso(d) {
    d = d || new Date();
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  }

  let toastTimer = null;
  function toast(msg) {
    const t = document.getElementById("toast");
    t.textContent = msg;
    t.classList.remove("hidden");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.add("hidden"), 2200);
  }

  function haptic(type) {
    try {
      const h = window.Telegram?.WebApp?.HapticFeedback;
      if (!h) return;
      if (type === "success") h.notificationOccurred("success");
      else h.impactOccurred(type || "light");
    } catch (e) {}
  }

  function skeletonList(n) {
    let s = "";
    for (let i = 0; i < n; i++) s += '<div class="skeleton"></div>';
    return s;
  }

  function empty(msg) {
    return `<div class="empty">${esc(msg)}</div>`;
  }
  // long apartment names ("BLV 2A-156") get a smaller one-line badge
  function badgeCls(name) {
    return String(name == null ? "" : name).length > 6 ? " aptbadge--long" : "";
  }

  NH.ui = { el, esc, usd, icon, sourceLabel, shortDate, rangeDate, localIso, toast, haptic, skeletonList, empty, badgeCls };
})();
