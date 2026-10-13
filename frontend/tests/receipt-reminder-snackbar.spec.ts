import { expect, test, type Page } from '@playwright/test';
import { fakeServerBasket } from './server-basket';

// Recibido is never automatic: while orders the supplier marked Entregado wait for the client's confirmation, the storefront reminds the
// person (the inbox summary's awaiting_receipt). The reminder only opens the order: the person confirms it there, never the reminder.
const empty = { count: 0, next: null, previous: null, results: [] };
const taller = { id: '11111111-1111-4111-8111-111111111111', name: 'TALLER CENTRAL' };
const norte = { id: '11111111-1111-4111-8111-222222222222', name: 'TALLER NORTE' };
const razor = { id: '44444444-4444-4444-8444-444444444444', name: 'GRUPO RAZOR PARTS' };
const istmo = { id: '55555555-5555-4555-8555-555555555555', name: 'AUTOPARTES DEL ISTMO' };
const clientAccount = (party: { id: string; name: string }) => ({ ...party, roles: ['client_business'], capabilities: ['client'], permission: 'owner' });
const supplierAccount = { ...razor, roles: ['supplier_retail'], capabilities: ['supplier'], permission: 'owner' };
const thread = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc';
const orderId = '33333333-3333-4333-8333-333333333333';
const at = (hour: number) => `2026-10-12T${String(hour).padStart(2, '0')}:00:00Z`;
type Waiting = { id: string; thread_id: string; reference: string; delivered_at: string; account: { id: string; name: string }; counterpart: { id: string; name: string } };
const waiting = (index: number, account = taller, counterpart = razor): Waiting => ({ id: index ? `33333333-3333-4333-8333-00000000000${index}` : orderId, thread_id: thread,
  reference: index ? `ORD-20261012-0000000${index}` : 'ORD-20261010-55791743', delivered_at: at(8 + index), account, counterpart });
// The server lists the three oldest Entregado first; latest_at is the newest one.
const summaryOf = (orders: Waiting[], count = orders.length) => ({ unread_messages: 0, unread_conversations: 0, unseen_notifications: 0, latest_message: null,
  latest_notification: null, awaiting_receipt: { count, latest_at: orders.length ? orders.reduce((latest, order) => order.delivered_at > latest ? order.delivered_at : latest, '') : null,
    orders: orders.slice(0, 3) } });

async function open(page: Page, { accounts = [clientAccount(taller)], superuser = false, signedIn = true, summary = { current: summaryOf([waiting(0)]) } } = {}) {
  const actions: string[] = [], listed: string[] = [];
  await page.route('**/api/**', route => route.fulfill({ json: empty }));
  await fakeServerBasket(page);
  await page.route('**/api/session', route => route.fulfill({ json: signedIn ? { authenticated: true, user: { id: 7, username: 'FRANCISCO', first_name: 'Francisco', last_name: '',
    is_superuser: superuser }, accounts: { ...empty, count: accounts.length, results: accounts } } : { authenticated: false } }));
  await page.route('**/api/market/wishlist/state', route => route.fulfill({ json: { part_ids: [], count: 0 } }));
  await page.route(/\/api\/market\/inbox$/, route => signedIn ? route.fulfill({ json: summary.current }) : route.fulfill({ status: 401, json: { detail: 'Inicia sesión para continuar.' } }));
  await page.route(/\/api\/market\/inbox\/notifications(\?.*)?$/, route => route.fulfill({ json: { results: [], has_more: false } }));
  const delivered = { id: orderId, reference: 'ORD-20261010-55791743', status: 'delivered' };
  await page.route(/\/api\/market\/inbox\/conversations(\?.*)?$/, route => route.fulfill({ json: { ...empty, count: 1, results: [{ id: thread, order_id: orderId, reference: delivered.reference,
    status: 'delivered', orders: [delivered], order_count: 1, open_orders: 1, side: 'client', account: taller, counterpart: razor, unread: 0, activity: at(8), last_message: null }] } }));
  await page.route(/\/threads\/[^/]+\/orders$/, route => route.fulfill({ json: { results: [{ ...delivered, created_at: at(1), reviewed_at: at(2), line_count: 1, unit_count: 5,
    quotation_revision: 1, quotation_total: '61.75', quotation_currency: 'USD', delivered_at: at(8) }] } }));
  const quote = { id: '88888888-8888-4888-8888-000000000001', revision: 1, currency: 'USD', terms: '', total: '61.75', created_at: at(2), supplier_confirmed_at: at(2), client_confirmed_at: at(3),
    lines: [{ order_line_id: 'l1', codigo: '58411-1R000-G', sku: '58411-1R000', description: 'TAMBOR', quantity: 5, unit_price: '12.35', total: '61.75' }] };
  const detail = { ...delivered, version: 7, client: taller, supplier: razor, submission_id: 's', created_at: at(1),
    reviewed_at: at(2), handshaked_at: at(3), processing_at: at(4), in_transit_at: null, delivered_at: at(8), received_at: null, line_count: 1, unit_count: 5, notes: '', events: [],
    lines: [{ id: 'l1', part_id: 'p', sku: '58411-1R000', name: 'TAMBOR', codigo: '58411-1R000-G', brand: 'KSM', description: 'TAMBOR', quantity: 5 }], quotation: quote, quotations: [quote],
    agreement: { quotation_id: quote.id, units: 5, total: '61.75', lines: [{ order_line_id: 'l1', codigo: '58411-1R000-G', description: 'TAMBOR', agreed: 5, missing: 0, pending_missing: 0,
      final: 5, unit_price: '12.35', total: '61.75' }] }, shortfalls: [], cancellation: null };
  await page.route(new RegExp(`/sent-requests/${orderId}$`), route => route.fulfill({ json: detail }));
  await page.route(/\/deals\/[^/]+\/messages(\?.*)?$/, route => route.fulfill({ json: { results: [], cursor: 0, has_more: false, has_earlier: false } }));
  await page.route(/\/deals\/[^/]+\/actions$/, route => { actions.push(route.request().method()); return route.fulfill({ status: 400, json: { detail: 'No previsto' } }); });
  await page.route(/\/api\/market\/accounts\/[^/]+\/sent-requests(\?.*)?$/, route => { const url = new URL(route.request().url());
    listed.push(`${url.pathname.split('/')[4]}:${url.searchParams.get('status') || ''}`); return route.fulfill({ json: empty }); });
  await page.route('**/api/management/catalog/proposals/summary', route => route.fulfill({ json: { pending: 3, approved: 0, linked: 0, rejected: 0, resolved: 0, pending_changed_at: at(6) } }));
  await page.goto('/');
  return { actions, listed, summary, detail };
}

const snackbar = (page: Page) => page.locator('.receipt-snackbar');
// The region marks when the summary has arrived, so a hidden reminder is checked after the answer, not before it.
const answered = (page: Page) => expect(page.locator('.receipt-snackbar-region[data-loaded]')).toHaveCount(1);
const pollNow = (page: Page) => page.evaluate(() => window.dispatchEvent(new Event('motionpartes:inbox')));

test('one order waiting: the reminder names it, and Confirmar recibido opens it in the panel without confirming anything', async ({ page }) => {
  const { actions } = await open(page);
  await expect(snackbar(page)).toContainText('GRUPO RAZOR PARTS marcó tu orden …55791743 como entregada. ¿Ya la recibiste?');
  await expect(page.locator('.receipt-snackbar-region')).toHaveAttribute('role', 'status');
  await expect(snackbar(page).getByRole('link', { name: 'Ver todas' })).toHaveCount(0);
  const box = (await snackbar(page).boundingBox())!;
  expect(box.x < 100 && box.y + box.height > 900).toBe(true);  // bottom left, clear of the toasts on the right
  await snackbar(page).getByRole('button', { name: 'Confirmar recibido', exact: true }).click();
  const panel = page.getByRole('dialog', { name: 'Conversaciones', exact: true });
  const document = panel.getByRole('region', { name: 'Documento de la orden', exact: true });
  await expect(document.getByRole('group', { name: 'Decisiones', exact: true }).getByRole('button', { name: 'Confirmar recibido', exact: true })).toBeVisible();
  await expect(panel.locator('.inbox-doc-title')).toContainText('ORD-20261010-55791743');
  await expect(snackbar(page)).toHaveCount(0);  // hidden while the panel is open
  await panel.getByRole('button', { name: 'Cerrar ventana', exact: true }).first().click();
  await expect(panel).toHaveCount(0);
  await expect(snackbar(page)).toBeVisible();
  expect(actions).toEqual([]);  // the reminder never confirms by itself
});

test('confirmed in the panel, the reminder goes at once, without waiting for the websocket', async ({ page }) => {
  const { summary, detail } = await open(page);
  const confirmed: unknown[] = [];
  await page.route(/\/deals\/[^/]+\/actions$/, route => {
    confirmed.push(route.request().postDataJSON());
    summary.current = summaryOf([]);  // the server no longer lists it
    return route.fulfill({ json: { ...detail, status: 'received', version: 8, received_at: at(9) } });
  });
  await snackbar(page).getByRole('button', { name: 'Confirmar recibido', exact: true }).click();
  const panel = page.getByRole('dialog', { name: 'Conversaciones', exact: true });
  const document = panel.getByRole('region', { name: 'Documento de la orden', exact: true });
  await document.getByRole('group', { name: 'Decisiones', exact: true }).getByRole('button', { name: 'Confirmar recibido', exact: true }).click();
  await document.getByRole('button', { name: 'Sí, la recibí', exact: true }).click();
  await expect.poll(() => confirmed.length).toBe(1);
  expect(confirmed[0]).toMatchObject({ action: 'receive' });
  await expect(document.locator('.deal-outcome.received')).toBeVisible();
  await panel.getByRole('button', { name: 'Cerrar ventana', exact: true }).first().click();
  await answered(page);
  await expect(snackbar(page)).toHaveCount(0);  // the summary was read again right after the action
});

test('several orders across accounts: it counts them, names the oldest with its account, and Ver todas opens Por recibir', async ({ page }) => {
  const orders = [waiting(0), waiting(1, norte, istmo), waiting(2, norte), waiting(3, norte)];
  const { listed } = await open(page, { accounts: [clientAccount(taller), clientAccount(norte)], summary: { current: summaryOf(orders, 4) } });
  await expect(snackbar(page)).toContainText('Tienes 4 órdenes marcadas como entregadas. ¿Ya las recibiste? La primera: …55791743 de GRUPO RAZOR PARTS · TALLER CENTRAL.');
  const all = snackbar(page).getByRole('link', { name: 'Ver todas', exact: true });
  await expect(all).toHaveAttribute('href', `/?vista=solicitudes&cuenta=${taller.id}&estado=por-recibir`);
  await all.click();
  const chips = page.getByRole('group', { name: 'Estado de solicitudes enviadas', exact: true });
  await expect(chips.getByRole('button', { name: 'Por recibir', exact: true })).toHaveAttribute('aria-pressed', 'true');
  await expect.poll(() => listed.at(-1)).toBe(`${taller.id}:delivered`);
  await chips.getByRole('button', { name: 'Recibidos', exact: true }).click();
  await expect.poll(() => listed.at(-1)).toBe(`${taller.id}:received`);
});

test('closed, it stays away for 24 hours unless a newer order is marked delivered', async ({ page }) => {
  await page.clock.install({ time: new Date('2026-10-13T12:00:00Z') });
  const { summary } = await open(page);
  await snackbar(page).getByRole('button', { name: 'Cerrar recordatorio de recibido', exact: true }).click();
  await expect(snackbar(page)).toHaveCount(0);
  const saved = await page.evaluate(() => JSON.parse(localStorage.getItem('motionpartes:receipt-reminder:7') || 'null'));
  expect(saved.latest_at).toBe(at(8));
  expect(saved.until - Date.parse('2026-10-13T12:00:00Z') - 24 * 3600 * 1000).toBeGreaterThanOrEqual(0);
  expect(saved.until - Date.parse('2026-10-13T12:00:00Z') - 24 * 3600 * 1000).toBeLessThan(60000);
  await page.reload();
  await answered(page);
  await expect(snackbar(page)).toHaveCount(0);  // the same order: still snoozed on this device
  summary.current = summaryOf([waiting(0), waiting(1)]);  // a newer Entregado
  await pollNow(page);
  await expect(snackbar(page)).toContainText('Tienes 2 órdenes marcadas como entregadas.');
  await snackbar(page).getByRole('button', { name: 'Cerrar recordatorio de recibido', exact: true }).click();
  await expect(snackbar(page)).toHaveCount(0);
  await page.clock.fastForward('23:00:00');
  await expect(snackbar(page)).toHaveCount(0);
  await page.clock.fastForward('01:00:30');
  await expect(snackbar(page)).toContainText('Tienes 2 órdenes marcadas como entregadas.');  // a day later, still waiting
});

test('nothing waiting, a supplier-only member or a guest: no reminder', async ({ page }) => {
  await open(page, { summary: { current: summaryOf([]) } });
  await answered(page);
  await expect(snackbar(page)).toHaveCount(0);
  await page.unrouteAll({ behavior: 'ignoreErrors' });
  await open(page, { accounts: [supplierAccount], summary: { current: summaryOf([]) } });  // the server counts client accounts only
  await answered(page);
  await expect(snackbar(page)).toHaveCount(0);
  await page.unrouteAll({ behavior: 'ignoreErrors' });
  await open(page, { signedIn: false });
  await expect(page.locator('.account-menu-trigger-copy')).toContainText('Hola, inicia sesión');
  await expect(page.locator('.receipt-snackbar-region')).toHaveCount(0);  // not even mounted
});

test('an administrator who is also a client sees both reminders stacked, also on a phone', async ({ page }) => {
  await open(page, { superuser: true });
  const admin = page.locator('.admin-snackbar');
  await expect(admin).toContainText('Hay 3 productos nuevos por revisar.');
  await expect(snackbar(page)).toBeVisible();
  const [top, bottom] = [(await admin.boundingBox())!, (await snackbar(page).boundingBox())!];
  expect(top.y + top.height).toBeLessThanOrEqual(bottom.y);  // Productos nuevos above, the Recibido reminder below
  expect(Math.round(top.x)).toBe(20);
  expect(Math.round(bottom.x)).toBe(20);
  await page.setViewportSize({ width: 390, height: 844 });
  for (const box of [(await admin.boundingBox())!, (await snackbar(page).boundingBox())!]) {
    expect(Math.round(box.x)).toBe(12);
    expect(Math.round(box.width)).toBe(366);
  }
  const phone = (await snackbar(page).boundingBox())!;
  expect(Math.round(phone.y + phone.height)).toBe(832);
  expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)).toBe(false);
  await page.screenshot({ path: '/tmp/motionpartes-receipt-reminder-mobile.png' });
});
