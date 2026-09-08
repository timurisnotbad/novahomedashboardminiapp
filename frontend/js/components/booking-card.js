/* Check-in row + debtor row + guest row (iOS list style). */
(function () {
  const NH = (window.NH = window.NH || {});
  const { esc, usd, icon, sourceLabel, shortDate } = NH.ui;

  function paymentPill(b) {
    if (b.is_paid || (b.debt_usd || 0) <= 0)
      return `<span class="status-pill sp-green owner-only">оплачено</span>`;
    return `<span class="status-pill sp-red owner-only">долг ${usd(b.debt_usd)}</span>`;
  }

  function checkinCard(b) {
    const time = b.arrival_time ? esc(b.arrival_time) : "—";
    return `
      <div class="li li--tap tappable" data-guest='${encodeURIComponent(JSON.stringify(b))}'>
        <span class="aptbadge${NH.ui.badgeCls(b.apartment)}">${esc(b.apartment)}</span>
        <div class="li__main">
          <div class="li__t">${esc(b.client_name || "—")}</div>
          <div class="li__s"><span class="owner-only">${sourceLabel(b.source)} · </span>заезд ${time}</div>
        </div>
        ${paymentPill(b)}
        <div class="li__chev">${icon("chev")}</div>
      </div>`;
  }

  function callBtn(phone) {
    return phone
      ? `<button class="round" style="width:32px;height:32px;flex:none" data-phone="${esc(phone)}" aria-label="Позвонить">${icon("phone")}</button>`
      : "";
  }

  function debtorRow(d) {
    return `
      <div class="li">
        <span class="aptbadge${NH.ui.badgeCls(d.apartment)}">${esc(d.apartment)}</span>
        <div class="li__main">
          <div class="li__t">${esc(d.client_name || "—")}</div>
          <div class="li__s">заехал ${shortDate(d.checkin_date)} · до ${shortDate(d.checkout_date)}</div>
        </div>
        <div class="li__v v-red">${usd(d.debt_usd)}</div>
        ${callBtn(d.client_phone)}
      </div>`;
  }

  function guestRow(g) {
    const pill = g.is_paid
      ? `<span class="status-pill sp-green owner-only">оплачено</span>`
      : `<span class="status-pill sp-red owner-only">долг</span>`;
    return `
      <div class="li">
        <span class="aptbadge${NH.ui.badgeCls(g.apartment)}">${esc(g.apartment)}</span>
        <div class="li__main">
          <div class="li__t">${esc(g.client_name || "—")}</div>
          <div class="li__s">до ${shortDate(g.checkout_date)}</div>
        </div>
        ${pill}
      </div>`;
  }

  function newBookingRow(b) {
    return `
      <div class="li">
        <span class="aptbadge${NH.ui.badgeCls(b.apartment)}">${esc(b.apartment)}</span>
        <div class="li__main">
          <div class="li__t">${esc(b.client_name || "—")}</div>
          <div class="li__s">${shortDate(b.checkin)} – ${shortDate(b.checkout)}</div>
        </div>
        <div class="li__v owner-only">${usd(b.amount_usd)}</div>
      </div>`;
  }

  NH.bookingCard = { checkinCard, debtorRow, guestRow, newBookingRow };
})();
