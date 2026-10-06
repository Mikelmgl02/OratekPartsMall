import { expect, Page, test } from '@playwright/test';

const accountId = '11111111-1111-4111-8111-111111111111';
const part = { id: '22222222-2222-4222-8222-222222222222', sku: '58411-1R000', name: '', description: 'TAMBOR HYU ACCENT', codes: [] };
const itemId = '33333333-3333-4333-8333-333333333333';
type Event = { kind: string; event_id: string; visit_id: string; account_id: string; term?: string; quantity?: number; part_id?: string; supplier_item_id?: string };
const catalog = { count: 1, next: null, previous: null, results: [part] };

async function mockClient(page: Page, superuser = false) {
  await page.route('**/api/session', route => route.fulfill({ json: { authenticated: true,
    user: { id: 101, username: 'CLIENTE', is_superuser: superuser },
    accounts: { count: 1, next: null, previous: null, results: [{ id: accountId, name: 'CLIENTE QA', roles: ['client_business'], capabilities: ['client'] }] } } }));
  await page.route(`**/api/market/catalog/${part.id}/suppliers`, route => route.fulfill({ json: [{
    supplier_id: '44444444-4444-4444-8444-444444444444', supplier_name: 'PROVEEDOR QA', in_stock: true,
    items: [{ id: itemId, codigo: '58411-1R000-G', brand: '', description: 'TAMBOR' }],
  }] }));
  await page.route('**/request-state', route => route.fulfill({ json: { part_id: part.id,
    totals: { sent_quantity: 0, pending_quantity: 0, reviewed_quantity: 0 }, items: [] } }));
}

test('real browsing events share a visit, retry safely and do not interrupt basket operations', async ({ page }) => {
  const events: Event[] = [];
  await mockClient(page);
  await page.route(/\/api\/market\/catalog(?:\?.*)?$/, route => route.fulfill({ json: catalog }));
  await page.route('**/api/market/analytics/events', route => {
    const event = route.request().postDataJSON() as Event; events.push(event);
    return route.fulfill(event.kind === 'basket_add' ? { status: 503, json: { detail: 'Temporalmente no disponible.' } } : { status: 202, json: { recorded: true } });
  });
  await page.goto('/');
  await expect(page.getByRole('button', { name: `Ver ${part.sku}`, exact: true })).toBeVisible();
  await expect.poll(() => events.filter(event => event.kind === 'visit').length).toBe(1);
  await page.getByLabel('Buscar repuestos, marcas o códigos', { exact: true }).fill('58411');
  await expect.poll(() => events.filter(event => event.kind === 'search').length).toBe(1);
  await page.getByRole('button', { name: `Ver ${part.sku}`, exact: true }).click();
  await page.getByLabel('Cantidad solicitada', { exact: true }).fill('3');
  await page.getByRole('button', { name: 'Agregar 58411-1R000-G de PROVEEDOR QA a la cesta', exact: true }).click();
  await expect(page.getByRole('button', { name: '58411-1R000-G agregado, 3 en cesta', exact: true })).toBeVisible();
  await expect.poll(() => events.filter(event => event.kind === 'basket_add').length).toBe(2);
  const adds = events.filter(event => event.kind === 'basket_add');
  expect(adds[0]).toEqual(adds[1]);
  expect(adds[0]).toMatchObject({ part_id: part.id, supplier_item_id: itemId, quantity: 3, account_id: accountId });
  expect(events.filter(event => event.kind === 'part_view')).toHaveLength(1);
  expect(new Set(events.map(event => event.visit_id)).size).toBe(1);
  expect(events.some(event => 'result_count' in event || 'user_id' in event)).toBe(false);
  await page.getByRole('button', { name: 'Cerrar ventana', exact: true }).click();
  await page.reload();
  await expect.poll(() => events.filter(event => event.kind === 'visit').length).toBe(2);
  expect(new Set(events.map(event => event.visit_id)).size).toBe(1);
  await page.getByRole('button', { name: `Ver ${part.sku}`, exact: true }).click();
  await expect(page.getByRole('button', { name: '58411-1R000-G agregado, 3 en cesta', exact: true })).toBeVisible();
});

test('obsolete searches and pagination do not inflate searches; zero-result searches are tracked', async ({ page }) => {
  const events: Event[] = [];
  let releaseOlder!: () => void;
  let olderRequested!: () => void;
  const requested = new Promise<void>(resolve => { olderRequested = resolve; });
  const older = new Promise<void>(resolve => { releaseOlder = resolve; });
  await mockClient(page);
  await page.route('**/api/market/analytics/events', route => {
    events.push(route.request().postDataJSON());
    return route.fulfill({ status: 202, json: { recorded: true } });
  });
  await page.route(/\/api\/market\/catalog(?:\?.*)?$/, async route => {
    const url = new URL(route.request().url());
    const search = url.searchParams.get('search');
    if (search === 'ANTERIOR') { olderRequested(); await older; }
    await route.fulfill({ json: search === 'SINRESULTADO' ? { ...catalog, count: 0, results: [] } : { ...catalog, count: 100, next: url.searchParams.get('page') === '2' ? null : '/api/market/catalog?page=2' } });
  });
  try {
    await page.goto('/');
    await expect(page.getByRole('button', { name: `Ver ${part.sku}`, exact: true })).toBeVisible();
    const input = page.getByLabel('Buscar repuestos, marcas o códigos', { exact: true });
    await input.fill('ANTERIOR'); await requested;
    await input.fill('ACTUAL');
    await expect.poll(() => events.filter(event => event.kind === 'search').map(event => event.term)).toEqual(['ACTUAL']);
    releaseOlder();
    await page.getByRole('button', { name: 'Siguiente', exact: true }).click();
    await expect(page.locator('.pagination')).toContainText('Página 2');
    await input.fill('SINRESULTADO');
    await expect(page.getByRole('heading', { name: 'No encontramos repuestos para esta búsqueda.', exact: true })).toBeVisible();
    await expect.poll(() => events.filter(event => event.kind === 'search').map(event => event.term)).toEqual(['ACTUAL', 'SINRESULTADO']);
  } finally { releaseOlder(); }
});

test('superuser browsing is excluded from telemetry', async ({ page }) => {
  const events: Event[] = [];
  await mockClient(page, true);
  await page.route(/\/api\/market\/catalog(?:\?.*)?$/, route => route.fulfill({ json: catalog }));
  await page.route('**/api/market/analytics/events', route => { events.push(route.request().postDataJSON()); return route.fulfill({ status: 202, json: {} }); });
  await page.goto('/');
  await page.getByLabel('Buscar repuestos, marcas o códigos', { exact: true }).fill('58411');
  await expect(page.locator('.results-heading>p')).toContainText('58411');
  await page.getByRole('button', { name: `Ver ${part.sku}`, exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Elige tu proveedor', exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Agregar 58411-1R000-G de PROVEEDOR QA a la cesta', exact: true }).click();
  await expect(page.getByRole('button', { name: '58411-1R000-G agregado, 1 en cesta', exact: true })).toBeVisible();
  expect(events).toEqual([]);
});

test('analytics proxies reject anonymous access and cross-origin event writes', async ({ request }) => {
  expect((await request.get('/api/management/analytics?days=30')).status()).toBe(401);
  expect((await request.post('/api/market/analytics/events', { data: {} })).status()).toBe(401);
  const denied = await request.post('/api/market/analytics/events', {
    headers: { Origin: 'https://unrelated.example', Cookie: 'partsmall_session=invalid-test-token' }, data: {},
  });
  expect(denied.status()).toBe(403);
});
