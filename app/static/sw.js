// JobHub background script (service worker): shows notifications sent by JobHub (Settings → Account →
// Notifications) and opens the right page when one is tapped. No offline caching yet: every page still comes from the
// server. Served from /sw.js so it covers the whole app.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));

self.addEventListener('push', e => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (x) { d = {body: e.data ? e.data.text() : ''}; }
  // iOS requires a visible notification for every push, so always show one.
  e.waitUntil(self.registration.showNotification(d.title || 'JobHub', {
    body: d.body || '', tag: d.tag || undefined, icon: '/static/icon-192.png', badge: '/static/icon-192.png',
    data: {url: d.url || '/'},
  }));
});

self.addEventListener('notificationclick', e => {
  e.notification.close();
  const want = new URL((e.notification.data && e.notification.data.url) || '/', self.location.origin);
  const url = want.origin === self.location.origin ? want.href : self.location.origin + '/';
  e.waitUntil((async () => {
    const wins = await self.clients.matchAll({type: 'window', includeUncontrolled: true});
    for (const w of wins) {
      if (w.url.startsWith(self.location.origin)) {
        await w.focus();
        try { await w.navigate(url); } catch (x) { /* not controlled yet: focusing is enough */ }
        return;
      }
    }
    await self.clients.openWindow(url);
  })());
});
