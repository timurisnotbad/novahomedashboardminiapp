/* Background: keeps the CRM informed (ping), fetches config and the outbox, and
   relays everything to the content script in the extranet tab. */
const DEF = { crm: "", token: "" };
async function cfg() { return Object.assign({}, DEF, await chrome.storage.sync.get(DEF)); }
async function api(path, opts) {
  const c = await cfg(); if (!c.crm || !c.token) throw new Error("Укажите адрес CRM и ключ в настройках расширения");
  const r = await fetch(c.crm.replace(/\/$/, "") + "/api/inbox/ext" + path, Object.assign({ headers: { "Content-Type": "application/json", "X-Ext-Token": c.token } }, opts || {}));
  if (!r.ok) throw new Error("CRM " + r.status + ": " + (await r.text()).slice(0, 120));
  return r.json();
}
async function extranetTabs() { return chrome.tabs.query({ url: "https://admin.booking.com/*" }); }
async function tick() {
  const tabs = await extranetTabs();
  let state = { page: "", unread: 0, error: "" };
  for (const t of tabs) {
    try { const r = await chrome.tabs.sendMessage(t.id, { type: "scan" }); if (r) { state = Object.assign(state, r); if (r.conversations && r.conversations.length) await api("/messages", { method: "POST", body: JSON.stringify({ conversations: r.conversations }) }); } }
    catch (e) { state.error = String(e.message || e); }
  }
  try {
    await api("/ping", { method: "POST", body: JSON.stringify({ page: state.page, unread: state.unread, version: chrome.runtime.getManifest().version, error: state.error }) });
    const out = await api("/outbox");
    for (const o of out) {
      let ok = false, error = "нет открытой вкладки экстранета";
      for (const t of tabs) { try { const r = await chrome.tabs.sendMessage(t.id, { type: "send", item: o }); if (r && r.ok) { ok = true; error = ""; break; } error = (r && r.error) || error; } catch (e) { error = String(e.message || e); } }
      await api("/outbox/" + o.id, { method: "POST", body: JSON.stringify({ ok, error }) });
    }
    await chrome.storage.local.set({ last: new Date().toISOString(), lastError: state.error || "" });
  } catch (e) { await chrome.storage.local.set({ lastError: String(e.message || e) }); }
}
chrome.alarms.create("tick", { periodInMinutes: 0.25 });
chrome.alarms.onAlarm.addListener((a) => { if (a.name === "tick") tick(); });
chrome.runtime.onMessage.addListener((m, s, reply) => {
  if (m.type === "config") { api("/config").then(reply).catch((e) => reply({ error: String(e.message || e) })); return true; }
  if (m.type === "tick") { tick().then(() => reply({ ok: true })); return true; }
});
tick();
