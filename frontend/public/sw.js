// MotionPartes service worker: shows the deal inbox's browser notifications (mall/push.py) and opens their conversation when clicked.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', event => event.waitUntil(self.clients.claim()));

self.addEventListener('push', event => {
  let data;
  try { data = event.data ? event.data.json() : {}; } catch { data = {}; }
  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    // Open pages refresh their inbox now. Every push shows a notification (Safari revokes a subscription whose pushes show none); with a
    // MotionPartes page in front it is closed at once, since the page announces it with its own toast.
    windows.forEach(client => client.postMessage({ type: 'motionpartes:push', order_id: data.order_id }));
    await self.registration.showNotification(data.title || 'MotionPartes', { body: data.body || '', tag: data.tag, icon: '/icons/icon-192.png',
      data: { url: data.url || '/', order_id: data.order_id } });
    if (data.tag && windows.some(client => client.visibilityState === 'visible' && client.focused))
      (await self.registration.getNotifications({ tag: data.tag })).forEach(notification => notification.close());
  })());
});

self.addEventListener('notificationclick', event => {
  event.notification.close();
  const { url = '/', order_id: orderId } = event.notification.data || {};
  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    const open = windows.find(client => new URL(client.url).origin === self.location.origin);
    if (open) {
      await open.focus();
      open.postMessage({ type: 'motionpartes:open-conversation', order_id: orderId });
      return;
    }
    await self.clients.openWindow(url);
  })());
});
