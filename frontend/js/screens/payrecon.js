/* "Оплаты" screen — payments channel vs PMS reconciliation (owner + viewers). */
(function () {
  const NH = (window.NH = window.NH || {});
  const { esc, icon } = NH.ui;

  const MONTHS_RU = ["январь","февраль","март","апрель","май","июнь",
    "июль","август","сентябрь","октябрь","ноябрь","декабрь"];

  const state = { off: 0, data: null, loading: false };

  function ym(off) {
    const d = new Date();
    d.setDate(1);
    d.setMonth(d.getMonth() + (off || 0));
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
  }

  function fmtAmount(n) {
    return (Math.round(n * 100) / 100).toLocaleString("ru-RU");
  }

  function skeleton() {
    return NH.ui.skeletonList(5);
  }

  function render() {
    if (state.loading || !state.data) return skeleton();
    const d = state.data;
    const [y, m] = d.month.split("-");
    let html = `<div class="pay-mnav">
      <button class="pay-mnav__btn" data-recon-month="-1">‹</button>
      <span class="pay-mnav__label">${MONTHS_RU[parseInt(m, 10) - 1]} ${y}</span>
      <button class="pay-mnav__btn" data-recon-month="1" ${state.off >= 0 ? "disabled" : ""}>›</button>
    </div>`;

    const dm = (iso) => (iso ? `${iso.slice(8, 10)}.${iso.slice(5, 7)}` : "—");
    const payload = (p) => encodeURIComponent(JSON.stringify(p));

    const nop = d.no_payment || [];
    html += `<div class="group"><div class="group__h">⚠️ Заехали — оплат в канале нет <span class="count">${nop.length}</span></div><div class="list">`;
    if (nop.length) {
      html += nop.map((b) => `
        <div class="li">
          <span class="aptbadge">${esc(b.apartment || "?")}</span>
          <span class="li__main"><div class="task__t">${esc(b.guest)}</div>
          <div class="task__meta"><span class="due">заезд ${dm(b.checkin)} · ${esc(b.source || "")}${b.debt_usd ? ` · долг $${b.debt_usd}` : ""}</span></div></span>
        </div>`).join("");
    } else {
      html += `<div class="li"><span class="li__main"><div class="li__t muted">По всем заездам месяца есть оплаты 💪</div></span></div>`;
    }
    html += `</div></div>`;

    const mt = d.matched || [];
    if (mt.length) {
      html += `<div class="group"><div class="group__h">✅ Оплаты из канала <span class="count">${mt.length}</span></div><div class="list">`;
      html += mt.map((p) => {
        const b = p.booking || {};
        const amt = p.amount ? `${fmtAmount(p.amount)} ${esc(p.currency || "")}` : "⚠️ сумма не указана";
        const method = p.method ? esc(p.method) : "⚠️ метод не указан";
        return `<div class="li">
          <span class="aptbadge">${esc(b.apartment || p.apartment || "?")}</span>
          <span class="li__main"><div class="task__t">${amt} · ${method}</div>
          <div class="task__meta"><span class="due">${esc(b.guest || "")} · заезд ${dm(b.checkin || p.checkin)}</span></div></span>
          <button class="task-del" data-precon-edit data-p="${payload(p)}" aria-label="Дополнить">✏️</button>
        </div>`;
      }).join("");
      html += `</div></div>`;
    }

    const um = d.unmatched || [];
    if (um.length) {
      html += `<div class="group"><div class="group__h">❓ Не сопоставлено <span class="count">${um.length}</span></div><div class="list">`;
      html += um.map((p) => {
        const bits = [];
        if (p.amount) bits.push(fmtAmount(p.amount) + " " + (p.currency || ""));
        if (p.method) bits.push(p.method);
        if (p.apartment) bits.push(p.apartment);
        if (p.checkin) bits.push("заезд " + dm(p.checkin));
        return `<div class="li"><span class="li__main">
          <div class="task__t">${bits.length ? esc(bits.join(" · ")) : "ничего не распознано"}</div>
          <div class="task__meta"><span class="due recon-raw">${esc(p.raw || "")}</span></div></span>
          <button class="task-del" data-precon-edit data-p="${payload(p)}" aria-label="Дополнить">✏️</button>
        </div>`;
      }).join("");
      html += `</div></div>`;
    }

    if (!mt.length && !um.length) {
      html += NH.ui.empty("Постов из канала оплат в этом месяце нет. Бот читает новые посты автоматически.");
    }
    return html;
  }

  NH.screens = NH.screens || {};
  NH.screens.payrecon = { render, skeleton, state, ym };
})();
