const $ = (id) => document.getElementById(id);
chrome.storage.sync.get({ crm: "", token: "" }, (c) => { $("crm").value = c.crm; $("token").value = c.token; });
function status() { chrome.storage.local.get({ last: "", lastError: "" }, (l) => { $("st").textContent = (l.last ? "Последний обмен с CRM: " + new Date(l.last).toLocaleTimeString() : "Ещё не связывалось с CRM") + (l.lastError ? "\nОшибка: " + l.lastError : ""); }); }
$("save").onclick = () => chrome.storage.sync.set({ crm: $("crm").value.trim().replace(/\/$/, ""), token: $("token").value.trim() }, () => { $("st").textContent = "Сохранено"; chrome.runtime.sendMessage({ type: "tick" }, status); });
$("now").onclick = () => chrome.runtime.sendMessage({ type: "tick" }, status);
$("snap").onclick = async () => { const tabs = await chrome.tabs.query({ url: "https://admin.booking.com/*" }); if (!tabs.length) { $("st").textContent = "Откройте вкладку экстранета"; return; } chrome.tabs.sendMessage(tabs[0].id, { type: "snapshot" }, () => { $("st").textContent = "Файл booking-extranet-snapshot.html сохранён в Загрузки — пришлите его разработчику"; }); };
status();
