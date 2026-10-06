import { expect, Page, test } from '@playwright/test';
import type { Account, BasketLine } from '../lib/types';
import { requestPayloadLines } from '../lib/basket-submissions';

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
async function mock(page: Page) {
  const unexpected: string[] = [];
  await page.route('**/api/**', route => { unexpected.push(`${route.request().method()} ${new URL(route.request().url()).pathname}`); return route.fulfill({ status: 404, json: { detail: 'Ruta no prevista.' } }); });
  await page.route('**/api/market/wishlist/state', route => route.fulfill({ json: { part_ids: [], count: 0 } }));
  await page.route('**/api/market/analytics/events', route => route.fulfill({ status: 202, json: { recorded: true } }));
  await page.route('**/api/session', route => route.fulfill({ json: { authenticated: true, user: { id: 81, username: 'CLIENTE', first_name: '', last_name: '', is_superuser: false }, accounts: { ...empty, count: 2, results: accounts } } }));
  await page.route(/\/api\/market\/catalog(?:\?.*)?$/, route => route.fulfill({ json: empty }));
  await page.route(new RegExp(`/api/market/accounts/${otherClientId}/sent-requests(?:\\?.*)?$`), route => route.fulfill({ json: { ...empty, count: 1, results: [summary('88888888-8888-4888-8888-888888888888', 'SOL-OTRO-001', 'PROVEEDOR DEL OTRO TALLER')] } }));
  await page.route(/\/api\/market\/accounts\/[^/]+\/deals\/[^/]+\/messages(?:\?.*)?$/, route => route.fulfill({ json: { results: [], cursor: 0, has_more: false, has_earlier: false } }));
  return unexpected;
}

test('sent history loads without browser receipts, searches, paginates and shows immutable quantities on mobile', async ({ page }) => {
  const unexpected = await mock(page);
  let listCalls = 0; let detailCalls = 0;
  await page.route(new RegExp(`${root}(?:\\?.*)?$`), route => {
    listCalls++;
    if (listCalls === 1) return route.fulfill({ status: 503, json: { detail: 'No se pudo cargar el historial.' } });
    const query = new URL(route.request().url()).searchParams;
    if (query.get('page') === '2') return route.fulfill({ json: { ...empty, count: 2, previous: root, results: [summary('99999999-9999-4999-8999-999999999999', 'SOL-ENVIADA-002', 'DISTRIBUIDORA MOTOR', 'reviewed')] } });
    const filtered = !!query.get('search') || !!query.get('status');
    return route.fulfill({ json: { ...empty, count: filtered ? 1 : 2, next: filtered ? null : root, results: [summary(requestId, 'SOL-ENVIADA-001', 'REPUESTOS CENTRAL', query.get('status') === 'reviewed' ? 'reviewed' : 'pending')] } });
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
  await expect(sent(page).getByRole('article')).toHaveCount(1);
  await sent(page).getByRole('button', { name: 'Siguiente', exact: true }).click();
  await expect(sent(page).getByRole('heading', { name: 'SOL-ENVIADA-002', exact: true })).toBeVisible();
  await sent(page).getByLabel('Buscar solicitudes enviadas', { exact: true }).fill('central');
  await expect(sent(page).getByLabel('Buscar solicitudes enviadas', { exact: true })).toHaveValue('CENTRAL');
  await expect(sent(page).getByText('Página 1', { exact: true })).toBeVisible();
  await sent(page).getByRole('group', { name: 'Estado de solicitudes enviadas', exact: true }).getByRole('button', { name: 'En revisión', exact: true }).click();
  await expect(sent(page).getByRole('article')).toContainText('En revisión');
  await sent(page).getByRole('button', { name: 'Ver enviada SOL-ENVIADA-001', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog.getByRole('alert')).toContainText('No se pudo abrir la solicitud.');
  await dialog.getByRole('button', { name: 'Actualizar acuerdo', exact: true }).click();
  await expect(dialog.getByText('58411-1R000-G', { exact: true })).toBeVisible();
  await expect(dialog.getByRole('cell', { name: '3', exact: true })).toBeVisible();
  await expect(dialog.getByRole('button', { name: /Marcar|Eliminar|Enviar/ })).toHaveCount(0);
  expect(await dialog.innerText()).not.toMatch(/Reportadas|Reservadas|ID de inventario|[$€]/);
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)).toBe(false);
  await dialog.getByRole('button', { name: 'Cerrar ventana', exact: true }).click();
  await page.reload();
  await expect(sent(page).getByRole('article')).toHaveCount(1);
  expect(await page.evaluate(() => localStorage.getItem('motionpartes:request-submissions:11111111-1111-4111-8111-111111111111'))).toBeNull();
  expect(unexpected).toEqual([]);
});

test('legacy confirmed basket items move to sent history once while unrelated and later identical drafts survive', async ({ page }) => {
  const unexpected = await mock(page);
  const original = draft(); const unrelated = draft('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', 'FILTRO-NUEVO');
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
  await sent(page).getByRole('navigation', { name: 'Tus solicitudes', exact: true }).getByRole('button', { name: /Borrador/ }).click();
  const basket = page.getByRole('main', { name: 'Cesta de solicitudes', exact: true });
  await expect(basket.getByRole('heading', { name: 'FILTRO-NUEVO', exact: true })).toBeVisible();
  await expect(basket.getByRole('heading', { name: '58411-1R000-G', exact: true })).toHaveCount(0);
  await page.evaluate(({ clientId, original }) => localStorage.setItem(`partsmall:basket:${clientId}`, JSON.stringify([original])), { clientId, original });
  await page.reload();
  await expect(basket.getByRole('heading', { name: '58411-1R000-G', exact: true })).toBeVisible();
  await expect(basket.getByRole('button', { name: 'Enviar solicitud', exact: true })).toBeEnabled();
  expect(await page.evaluate(clientId => JSON.parse(localStorage.getItem(`motionpartes:request-submissions:${clientId}`)!)[0].draftCleared, clientId)).toBe(true);
  expect(unexpected).toEqual([]);
});

test('late sent history from another client cannot appear after account switching', async ({ page }) => {
  const unexpected = await mock(page);
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
    await page.getByLabel('Cuenta activa', { exact: true }).selectOption(otherClientId);
    await expect(sent(page).getByRole('heading', { name: 'SOL-OTRO-001', exact: true })).toBeVisible();
    release(); await done;
    await expect(sent(page).getByRole('heading', { name: 'SOL-ENVIADA-001', exact: true })).toHaveCount(0);
    await expect(sent(page).getByRole('article')).toHaveCount(1);
    expect(unexpected).toEqual([]);
  } finally { release(); }
});

test('successful send preserves new draft items added while the response is pending', async ({ page }) => {
  const unexpected = await mock(page);
  const original = draft(); const fresh = draft('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', 'FILTRO-NUEVO');
  await page.addInitScript(({ clientId, original }) => localStorage.setItem(`partsmall:basket:${clientId}`, JSON.stringify([original])), { clientId, original });
  await page.route(new RegExp(`${root}(?:\\?.*)?$`), route => route.fulfill({ json: { ...empty, count: 1, results: [summary()] } }));
  let release!: () => void; let started!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  const requested = new Promise<void>(resolve => { started = resolve; });
  await page.route(`/api/market/accounts/${clientId}/requests`, async route => {
    started(); await gate;
    return route.fulfill({ json: { submission_id: route.request().postDataJSON().submission_id, created_at: '2026-10-02T12:00:00Z',
      requests: [{ id: requestId, reference: 'SOL-ENVIADA-001', supplier: { id: supplierId, name: 'REPUESTOS CENTRAL' }, line_count: 1, unit_count: 3 }] } });
  });
  try {
    await page.goto('/?vista=cesta');
    await page.getByRole('main', { name: 'Cesta de solicitudes', exact: true }).getByRole('button', { name: 'Enviar solicitud', exact: true }).click();
    await requested;
    await page.evaluate(({ clientId, original, fresh }) => localStorage.setItem(`partsmall:basket:${clientId}`, JSON.stringify([original, fresh])), { clientId, original, fresh });
    release();
    await expect(sent(page)).toBeVisible();
    await page.getByRole('button', { name: 'Abrir cesta, 1 artículo', exact: true }).click();
    const basket = page.getByRole('main', { name: 'Cesta de solicitudes', exact: true });
    await expect(basket.getByRole('article')).toHaveCount(1);
    await expect(basket.getByRole('heading', { name: 'FILTRO-NUEVO', exact: true })).toBeVisible();
    expect(await page.evaluate(clientId => JSON.parse(localStorage.getItem(`partsmall:basket:${clientId}`)!), clientId)).toEqual([fresh]);
    expect(unexpected).toEqual([]);
  } finally { release(); }
});
