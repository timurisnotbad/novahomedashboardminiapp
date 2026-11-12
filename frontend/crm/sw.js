/* Nova Home CRM service worker: shows push notifications and opens the chat on tap. */
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));
self.addEventListener("push", (e) => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (err) { d = { title: "Nova Home CRM", body: e.data ? e.data.text() : "" }; }
  e.waitUntil(self.registration.showNotification(d.title || "Nova Home CRM", {
    body: d.body || "", icon: "/crm/icon.svg", badge: "/crm/icon.svg", tag: d.tag || "nh", renotify: true,
    data: { url: d.url || "/crm/" }, vibrate: [120, 60, 120],
  }));
});
self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || "/crm/";
  e.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((list) => {
    for (const c of list) { if (c.url.includes("/crm/") && "focus" in c) { c.navigate(url); return c.focus(); } }
    return self.clients.openWindow(url);
  }));
});
