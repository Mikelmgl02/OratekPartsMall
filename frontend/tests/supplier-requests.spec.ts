import { expect, Page, test } from '@playwright/test';
import type { Account, BasketLine, Part } from '../lib/types';
import type { SupplierRequestDetail } from '../lib/request-types';
import { openSupplierNavigation } from './supplier-navigation';

const supplierAccount = '11111111-1111-4111-8111-111111111111';
const otherSupplierAccount = '22222222-2222-4222-8222-222222222222';
const clientAccount = '33333333-3333-4333-8333-333333333333';
const requestId = '44444444-4444-4444-8444-444444444444';
const otherRequestId = '55555555-5555-4555-8555-555555555555';
const itemA = '66666666-6666-4666-8666-666666666666';
const itemB = '77777777-7777-4777-8777-777777777777';
const itemC = '88888888-8888-4888-8888-888888888888';
const partId = '99999999-9999-4999-8999-999999999999';
const root = `/api/market/accounts/${supplierAccount}/requests`;
const emptyPage = { count: 0, next: null, previous: null, results: [] };
const supplierAccounts: Account[] = [
  { id: supplierAccount, name: 'REPUESTOS CENTRAL', roles: ['supplier_retail'], capabilities: ['supplier'] },
  { id: otherSupplierAccount, name: 'DISTRIBUIDORA MOTOR', roles: ['supplier_wholesale'], capabilities: ['supplier'] },
];
const part: Part = { id: partId, sku: '58411-1R000', name: 'DISCO DE FRENO', description: 'DISCO DELANTERO', codes: [] };

function summary(id = requestId, reference = 'SOL-PRUEBA-001', clientName = 'TALLER CENTRAL', status: 'pending' | 'reviewed' = 'pending') {
  return { id, reference, client: { id: clientAccount, name: clientName }, status,
    created_at: '2026-10-02T12:00:00Z', reviewed_at: status === 'reviewed' ? '2026-10-02T13:00:00Z' : null,
    line_count: id === requestId ? 2 : 1, unit_count: id === requestId ? 5 : 1 };
}

function detail(status: 'pending' | 'reviewed' = 'pending'): SupplierRequestDetail {
  return { ...summary(requestId, 'SOL-PRUEBA-001', 'TALLER CENTRAL', status), notes: 'RETIRAR EN SUCURSAL', lines: [
    { id: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb', supplier_item_id: itemA, supplier_invent_id: 'supplier-row-lowercase', part_id: partId,
      sku: part.sku, name: part.name, codigo: '58411-1R000-G', brand: 'MARCA A', description: 'DISCO EQUIVALENTE A', quantity: 2,
      stock: { reported_quantity: 10, reserved_quantity: 2, available_quantity: 8, shortfall: 0, updated_at: '2026-10-02T12:00:00Z' } },
    { id: 'cccccccc-cccc-4ccc-8ccc-cccccccccccc', supplier_item_id: itemB, supplier_invent_id: '00045', part_id: partId,
      sku: part.sku, name: part.name, codigo: '58-1R0', brand: 'MARCA B', description: 'DISCO EQUIVALENTE B', quantity: 3,
      stock: { reported_quantity: 1, reserved_quantity: 0, available_quantity: 1, shortfall: 2, updated_at: '2026-10-02T12:00:00Z' } },
  ] };
}

async function mockSession(page: Page, accounts = supplierAccounts.slice(0, 1)) {
  const unexpected: string[] = [];
  await page.route('**/api/**', async route => {
    unexpected.push(`${route.request().method()} ${new URL(route.request().url()).pathname}`);
    await route.fulfill({ status: 404, json: { detail: 'Ruta no prevista en esta prueba.' } });
  });
  await page.route('**/api/market/wishlist/state', route => route.fulfill({ json: { part_ids: [], count: 0 } }));
  await page.route('**/api/market/analytics/events', route => route.fulfill({ status: 202, json: { recorded: true } }));
  await page.route('**/api/session', route => route.fulfill({ json: { authenticated: true,
    user: { id: 43, username: 'USUARIO DE PRUEBA', first_name: '', last_name: '', is_superuser: false },
    accounts: { ...emptyPage, count: accounts.length, results: accounts } } }));
  await page.route(/\/api\/market\/catalog(?:\?.*)?$/, route => route.fulfill({ json: { ...emptyPage, count: 1, results: [part] } }));
  await page.route(/\/api\/market\/accounts\/[^/]+\/inventory(?:\?.*)?$/, route => route.fulfill({ json: { ...emptyPage, count: 1, results: [
    { id: itemA, supplier_invent_id: 'inventario-existente', part: partId, codigo: 'INVENTARIO-EXISTENTE',
      brand: 'MARCA A', description: 'SALDO DEL PROVEEDOR', matching_status: 'matched', source: 'upload',
      reported_quantity: 10, reserved_quantity: 0, available_quantity: 10, updated_at: '2026-10-02T12:00:00Z' },
  ] } }));
  await page.route(/\/api\/market\/accounts\/[^/]+\/sent-requests(?:\?.*)?$/, route => route.fulfill({ json: emptyPage }));
  await page.route('**/request-state', route => route.fulfill({ json: { totals: {sent_quantity:0, pending_quantity:0, reviewed_quantity:0}, items:[] } }));
  await page.route(/\/api\/market\/accounts\/[^/]+\/deals\/[^/]+\/messages(?:\?.*)?$/, route => route.fulfill({ json: { results: [], cursor: 0, has_more: false, has_earlier: false } }));
  await page.route(/\/api\/market\/accounts\/[^/]+\/requests\/[^/]+\/review$/, route => route.fulfill({ json: detail('reviewed') }));
  return unexpected;
}

async function openSupplier(page: Page) {
  await page.goto('/');
  await expect(page.getByText('Cuenta conectada', { exact: true })).toBeVisible();
  await expect(page.getByRole('link', { name: 'Administración', exact: true })).toHaveCount(0);
  if (page.viewportSize()!.width < 760) await page.getByRole('button', { name: 'Abrir tu panel', exact: true }).click();
  else await page.getByRole('button', { name: 'Para proveedores', exact: true }).click();
  await openSupplierNavigation(page);
  await expect(page.getByRole('tab', { name: 'Inventario', exact: true })).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByRole('cell', { name: 'inventario-existente', exact: true })).toBeVisible();
  await page.getByRole('tab', { name: 'Solicitudes', exact: true }).click();
}
function requests(page: Page) { return page.getByRole('region', { name: 'Solicitudes de clientes', exact: true }); }
function requestDialog(page: Page) { return page.getByRole('main', { name: 'Detalle de orden de proveedor', exact: true }); }

test('order has its own URL, reloads safely and returns to supplier requests with browser Back', async ({ page }) => {
  const unexpected = await mockSession(page);
  let reviews = 0;
  await page.route(new RegExp(`${root}(?:\\?.*)?$`), route => route.fulfill({ json: { ...emptyPage, count: 1, results: [summary(requestId, 'SOL-PRUEBA-001', 'TALLER CENTRAL', reviews ? 'reviewed' : 'pending')] } }));
  await page.route(`${root}/${requestId}/review`, route => { reviews++; return route.fulfill({ json: detail('reviewed') }); });
  await openSupplier(page);
  const link = requests(page).getByRole('link', { name: 'Ver solicitud SOL-PRUEBA-001', exact: true });
  await expect(link).toHaveAttribute('href', `/proveedor/${supplierAccount}/ordenes/${requestId}`);
  expect(reviews).toBe(0);
  await link.click();
  await expect(page).toHaveURL(`/proveedor/${supplierAccount}/ordenes/${requestId}`);
  await expect(page.getByRole('heading', { name: 'Orden SOL-PRUEBA-001', exact: true })).toBeVisible();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await page.reload();
  await expect(page.getByRole('heading', { name: 'Orden SOL-PRUEBA-001', exact: true })).toBeVisible();
  await expect(requestDialog(page).getByText('En revisión', { exact: true }).first()).toBeVisible();
  expect(reviews).toBe(2);
  await page.goBack();
  await expect(page.getByLabel('Cuenta activa', { exact: true })).toHaveValue(supplierAccount);
  await expect(page.getByRole('tab', { name: 'Solicitudes', exact: true })).toHaveAttribute('aria-selected', 'true');
  await expect(requests(page).getByRole('heading', { name: 'SOL-PRUEBA-001', exact: true })).toBeVisible();
  expect(unexpected).toEqual([]);
});

test('direct order URLs require sign-in and access to a supplier account before requesting private details', async ({ page }) => {
  const unexpected = await mockSession(page);
  let privateReads = 0;
  await page.route(`${root}/${requestId}/review`, route => { privateReads++; return route.fulfill({ json: detail('reviewed') }); });
  await page.route('**/api/session', route => route.fulfill({ json: { authenticated: false } }));
  await page.goto(`/proveedor/${supplierAccount}/ordenes/${requestId}`);
  await expect(page.getByRole('heading', { name: 'Inicia sesión para ver esta orden.', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Iniciar sesión', exact: true })).toBeVisible();
  await page.route('**/api/session', route => route.fulfill({ json: { authenticated: true, accounts: { ...emptyPage, results: supplierAccounts.slice(1) } } }));
  await page.reload();
  await expect(page.getByRole('heading', { name: 'No tienes acceso a esta cuenta de proveedor.', exact: true })).toBeVisible();
  await page.route('**/api/session', route => route.fulfill({ json: { authenticated: true, accounts: { ...emptyPage, results: [{ ...supplierAccounts[0], capabilities: ['client'] }] } } }));
  await page.reload();
  await expect(page.getByRole('heading', { name: 'No tienes acceso a esta cuenta de proveedor.', exact: true })).toBeVisible();
  expect(privateReads).toBe(0);
  expect(unexpected).toEqual([]);
});

test('supplier sees requested units, stock shortfalls and refreshed availability without changing the request', async ({ page }) => {
  const unexpected = await mockSession(page);
  let reads = 0;
  await page.route(new RegExp(`${root}(?:\\?.*)?$`), route => route.fulfill({ json: { ...emptyPage, count: 1, results: [summary()] } }));
  await page.route(`${root}/${requestId}`, route => {
    expect(route.request().method()).toBe('GET');
    const response = detail('reviewed');
    if (++reads > 1) {
      response.lines[0].stock = null;
      response.lines[1].stock = { reported_quantity: 0, reserved_quantity: 2, available_quantity: 0, shortfall: 3, updated_at: '2026-10-02T13:30:00Z' };
    }
    return route.fulfill({ json: response });
  });
  await page.route(`${root}/${requestId}/review`, route => { reads++; return route.fulfill({ json: detail('reviewed') }); });
  await openSupplier(page);
  await requests(page).getByRole('link', { name: 'Ver solicitud SOL-PRUEBA-001', exact: true }).click();
  await expect(page).toHaveURL(`/proveedor/${supplierAccount}/ordenes/${requestId}`);
  await expect(page.getByRole('dialog')).toHaveCount(0);
  const dialog = requestDialog(page);
  const first = dialog.getByRole('row').filter({ hasText: '58411-1R000-G' });
  const second = dialog.getByRole('row').filter({ hasText: '58-1R0' });
  await expect(first).toContainText('Reportadas: 10 · Reservadas: 2');
  await expect(first).toContainText('Existencias suficientes');
  await expect(second).toContainText('Faltan 2 unidades');
  await expect(second.getByRole('cell').nth(3)).toHaveText('3');
  expect(await dialog.locator('.supplier-request-lines').evaluate(element => element.scrollWidth > element.clientWidth)).toBe(false);
  await expect(dialog.getByRole('status')).toContainText('1 artículo requiere confirmar disponibilidad');
  await dialog.getByRole('button', { name: 'Actualizar existencias', exact: true }).click();
  await expect(first).toContainText('Revisar artículo');
  await expect(first).not.toContainText('Reportadas:');
  await expect(second).toContainText('Reportadas: 0 · Reservadas: 2');
  await expect(second).toContainText('Faltan 3 unidades');
  await expect(second.getByRole('cell').nth(3)).toHaveText('3');
  await expect(dialog.getByRole('status')).toContainText('2 artículos requieren confirmar disponibilidad');
  await expect(dialog.getByText('En revisión', { exact: true }).first()).toBeVisible();
  expect(reads).toBe(2);
  expect(unexpected).toEqual([]);
});

test('ordinary supplier searches, filters and pages requests, sees each own item and marks one reviewed', async ({ page }) => {
  const unexpected = await mockSession(page);
  const queries: { search: string; status: string; page: string }[] = [];
  const reviewed = summary(otherRequestId, 'SOL-PRUEBA-002', 'MOTOR CAR', 'reviewed');
  let wasReviewed = false;
  let reviewPosts = 0;
  await page.route(new RegExp(`${root}(?:\\?.*)?$`), route => {
    const url = new URL(route.request().url());
    const query = { search: url.searchParams.get('search') || '', status: url.searchParams.get('status') || '', page: url.searchParams.get('page') || '1' };
    queries.push(query);
    const pending = summary(requestId, 'SOL-PRUEBA-001', 'TALLER CENTRAL', wasReviewed ? 'reviewed' : 'pending');
    if (query.search === 'MOTOR' || query.status === 'reviewed') return route.fulfill({ json: { ...emptyPage, count: 1, results: [reviewed] } });
    if (query.status === 'pending') return route.fulfill({ json: { ...emptyPage, count: wasReviewed ? 0 : 1, results: wasReviewed ? [] : [pending] } });
    if (query.page === '2') return route.fulfill({ json: { ...emptyPage, count: 3, previous: `${root}?page=1`, results: [summary('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', 'SOL-PRUEBA-003', 'TALLER NORTE')] } });
    return route.fulfill({ json: { ...emptyPage, count: 3, next: `${root}?page=2`, results: [pending, reviewed] } });
  });
  await page.route(`${root}/${requestId}`, route => route.fulfill({ json: detail(wasReviewed ? 'reviewed' : 'pending') }));
  await page.route(`${root}/${requestId}/review`, route => {
    expect(route.request().method()).toBe('POST'); reviewPosts++; wasReviewed = true;
    return route.fulfill({ json: detail('reviewed') });
  });
  await openSupplier(page);
  await expect(requests(page).getByRole('article')).toHaveCount(2);
  const sidebar = page.getByRole('complementary', { name: 'Navegación del proveedor', exact: true });
  const sidebarBounds = await sidebar.boundingBox();
  const contentBounds = await requests(page).boundingBox();
  expect(sidebarBounds!.x + sidebarBounds!.width).toBeLessThan(contentBounds!.x);
  await expect(sidebar.getByRole('tablist')).toHaveAttribute('aria-orientation', 'vertical');
  const requestsTab = sidebar.getByRole('tab', { name: 'Solicitudes', exact: true });
  await requestsTab.focus();
  await requestsTab.press('ArrowUp');
  await expect(sidebar.getByRole('tab', { name: 'Inventario', exact: true })).toBeFocused();
  await page.keyboard.press('ArrowDown');
  await expect(requestsTab).toBeFocused();
  await expect(requestsTab).toHaveAttribute('aria-selected', 'true');
  await expect(requests(page).getByRole('article')).toHaveCount(2);
  await page.screenshot({ path: '../output/ui/supplier-sidebar-desktop.png' });
  await page.screenshot({ path: '/tmp/motionpartes-supplier-requests-desktop.png' });
  await requests(page).getByRole('button', { name: 'Siguiente', exact: true }).click();
  await expect(requests(page).getByText('Página 2', { exact: true })).toBeVisible();
  await expect(requests(page).getByRole('heading', { name: 'SOL-PRUEBA-003', exact: true })).toBeVisible();
  const search = requests(page).getByLabel('Buscar solicitudes', { exact: true });
  await expect(search).toHaveAttribute('autocomplete', 'off');
  await search.fill('motor');
  await expect(search).toHaveValue('MOTOR');
  await expect(requests(page).getByRole('heading', { name: 'SOL-PRUEBA-002', exact: true })).toBeVisible();
  await expect(requests(page).getByText('Página 1', { exact: true })).toBeVisible();
  expect(queries.at(-1)).toEqual({ search: 'MOTOR', status: '', page: '1' });
  await search.fill('');
  await requests(page).getByRole('group', { name: 'Estado de las solicitudes', exact: true }).getByRole('button', { name: 'En revisión', exact: true }).click();
  await expect(requests(page).getByRole('article')).toHaveCount(1);
  await expect.poll(() => queries.at(-1)?.status).toBe('reviewed');
  await requests(page).getByRole('button', { name: 'Pendientes', exact: true }).click();
  await expect(requests(page).getByRole('heading', { name: 'SOL-PRUEBA-001', exact: true })).toBeVisible();
  await requests(page).getByRole('link', { name: 'Ver solicitud SOL-PRUEBA-001', exact: true }).click();
  const dialog = requestDialog(page);
  await expect(dialog.locator('.supplier-request-lines').getByText('58411-1R000-G', { exact: true })).toBeVisible();
  await expect(dialog.locator('.supplier-request-lines').getByText('58-1R0', { exact: true })).toBeVisible();
  await expect(dialog.getByText('supplier-row-lowercase', { exact: true })).toBeVisible();
  await expect(dialog.getByText('00045', { exact: true })).toBeVisible();
  await expect(dialog).toContainText('TALLER CENTRAL');
  await expect(dialog).toContainText('RETIRAR EN SUCURSAL');
  expect(await dialog.innerText()).not.toMatch(/[$€]|\b(?:PAB|USD)\b/);
  await page.screenshot({ path: '/tmp/motionpartes-supplier-request-detail-desktop.png' });

  await expect(dialog.getByText('En revisión', { exact: true }).first()).toBeVisible();
  expect(reviewPosts).toBe(1);
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await dialog.evaluate(element => element.scrollWidth > element.clientWidth)).toBe(false);
  expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)).toBe(false);
  await page.screenshot({ path: '/tmp/motionpartes-supplier-requests-mobile.png' });
  await page.getByRole('link', { name: 'Volver a solicitudes', exact: true }).click();
  await requests(page).getByRole('button', { name: 'Todas', exact: true }).click();
  await expect(requests(page).getByRole('heading', { name: 'SOL-PRUEBA-001', exact: true })).toBeVisible();
  await openSupplierNavigation(page);
  await expect(sidebar).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)).toBe(false);
  await page.screenshot({ path: '../output/ui/supplier-sidebar-mobile.png' });
  await page.getByRole('tab', { name: 'Inventario', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Menú de proveedor', exact: false })).toHaveAttribute('aria-expanded', 'false');
  await expect(page.getByRole('button', { name: 'Menú de proveedor', exact: false })).toBeFocused();
  await expect(page.getByRole('cell', { name: 'inventario-existente', exact: true })).toBeVisible();
  expect(unexpected).toEqual([]);
});

test('failed review on opening is retried without claiming success', async ({ page }) => {
  const unexpected = await mockSession(page);
  let reviews = 0;
  await page.route(new RegExp(`${root}(?:\\?.*)?$`), route => route.fulfill({ json: { ...emptyPage, count: 1, results: [summary()] } }));
  await page.route(`${root}/${requestId}/review`, route => {
    reviews++;
    return reviews === 1 ? route.fulfill({ status: 503, json: { detail: 'No se pudo guardar la revisión.' } }) : route.fulfill({ json: detail('reviewed') });
  });
  await openSupplier(page);
  await requests(page).getByRole('link', { name: 'Ver solicitud SOL-PRUEBA-001', exact: true }).click();
  const dialog = requestDialog(page);
  await expect(dialog.getByRole('alert')).toContainText('No se pudo guardar la revisión.');
  await expect(dialog.getByText('En revisión', { exact: true })).toHaveCount(0);
  await dialog.getByRole('button', { name: 'Actualizar acuerdo', exact: true }).click();
  await expect(dialog.getByText('En revisión', { exact: true }).first()).toBeVisible();
  expect(reviews).toBe(2);
  expect(unexpected).toEqual([]);
});

test('late request list and detail responses from a previous supplier cannot appear after switching accounts', async ({ page }) => {
  const unexpected = await mockSession(page, supplierAccounts);
  let releaseList!: () => void; let releaseDetail!: () => void;
  let listStarted!: () => void; let detailStarted!: () => void;
  let listFinished!: () => void; let detailFinished!: () => void;
  const listGate = new Promise<void>(resolve => { releaseList = resolve; });
  const detailGate = new Promise<void>(resolve => { releaseDetail = resolve; });
  const listRequested = new Promise<void>(resolve => { listStarted = resolve; });
  const detailRequested = new Promise<void>(resolve => { detailStarted = resolve; });
  const listDone = new Promise<void>(resolve => { listFinished = resolve; });
  const detailDone = new Promise<void>(resolve => { detailFinished = resolve; });
  let calls = 0;
  await page.route(new RegExp(`${root}(?:\\?.*)?$`), async route => {
    if (++calls > 1) { listStarted(); await listGate; }
    try { await route.fulfill({ json: { ...emptyPage, count: 1, results: [summary()] } }); }
    catch (error) { if (!route.request().failure()) throw error; }
    finally { if (calls > 1) listFinished(); }
  });
  await page.route(`${root}/${requestId}/review`, async route => {
    detailStarted(); await detailGate;
    try { await route.fulfill({ json: detail() }); }
    catch (error) { if (!route.request().failure()) throw error; }
    finally { detailFinished(); }
  });
  await page.route(new RegExp(`/api/market/accounts/${otherSupplierAccount}/requests(?:\\?.*)?$`), route => route.fulfill({ json: { ...emptyPage, count: 1, results: [summary(otherRequestId, 'SOL-SEGUNDO-001', 'CLIENTE DEL SEGUNDO PROVEEDOR')] } }));
  try {
    await openSupplier(page);
    await requests(page).getByRole('link', { name: 'Ver solicitud SOL-PRUEBA-001', exact: true }).click();
    await detailRequested;
    await page.getByRole('link', { name: 'Volver a solicitudes', exact: true }).click();
    await listRequested;
    await page.getByLabel('Cuenta activa', { exact: true }).selectOption(otherSupplierAccount);
    await page.getByRole('button', { name: 'Para proveedores', exact: true }).click();
    await page.getByRole('tab', { name: 'Solicitudes', exact: true }).click();
    await expect(requests(page).getByRole('heading', { name: 'SOL-SEGUNDO-001', exact: true })).toBeVisible();
    releaseList(); releaseDetail(); await Promise.all([listDone, detailDone]);
    await page.evaluate(() => new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
    await expect(requests(page).getByRole('article')).toHaveCount(1);
    await expect(requests(page).getByRole('heading', { name: 'SOL-SEGUNDO-001', exact: true })).toBeVisible();
    await expect(requests(page).getByRole('heading', { name: 'SOL-PRUEBA-001', exact: true })).toHaveCount(0);
    await expect(page.getByRole('dialog')).toHaveCount(0);
    expect(unexpected).toEqual([]);
  } finally { releaseList(); releaseDetail(); }
});

function clientDraft(): BasketLine[] {
  return [
    { id: itemA, codigo: '58411-1R000-G', supplier: supplierAccounts[0], quantity: 2 },
    { id: itemB, codigo: '58-1R0', supplier: supplierAccounts[0], quantity: 1 },
    { id: itemC, codigo: 'D-HYU-1R', supplier: supplierAccounts[1], quantity: 4 },
  ].map(item => ({ key: `${partId}:${item.supplier.id}:${item.id}`, part,
    offer: { supplier_id: item.supplier.id, supplier_name: item.supplier.name, in_stock: true,
      selected_item: { id: item.id, codigo: item.codigo, brand: 'MARCA DE PRUEBA', description: part.description } }, quantity: item.quantity }));
}

test('client submission survives a lost response, clears the confirmed draft and permits a new identical request', async ({ page }) => {
  const unexpected = await mockSession(page, [{ id: clientAccount, name: 'TALLER CLIENTE', roles: ['client_business'], capabilities: ['client'] }]);
  const draft = clientDraft();
  await page.route(new RegExp(`/api/market/accounts/${clientAccount}/sent-requests(?:\\?.*)?$`), route => route.fulfill({ json: { ...emptyPage, count: 2, results: [
    { ...summary(), supplier: { id: supplierAccount, name: supplierAccounts[0].name }, reference: 'SOL-CLIENTE-001', line_count: 2, unit_count: 3 },
    { ...summary(otherRequestId), supplier: { id: otherSupplierAccount, name: supplierAccounts[1].name }, reference: 'SOL-CLIENTE-002', line_count: 1, unit_count: 4 },
  ] } }));
  await page.addInitScript(({ accountId, lines }) => {
    if (!sessionStorage.getItem('request-cart-seeded')) {
      localStorage.setItem(`partsmall:basket:${accountId}`, JSON.stringify(lines));
      sessionStorage.setItem('request-cart-seeded', '1');
    }
  }, { accountId: clientAccount, lines: draft });
  const submissions: { submission_id: string; lines: { supplier_item_id: string; quantity: number; expected_part_id: string; expected_codigo: string; expected_brand: string }[] }[] = [];
  let release!: () => void; let started!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  const firstRequested = new Promise<void>(resolve => { started = resolve; });
  await page.route(`/api/market/accounts/${clientAccount}/requests`, async route => {
    expect(route.request().method()).toBe('POST');
    const body = route.request().postDataJSON(); submissions.push(body);
    if (submissions.length === 1) { started(); await held; await route.abort('failed'); return; }
    await route.fulfill({ json: { submission_id: body.submission_id, created_at: '2026-10-02T12:00:00Z', requests: [
      { id: requestId, reference: 'SOL-CLIENTE-001', supplier: { id: supplierAccount, name: supplierAccounts[0].name }, line_count: 2, unit_count: submissions.length === 2 ? 3 : 4 },
      { id: otherRequestId, reference: 'SOL-CLIENTE-002', supplier: { id: otherSupplierAccount, name: supplierAccounts[1].name }, line_count: 1, unit_count: 4 },
    ] } });
  });
  const basket = page.getByRole('main', { name: 'Cesta de solicitudes', exact: true });
  try {
    await page.goto('/?vista=cesta');
    await basket.getByRole('button', { name: 'Enviar solicitud', exact: true }).click();
    await firstRequested;
    await expect(basket.getByLabel('Cantidad de 58411-1R000-G', { exact: true })).toBeDisabled();
    await expect(basket.getByRole('button', { name: 'Eliminar 58411-1R000-G', exact: true })).toBeDisabled();
    release();
    await expect(basket.getByRole('alert')).toBeVisible();
    await expect(basket.getByRole('button', { name: 'Enviar solicitud', exact: true })).toBeEnabled();
    await page.reload();
    await expect(basket.getByRole('article')).toHaveCount(3);
    await basket.getByRole('button', { name: 'Enviar solicitud', exact: true }).click();
    const sent = page.getByRole('main', { name: 'Solicitudes enviadas', exact: true });
    await expect(sent.getByRole('status').filter({ hasText: 'Solicitud enviada' })).toBeVisible();
    await expect(sent.getByText(`${supplierAccounts[0].name} · SOL-CLIENTE-001`, { exact: true })).toBeVisible();
    await expect(sent.getByText(`${supplierAccounts[1].name} · SOL-CLIENTE-002`, { exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Abrir cesta, 0 artículos', exact: true })).toBeVisible();
    expect(submissions).toHaveLength(2);
    expect(submissions[0].submission_id).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i);
    expect(submissions[1]).toEqual(submissions[0]);
    expect(submissions[0].lines).toEqual(draft.map(line => ({ supplier_item_id: line.offer.selected_item!.id, quantity: line.quantity,
      expected_part_id: partId, expected_codigo: line.offer.selected_item!.codigo, expected_brand: 'MARCA DE PRUEBA' })));
    expect(await page.evaluate(accountId => JSON.parse(localStorage.getItem(`partsmall:basket:${accountId}`)!), clientAccount)).toEqual([]);
    await page.reload();
    await expect(sent.getByRole('article')).toHaveCount(2);
    expect(submissions).toHaveLength(2);
    await page.getByRole('button', { name: 'Abrir cesta, 0 artículos', exact: true }).click();
    await expect(basket.getByRole('region', { name: 'Cesta vacía', exact: true })).toBeVisible();
    await page.evaluate(({ accountId, lines }) => localStorage.setItem(`partsmall:basket:${accountId}`, JSON.stringify(lines)), { accountId: clientAccount, lines: draft });
    await page.reload();
    await expect(basket.getByRole('article')).toHaveCount(3);
    await basket.getByRole('button', { name: 'Enviar solicitud', exact: true }).click();
    await expect(sent.getByRole('status').filter({ hasText: 'Solicitud enviada' })).toBeVisible();
    expect(submissions).toHaveLength(3);
    expect(submissions[2].submission_id).not.toBe(submissions[0].submission_id);
    expect(submissions[2].lines).toEqual(submissions[0].lines);
    expect(await page.evaluate(accountId => JSON.parse(localStorage.getItem(`partsmall:basket:${accountId}`)!), clientAccount)).toEqual([]);
    expect(unexpected).toEqual([]);
  } finally { release(); }
});

test('preview and supplier-only baskets cannot send requests even with selected supplier item ids', async ({ page }) => {
  const writes: string[] = [];
  await page.route('**/api/**', route => {
    if (route.request().method() !== 'GET') writes.push(route.request().url());
    return route.fulfill({ json: { authenticated: false } });
  });
  await page.addInitScript(lines => localStorage.setItem('partsmall:basket:preview', JSON.stringify(lines)), clientDraft());
  await page.goto('/?vista=cesta');
  const basket = page.getByRole('main', { name: 'Cesta de solicitudes', exact: true });
  await expect(basket.getByRole('article')).toHaveCount(3);
  await expect(basket.getByRole('button', { name: 'Enviar solicitud de ejemplo', exact: true })).toBeDisabled();
  await expect(basket).toContainText(/inicia sesión/i);
  expect(writes).toEqual([]);
  const unexpected = await mockSession(page);
  await page.evaluate(({ accountId, lines }) => localStorage.setItem(`partsmall:basket:${accountId}`, JSON.stringify(lines)), { accountId: supplierAccount, lines: clientDraft() });
  await page.reload();
  await expect(basket.getByRole('article')).toHaveCount(3);
  await expect(basket.getByRole('button', { name: 'Enviar solicitud', exact: true })).toBeDisabled();
  await expect(basket).toContainText('Selecciona una cuenta con rol de cliente');
  expect(unexpected).toEqual([]);
});

test('reselecting a remapped supplier item recovers a stale submission with fresh SKU, code and brand without duplicates', async ({ page }) => {
  const unexpected = await mockSession(page, [{ id: clientAccount, name: 'TALLER CLIENTE', roles: ['client_business'], capabilities: ['client'] }]);
  const original = clientDraft().slice(0, 1);
  const freshPart: Part = { id: 'dddddddd-dddd-4ddd-8ddd-dddddddddddd', sku: 'SKU-ACTUALIZADO', name: 'DISCO ACTUALIZADO', description: 'REPUESTO REMAPEADO', codes: [{ code: 'CODIGO-ACTUALIZADO', brand: '' }] };
  await page.addInitScript(({ accountId, lines }) => localStorage.setItem(`partsmall:basket:${accountId}`, JSON.stringify(lines)), { accountId: clientAccount, lines: original });
  await page.route(/\/api\/market\/catalog(?:\?.*)?$/, route => route.fulfill({ json: { ...emptyPage, count: 1, results: [freshPart] } }));
  await page.route(`/api/market/catalog/${freshPart.id}/suppliers`, route => route.fulfill({ json: [{
    supplier_id: supplierAccount, supplier_name: supplierAccounts[0].name, in_stock: true,
    items: [{ id: itemA, codigo: 'CODIGO-ACTUALIZADO', brand: 'MARCA ACTUALIZADA', description: freshPart.description }],
  }] }));
  const submitted: { submission_id: string; lines: { supplier_item_id: string; quantity: number; expected_part_id: string; expected_codigo: string; expected_brand: string }[] }[] = [];
  await page.route(`/api/market/accounts/${clientAccount}/requests`, route => {
    const payload = route.request().postDataJSON(); submitted.push(payload);
    if (submitted.length === 1) return route.fulfill({ status: 409, json: { detail: 'El SKU, código o marca del artículo cambió. Selecciónalo otra vez en el catálogo.' } });
    return route.fulfill({ json: { submission_id: payload.submission_id, created_at: '2026-10-02T12:00:00Z', requests: [
      { id: requestId, reference: 'SOL-RECUPERADA-001', supplier: { id: supplierAccount, name: supplierAccounts[0].name }, line_count: 1, unit_count: 3 },
    ] } });
  });
  await page.goto('/?vista=cesta');
  const basket = page.getByRole('main', { name: 'Cesta de solicitudes', exact: true });
  await basket.getByRole('button', { name: 'Enviar solicitud', exact: true }).click();
  await expect(basket.getByRole('alert')).toContainText('Selecciónalo otra vez en el catálogo.');
  await basket.getByRole('button', { name: 'Volver al catálogo', exact: true }).click();
  await page.getByRole('button', { name: 'Ver DISCO ACTUALIZADO', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByRole('button', { name: `Agregar CODIGO-ACTUALIZADO de ${supplierAccounts[0].name} a la cesta`, exact: true }).click();
  await dialog.getByRole('button', { name: 'Cerrar ventana', exact: true }).click();
  await page.getByRole('button', { name: 'Abrir cesta, 1 artículo', exact: true }).click();
  await expect(basket.getByRole('article')).toHaveCount(1);
  await expect(basket.getByRole('heading', { name: 'CODIGO-ACTUALIZADO', exact: true })).toBeVisible();
  await expect(basket.getByText('CODIGO-ACTUALIZADO · MARCA ACTUALIZADA', { exact: true })).toBeVisible();
  await expect(basket.getByLabel('Cantidad de CODIGO-ACTUALIZADO', { exact: true })).toHaveValue('3');
  await basket.getByRole('button', { name: 'Enviar solicitud', exact: true }).click();
  await expect(page.getByRole('main', { name: 'Solicitudes enviadas', exact: true }).getByRole('status').filter({ hasText: 'Solicitud enviada' })).toBeVisible();
  expect(submitted).toHaveLength(2);
  expect(submitted[1].submission_id).not.toBe(submitted[0].submission_id);
  expect(submitted[1].lines).toEqual([{ supplier_item_id: itemA, quantity: 3, expected_part_id: freshPart.id, expected_codigo: 'CODIGO-ACTUALIZADO', expected_brand: 'MARCA ACTUALIZADA' }]);
  const saved = await page.evaluate(accountId => JSON.parse(localStorage.getItem(`partsmall:basket:${accountId}`) || '[]') as BasketLine[], clientAccount);
  expect(saved).toHaveLength(0);
  expect(unexpected).toEqual([]);
});
