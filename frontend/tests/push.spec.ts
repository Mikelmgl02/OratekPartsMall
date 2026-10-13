import { expect, Page, test } from '@playwright/test';

const empty = { count: 0, next: null, previous: null, results: [] };
const acct = { id: '11111111-1111-1111-1111-111111111111', name: 'FRANK MOTORS', roles: ['client_business'], capabilities: ['client'], permission: 'owner' };
const order = '22222222-2222-2222-2222-222222222222';
const supplier = { id: '44444444-4444-4444-4444-444444444444', name: 'GRUPO RAZOR PARTS' };
const conversation = { id: 'cccccccc-cccc-4ccc-8ccc-cccccccccccc', order_id: order, orders: [{ id: order, reference: 'ORD-0123', status: 'quoted' }], order_count: 1, open_orders: 1, reference: 'ORD-0123', status: 'quoted', side: 'client', account: { id: acct.id, name: acct.name }, counterpart: supplier, unread: 1, activity: new Date().toISOString(), last_message: null };
// The default headless shell denies notifications; Chromium's new headless mode grants them like a desktop browser.
test.use({ channel: 'chromium' });

const KEY = 'BEl62iUYgUivxIkv69yViEuiBIa-Ib9-SkvMeAtA3LFgDzkrxZJjSgSnfckjBJuBkr3qBUYIHBQFLXYp5Nksh8U';

async function mock(page: Page, enabled: boolean) {
  const posts: { path: string; body: unknown }[] = [];
  await page.route('**/api/**', route => route.fulfill({ json: empty }));
  await page.route('**/api/session', route => route.fulfill({ json: { authenticated: true, user: { id: 1, username: 'FRANCISCO', first_name: 'Francisco', last_name: '', is_superuser: false }, accounts: { count: 1, next: null, previous: null, results: [acct] } } }));
  await page.route('**/api/market/wishlist/state', route => route.fulfill({ json: { part_ids: [], count: 0 } }));
  await page.route(/\/api\/market\/inbox$/, route => route.fulfill({ json: { unread_messages: 1, unread_conversations: 1, unseen_notifications: 0, latest_message: null, latest_notification: null,
    awaiting_receipt: { count: 0, latest_at: null, orders: [] } } }));
  await page.route(/\/api\/market\/inbox\/notifications(\?.*)?$/, route => route.fulfill({ json: { results: [], has_more: false } }));
  await page.route(/\/api\/market\/inbox\/conversations(\?.*)?$/, route => {
    posts.push({ path: new URL(route.request().url()).search, body: null });
    return route.fulfill({ json: { count: 1, next: null, previous: null, results: [conversation] } });
  });
  await page.route(/\/sent-requests\/[^/?]+$/, route => route.fulfill({ json: { id: order, reference: 'ORD-0123', status: 'quoted', version: 2, notes: '', lines: [], quotation: null, quotations: [], events: [],
    client: conversation.account, supplier, created_at: conversation.activity, updated_at: conversation.activity, handshaked_at: null, line_count: 0, unit_count: 0 } }));
  await page.route(/\/deals\/[^/]+\/messages(\?.*)?$/, route => route.fulfill({ json: { results: [], cursor: 0, has_more: false, has_earlier: false, assistant_notice: false } }));
  await page.route(/\/api\/market\/inbox\/push(\/remove)?$/, route => {
    if (route.request().method() === 'GET') return route.fulfill({ json: { enabled, public_key: enabled ? KEY : '' } });
    posts.push({ path: new URL(route.request().url()).pathname, body: route.request().postDataJSON() });
    return route.fulfill({ json: {} });
  });
  return posts;
}

// Playwright's Chromium has no push service: the subscription is a stand-in, everything around it (permission, service worker,
// what reaches the server) is real.
async function fakePushService(page: Page) {
  await page.addInitScript(() => {
    let current: unknown = null;
    const fake = { endpoint: 'https://push.example.invalid/navegador', options: { applicationServerKey: null as ArrayBuffer | null },
      toJSON: () => ({ endpoint: 'https://push.example.invalid/navegador', keys: { p256dh: 'clave-publica', auth: 'secreto' } }),
      unsubscribe: async () => { current = null; return true; } };
    PushManager.prototype.subscribe = async function (options?: PushSubscriptionOptionsInit) {
      fake.options.applicationServerKey = (options?.applicationServerKey as Uint8Array).buffer as ArrayBuffer; current = fake; return fake as unknown as PushSubscription;
    };
    PushManager.prototype.getSubscription = async () => current as PushSubscription | null;
  });
}

test('the bell offers browser notifications only when the server sends them', async ({ page, context }) => {
  await context.grantPermissions(['notifications']);
  await mock(page, false);
  await page.goto('/');
  await page.getByRole('button', { name: 'Notificaciones', exact: true }).click();
  await expect(page.getByRole('region', { name: 'Notificaciones', exact: true })).toContainText('Aún no tienes notificaciones.');
  await expect(page.getByRole('button', { name: /Recibir notificaciones/ })).toHaveCount(0);
});

test('turning notifications on subscribes this browser for the person; turning them off removes it', async ({ page, context }) => {
  await context.grantPermissions(['notifications']);
  await fakePushService(page);
  const posts = await mock(page, true);
  await page.goto('/');
  await page.getByRole('button', { name: 'Notificaciones', exact: true }).click();
  await page.getByRole('button', { name: 'Recibir notificaciones en este navegador', exact: true }).click();
  await expect(page.getByText('Notificaciones del navegador activas', { exact: true })).toBeVisible();
  expect(posts.find(post => post.path === '/api/market/inbox/push')?.body).toEqual({ endpoint: 'https://push.example.invalid/navegador', keys: { p256dh: 'clave-publica', auth: 'secreto' } });
  expect(await page.evaluate(async () => (await navigator.serviceWorker.getRegistration('/'))?.active?.scriptURL)).toMatch(/\/sw\.js$/);
  await page.getByRole('button', { name: 'Desactivar', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Recibir notificaciones en este navegador', exact: true })).toBeVisible();
  expect(posts.find(post => post.path === '/api/market/inbox/push/remove')?.body).toEqual({ endpoint: 'https://push.example.invalid/navegador' });
  expect(await page.evaluate(() => localStorage.getItem('motionpartes:push:off'))).toBe('1');
});

test('a delivered push shows a system notification when the site is not in front, and its link opens the conversation', async ({ page, context, browserName }) => {
  test.skip(browserName !== 'chromium', 'Delivers the push through the Chrome DevTools Protocol.');
  await context.grantPermissions(['notifications']);
  await mock(page, true);
  await page.goto('/');
  await page.evaluate(async () => { await navigator.serviceWorker.register('/sw.js'); await navigator.serviceWorker.ready; });
  const cdp = await context.newCDPSession(page);
  const registration = new Promise<string>(resolve => cdp.on('ServiceWorker.workerRegistrationUpdated', event => {
    const found = event.registrations.find(item => item.scopeURL.endsWith('/') && !item.isDeleted);
    if (found) resolve(found.registrationId);
  }));
  await cdp.send('ServiceWorker.enable');
  const registrationId = await registration;
  const origin = new URL(page.url()).origin;
  await page.goto('about:blank');  // no MotionPartes page in front: the service worker shows the notification itself
  await cdp.send('ServiceWorker.deliverPushMessage', { origin, registrationId, data: JSON.stringify({ title: 'GRUPO RAZOR PARTS · ORD-0123', body: 'SÍ, EL LUNES', tag: `deal-${order}`, url: `/?conversacion=${order}`, order_id: order }) });
  await cdp.send('ServiceWorker.disable');
  await cdp.detach();
  const viewer = await context.newPage();
  await viewer.goto(`${origin}/sw.js`);
  await expect.poll(async () => viewer.evaluate(async () => (await (await navigator.serviceWorker.ready).getNotifications()).map(item => [item.title, item.body, item.data?.url]))).toEqual([['GRUPO RAZOR PARTS · ORD-0123', 'SÍ, EL LUNES', `/?conversacion=${order}`]]);

  await viewer.close();

  const link = await context.newPage();
  const posts = await mock(link, true);
  await link.goto(`/?conversacion=${order}`);
  await expect(link.getByRole('dialog', { name: 'Conversaciones', exact: true }).getByRole('region', { name: 'Conversación con GRUPO RAZOR PARTS', exact: true })).toBeVisible();
  expect(posts.some(post => post.path === `?order=${order}`)).toBe(true);
  expect(new URL(link.url()).searchParams.get('conversacion')).toBeNull();
});

test('MotionPartes is installable: its manifest, icons and home-screen tags', async ({ page, context, browserName }) => {
  test.skip(browserName !== 'chromium', 'Reads installability through the Chrome DevTools Protocol.');
  await mock(page, true);
  await page.goto('/');
  await expect(page.locator('link[rel="manifest"]')).toHaveAttribute('href', '/manifest.webmanifest');
  await expect(page.locator('link[rel="apple-touch-icon"]')).toHaveAttribute('sizes', '180x180');
  await expect(page.locator('meta[name="apple-mobile-web-app-title"]')).toHaveAttribute('content', 'MotionPartes');
  const manifest = await (await page.request.get('/manifest.webmanifest')).json();
  expect({ display: manifest.display, start_url: manifest.start_url, sizes: manifest.icons.map((icon: { sizes: string; purpose: string }) => `${icon.sizes} ${icon.purpose}`) })
    .toEqual({ display: 'standalone', start_url: '/', sizes: ['192x192 any', '512x512 any', '512x512 maskable'] });
  const cdp = await context.newCDPSession(page);
  // Playwright's contexts are private windows, where Chrome never installs: every other criterion has to pass.
  await expect.poll(async () => (await cdp.send('Page.getInstallabilityErrors')).installabilityErrors.map(error => error.errorId).filter(id => id !== 'in-incognito')).toEqual([]);
});

test('an iPhone browser tab explains how to add MotionPartes to the home screen to get notifications', async ({ browser }) => {
  const context = await browser.newContext({ userAgent: 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Mobile/15E148 Safari/604.1', viewport: { width: 390, height: 844 } });
  const page = await context.newPage();
  await page.addInitScript(() => { delete (window as Partial<Window & { PushManager: unknown }>).PushManager; });  // as in Safari outside the home-screen app
  await mock(page, true);
  await page.goto('/');
  await page.getByRole('button', { name: 'Notificaciones', exact: true }).click();
  await expect(page.getByRole('region', { name: 'Notificaciones', exact: true })).toContainText('toca Compartir y luego Agregar a inicio');
  await expect(page.getByRole('button', { name: /Recibir notificaciones/ })).toHaveCount(0);
  const menu = (await page.getByRole('region', { name: 'Notificaciones', exact: true }).boundingBox())!;
  expect(menu.x).toBeGreaterThanOrEqual(0);
  expect(menu.x + menu.width).toBeLessThanOrEqual(390);  // spans the phone screen instead of hanging off the bell
  await context.close();
});
