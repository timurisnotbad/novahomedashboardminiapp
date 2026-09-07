/* API layer — all backend requests. */
(function () {
  // Same-origin by default (frontend is served by FastAPI). Override with
  // window.NH_API_BASE = "http://localhost:8000" for split deployments.
  const BASE = (window.NH_API_BASE || "") + "/api";

  function initData() {
    try {
      return (window.Telegram && window.Telegram.WebApp && window.Telegram.WebApp.initData) || "";
    } catch (e) {
      return "";
    }
  }

  // Owner access key: the bot appends ?okey=... to the dashboard URL for owner
  // ids only (fallback for clients that pass no initData, e.g. macOS Telegram).
  // Persist it so later opens without the parameter stay unlocked.
  let sessionKey = "";
  try {
    const params = new URLSearchParams(window.location.search);
    const okey = params.get("okey");
    if (okey) {
      sessionKey = okey;
      try { localStorage.setItem("nh_okey", okey); } catch (e) { /* private mode */ }
      // don't leave the key in the address bar / history / screenshots
      try {
        params.delete("okey");
        const q = params.toString();
        history.replaceState(null, "", window.location.pathname + (q ? "?" + q : "") + window.location.hash);
      } catch (e) { /* ignore */ }
    }
  } catch (e) { /* ignore */ }

  function ownerKey() {
    try {
      return localStorage.getItem("nh_okey") || sessionKey || "";
    } catch (e) {
      return sessionKey || "";
    }
  }

  const TIMEOUT_MS = 20000;

  async function req(path, options) {
    options = options || {};
    options.headers = Object.assign({}, options.headers, {
      "X-Telegram-Init-Data": initData(),
      "X-Owner-Key": ownerKey(),
    });
    // a hung tunnel must not leave the screen on a skeleton forever
    const ctl = typeof AbortController !== "undefined" ? new AbortController() : null;
    const timer = ctl ? setTimeout(() => ctl.abort(), TIMEOUT_MS) : null;
    if (ctl) options.signal = ctl.signal;
    let res;
    try {
      res = await fetch(BASE + path, options);
    } catch (e) {
      const err = new Error(e && e.name === "AbortError" ? "timeout" : "network");
      err.status = 0;
      throw err;
    } finally {
      if (timer) clearTimeout(timer);
    }
    if (!res.ok) {
      const err = new Error("HTTP " + res.status);
      err.status = res.status;
      throw err;
    }
    return res.json();
  }

  window.NH = window.NH || {};
  window.NH.api = {
    getToday: () => req("/dashboard/today"),
    getTomorrow: () => req("/dashboard/tomorrow"),
    getCleaning: (days = 2) => req(`/cleaning?days=${days}`),
    getGuests: () => req("/guests"),
    getOccupancy: (days = 7) => req(`/occupancy?days=${days}`),
    getMe: () => req("/me"),
    getBalances: () => req("/finance/balances"),
    getPenalties: () => req("/penalties"),
    addPenalty: (payload) =>
      req("/penalties", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      }),
    deletePenalty: (id) => req(`/penalties/${id}`, { method: "DELETE" }),
    getPayroll: (month) => req(`/payroll?month=${month || ""}`),
    setPayTerms: (payload) =>
      req("/payroll/terms", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      }),
    addPayPayment: (payload) =>
      req("/payroll/pay", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      }),
    delPayPayment: (id) => req(`/payroll/pay/${id}`, { method: "DELETE" }),
    getPayRecon: (month) => req(`/payrecon?month=${month || ""}`),
    patchPayRecon: (payload) =>
      req("/payrecon/item", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      }),
    patchPayPayment: (id, payload) =>
      req(`/payroll/pay/${id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      }),
    getPrices: (checkin, checkout, refresh) =>
      req(`/prices?checkin=${checkin || ""}&checkout=${checkout || ""}${refresh ? "&refresh=1" : ""}`),
    getAttendanceStats: (month) => req(`/control/attendance?month=${month || ""}`),
    getCleaningStats: (month) => req(`/control/cleaning?month=${month || ""}`),
    getSupplies: () => req("/control/supplies"),
    addSupply: (payload) =>
      req("/control/supplies", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      }),
    setSupplyBought: (id, bought) =>
      req(`/control/supplies/${id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ bought }),
      }),
    delSupply: (id) => req(`/control/supplies/${id}`, { method: "DELETE" }),
    getTasks: () => req("/tasks"),
    addTask: (payload) =>
      req("/tasks", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      }),
    setTaskStatus: (id, status) =>
      req(`/tasks/${id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status }),
      }),
    deleteTask: (id) => req(`/tasks/${id}`, { method: "DELETE" }),
    updateCleaningStatus: (apartment, status, date) =>
      req(`/cleaning/${encodeURIComponent(apartment)}/status`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status, date }),
      }),
    sync: () => req("/sync", { method: "POST" }),
    addPayment: (payload) =>
      req("/payments", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      }),
  };
})();
