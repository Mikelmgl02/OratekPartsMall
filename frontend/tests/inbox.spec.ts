import { expect, Page, test } from '@playwright/test';
import { fakeServerBasket } from './server-basket';

const empty = { count: 0, next: null, previous: null, results: [] };
const acct = { id: '11111111-1111-1111-1111-111111111111', name: 'FRANK MOTORS', roles: ['client_business'], capabilities: ['client'], permission: 'owner' };
const order = '22222222-2222-2222-2222-222222222222', order2 = '33333333-3333-3333-3333-333333333333';
const now = Date.now(), iso = (m: number) => new Date(now - m * 60000).toISOString();
const party = { id: acct.id, name: acct.name }, sup = { id: '44444444-4444-4444-4444-444444444444', name: 'GRUPO RAZOR PARTS' };
// Each conversation (one per client and supplier) here has one order; the server lists them with their own ids.
const conversationOf = (index: number) => `aaaaaaaa-aaaa-4aaa-8aaa-${String(index).padStart(12, '0')}`;
const conversations = [
  { id: order, reference: 'ORD-0123', status: 'quoted', side: 'client', account: party, counterpart: sup, unread: 2, activity: iso(2), last_message: { id: 12, body: 'SÍ, PODEMOS ENTREGAR EL LUNES ANTES DE LAS 12.', account_id: sup.id, account_name: sup.name, actor_name: 'Carlos Ruiz', mine: false, created_at: iso(2) } },
  { id: order2, reference: 'ORD-0119', status: 'reviewed', side: 'client', account: party, counterpart: { id: '5', name: 'AUTOPARTES DEL ISTMO' }, unread: 1, activity: iso(95), last_message: { id: 9, body: '¿EL DISCO ES VENTILADO?', account_id: '5', account_name: 'AUTOPARTES DEL ISTMO', actor_name: 'Ana', mine: false, created_at: iso(95) } },
  { id: '66666666-6666-6666-6666-666666666666', reference: 'ORD-0102', status: 'handshaked', side: 'client', account: party, counterpart: { id: '7', name: 'REPUESTOS PANAMÁ' }, unread: 0, activity: iso(60 * 30), last_message: { id: 3, body: 'GRACIAS, CONFIRMADO.', account_id: acct.id, account_name: acct.name, actor_name: 'Francisco', mine: true, created_at: iso(60 * 30) } },
].map((row, index) => ({ ...row, id: conversationOf(index + 1), order_id: row.id, orders: [{ id: row.id, reference: row.reference, status: row.status }],
  order_count: 1, open_orders: ['delivered', 'cancelled'].includes(row.status) ? 0 : 1 }));
const notifications = [
  { id: 30, kind: 'quote', title: 'GRUPO RAZOR PARTS publicó la cotización v2', description: '', order_id: order, thread_id: conversationOf(1), reference: 'ORD-0123', status: 'quoted', side: 'client', account: party, counterpart: sup, created_at: iso(5), unseen: true },
  { id: 28, kind: 'review_started', title: 'AUTOPARTES DEL ISTMO empezó a revisar tu orden', description: 'EL PROVEEDOR ABRIÓ LA ORDEN. ARTÍCULOS BLOQUEADOS.', order_id: order2, thread_id: conversationOf(2), reference: 'ORD-0119', status: 'reviewed', side: 'client', account: party, counterpart: conversations[1].counterpart, created_at: iso(100), unseen: true },
  { id: 20, kind: 'accept', title: 'REPUESTOS PANAMÁ confirmó el acuerdo', description: '', order_id: conversations[2].order_id, thread_id: conversations[2].id, reference: 'ORD-0102', status: 'handshaked', side: 'client', account: party, counterpart: conversations[2].counterpart, created_at: iso(60 * 30), unseen: false },
];
const line = (id: string, codigo: string, desc: string, q: number) => ({ id, part_id: 'p', sku: codigo.replace(/-[A-Z]$/, ''), name: '', codigo, brand: 'KSM', description: desc, quantity: q });
const deal: Record<string, unknown> & { quotation: unknown } = { id: order, reference: 'ORD-0123', status: 'quoted', version: 4, created_at: iso(300), updated_at: iso(5), notes: 'ENTREGA EN TALLER DE VÍA ESPAÑA.', handshaked_at: null,
  client: party, supplier: sup, line_count: 3, unit_count: 7, events: [],
  lines: [line('l1', '58411-1R000-G', 'TAMBOR TRASERO HYUNDAI ACCENT', 2), line('l2', '43512-0C020', 'DISCO FRENO DEL TOY TUNDRA', 2), line('l3', '04465-0K240', 'PASTILLA FRENO DEL TOY HILUX', 3)],
  quotation: { id: 'q2', revision: 2, currency: 'USD', terms: 'PAGO A 30 DÍAS. PRECIOS INCLUYEN ITBMS.', total: '612.40', created_at: iso(5), supplier_confirmed_at: iso(5), client_confirmed_at: null,
    lines: [{ order_line_id: 'l1', codigo: '58411-1R000-G', sku: '58411-1R000', description: 'TAMBOR TRASERO HYUNDAI ACCENT', quantity: 2, unit_price: '86.50', total: '173.00' },
            { order_line_id: 'l2', codigo: '43512-0C020', sku: '43512-0C020', description: 'DISCO FRENO DEL TOY TUNDRA', quantity: 2, unit_price: '142.20', total: '284.40' },
            { order_line_id: 'l3', codigo: '04465-0K240', sku: '04465-0K240', description: 'PASTILLA FRENO DEL TOY HILUX', quantity: 3, unit_price: '51.67', total: '155.00' }] } };
deal.quotations = [deal.quotation];
const messages = [
  { id: 10, message_id: 'a', body: 'BUENOS DÍAS, ¿TIENEN LOS DISCOS EN EXISTENCIA?', account_id: acct.id, account_name: acct.name, actor_name: 'Francisco', created_at: iso(40) },
  { id: 11, message_id: 'b', body: 'SÍ, LOS DOS. TE ENVIAMOS LA COTIZACIÓN V2 CON EL TAMBOR INCLUIDO.', account_id: sup.id, account_name: sup.name, actor_name: 'Carlos Ruiz', created_at: iso(6), channel: 'whatsapp' },
  { id: 12, message_id: 'c', body: 'SÍ, PODEMOS ENTREGAR EL LUNES ANTES DE LAS 12.', account_id: sup.id, account_name: sup.name, actor_name: 'Carlos Ruiz', created_at: iso(2) },
];

// A conversation's orders as its Cotizaciones table lists them (threads/<id>/orders).
const docOf = (item: { id: string; reference: string; status: string }) => ({ id: item.id, reference: item.reference, status: item.status, created_at: iso(300), updated_at: iso(5),
  reviewed_at: null, line_count: 3, unit_count: 7, quotation_revision: item.status === 'quoted' ? 2 : null, quotation_total: item.status === 'quoted' ? '612.40' : null, quotation_currency: 'USD' });

type Summary = { unread_messages: number; unread_conversations: number; unseen_notifications: number; latest_message: unknown; latest_notification: unknown };

async function mockInbox(page: Page, summary: { current: Summary }) {
  const calls: { method: string; url: string; body: unknown }[] = [];
  await page.route('**/api/**', route => route.fulfill({ json: empty }));
  await page.route('**/api/session', route => route.fulfill({ json: { authenticated: true, user: { id: 1, username: 'FRANCISCO', first_name: 'Francisco', last_name: '', is_superuser: false }, accounts: { count: 1, next: null, previous: null, results: [acct] } } }));
  await page.route('**/api/market/wishlist/state', route => route.fulfill({ json: { part_ids: [], count: 0 } }));
  await page.route(/\/api\/market\/inbox$/, route => route.fulfill({ json: summary.current }));
  await page.route(/\/api\/market\/inbox\/conversations(\?.*)?$/, route => {
    const url = new URL(route.request().url()); calls.push({ method: 'GET', url: url.pathname + url.search, body: null });
    const search = url.searchParams.get('search'), named = url.searchParams.get('order');  // order: a conversation's id or one of its orders'
    const rows = conversations.filter(row => (!search || row.reference.includes(search) || row.counterpart.name.includes(search.toUpperCase())) && (url.searchParams.get('unread') !== '1' || row.unread > 0)
      && (!named || row.id === named || row.orders.some(item => item.id === named)));
    return route.fulfill({ json: { count: rows.length, next: null, previous: null, results: rows } });
  });
  await page.route(/\/api\/market\/inbox\/notifications(\?.*)?$/, route => route.fulfill({ json: { results: notifications, has_more: false } }));
  await page.route(/\/api\/market\/inbox\/(notifications\/seen|conversations\/.*\/read)$/, route => { calls.push({ method: 'POST', url: new URL(route.request().url()).pathname, body: route.request().postDataJSON() }); return route.fulfill({ json: {} }); });
  await page.route(/\/sent-requests\/[^/?]+$/, route => route.fulfill({ json: deal }));
  await page.route(/\/threads\/[^/]+\/orders$/, route => {
    const id = new URL(route.request().url()).pathname.split('/').at(-2);
    return route.fulfill({ json: { results: (conversations.find(row => row.id === id)?.orders ?? []).map(docOf) } });
  });
  await page.route(/\/deals\/[^/]+\/messages(\?.*)?$/, route => {
    if (route.request().method() === 'POST') { const body = route.request().postDataJSON(); calls.push({ method: 'POST', url: 'messages', body }); return route.fulfill({ json: { id: 13, message_id: body.message_id, body: body.body, account_id: acct.id, account_name: acct.name, actor_name: 'Francisco', created_at: new Date().toISOString() } }); }
    return route.fulfill({ json: { results: route.request().url().includes('after=') ? [] : messages, cursor: 12, has_more: false, has_earlier: false, assistant_notice: false } });
  });
  return calls;
}
const pending = (): Summary => ({ unread_messages: 3, unread_conversations: 2, unseen_notifications: 2, latest_message: null, latest_notification: null });

test('signed-out visitors get no inbox; members see unread messages and new notifications in the header', async ({ page }) => {
  await page.route('**/api/session', route => route.fulfill({ json: { authenticated: false } }));
  await page.goto('/');
  await expect(page.getByRole('heading', { name: /El repuesto indicado/ })).toBeVisible();
  await expect(page.getByRole('button', { name: /^Mensajes/ })).toHaveCount(0);
  await page.unroute('**/api/session');
  await mockInbox(page, { current: pending() });
  await page.goto('/');
  await expect(page.getByRole('button', { name: 'Mensajes, 3 sin leer', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Notificaciones, 2 nuevas', exact: true })).toBeVisible();
});

test('a notification opens the panel on its deal: the quotation beside the chat, marked seen and read', async ({ page }) => {
  const summary = { current: pending() };
  const calls = await mockInbox(page, summary);
  await page.goto('/');
  await page.getByRole('button', { name: 'Notificaciones, 2 nuevas', exact: true }).click();
  const menu = page.getByRole('region', { name: 'Notificaciones', exact: true });
  await expect(menu.getByRole('button')).toHaveCount(3);
  await expect.poll(() => calls.find(call => call.url.endsWith('/notifications/seen'))?.body).toEqual({ last_event_id: 30 });
  await menu.getByRole('button', { name: /GRUPO RAZOR PARTS publicó la cotización v2/ }).click();
  const panel = page.getByRole('dialog', { name: 'Conversaciones', exact: true });
  const conversation = panel.getByRole('region', { name: 'Conversación con GRUPO RAZOR PARTS', exact: true });
  await expect(conversation.getByRole('region', { name: 'Cotización vigente v2', exact: true })).toContainText('USD 612.40');
  await expect(conversation.getByRole('log', { name: 'Mensajes del acuerdo', exact: true })).toContainText('SÍ, PODEMOS ENTREGAR EL LUNES ANTES DE LAS 12.');
  await expect(conversation.getByRole('log').getByRole('article').filter({ hasText: 'COTIZACIÓN V2' })).toContainText('Carlos Ruiz · ');
  await expect(conversation.getByRole('log').getByRole('article').filter({ hasText: 'COTIZACIÓN V2' })).toContainText('vía WhatsApp');  // a reply that came in by WhatsApp
  await expect.poll(() => calls.find(call => call.url.endsWith(`/conversations/${order}/read`))?.body).toEqual({ last_message_id: 12 });
  const composer = conversation.getByLabel('Mensaje del acuerdo', { exact: true });
  await composer.fill('Perfecto, el lunes');
  await composer.press('Shift+Enter');  // a new line, not a send
  await composer.pressSequentially('Gracias');
  expect(calls.some(call => call.url === 'messages')).toBe(false);
  await composer.press('Enter');
  await expect(conversation.getByRole('log')).toContainText('Perfecto, el lunes\nGracias');
  expect(calls.find(call => call.url === 'messages')?.body).toMatchObject({ body: 'Perfecto, el lunes\nGracias' });  // as typed
  await expect(composer).toHaveValue('');
  await expect(composer).toBeFocused();  // ready for the next message
  await panel.getByRole('button', { name: 'Mostrar conversaciones', exact: true }).click();  // folded while the document shows
  await panel.getByRole('button', { name: /^AUTOPARTES DEL ISTMO/ }).click();
  await expect(panel.getByRole('region', { name: 'Conversación con AUTOPARTES DEL ISTMO', exact: true })).toBeVisible();
  await panel.getByRole('button', { name: 'No leídas', exact: true }).click();
  await expect(panel.getByRole('complementary', { name: 'Tus conversaciones', exact: true }).getByRole('listitem')).toHaveCount(2);
  expect(calls.some(call => call.url.endsWith('?unread=1'))).toBe(true);
  await panel.getByPlaceholder('Orden o cuenta…').fill('0119');
  await expect(panel.getByRole('complementary', { name: 'Tus conversaciones', exact: true }).getByRole('listitem')).toHaveCount(1);
});

test('a new message or notification between polls shows a toast that opens its conversation', async ({ page }) => {
  const summary = { current: pending() };
  await mockInbox(page, summary);
  await page.goto('/');
  await expect(page.getByRole('button', { name: 'Mensajes, 3 sin leer', exact: true })).toBeVisible();
  summary.current = { ...pending(), unread_messages: 4, latest_message: { id: 14, body: 'YA SALIÓ EL PEDIDO.', account_id: sup.id, account_name: sup.name, actor_name: 'Carlos Ruiz', mine: false, created_at: new Date().toISOString(), order_id: order, reference: 'ORD-0123', side: 'client', account: party, counterpart: sup } };
  await page.evaluate(() => window.dispatchEvent(new Event('motionpartes:inbox')));
  const toast = page.getByRole('button', { name: /GRUPO RAZOR PARTS · ORD-0123/ });
  await expect(toast).toContainText('YA SALIÓ EL PEDIDO.');
  await expect(page.getByRole('button', { name: 'Mensajes, 4 sin leer', exact: true })).toBeVisible();
  await toast.click();
  await expect(page.getByRole('dialog', { name: 'Conversaciones', exact: true }).getByRole('region', { name: 'Conversación con GRUPO RAZOR PARTS', exact: true })).toBeVisible();
});

test('mobile: the list, the chat and the document take turns in a full-screen panel', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await mockInbox(page, { current: pending() });
  await page.goto('/');
  await page.getByRole('button', { name: 'Mensajes, 3 sin leer', exact: true }).click();
  const panel = page.getByRole('dialog', { name: 'Conversaciones', exact: true });
  await expect(panel.getByRole('complementary', { name: 'Tus conversaciones', exact: true })).toBeVisible();
  await panel.getByRole('button', { name: /^GRUPO RAZOR PARTS/ }).click();
  await expect(panel.getByRole('complementary', { name: 'Tus conversaciones', exact: true })).toBeHidden();
  await expect(panel.getByRole('log', { name: 'Mensajes del acuerdo', exact: true })).toBeVisible();
  const toggle = panel.getByRole('button', { name: 'Cotizaciones', exact: true });
  await toggle.click();  // the table takes the chat's place
  await expect(panel.getByRole('table', { name: 'Cotizaciones con GRUPO RAZOR PARTS', exact: true })).toBeVisible();
  await expect(panel.getByRole('log', { name: 'Mensajes del acuerdo', exact: true })).toBeHidden();
  await panel.getByRole('button', { name: 'Abrir ORD-0123', exact: true }).click();  // a row opens its document
  await expect(panel.getByRole('region', { name: 'Cotización vigente v2', exact: true })).toBeVisible();
  await panel.getByRole('button', { name: 'Volver a las cotizaciones', exact: true }).click();
  await expect(panel.getByRole('table', { name: 'Cotizaciones con GRUPO RAZOR PARTS', exact: true })).toBeVisible();
  await toggle.click();  // pressed again: back to the chat
  await expect(panel.getByRole('log', { name: 'Mensajes del acuerdo', exact: true })).toBeVisible();
  const box = await panel.boundingBox();
  expect(box!.width).toBeGreaterThanOrEqual(389);
  await panel.getByRole('button', { name: 'Volver a conversaciones', exact: true }).click();
  await expect(panel.getByRole('complementary', { name: 'Tus conversaciones', exact: true })).toBeVisible();
});

test('the bell menu keeps the email notices each person wants, and hides them when the server sends no email', async ({ page }) => {
  await mockInbox(page, { current: pending() });
  const saved: unknown[] = [];
  let enabled = false;
  await page.route(/\/api\/market\/inbox\/email$/, route => {
    if (route.request().method() === 'POST') saved.push(route.request().postDataJSON());
    return route.fulfill({ json: { enabled, address: 'francisco@example.invalid', messages: route.request().method() === 'POST' ? false : true, updates: true } });
  });
  await page.goto('/');
  const bell = page.getByRole('button', { name: 'Notificaciones, 2 nuevas', exact: true });
  await bell.click();
  const menu = page.getByRole('region', { name: 'Notificaciones', exact: true });
  await expect(menu.getByRole('button', { name: /REPUESTOS PANAMÁ confirmó el acuerdo/ })).toBeVisible();
  await expect(menu.getByRole('group', { name: /Avisos por correo/ })).toHaveCount(0);
  await bell.click();
  enabled = true;
  await bell.click();
  const email = menu.getByRole('group', { name: 'Avisos por correo a francisco@example.invalid', exact: true });
  await expect(email.getByRole('checkbox', { name: 'Cambios en tus órdenes', exact: true })).toBeChecked();
  await email.getByRole('checkbox', { name: 'Mensajes nuevos, cuando no estés en MotionPartes', exact: true }).uncheck();
  await expect(email.getByRole('checkbox', { name: 'Mensajes nuevos, cuando no estés en MotionPartes', exact: true })).not.toBeChecked();
  expect(saved).toEqual([{ messages: false }]);
});

test('with the websocket connected, a nudge brings the new message at once (polling slows to a safety net)', async ({ page }) => {
  let socket: { send: (message: string) => void } | null = null;
  await page.routeWebSocket(/\/ws\/inbox\/$/, ws => { socket = ws; });  // stands in for the realtime service
  const summary = { current: pending() };
  await mockInbox(page, summary);
  const later = { id: 13, message_id: 'd', body: '¿Confirmamos el lunes?', account_id: sup.id, account_name: sup.name, actor_name: 'Carlos Ruiz', created_at: new Date().toISOString() };
  let current = messages;
  await page.route(/\/deals\/[^/]+\/messages(\?.*)?$/, route => route.fulfill({ json: { results: route.request().url().includes('after=') ? current.filter(item => item.id > 12) : current, cursor: current.at(-1)!.id, has_more: false, has_earlier: false, assistant_notice: false } }));
  await page.goto('/');
  await expect.poll(() => socket !== null).toBe(true);
  await page.getByRole('button', { name: 'Mensajes, 3 sin leer', exact: true }).click();
  const log = page.getByRole('dialog', { name: 'Conversaciones', exact: true }).getByRole('log', { name: 'Mensajes del acuerdo', exact: true });
  await expect(log).toContainText('SÍ, PODEMOS ENTREGAR EL LUNES ANTES DE LAS 12.');
  current = [...messages, later];
  summary.current = { ...pending(), unread_messages: 4 };
  await page.waitForTimeout(3500);  // past the 3-second poll: while connected it waits 30 s
  await expect(log).not.toContainText('¿Confirmamos el lunes?');
  socket!.send(JSON.stringify({ kind: 'message', order_id: order }));
  await expect(log).toContainText('¿Confirmamos el lunes?', { timeout: 1500 });
  await expect(page.getByRole('button', { name: 'Mensajes, 4 sin leer', exact: true })).toBeVisible({ timeout: 1500 });
});

test('WhatsApp notices need the number, an explicit consent and the code sent there; each kind can be turned off', async ({ page }) => {
  await mockInbox(page, { current: pending() });
  const saved: { url: string; body: Record<string, unknown> }[] = [];
  let state = { enabled: false, number: '', opted_in: false, verified: false, active: false, pending_number: '', code_sent_at: null as string | null,
    code_expires_at: null as string | null, resend_at: null as string | null, messages: true, updates: true };
  await page.route(/\/api\/market\/inbox\/whatsapp(\/verify)?$/, route => {
    if (route.request().method() === 'POST') {
      const verify = new URL(route.request().url()).pathname.endsWith('/verify'), body = route.request().postDataJSON();
      saved.push({ url: verify ? 'verify' : 'whatsapp', body });
      if (verify) {
        if (body.code !== '123456') return route.fulfill({ status: 400, json: { code: ['El código no es correcto. Te quedan 4 intentos.'] } });
        state = { ...state, number: state.pending_number, opted_in: true, verified: true, active: true, pending_number: '', code_sent_at: null, code_expires_at: null, resend_at: null };
      } else if (body.number) {
        const sent = Date.now();
        state = { ...state, pending_number: '+50761234567', code_sent_at: new Date(sent).toISOString(), code_expires_at: new Date(sent + 600000).toISOString(), resend_at: new Date(sent + 60000).toISOString() };
      } else {
        state = { ...state, ...('opt_in' in body ? { opted_in: body.opt_in, active: body.opt_in && state.verified } : {}), ...('updates' in body ? { updates: body.updates } : {}) };
      }
    }
    return route.fulfill({ json: state });
  });
  await page.goto('/');
  const bell = page.getByRole('button', { name: 'Notificaciones, 2 nuevas', exact: true });
  await bell.click();
  const menu = page.getByRole('region', { name: 'Notificaciones', exact: true });
  await expect(menu.getByRole('button', { name: /REPUESTOS PANAMÁ confirmó el acuerdo/ })).toBeVisible();
  await expect(menu.getByRole('group', { name: 'Avisos por WhatsApp', exact: true })).toHaveCount(0);  // the server sends none
  await bell.click();
  state = { ...state, enabled: true };
  await bell.click();
  const whatsapp = menu.getByRole('group', { name: 'Avisos por WhatsApp', exact: true });
  await whatsapp.getByLabel('Tu número de WhatsApp').fill('6123 4567');
  const send = whatsapp.getByRole('button', { name: 'Enviar código', exact: true });
  await expect(send).toBeDisabled();  // consent first
  await whatsapp.getByRole('checkbox', { name: /Acepto recibir avisos de MotionPartes por WhatsApp/ }).check();
  await send.click();
  await expect(whatsapp).toContainText('Te enviamos un código por WhatsApp a +50761234567. Escríbelo para activar los avisos.');
  expect(saved[0]).toEqual({ url: 'whatsapp', body: { number: '6123 4567', opt_in: true } });
  await expect(whatsapp.getByRole('button', { name: /^Reenviar en \d+ s$/ })).toBeDisabled();  // a minute between codes
  const code = whatsapp.getByLabel('Código de verificación', { exact: true });
  await code.fill('111111');
  await whatsapp.getByRole('button', { name: 'Confirmar', exact: true }).click();
  await expect(whatsapp.getByRole('alert')).toContainText('El código no es correcto. Te quedan 4 intentos.');
  await code.fill('123456');
  await whatsapp.getByRole('button', { name: 'Confirmar', exact: true }).click();
  await expect(whatsapp).toContainText('Activos en +50761234567Verificado');
  expect(saved.at(-1)).toEqual({ url: 'verify', body: { code: '123456' } });
  await whatsapp.getByRole('checkbox', { name: 'Cambios en tus órdenes', exact: true }).uncheck();
  await expect.poll(() => saved.at(-1)?.body).toEqual({ updates: false });
  await whatsapp.getByRole('button', { name: 'Desactivar', exact: true }).click();
  await expect.poll(() => saved.at(-1)?.body).toEqual({ opt_in: false });
  await expect(whatsapp.getByLabel('Tu número de WhatsApp')).toHaveValue('+50761234567');
});

test('like a desktop chat app: the Cotizaciones table on the right opens each document, the side columns resize and the layout is remembered', async ({ page }) => {
  await mockInbox(page, { current: pending() });
  await page.goto('/');
  await page.getByRole('button', { name: 'Mensajes, 3 sin leer', exact: true }).click();
  const panel = page.getByRole('dialog', { name: 'Conversaciones', exact: true });
  const docsToggle = panel.getByRole('button', { name: 'Cotizaciones', exact: true });
  const side = panel.locator('.inbox-side');
  const chat = panel.getByRole('log', { name: 'Mensajes del acuerdo', exact: true });
  const table = panel.getByRole('table', { name: 'Cotizaciones con GRUPO RAZOR PARTS', exact: true });
  await expect(docsToggle).toHaveAttribute('aria-pressed', 'true');
  await expect(table.getByRole('columnheader')).toHaveText(['Documento', 'Estado', 'Artículos', 'Total']);
  const row = table.getByRole('row').filter({ hasText: 'ORD-0123' });
  await expect(row).toHaveAttribute('aria-current', 'true');  // the order the chat writes about
  for (const text of ['Cotizada', '3', '7 u.', '612.40', 'v2']) await expect(row).toContainText(text);
  await row.getByRole('cell').nth(1).click();  // the whole row opens its document in the table's place
  await expect(panel.getByRole('region', { name: 'Cotización vigente v2', exact: true })).toBeVisible();  // on its quotation
  await expect(table).toBeHidden();
  // One document: each item asked for with what the quotation offers of it. The column widens for it, so its prices fit without scrolling.
  await expect.poll(async () => Math.round((await side.boundingBox())!.width)).toBeGreaterThan(540);
  const lines = panel.getByRole('region', { name: 'Líneas de la cotización v2', exact: true });
  await expect(lines.getByRole('columnheader')).toHaveText(['Código / descripción', 'Solicitadas', 'Ofrecidas', 'Precio', 'Importe']);
  await expect(lines.getByRole('row').filter({ hasText: '58411-1R000-G' })).toContainText('USD 86.50');
  expect(await lines.locator('.ag-center-cols-viewport').evaluate(element => element.scrollWidth > element.clientWidth)).toBe(false);
  await panel.getByRole('button', { name: 'Ampliar la tabla', exact: true }).click();  // every column, as large as the screen
  const large = page.getByRole('dialog', { name: 'ORD-0123 · Cotización v2', exact: true });
  await expect(large.getByRole('columnheader')).toHaveText(['Código / descripción', 'Solicitadas', 'Ofrecidas', 'Precio unitario', 'Importe', 'SKU interno']);
  await large.getByRole('button', { name: 'Cerrar ventana', exact: true }).click();
  await expect(large).toHaveCount(0);
  await expect(panel).toBeVisible();
  const tabs = panel.getByRole('navigation', { name: 'Secciones del acuerdo', exact: true });
  await expect(tabs.getByRole('button')).toHaveText(['Cotización', 'Actividad']);
  await tabs.getByRole('button', { name: 'Actividad', exact: true }).click();
  await expect(panel.getByRole('region', { name: 'Cotización vigente v2', exact: true })).toBeHidden();
  await panel.getByRole('button', { name: 'Volver a las cotizaciones', exact: true }).click();
  await expect(table).toBeVisible();
  await docsToggle.click();  // pressed again: the column hides, the chat stays
  await expect(side).toBeHidden();
  await expect(chat).toBeVisible();
  await docsToggle.click();
  const width = async () => Math.round((await side.boundingBox())!.width);
  const before = await width();
  await panel.getByRole('separator', { name: 'Ajustar el ancho de las cotizaciones', exact: true }).focus();
  for (let i = 0; i < 3; i++) await page.keyboard.press('ArrowLeft');  // the column grows to the left
  await expect.poll(width).toBe(before + 72);
  const list = panel.getByRole('complementary', { name: 'Tus conversaciones', exact: true });
  const listBefore = Math.round((await list.boundingBox())!.width);
  const divider = (await panel.getByRole('separator', { name: 'Ajustar el ancho de las conversaciones', exact: true }).boundingBox())!;
  await page.mouse.move(divider.x + divider.width / 2, divider.y + 200);
  await page.mouse.down(); await page.mouse.move(divider.x + 60, divider.y + 200, { steps: 4 }); await page.mouse.up();
  await expect.poll(async () => Math.round((await list.boundingBox())!.width)).toBeGreaterThan(listBefore + 40);
  await panel.getByRole('button', { name: 'Ocultar conversaciones', exact: true }).click();
  await expect(list).toHaveCount(0);
  const toggle = panel.getByRole('button', { name: 'Mostrar conversaciones', exact: true });
  await chat.hover();
  await toggle.hover();  // hovering the hidden list's button shows it floating over the chat
  await expect(list).toBeVisible();
  await expect(list).toHaveClass(/floating/);
  const chatBox = (await chat.boundingBox())!;
  expect(Math.round((await list.boundingBox())!.x)).toBeLessThan(chatBox.x + 20);  // over the chat, which keeps its place
  await chat.hover({ position: { x: chatBox.width - 40, y: chatBox.height / 2 } });  // the pointer leaves: it hides again
  await expect(list).toBeHidden();
  await toggle.hover();
  await list.getByPlaceholder('Orden o cuenta…').click();
  await page.keyboard.press('Escape');  // Escape hides it, never the panel
  await expect(list).toBeHidden();
  await expect(panel).toBeVisible();
  await toggle.hover();
  await list.getByRole('button', { name: /^AUTOPARTES DEL ISTMO/ }).click();  // choosing a conversation there hides it too
  await expect(panel.getByRole('region', { name: 'Conversación con AUTOPARTES DEL ISTMO', exact: true })).toBeVisible();
  await expect(list).toBeHidden();
  await panel.getByRole('button', { name: 'Cerrar ventana', exact: true }).click();
  await page.getByRole('button', { name: 'Mensajes, 3 sin leer', exact: true }).click();
  await expect(panel.getByRole('button', { name: 'Mostrar conversaciones', exact: true })).toBeVisible();  // remembered
  await expect.poll(width).toBe(before + 72);
});

test('like a desktop sidebar: one line per conversation, pinned ones first, the person’s own groups, kept on the server', async ({ page }) => {
  await mockInbox(page, { current: pending() });
  type Row = (typeof conversations)[number] & { pinned: boolean; group: string | null };
  const rows: Row[] = conversations.map(row => ({ ...row, pinned: false, group: null }));
  let groups: { id: string; name: string }[] = [];
  const writes: { url: string; method: string; body: unknown }[] = [];
  const listed = () => {
    const at = (row: Row) => row.pinned ? [0, 0] : row.group ? [1, groups.findIndex(group => group.id === row.group)] : [2, 0];
    return [...rows].sort((a, b) => at(a)[0] - at(b)[0] || at(a)[1] - at(b)[1] || b.activity.localeCompare(a.activity));
  };
  await page.route(/\/api\/market\/inbox\/conversations(\?.*)?$/, route => route.fulfill({ json: { count: rows.length, next: null, previous: null, results: listed(),
    groups: groups.map(group => ({ ...group, count: rows.filter(row => !row.pinned && row.group === group.id).length })) } }));
  await page.route(/\/api\/market\/inbox\/(groups.*|conversations\/[^/]+\/place)$/, route => {
    const request = route.request(), path = new URL(request.url()).pathname.replace('/api/market/inbox/', ''), body = request.method() === 'POST' ? request.postDataJSON() : null;
    writes.push({ url: path, method: request.method(), body });
    if (path === 'groups') {
      const group = { id: `aaaaaaaa-aaaa-4aaa-8aaa-00000000000${groups.length + 1}`, name: body.name.trim() };
      groups.push(group); return route.fulfill({ status: 201, json: { ...group, count: 0 } });
    }
    const id = path.split('/')[1];
    if (path.startsWith('groups/') && request.method() === 'DELETE') {
      groups = groups.filter(group => group.id !== id); rows.forEach(row => { if (row.group === id) row.group = null; });
      return route.fulfill({ status: 204 });
    }
    if (path.startsWith('groups/')) { groups.find(group => group.id === id)!.name = body.name; return route.fulfill({ json: { id, name: body.name, count: 0 } }); }
    Object.assign(rows.find(row => row.id === id)!, body);
    return route.fulfill({ json: { order_id: id } });
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'Mensajes, 3 sin leer', exact: true }).click();
  const panel = page.getByRole('dialog', { name: 'Conversaciones', exact: true });
  const list = panel.getByRole('complementary', { name: 'Tus conversaciones', exact: true });
  const row = (counterpart: string) => list.getByRole('listitem').filter({ has: page.getByRole('button', { name: `Organizar ${counterpart}`, exact: true }) });
  const istmo = list.getByRole('button', { name: /^AUTOPARTES DEL ISTMO/ });
  await expect(istmo).toHaveText(/^AUTOPARTES DEL ISTMOORD-0119, En revisión1$/);  // a single line: the other party, the reference, the unread count
  await expect(istmo).toHaveAttribute('title', /¿EL DISCO ES VENTILADO\?/);  // the last message only in its tooltip
  await expect(list.getByRole('group')).toHaveCount(1);  // no Fijadas, groups or Sin grupo headings until the person organizes the list

  await list.getByRole('button', { name: 'Nuevo grupo', exact: true }).click();
  await list.getByLabel('Nombre del nuevo grupo', { exact: true }).fill('Urgentes');
  await list.getByLabel('Nombre del nuevo grupo', { exact: true }).press('Enter');
  const urgent = list.getByRole('group', { name: 'Urgentes', exact: true });
  await expect(urgent).toContainText('Vacío.');
  await row('AUTOPARTES DEL ISTMO').getByRole('button', { name: 'Organizar AUTOPARTES DEL ISTMO', exact: true }).click();
  const menu = list.getByRole('menu', { name: 'Organizar AUTOPARTES DEL ISTMO', exact: true });
  await expect(menu.getByRole('menuitem').first()).toBeFocused();
  await page.keyboard.press('Escape');  // closes the menu, never the panel
  await expect(menu).toHaveCount(0);
  await expect(panel).toBeVisible();
  await row('AUTOPARTES DEL ISTMO').getByRole('button', { name: 'Organizar AUTOPARTES DEL ISTMO', exact: true }).click();
  await menu.getByRole('menuitemradio', { name: 'Mover a Urgentes', exact: true }).click();
  await expect(urgent.getByRole('listitem')).toHaveText([/AUTOPARTES DEL ISTMO/]);
  expect(writes.at(-1)).toEqual({ url: `conversations/${conversations[1].id}/place`, method: 'POST', body: { group: groups[0].id, pinned: false } });

  await row('GRUPO RAZOR PARTS').getByRole('button', { name: 'Organizar GRUPO RAZOR PARTS', exact: true }).click();
  await list.getByRole('menuitem', { name: 'Fijar', exact: true }).click();
  await expect(list.getByRole('group', { name: 'Fijadas', exact: true }).getByRole('listitem')).toHaveText([/GRUPO RAZOR PARTS/]);
  await row('REPUESTOS PANAMÁ').dragTo(urgent);  // or dragged onto a group
  await expect(urgent.getByRole('listitem')).toHaveCount(2);
  expect(writes.at(-1)).toEqual({ url: `conversations/${conversations[2].id}/place`, method: 'POST', body: { group: groups[0].id, pinned: false } });
  await expect(list.getByRole('group', { name: 'Sin grupo', exact: true }).getByRole('listitem')).toHaveCount(0);

  await urgent.getByRole('button', { name: 'Urgentes', exact: true }).click();  // folded, with its unread count on the heading
  await expect(urgent.getByRole('listitem')).toHaveCount(0);
  await expect(urgent.getByLabel('1 sin leer', { exact: true })).toBeVisible();
  await panel.getByRole('button', { name: 'Cerrar ventana', exact: true }).click();
  await page.getByRole('button', { name: 'Mensajes, 3 sin leer', exact: true }).click();
  await expect(urgent.getByRole('button', { name: /^Urgentes/ })).toHaveAttribute('aria-expanded', 'false');  // remembered

  await urgent.getByRole('button', { name: 'Opciones del grupo Urgentes', exact: true }).click();
  await list.getByRole('menuitem', { name: 'Cambiar nombre', exact: true }).click();
  await list.getByLabel('Nuevo nombre de Urgentes', { exact: true }).fill('Por cobrar');
  await list.getByLabel('Nuevo nombre de Urgentes', { exact: true }).press('Enter');
  const renamed = list.getByRole('group', { name: 'Por cobrar', exact: true });
  await expect(renamed).toBeVisible();
  await renamed.getByRole('button', { name: 'Opciones del grupo Por cobrar', exact: true }).click();
  await list.getByRole('menuitem', { name: 'Eliminar grupo…', exact: true }).click();
  await expect(list.getByRole('menu')).toContainText('¿Eliminar «Por cobrar»? Sus conversaciones quedan en Sin grupo.');
  await list.getByRole('menuitem', { name: 'Eliminar grupo', exact: true }).click();
  await expect(renamed).toHaveCount(0);
  await expect(list.getByRole('group', { name: 'Sin grupo', exact: true }).getByRole('listitem')).toHaveCount(2);  // its conversations stay
  expect(writes.map(write => `${write.method} ${write.url.replace(/[a-f0-9-]{36}/, 'ID')}`)).toEqual(['POST groups', 'POST conversations/ID/place', 'POST conversations/ID/place',
    'POST conversations/ID/place', 'POST groups/ID', 'DELETE groups/ID']);
});

test('someone who buys and sells sees Compras and Ventas apart; the choice is remembered and a purchase opened elsewhere shows Compras', async ({ page }) => {
  await mockInbox(page, { current: pending() });
  const rows = conversations.map(row => row.order_id === order2 ? { ...row, side: 'supplier' } : row);  // they sell to AUTOPARTES DEL ISTMO
  await page.route(/\/accounts\/[^/]+\/requests\/[^/?]+$/, route => route.fulfill({ json: { ...deal, id: order2, reference: 'ORD-0119' } }));  // the sale, seen as its supplier
  const sides = { client: { conversations: 2, unread: 2 }, supplier: { conversations: 1, unread: 1 } };
  const asked: string[] = [];
  await page.route(/\/api\/market\/inbox\/conversations(\?.*)?$/, route => {
    const side = new URL(route.request().url()).searchParams.get('side'); asked.push(side ?? 'all');
    const results = rows.filter(row => !side || row.side === side);
    return route.fulfill({ json: { count: results.length, next: null, previous: null, results, groups: [], sides } });
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'Mensajes, 3 sin leer', exact: true }).click();
  const panel = page.getByRole('dialog', { name: 'Conversaciones', exact: true });
  const list = panel.getByRole('complementary', { name: 'Tus conversaciones', exact: true });
  const switcher = list.getByRole('group', { name: 'Compras y ventas', exact: true });
  await expect(switcher.getByRole('button')).toHaveText(['Ambas', 'Compras2', 'Ventas1']);  // with each side's unread messages
  await expect(list.getByRole('button', { name: /^AUTOPARTES DEL ISTMO/ })).toContainText('Venta');
  await expect(list.getByRole('button', { name: /^GRUPO RAZOR PARTS/ })).toContainText('Compra');
  await switcher.getByRole('button', { name: /^Ventas/ }).click();
  await expect(list.getByRole('listitem')).toHaveCount(1);
  await expect(list.getByRole('button', { name: /^AUTOPARTES DEL ISTMO/ })).not.toContainText('Venta');  // one side: no tags
  expect(asked.at(-1)).toBe('supplier');
  await panel.getByRole('button', { name: 'Cerrar ventana', exact: true }).click();
  await page.getByRole('button', { name: 'Mensajes, 3 sin leer', exact: true }).click();
  await expect(switcher.getByRole('button', { name: /^Ventas/ })).toHaveAttribute('aria-pressed', 'true');  // remembered
  await panel.getByRole('button', { name: 'Cerrar ventana', exact: true }).click();
  await page.getByRole('button', { name: 'Notificaciones, 2 nuevas', exact: true }).click();
  await page.getByRole('region', { name: 'Notificaciones', exact: true }).getByRole('button', { name: /GRUPO RAZOR PARTS publicó la cotización v2/ }).click();
  await panel.getByRole('button', { name: 'Mostrar conversaciones', exact: true }).click();  // folded while the order's document shows
  await expect(switcher.getByRole('button', { name: /^Compras/ })).toHaveAttribute('aria-pressed', 'true');  // the purchase it opened
  await expect(list.getByRole('button', { name: /^GRUPO RAZOR PARTS/ })).toHaveAttribute('aria-current', 'true');
});

test('a dated reference shows its last part on the conversation line, whole in its tooltip', async ({ page }) => {
  await mockInbox(page, { current: pending() });
  const dated = { ...conversations[0], reference: 'ORD-20261010-55791743', orders: [{ ...conversations[0].orders[0], reference: 'ORD-20261010-55791743' }] };
  await page.route(/\/api\/market\/inbox\/conversations(\?.*)?$/, route => route.fulfill({ json: { count: 1, next: null, previous: null, results: [dated] } }));
  await page.goto('/');
  await page.getByRole('button', { name: 'Mensajes, 3 sin leer', exact: true }).click();
  const line = page.getByRole('complementary', { name: 'Tus conversaciones', exact: true }).getByRole('button', { name: /^GRUPO RAZOR PARTS/ });
  await expect(line).toContainText('…55791743');
  await expect(line).not.toContainText('ORD-20261010');
  await expect(line).toHaveAttribute('title', /^GRUPO RAZOR PARTS\nORD-20261010-55791743 · Cotizada/);  // its orders, whole, in the tooltip
});

test('a page left open while a newer build went live offers to reload, over the conversations panel too', async ({ page }) => {
  await mockInbox(page, { current: pending() });
  let checks = 0;
  await page.route('**/build-version', route => { checks++; return route.fulfill({ json: { deployment: 'otra-version' } }); });
  await page.goto('/');
  const notice = page.getByText('Hay una versión nueva de MotionPartes.', { exact: true });
  await expect(notice).toBeVisible();
  await page.getByRole('button', { name: 'Mensajes, 3 sin leer', exact: true }).click();
  const panel = page.getByRole('dialog', { name: 'Conversaciones', exact: true });
  await expect(panel.getByText('Hay una versión nueva de MotionPartes.', { exact: true })).toBeVisible();  // above the dialog's top layer
  await Promise.all([page.waitForEvent('load'), panel.getByRole('button', { name: 'Recargar', exact: true }).click()]);
  await expect(notice).toBeVisible();  // still newer after the reload: the route keeps answering another build
  await page.getByRole('button', { name: 'Ahora no', exact: true }).click();
  await expect(notice).toHaveCount(0);
  expect(checks).toBeGreaterThanOrEqual(2);
});

test('a notice link opened signed out goes straight to the sign-in with the username filled in, then opens the conversation', async ({ page }) => {
  await mockInbox(page, { current: pending() });
  let signedIn = false;
  const hints: string[] = [];
  await page.route('**/api/session', route => {
    if (route.request().method() === 'POST') {
      expect(route.request().postDataJSON()).toEqual({ username: 'FRANCISCO', password: 'Clave938!' });
      signedIn = true;
      return route.fulfill({ json: { authenticated: true } });
    }
    return route.fulfill({ json: signedIn ? { authenticated: true, user: { id: 1, username: 'FRANCISCO', first_name: 'Francisco', last_name: '', is_superuser: false },
      accounts: { count: 1, next: null, previous: null, results: [acct] } } : { authenticated: false } });
  });
  await page.route(/\/api\/session\/hint\?/, route => { hints.push(new URL(route.request().url()).searchParams.get('hint')!); return route.fulfill({ json: { username: 'FRANCISCO' } }); });
  await page.goto(`/?conversacion=${order}~MQ:1xFlI0:firma-de-prueba`);
  const signIn = page.getByRole('dialog', { name: 'Bienvenido a MotionPartes', exact: true });
  await expect(signIn.getByRole('status')).toContainText('Inicia sesión para abrir la conversación de tu aviso.');
  await expect(signIn.getByLabel('Usuario', { exact: true })).toHaveValue('FRANCISCO');
  await expect(signIn.getByLabel('Contraseña', { exact: true })).toBeFocused();  // only the password is left
  expect(hints).toEqual(['MQ:1xFlI0:firma-de-prueba']);
  await signIn.getByLabel('Contraseña', { exact: true }).fill('Clave938!');
  await signIn.locator('form').getByRole('button', { name: 'Iniciar sesión' }).click();
  await expect(page.getByRole('dialog', { name: 'Conversaciones', exact: true }).getByRole('region', { name: 'Conversación con GRUPO RAZOR PARTS', exact: true })).toBeVisible();
});

test('the WhatsApp assistant’s cesta link opened signed out goes to the sign-in, then shows that account’s cesta without the hint', async ({ page }) => {
  await mockInbox(page, { current: pending() });
  let signedIn = false;
  await page.route('**/api/session', route => {
    if (route.request().method() === 'POST') { signedIn = true; return route.fulfill({ json: { authenticated: true } }); }
    return route.fulfill({ json: signedIn ? { authenticated: true, user: { id: 1, username: 'FRANCISCO', first_name: 'Francisco', last_name: '', is_superuser: false },
      accounts: { count: 1, next: null, previous: null, results: [acct] } } : { authenticated: false } });
  });
  await page.route(/\/api\/session\/hint\?/, route => route.fulfill({ json: { username: 'FRANCISCO' } }));
  await fakeServerBasket(page);
  await page.goto(`/?vista=cesta&cuenta=${acct.id}&ingreso=MQ:1xFlI0:firma-de-prueba`);
  const signIn = page.getByRole('dialog', { name: 'Bienvenido a MotionPartes', exact: true });
  await expect(signIn.getByRole('status')).toContainText('Inicia sesión para ver tu cesta.');
  await expect(signIn.getByLabel('Usuario', { exact: true })).toHaveValue('FRANCISCO');
  await signIn.getByLabel('Contraseña', { exact: true }).fill('Clave938!');
  await signIn.locator('form').getByRole('button', { name: 'Iniciar sesión' }).click();
  await expect(page.getByRole('main', { name: 'Cesta de solicitudes', exact: true })).toContainText('Tu cesta se guarda en tu cuenta');
  await expect.poll(() => new URL(page.url()).searchParams.has('ingreso')).toBe(false);
  expect(new URL(page.url()).searchParams.get('vista')).toBe('cesta');
});

test.describe('on a touch phone', () => {
  test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
  test('fields are 16px, so iOS does not zoom in when the chat composer gets focus', async ({ page }) => {
    await mockInbox(page, { current: pending() });
    await page.goto('/');
    await page.getByRole('button', { name: 'Mensajes, 3 sin leer', exact: true }).click();
    const panel = page.getByRole('dialog', { name: 'Conversaciones', exact: true });
    await expect(panel.getByPlaceholder('Orden o cuenta…')).toHaveCSS('font-size', '16px');
    await panel.getByRole('button', { name: /^GRUPO RAZOR PARTS/ }).click();
    const composer = panel.getByLabel('Mensaje del acuerdo', { exact: true });
    await expect(composer).toHaveCSS('font-size', '16px');
    await composer.focus();
    expect(await page.evaluate(() => window.visualViewport?.scale ?? 1)).toBe(1);
  });
});

test('the chat goes all the way down when a message is sent or arrives; reading earlier ones, it offers the new one instead', async ({ page }) => {
  await mockInbox(page, { current: pending() });
  const chat = Array.from({ length: 30 }, (_, index) => ({ id: index + 1, message_id: `m${index}`, body: `MENSAJE ${index + 1}`, account_id: index % 2 ? sup.id : acct.id,
    account_name: index % 2 ? sup.name : acct.name, actor_name: index % 2 ? 'Carlos Ruiz' : 'Francisco', created_at: iso(60 - index) }));
  const arrive = (id: number, body: string) => chat.push({ id, message_id: `r${id}`, body, account_id: sup.id, account_name: sup.name, actor_name: 'Carlos Ruiz', created_at: new Date().toISOString() });
  await page.route(/\/deals\/[^/]+\/messages(\?.*)?$/, route => {
    if (route.request().method() === 'POST') {
      const body = route.request().postDataJSON();
      const sent = { id: 500 + chat.length, message_id: body.message_id, body: body.body, account_id: acct.id, account_name: acct.name, actor_name: 'Francisco', created_at: new Date().toISOString() };
      chat.push(sent);
      return route.fulfill({ json: sent });
    }
    const after = Number(new URL(route.request().url()).searchParams.get('after') || 0);
    return route.fulfill({ json: { results: chat.filter(message => message.id > after), cursor: Math.max(...chat.map(message => message.id)), has_more: false, has_earlier: false } });
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'Mensajes, 3 sin leer', exact: true }).click();
  const log = page.getByRole('dialog', { name: 'Conversaciones', exact: true }).getByRole('log', { name: 'Mensajes del acuerdo', exact: true });
  const below = () => log.evaluate(element => Math.round(element.scrollHeight - element.scrollTop - element.clientHeight));
  await expect(log).toContainText('MENSAJE 30');
  await expect.poll(below).toBeLessThan(2);  // opens on the latest
  arrive(200, 'LLEGÓ AHORA');
  await expect(log).toContainText('LLEGÓ AHORA', { timeout: 8000 });
  await expect.poll(below).toBeLessThan(2);  // a message arrives: all the way down
  await log.evaluate(element => { element.scrollTop = 0; element.dispatchEvent(new Event('scroll')); });  // reading earlier messages
  arrive(201, 'OTRO MÁS');
  const newer = page.getByRole('button', { name: 'Mensajes nuevos', exact: true });
  await expect(newer).toBeVisible({ timeout: 8000 });
  expect(await below()).toBeGreaterThan(100);  // left where they were reading
  await newer.click();
  await expect.poll(below).toBeLessThan(2);
  await expect(newer).toHaveCount(0);
  await log.evaluate(element => { element.scrollTop = 0; element.dispatchEvent(new Event('scroll')); });
  const composer = page.getByLabel('Mensaje del acuerdo', { exact: true });
  await composer.fill('Mi respuesta');
  await composer.press('Enter');
  await expect(log).toContainText('Mi respuesta');
  await expect.poll(below).toBeLessThan(2);  // sending: all the way down
});

test('the chat shows its files (a voice note with its transcript) and sends photos and documents, with or without text', async ({ page }) => {
  await mockInbox(page, { current: pending() });
  const PNG = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==', 'base64');
  const file = (id: number, kind: string, name: string, content_type: string, extra = {}) => ({ id, kind, name, content_type, size: 48213, voice: false, failed: false, transcript: '', ...extra });
  const chat = [
    { ...messages[0], id: 20, body: '¿ASÍ ES EL TAMBOR?', attachments: [file(501, 'image', 'tambor.jpg', 'image/jpeg')] },
    { ...messages[1], id: 21, body: '', channel: 'whatsapp', attachments: [file(502, 'audio', 'audio-20261011.ogg', 'audio/ogg', { voice: true, transcript: 'Sí, ese mismo, lo tengo en bodega.' })] },
    { ...messages[2], id: 22, body: 'TE PASO LA LISTA', attachments: [file(503, 'document', 'lista de precios.pdf', 'application/pdf', { size: 2.4 * 1024 * 1024 }), file(504, 'video', 'video.mp4', 'video/mp4', { failed: true, retryable: true })] },
  ];
  const sent: { type: string; body: string }[] = [];
  await page.route(/\/deals\/[^/]+\/messages(\?.*)?$/, route => route.fulfill({ json: { results: route.request().url().includes('after=') ? [] : chat, cursor: 22, has_more: false, has_earlier: false, assistant_notice: false } }));
  await page.route(/\/deals\/[^/]+\/attachments\/\d+$/, route => route.fulfill({ body: Buffer.from(''), contentType: 'application/octet-stream' }));
  await page.route(/\/deals\/[^/]+\/attachments\/501$/, route => route.fulfill({ body: PNG, contentType: 'image/png' }));
  let retried = 0;
  await page.route(/\/deals\/[^/]+\/attachments\/504\/retry$/, route => {
    retried += 1;
    return route.fulfill({ json: { ...chat[2], attachments: [chat[2].attachments[0], file(504, 'video', 'video.mp4', 'video/mp4')] } });
  });
  await page.route(/\/deals\/[^/]+\/attachments$/, route => {
    const request = route.request();
    sent.push({ type: request.headers()['content-type'] || '', body: request.postDataBuffer()?.toString('latin1') || '' });
    const reply = { id: 30, message_id: 'n', body: '', account_id: acct.id, account_name: acct.name, actor_name: 'Francisco', created_at: new Date().toISOString(), attachments: [file(505, 'document', 'factura.pdf', 'application/pdf')] };
    chat.push(reply);
    return route.fulfill({ json: reply });
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'Mensajes, 3 sin leer', exact: true }).click();
  const log = page.getByRole('dialog', { name: 'Conversaciones', exact: true }).getByRole('log', { name: 'Mensajes del acuerdo', exact: true });
  await expect(log.getByRole('link', { name: 'Abrir tambor.jpg', exact: true }).getByRole('img', { name: 'tambor.jpg' })).toBeVisible();
  await log.getByRole('img', { name: 'tambor.jpg' }).scrollIntoViewIfNeeded();  // photos load as they come into view
  await expect.poll(() => log.getByRole('img', { name: 'tambor.jpg' }).evaluate(image => (image as HTMLImageElement).naturalWidth)).toBe(1);  // loaded
  const voice = log.getByRole('article').filter({ has: page.getByLabel('Nota de voz', { exact: true }) });
  await expect(voice).toContainText('vía WhatsApp');
  await expect(voice.getByRole('figure')).toContainText('Transcripción automáticaSí, ese mismo, lo tengo en bodega.');
  await expect(log.getByRole('link', { name: /lista de precios\.pdf/ })).toContainText('2,4 MB');
  await expect(log).toContainText('No se pudo recibir el video enviado por WhatsApp.');
  await log.getByRole('button', { name: 'Reintentar', exact: true }).click();  // asked of WhatsApp again
  await expect(log.getByLabel('video.mp4', { exact: true })).toBeVisible();
  await expect(log).not.toContainText('No se pudo recibir');
  expect(retried).toBe(1);

  const conversation = page.getByRole('region', { name: 'Conversación con GRUPO RAZOR PARTS', exact: true });
  const picker = conversation.getByLabel('Elegir archivos', { exact: true });
  await picker.setInputFiles({ name: 'grande.mp4', mimeType: 'video/mp4', buffer: Buffer.alloc(17 * 1024 * 1024) });
  await expect(conversation.getByRole('alert')).toContainText('«grande.mp4» pesa más de 16 MB.');
  await picker.setInputFiles([{ name: 'factura.pdf', mimeType: 'application/pdf', buffer: Buffer.from('%PDF-1.4') }, { name: 'otra.pdf', mimeType: 'application/pdf', buffer: Buffer.from('%PDF-1.4') }]);
  const attached = conversation.getByRole('list', { name: 'Archivos para enviar', exact: true });
  await expect(attached.getByRole('listitem')).toHaveCount(2);
  await attached.getByRole('button', { name: 'Quitar otra.pdf', exact: true }).click();
  await expect(attached.getByRole('listitem')).toHaveText([/factura\.pdf/]);
  const composer = conversation.getByLabel('Mensaje del acuerdo', { exact: true });
  await expect(composer).toHaveAttribute('placeholder', 'Agrega un mensaje (opcional)…');
  await conversation.getByRole('button', { name: 'Enviar mensaje', exact: true }).click();  // a file and no text
  await expect(log.getByRole('link', { name: /factura\.pdf/ })).toBeVisible();
  expect(sent).toHaveLength(1);
  expect(sent[0].type).toContain('multipart/form-data');
  expect(sent[0].body).toContain('filename="factura.pdf"');
  expect(sent[0].body).not.toContain('otra.pdf');
  await expect(attached).toHaveCount(0);
});

test('one conversation per supplier: its orders’ messages in one chat with their chips, a filter, and what a message goes about', async ({ page }) => {
  const calls = await mockInbox(page, { current: pending() });
  const both = { ...conversations[0], order_count: 2, open_orders: 2, orders: [{ id: order, reference: 'ORD-0123', status: 'quoted' }, { id: order2, reference: 'ORD-0119', status: 'reviewed' }] };
  await page.route(/\/api\/market\/inbox\/conversations(\?.*)?$/, route => route.fulfill({ json: { count: 1, next: null, previous: null, results: [both] } }));
  const second = { ...deal, id: order2, reference: 'ORD-0119', status: 'reviewed', quotation: null, quotations: [] };
  await page.route(new RegExp(`/sent-requests/${order2}$`), route => route.fulfill({ json: second }));
  await page.route(/\/threads\/[^/]+\/orders$/, route => route.fulfill({ json: { results: both.orders.map(docOf) } }));
  const chat = [
    { id: 40, message_id: 'x1', body: '¿ENVÍAN EL TAMBOR HOY?', account_id: acct.id, account_name: acct.name, actor_name: 'Francisco', created_at: iso(30), order_id: order, order_reference: 'ORD-0123' },
    { id: 41, message_id: 'x2', body: 'EL DISCO LLEGA EL LUNES.', account_id: sup.id, account_name: sup.name, actor_name: 'Carlos Ruiz', created_at: iso(20), order_id: order2, order_reference: 'ORD-0119' },
    { id: 42, message_id: 'x3', body: 'GRACIAS POR SU PREFERENCIA.', account_id: sup.id, account_name: sup.name, actor_name: 'Carlos Ruiz', created_at: iso(10), order_id: null, order_reference: null },
  ];
  const posted: Record<string, unknown>[] = [];
  await page.route(/\/deals\/[^/]+\/messages(\?.*)?$/, route => {
    const url = new URL(route.request().url()), on = url.pathname.split('/').at(-2);
    if (route.request().method() === 'POST') {
      const body = route.request().postDataJSON(); posted.push({ order: on, ...body });
      return route.fulfill({ json: { id: 50, message_id: body.message_id, body: body.body, account_id: acct.id, account_name: acct.name, actor_name: 'Francisco', created_at: new Date().toISOString(),
        order_id: body.general ? null : on, order_reference: body.general ? null : on === order ? 'ORD-0123' : 'ORD-0119' } });
    }
    const shown = url.searchParams.get('solo') === '1' ? chat.filter(message => message.order_id === on) : chat;
    return route.fulfill({ json: { results: url.searchParams.has('after') ? [] : shown, cursor: 42, has_more: false, has_earlier: false, assistant_notice: false, thread_id: both.id } });
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'Mensajes, 3 sin leer', exact: true }).click();
  const panel = page.getByRole('dialog', { name: 'Conversaciones', exact: true });
  await expect(panel.getByRole('complementary', { name: 'Tus conversaciones', exact: true }).getByRole('button', { name: /^GRUPO RAZOR PARTS/ })).toContainText('2 órdenes');
  const conversation = panel.getByRole('region', { name: 'Conversación con GRUPO RAZOR PARTS', exact: true });
  const log = conversation.getByRole('log', { name: 'Mensajes del acuerdo', exact: true });
  await expect(log.getByRole('article')).toHaveCount(3);  // every order's messages, and the general one, in one line
  await expect(log.getByRole('article').nth(0)).toContainText('ORD-0123');
  await expect(log.getByRole('article').nth(1)).toContainText('ORD-0119');
  await expect(log.getByRole('article').nth(2)).toContainText('General');
  const table = conversation.getByRole('table', { name: 'Cotizaciones con GRUPO RAZOR PARTS', exact: true });
  await expect(table.getByRole('row')).toHaveCount(3);  // its two orders under the heading
  await expect(table.getByRole('row').filter({ hasText: 'ORD-0123' })).toHaveAttribute('aria-current', 'true');  // the one in progress, written about
  await expect(conversation.getByRole('button', { name: 'Escribes sobre ORD-0123. Cambiar a general', exact: true })).toBeVisible();

  await conversation.getByRole('button', { name: /^Solo esta orden/ }).click();  // this order's messages
  await expect(log.getByRole('article')).toHaveText([/ENVÍAN EL TAMBOR HOY/]);
  expect(calls.some(call => call.url.includes('read'))).toBe(true);
  await conversation.getByRole('button', { name: /^Solo esta orden/ }).click();
  await expect(log.getByRole('article')).toHaveCount(3);

  await table.getByRole('button', { name: 'Abrir ORD-0119', exact: true }).click();  // the other order: its document beside the chat, and what is written goes about it
  await expect(conversation.locator('.inbox-doc-title')).toContainText('ORD-0119');
  await expect(conversation.getByRole('button', { name: 'Escribes sobre ORD-0119. Cambiar a general', exact: true })).toBeVisible();
  await expect(log.getByRole('article')).toHaveCount(3);  // the same conversation
  const composer = conversation.getByLabel('Mensaje del acuerdo', { exact: true });
  await composer.fill('¿Y la factura?');
  await composer.press('Enter');
  await expect.poll(() => posted.at(-1)).toMatchObject({ order: order2, body: '¿Y la factura?', general: false });
  await conversation.getByRole('button', { name: 'Escribes sobre ORD-0119. Cambiar a general', exact: true }).click();
  await composer.fill('Saludos a todos');
  await composer.press('Enter');
  await expect.poll(() => posted.at(-1)).toMatchObject({ body: 'Saludos a todos', general: true });
  await expect(log.getByRole('article').last()).toContainText('General');
  await log.getByRole('button', { name: 'Abrir la orden ORD-0123', exact: true }).click();  // an order's chip in the chat opens its document
  await expect(conversation.locator('.inbox-doc-title')).toContainText('ORD-0123');
  await expect(conversation.getByRole('region', { name: 'Cotización vigente v2', exact: true })).toBeVisible();
});
