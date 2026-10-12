const $ = (id) => document.getElementById(id);
if (!(window.chrome && chrome.storage && chrome.storage.local)) {
  document.body.innerHTML = "<b>Это окно нужно открывать через значок расширения</b><p style='font-size:13px;color:#555'>Файл открыт как обычная страница, поэтому настройки сохранить нельзя.<br><br>1. Chrome → chrome://extensions → «Загрузить распакованное расширение» → папка booking-ext.<br>2. Нажмите на значок-пазл справа от адресной строки, закрепите «Nova Home CRM» булавкой.<br>3. Нажмите на значок Nova Home CRM — откроется это же окно, и «Сохранить» сработает.</p>";
  throw new Error("not in extension context");
}
chrome.storage.local.get({ crm: "", token: "" }, (c) => { $("crm").value = c.crm; $("token").value = c.token; });
function status() { chrome.storage.local.get({ last: "", lastError: "" }, (l) => { $("st").textContent = (l.last ? "Последний обмен с CRM: " + new Date(l.last).toLocaleTimeString() : "Ещё не связывалось с CRM") + (l.lastError ? "\nОшибка: " + l.lastError : ""); }); }
$("save").onclick = () => {
  const crm = $("crm").value.trim().replace(/\/$/, "");
  if (!/^https:\/\//.test(crm) && !/^http:\/\/(localhost|127\.0\.0\.1)/.test(crm)) { $("st").textContent = "Адрес CRM должен начинаться с https://"; return; }
  chrome.storage.local.set({ crm, token: $("token").value.trim() }, () => { $("st").textContent = "Сохранено"; chrome.runtime.sendMessage({ type: "tick" }, status); });
};
$("now").onclick = () => chrome.runtime.sendMessage({ type: "tick" }, status);
$("snap").onclick = async () => { const tabs = await chrome.tabs.query({ url: "https://admin.booking.com/*" }); if (!tabs.length) { $("st").textContent = "Откройте вкладку экстранета"; return; } chrome.tabs.sendMessage(tabs[0].id, { type: "snapshot" }, () => { $("st").textContent = "Файл booking-extranet-snapshot.html сохранён в Загрузки — пришлите его разработчику"; }); };
status();
