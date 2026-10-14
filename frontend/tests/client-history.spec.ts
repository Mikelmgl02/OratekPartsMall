import { expect, Page, test } from '@playwright/test';
import { switchAccount } from './header';
import { signedOutInbox } from './inbox-route';
import type { Account, BasketLine } from '../lib/types';
import { requestPayloadLines } from '../lib/basket-submissions';
import { fakeServerBasket } from './server-basket';

const clientId = '11111111-1111-4111-8111-111111111111';
const otherClientId = '22222222-2222-4222-8222-222222222222';
const supplierId = '33333333-3333-4333-8333-333333333333';
const requestId = '44444444-4444-4444-8444-444444444444';
const submissionId = '55555555-5555-4555-8555-555555555555';
const accounts: Account[] = [
  { id: clientId, name: 'TALLER CENTRAL', capabilities: ['client'], roles: ['client_business'] },
  { id: otherClientId, name: 'OTRO TALLER', capabilities: ['client'], roles: ['client_business'] },
];
const empty = { count: 0, next: null, previous: null, results: [] };
const root = `/api/market/accounts/${clientId}/sent-requests`;
const sent = (page: Page) => page.getByRole('main', { name: 'Solicitudes enviadas', exact: true });
function summary(id = requestId, reference = 'SOL-ENVIADA-001', supplier = 'REPUESTOS CENTRAL', status: 'pending' | 'reviewed' = 'pending') {
  return { id, reference, supplier: { id: supplierId, name: supplier }, submission_id: submissionId, status,
    created_at: '2026-10-02T12:00:00Z', reviewed_at: status === 'reviewed' ? '2026-10-02T13:00:00Z' : null, line_count: 1, unit_count: 3 };
}
function draft(id = '66666666-6666-4666-8666-666666666666', codigo = '58411-1R000-G'): BasketLine {
  return { key: `part:${supplierId}:${id}`, part: { id: '77777777-7777-4777-8777-777777777777', sku: '58411-1R000', name: 'DISCO DE FRENO', description: '', codes: [] },
    offer: { supplier_id: supplierId, supplier_name: 'REPUESTOS CENTRAL', in_stock: true, selected_item: { id, codigo, brand: 'MARCA A', description: 'DISCO EQUIVALENTE' } }, quantity: 3 };
}
async function mock(page: Page, known: BasketLine[] = []) {
  const unexpected: string[] = [];
  await page.route('**/api/**', route => { if (signedOutInbox(route)) return; unexpected.push(`${route.request().method()} ${new URL(route.request().url()).pathname}`); return route.fulfill({ status: 404, json: { detail: 'Ruta no prevista.' } }); });
  await page.route('**/api/market/wishlist/state', route => route.fulfill({ json: { part_ids: [], count: 0 } }));
  await page.route('**/api/market/analytics/events', route => route.fulfill({ status: 202, json: { recorded: true } }));
  await page.route('**/api/session', route => route.fulfill({ json: { authenticated: true, user: { id: 81, username: 'CLIENTE', first_name: '', last_name: '', is_superuser: false }, accounts: { ...empty, count: 2, results: accounts } } }));
  await page.route(/\/api\/market\/catalog(?:\?.*)?$/, route => route.fulfill({ json: empty }));
  await page.route(new RegExp(`/api/market/accounts/${otherClientId}/sent-requests(?:\\?.*)?$`), route => route.fulfill({ json: { ...empty, count: 1, results: [summary('88888888-8888-4888-8888-888888888888', 'SOL-OTRO-001', 'PROVEEDOR DEL OTRO TALLER')] } }));
  await page.route(/\/api\/market\/accounts\/[^/]+\/deals\/[^/]+\/messages(?:\?.*)?$/, route => route.fulfill({ json: { results: [], cursor: 0, has_more: false, has_earlier: false } }));
  const cesta = await fakeServerBasket(page, { known });
  return { unexpected, cesta };
}

test('sent history loads without browser receipts, sorts on the server, searches and shows immutable quantities on mobile', async ({ page }) => {
  const { unexpected } = await mock(page);
  let listCalls = 0; let detailCalls = 0; const orderings: string[] = [];
  await page.route(new RegExp(`${root}(?:\\?.*)?$`), route => {
    listCalls++;
    if (listCalls === 1) return route.fulfill({ status: 503, json: { detail: 'No se pudo cargar el historial.' } });
    const query = new URL(route.request().url()).searchParams;
    orderings.push(query.get('ordering') || '');
    const filtered = !!query.get('search') || !!query.get('status');
    const first = summary(requestId, 'SOL-ENVIADA-001', 'REPUESTOS CENTRAL', query.get('status') === 'reviewed' ? 'reviewed' : 'pending');
    return route.fulfill({ json: { ...empty, count: filtered ? 1 : 2, results: filtered ? [first] : [first, summary('99999999-9999-4999-8999-999999999999', 'SOL-ENVIADA-002', 'DISTRIBUIDORA MOTOR', 'reviewed')] } });
  });
  await page.route(`${root}/${requestId}`, route => {
    if (++detailCalls === 1) return route.fulfill({ status: 503, json: { detail: 'No se pudo abrir la solicitud.' } });
    return route.fulfill({ json: { ...summary(), notes: 'RETIRAR EN SUCURSAL', lines: [
      { id: 'line-a', part_id: draft().part.id, sku: '58411-1R000', name: 'DISCO DE FRENO', codigo: '58411-1R000-G', brand: 'MARCA A', description: 'DISCO EQUIVALENTE', quantity: 3 },
    ] } });
  });
  await page.goto('/?vista=solicitudes');
  await expect(sent(page).getByRole('alert')).toContainText('No se pudo cargar el historial.');
  await sent(page).getByRole('button', { name: 'Intentar de nuevo', exact: true }).click();
  // One grid: each order a row, with its supplier, counts and state; a header sorts on the server.
  const grid = sent(page).getByRole('region', { name: 'Órdenes enviadas', exact: true });
  const rows = grid.getByRole('row').filter({ hasText: /SOL-ENVIADA/ });
  await expect(rows).toHaveCount(2);
  await expect(sent(page).getByText('2 solicitudes', { exact: true })).toBeVisible();
  await expect(rows.filter({ hasText: 'SOL-ENVIADA-002' })).toContainText('DISTRIBUIDORA MOTOR');
  await grid.getByRole('columnheader', { name: 'Proveedor' }).click();
  await expect.poll(() => orderings.at(-1)).toBe('supplier_name');
  await sent(page).getByLabel('Buscar solicitudes enviadas', { exact: true }).fill('central');
  await expect(sent(page).getByLabel('Buscar solicitudes enviadas', { exact: true })).toHaveValue('CENTRAL');
  await expect(rows).toHaveCount(1);
  await sent(page).getByRole('group', { name: 'Estado de solicitudes enviadas', exact: true }).getByRole('button', { name: 'En revisión', exact: true }).click();
  await expect(rows).toContainText('En revisión');
  await sent(page).getByRole('button', { name: 'Ver enviada SOL-ENVIADA-001', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog.getByRole('alert')).toContainText('No se pudo abrir la solicitud.');
  await dialog.getByRole('button', { name: 'Actualizar acuerdo', exact: true }).click();
  await expect(dialog.getByText('58411-1R000-G', { exact: true })).toBeVisible();
  await expect(dialog.getByRole('table', { name: 'Artículos de la orden', exact: true }).getByRole('cell', { name: '3', exact: true })).toBeVisible();
  await expect(dialog.getByRole('button', { name: /Marcar|Eliminar|Enviar/ })).toHaveCount(0);
  expect(await dialog.innerText()).not.toMatch(/reportadas|reservadas|ID de inventario|[$€]/i);
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)).toBe(false);
  await dialog.getByRole('button', { name: 'Cerrar ventana', exact: true }).click();
  await page.reload();
  await expect(sent(page).getByRole('row').filter({ hasText: /SOL-ENVIADA/ })).toHaveCount(2);
  expect(await page.evaluate(() => localStorage.getItem('motionpartes:request-submissions:11111111-1111-4111-8111-111111111111'))).toBeNull();
  expect(unexpected).toEqual([]);
});

test('legacy confirmed basket items move to sent history once while unrelated and later identical drafts survive', async ({ page }) => {
  const original = draft(); const unrelated = draft('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', 'FILTRO-NUEVO');
  const { unexpected, cesta } = await mock(page, [original, unrelated]);
  const receipt = { submission_id: submissionId, created_at: '2026-10-02T12:00:00Z', requests: [{ id: requestId, reference: 'SOL-ENVIADA-001', supplier: { id: supplierId, name: 'REPUESTOS CENTRAL' }, line_count: 1, unit_count: 3 }] };
  await page.addInitScript(({ clientId, original, unrelated, receipt, fingerprint }) => {
    if (sessionStorage.getItem('legacy-seeded')) return;
    localStorage.setItem(`partsmall:basket:${clientId}`, JSON.stringify([original, unrelated]));
    localStorage.setItem(`motionpartes:request-submissions:${clientId}`, JSON.stringify([{ submissionId: receipt.submission_id, fingerprint, receipt }]));
    sessionStorage.setItem('legacy-seeded', '1');
  }, { clientId, original, unrelated, receipt, fingerprint: JSON.stringify(requestPayloadLines([original])) });
  await page.route(new RegExp(`${root}(?:\\?.*)?$`), route => route.fulfill({ json: { ...empty, count: 1, results: [summary()] } }));
  await page.goto('/?vista=cesta');
  await expect(sent(page).getByRole('status').filter({ hasText: 'Solicitud enviada' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Abrir cesta, 1 artículo', exact: true })).toBeVisible();
  await sent(page).getByRole('navigation', { name: 'Tus solicitudes', exact: true }).getByRole('button', { name: /Cesta/ }).click();
  const basket = page.getByRole('main', { name: 'Cesta de solicitudes', exact: true });
  await expect(basket.getByRole('heading', { name: 'FILTRO-NUEVO', exact: true })).toBeVisible();
  await expect(basket.getByRole('heading', { name: '58411-1R000-G', exact: true })).toHaveCount(0);
  expect(cesta.lines[clientId].map(line => line.offer.selected_item!.codigo)).toEqual(['FILTRO-NUEVO']);  // only the unsent one moved in
  await page.evaluate(({ clientId, original }) => localStorage.setItem(`partsmall:basket:${clientId}`, JSON.stringify([original])), { clientId, original });
  await page.reload();
  await expect(basket.getByRole('heading', { name: '58411-1R000-G', exact: true })).toBeVisible();
  await expect(basket.getByRole('button', { name: 'Enviar solicitud', exact: true })).toBeEnabled();
  expect(await page.evaluate(clientId => JSON.parse(localStorage.getItem(`motionpartes:request-submissions:${clientId}`)!)[0].draftCleared, clientId)).toBe(true);
  expect(unexpected).toEqual([]);
});

test('late sent history from another client cannot appear after account switching', async ({ page }) => {
  const { unexpected } = await mock(page);
  let release!: () => void; let started!: () => void; let finished!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  const requested = new Promise<void>(resolve => { started = resolve; });
  const done = new Promise<void>(resolve => { finished = resolve; });
  await page.route(new RegExp(`${root}(?:\\?.*)?$`), async route => {
    started(); await gate;
    try { await route.fulfill({ json: { ...empty, count: 1, results: [summary()] } }); }
    catch (error) { if (!route.request().failure()) throw error; }
    finally { finished(); }
  });
  try {
    await page.goto('/?vista=solicitudes'); await requested;
    await switchAccount(page, otherClientId);
    await expect(sent(page).getByRole('row').filter({ hasText: 'SOL-OTRO-001' })).toBeVisible();
    release(); await done;
    await expect(sent(page).getByRole('row').filter({ hasText: 'SOL-ENVIADA-001' })).toHaveCount(0);
    await expect(sent(page).getByRole('row').filter({ hasText: /SOL-/ })).toHaveCount(1);
    expect(unexpected).toEqual([]);
  } finally { release(); }
});

test('successful send keeps what another device or WhatsApp added to the cesta while the response was pending', async ({ page }) => {
  const original = draft(); const fresh = draft('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', 'FILTRO-NUEVO');
  const { unexpected, cesta } = await mock(page, [original, fresh]);
  cesta.lines[clientId] = [original];
  await page.route(new RegExp(`${root}(?:\\?.*)?$`), route => route.fulfill({ json: { ...empty, count: 1, results: [summary()] } }));
  let release!: () => void; let started!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  const requested = new Promise<void>(resolve => { started = resolve; });
  await page.route(`/api/market/accounts/${clientId}/requests`, async route => {
    started(); await gate;
    cesta.settle(clientId, route.request().postDataJSON().lines);  // the server takes the sent units out as it saves the solicitud
    return route.fulfill({ json: { submission_id: route.request().postDataJSON().submission_id, created_at: '2026-10-02T12:00:00Z',
      requests: [{ id: requestId, reference: 'SOL-ENVIADA-001', supplier: { id: supplierId, name: 'REPUESTOS CENTRAL' }, line_count: 1, unit_count: 3 }] } });
  });
  try {
    await page.goto('/?vista=cesta');
    await page.getByRole('main', { name: 'Cesta de solicitudes', exact: true }).getByRole('button', { name: 'Enviar solicitud', exact: true }).click();
    await requested;
    cesta.lines[clientId].push(fresh);  // added elsewhere meanwhile
    release();
    await expect(sent(page)).toBeVisible();
    await page.getByRole('button', { name: 'Abrir cesta, 1 artículo', exact: true }).click();
    const basket = page.getByRole('main', { name: 'Cesta de solicitudes', exact: true });
    await expect(basket.locator('.basket-grid .ag-center-cols-container .ag-row')).toHaveCount(1);
    await expect(basket.getByRole('heading', { name: 'FILTRO-NUEVO', exact: true })).toBeVisible();
    expect(cesta.lines[clientId]).toEqual([fresh]);
    expect(await page.evaluate(clientId => localStorage.getItem(`partsmall:basket:${clientId}`), clientId)).toBeNull();  // nothing kept in the browser
    expect(unexpected).toEqual([]);
  } finally { release(); }
});

test('Actividad shows the cesta and purchase ledgers: each order event with its supplier and who did it, older ones on demand, and opens the order', async ({ page }) => {
  const { unexpected } = await mock(page);
  const event = (id: number, description: string, reference = 'SOL-ENVIADA-001') => ({ id, kind: 'quote', description, created_at: '2026-10-02T12:00:00Z',
    order: { id: requestId, reference, status: 'quoted' }, supplier_name: 'REPUESTOS CENTRAL', account_name: 'REPUESTOS CENTRAL', actor_name: 'VENDEDOR' });
  const asked: string[] = [];
  await page.route(new RegExp(`/api/market/accounts/${clientId}/purchase-activity(?:\\?.*)?$`), route => {
    const before = new URL(route.request().url()).searchParams.get('before');
    asked.push(before || '');
    return route.fulfill({ json: before ? { results: [event(5, 'El cliente envió la orden.')], has_more: false }
      : { results: [event(9, 'Cotización v2 enviada al cliente.'), event(7, 'Cotización v1 enviada al cliente.')], has_more: true } });
  });
  await page.route(`${root}/${requestId}`, route => route.fulfill({ json: { ...summary(), notes: '', lines: [] } }));
  await page.goto('/?vista=actividad');
  const activity = page.getByRole('main', { name: 'Actividad', exact: true });
  await expect(activity.getByRole('navigation', { name: 'Tus solicitudes', exact: true }).getByRole('button', { name: 'Actividad', exact: true })).toHaveAttribute('aria-current', 'page');
  await expect(activity.getByRole('heading', { name: 'Actividad de cesta', exact: true })).toBeVisible();
  expect(asked).toEqual([]);  // the orders' ledger loads only when its view opens
  await activity.getByRole('group', { name: 'Vista de actividad', exact: true }).getByRole('button', { name: 'Actividad de compras', exact: true }).click();
  const rows = activity.getByRole('region', { name: 'Actividad de compras', exact: true }).locator('.ag-center-cols-container .ag-row');
  await expect(rows).toHaveCount(2);
  await expect(rows.nth(0)).toContainText('Cotización v2 enviada al cliente.');
  await expect(rows.nth(0)).toContainText('REPUESTOS CENTRAL');
  await expect(rows.nth(0)).toContainText('VENDEDOR');
  await activity.getByRole('button', { name: 'Ver más actividad', exact: true }).click();
  await expect(rows).toHaveCount(3);
  expect(asked).toEqual(['', '7']);  // before= the last id shown
  await expect(activity.getByRole('button', { name: 'Ver más actividad', exact: true })).toHaveCount(0);
  await rows.nth(0).getByRole('button', { name: 'Abrir orden SOL-ENVIADA-001', exact: true }).click();
  await expect(page.getByRole('dialog', { name: 'Orden SOL-ENVIADA-001', exact: true })).toBeVisible();
  expect(unexpected).toEqual([]);
});
