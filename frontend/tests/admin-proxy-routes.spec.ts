import { expect, test } from '@playwright/test';

// The management proxy forwards only allowlisted routes ("La ruta solicitada no existe." otherwise). The screen specs mock these
// routes in the browser, before the proxy, so this one calls the real proxy and backend: GETs, and a batch the backend refuses unread.
const origin = process.env.E2E_BASE_URL || 'http://localhost:8080';

test('the proxy forwards the routes of Editar celdas (Inventario and Biblioteca OEM), Separar SKU, Por revisar and the SKU editor', async ({ page }) => {
  test.skip(!process.env.E2E_ADMIN_TOKEN, 'A disposable superuser token is required.');
  await page.context().addCookies([{ name: 'partsmall_session', value: process.env.E2E_ADMIN_TOKEN!, url: origin, httpOnly: true, sameSite: 'Strict' }]);
  const catalog = await page.request.get('/api/management/catalog?page=1');
  expect(catalog.status()).toBe(200);
  const first = (await catalog.json()).results[0]?.id;
  const paths = ['/api/management/catalog/taxonomy', '/api/management/alternate-review', '/api/management/catalog/merged?page=1&origin=automatic',
    '/api/management/oem-references/part-types',
    ...(first ? [`/api/management/catalog/${first}/oem-equivalents`, `/api/management/catalog/${first}/split`] : [])];
  for (const path of paths) {
    const response = await page.request.get(path);
    expect(response.status(), `${path}: ${await response.text()}`).toBe(200);
  }
  const reference = (await (await page.request.get('/api/management/oem-references?page=1')).json()).results[0]?.id;
  if (reference) {  // Biblioteca OEM's Editar celdas re-reads its rows the same way
    const oem = await page.request.get(`/api/management/oem-references?ids=${reference}`);
    expect((await oem.json()).results.map((row: { id: string }) => row.id)).toEqual([reference]);
  }
  if (first) {  // the rows Editar celdas re-reads after a save, a popup or a poll
    const reread = await page.request.get(`/api/management/catalog?ids=${first}`);
    expect((await reread.json()).results.map((row: { id: string }) => row.id)).toEqual([first]);
  }
  // Empty batches reach the backend and are refused there, so nothing is written.
  for (const path of ['/api/management/catalog/bulk-edit', '/api/management/oem-references/bulk-edit']) {
    const batch = await page.request.post(path, { data: { rows: [] }, headers: { Origin: origin } });
    expect(batch.status(), `${path}: ${await batch.text()}`).toBe(400);
    expect(await batch.text()).not.toContain('La ruta solicitada no existe');
  }
});

test('the proxy no longer forwards the OEM-link routes the owner retired on 2026-10-14', async ({ page }) => {
  test.skip(!process.env.E2E_ADMIN_TOKEN, 'A disposable superuser token is required.');
  await page.context().addCookies([{ name: 'partsmall_session', value: process.env.E2E_ADMIN_TOKEN!, url: origin, httpOnly: true, sameSite: 'Strict' }]);
  // Marcar como OEM, Revisión OEM and Aplicación automática: the proxy answers itself. Only reads and empty batches are sent, which an
  // older proxy would forward and the backend refuse, so nothing is written either way.
  const zero = '00000000-0000-4000-8000-000000000000';
  for (const path of [`/api/management/catalog/mark-oem?ids=${zero}`, `/api/management/catalog/create-oem?ids=${zero}`, '/api/management/oem-review/summary',
    '/api/management/oem-review?status=review', '/api/management/oem-finder/pending', '/api/management/oem-finder/runs?mode=apply_auto']) {
    const response = await page.request.get(path);
    expect(response.status(), path).toBe(404);
    expect(await response.text()).toContain('La ruta solicitada no existe');
  }
  for (const path of ['/api/management/catalog/mark-oem', '/api/management/catalog/create-oem', '/api/management/catalog/unmark-oem']) {
    const response = await page.request.post(path, { data: { rows: [] }, headers: { Origin: origin } });
    expect(response.status(), path).toBe(404);
    expect(await response.text()).toContain('La ruta solicitada no existe');
  }
});

test('the proxy forwards the apiag-cloud connection of Cuentas', async ({ page }) => {
  test.skip(!process.env.E2E_ADMIN_TOKEN, 'A disposable superuser token is required.');
  await page.context().addCookies([{ name: 'partsmall_session', value: process.env.E2E_ADMIN_TOKEN!, url: origin, httpOnly: true, sameSite: 'Strict' }]);
  // An account that does not exist: the backend answers 404 itself, so nothing is written.
  const path = '/api/management/accounts/00000000-0000-4000-8000-000000000000/apiag';
  for (const response of [await page.request.post(path, { data: { server_url: 'https://empresa.apiag.cloud', access_token: '' }, headers: { Origin: origin } }), await page.request.delete(path, { headers: { Origin: origin } })]) {
    expect(response.status(), await response.text()).toBe(404);
    expect(await response.text()).not.toContain('La ruta solicitada no existe');
  }
});

test('the proxy forwards Probar conexión of Cuentas', async ({ page }) => {
  test.skip(!process.env.E2E_ADMIN_TOKEN, 'A disposable superuser token is required.');
  await page.context().addCookies([{ name: 'partsmall_session', value: process.env.E2E_ADMIN_TOKEN!, url: origin, httpOnly: true, sameSite: 'Strict' }]);
  // An account that does not exist: the backend answers 404 before calling any server.
  const response = await page.request.post('/api/management/accounts/00000000-0000-4000-8000-000000000000/apiag/test', { data: { server_url: 'empresa.apiag.cloud', access_token: '' }, headers: { Origin: origin } });
  expect(response.status(), await response.text()).toBe(404);
  expect(await response.text()).not.toContain('La ruta solicitada no existe');
});

test('the proxy forwards Productos nuevos', async ({ page }) => {
  test.skip(!process.env.E2E_ADMIN_TOKEN, 'A disposable superuser token is required.');
  await page.context().addCookies([{ name: 'partsmall_session', value: process.env.E2E_ADMIN_TOKEN!, url: origin, httpOnly: true, sameSite: 'Strict' }]);
  for (const path of ['/api/management/catalog/proposals/summary', '/api/management/catalog/proposals?status=pending&page=1']) {
    const response = await page.request.get(path);
    expect(response.status(), `${path}: ${await response.text()}`).toBe(200);
  }
  // An empty batch reaches the backend and is refused there, so nothing is written.
  const bulk = await page.request.post('/api/management/catalog/proposals/bulk', { data: { action: 'approve', proposals: [] }, headers: { Origin: origin } });
  expect(bulk.status(), await bulk.text()).toBe(400);
  expect(await bulk.text()).not.toContain('La ruta solicitada no existe');
  // Revisión IA: the overview and an unknown run are read; starting one without an id is refused by the backend, never billed.
  const overview = await page.request.get('/api/management/catalog/proposals/review?search=');
  expect(overview.status(), await overview.text()).toBe(200);
  expect(Object.keys(await overview.json())).toEqual(expect.arrayContaining(['configured', 'cap_micro_usd', 'eligible_count', 'unreviewed_count', 'runs']));
  const missing = await page.request.get('/api/management/catalog/proposals/review/00000000-0000-4000-8000-000000000000?verdict=good&mode=actionable');
  expect(missing.status(), await missing.text()).toBe(404);
  expect(await missing.text()).not.toContain('La ruta solicitada no existe');
  for (const [path, data] of [['/api/management/catalog/proposals/review', {}], ['/api/management/catalog/proposals/review/00000000-0000-4000-8000-000000000000', { mode: 'nope' }]] as const) {
    const refused = await page.request.post(path, { data, headers: { Origin: origin } });
    expect(refused.status(), `${path}: ${await refused.text()}`).toBe(400);
  }
  const reject = await page.request.post('/api/management/catalog/proposals/bulk', { data: { action: 'reject', proposals: [] }, headers: { Origin: origin } });
  expect(reject.status(), await reject.text()).toBe(400);
});

test('the proxies forward the supplier ledger routes and their filters', async ({ page }) => {
  test.skip(!process.env.E2E_ADMIN_TOKEN, 'A disposable superuser token is required.');
  await page.context().addCookies([{ name: 'partsmall_session', value: process.env.E2E_ADMIN_TOKEN!, url: origin, httpOnly: true, sameSite: 'Strict' }]);
  const roles = await (await page.request.get('/api/management/roles')).json() as { code: string; capability: string }[];
  const suppliers = new Set(roles.filter(role => role.capability === 'supplier').map(role => role.code));
  const accounts = await (await page.request.get('/api/management/accounts?page=1')).json();
  const supplier = (accounts.results ?? accounts).find((row: { roles: string[] }) => row.roles.some(code => suppliers.has(code)))?.id;
  const zero = '00000000-0000-4000-8000-000000000000';
  if (supplier) {
    for (const path of [`/api/management/accounts/${supplier}/ledger?page=1&origin=excel&kind=sync&after=2026-01-01&before=2026-12-31&search=X`,
      `/api/management/accounts/${supplier}/ledger/runs?page=1&origin=apiag`]) {
      const response = await page.request.get(path);
      expect(response.status(), `${path}: ${await response.text()}`).toBe(200);
    }
  }
  // Unknown runs and a superuser on the members' route are the backend's 404s, never the proxy's.
  for (const path of [`/api/management/accounts/${supplier ?? zero}/ledger/runs/${zero}`, `/api/management/accounts/${supplier ?? zero}/ledger/runs/${zero}/changes?action=created&search=X`,
    `/api/market/accounts/${supplier ?? zero}/ledger?page=1&origin=quotation&item=${zero}`, `/api/market/accounts/${supplier ?? zero}/ledger/runs/${zero}/changes?action=stock`]) {
    expect(await (await page.request.get(path)).text(), path).not.toContain('La ruta solicitada no existe');
  }
});

test('the proxy forwards Agrupación inteligente: the cases by kind and the S6 decisions on matching/<id>', async ({ page }) => {
  test.skip(!process.env.E2E_ADMIN_TOKEN, 'A disposable superuser token is required.');
  await page.context().addCookies([{ name: 'partsmall_session', value: process.env.E2E_ADMIN_TOKEN!, url: origin, httpOnly: true, sameSite: 'Strict' }]);
  for (const path of ['/api/management/matching?status=review&offset=0', '/api/management/matching?status=applied&offset=0&kind=merge']) {
    const response = await page.request.get(path);
    expect(response.status(), `${path}: ${await response.text()}`).toBe(200);
  }
  // A case that does not exist: the backend refuses each decision itself (404, or 400 for an action it does not know), so nothing is written.
  const zero = '00000000-0000-4000-8000-000000000000';
  for (const data of [{ action: 'approve', line: zero }, { action: 'separar_referencia' }, { action: 'merge', line: zero }, { action: 'keep_apart' }]) {
    const response = await page.request.post(`/api/management/matching/${zero}`, { data: { ...data, fingerprint: '0'.repeat(64) }, headers: { Origin: origin } });
    expect([400, 404], `${data.action}: ${await response.text()}`).toContain(response.status());
    expect(await response.text()).not.toContain('La ruta solicitada no existe');
  }
});

test('the proxy forwards the S7 routes: OEM lines, References, cases, merges, Marcas, Grupos and Separar referencia', async ({ page }) => {
  test.skip(!process.env.E2E_ADMIN_TOKEN, 'A disposable superuser token is required.');
  await page.context().addCookies([{ name: 'partsmall_session', value: process.env.E2E_ADMIN_TOKEN!, url: origin, httpOnly: true, sameSite: 'Strict' }]);
  const lines = await page.request.get('/api/management/lines?page=1&estado=publicada,retirada&tipo=OEM&ordering=sku');
  expect(lines.status(), await lines.text()).toBe(200);
  const first = (await lines.json()).results[0]?.id;
  const brands = await page.request.get('/api/management/brands?page=1&search=a&tipo=vehiculo');
  expect(brands.status(), await brands.text()).toBe(200);
  const brand = (await brands.json()).results[0]?.id;
  const grupos = await page.request.get('/api/management/grupos');
  expect(grupos.status(), await grupos.text()).toBe(200);
  const paths = ['/api/management/lines?page=1&estado=preparacion&origen=proveedor&grupo=none', '/api/management/cases?status=review&kind=merge&route=lote&page=1',
    '/api/management/merges?page=1&since=2026-01-01&search=A',
    ...(first ? [`/api/management/lines/${first}`, `/api/management/lines/${first}/references`, `/api/management/lines/${first}/main`,
      `/api/management/cases?line=${first}`, `/api/management/lines?ids=${first}`] : []),
    ...(brand ? [`/api/management/brands/${brand}`, `/api/management/brands?familia=${brand}`] : [])];
  for (const path of paths) {
    const response = await page.request.get(path);
    expect(response.status(), `${path}: ${await response.text()}`).toBe(200);
  }
  // Rows that do not exist, and bodies the backend refuses before it reads anything: its own 400s and 404s, so nothing is written.
  const zero = '00000000-0000-4000-8000-000000000000';
  const refused: [string, string, object | undefined][] = [
    ['GET', `/api/management/cases/${zero}`, undefined], ['GET', `/api/management/grupos/${zero}`, undefined],
    ['GET', '/api/management/cases/batches/no-such-batch', undefined],
    ['POST', `/api/management/lines/${zero}/transition`, {}], ['POST', `/api/management/lines/${zero}/references`, {}],
    ['POST', `/api/management/lines/${zero}/main`, {}], ['POST', `/api/management/cases/${zero}`, { action: 'dismiss', fingerprint: '0'.repeat(64) }],
    ['POST', '/api/management/cases/batches/no-such-batch', { action: 'merge', cases: [] }], ['POST', '/api/management/brands', {}],
    ['POST', '/api/management/brands/0/aliases', {}], ['POST', '/api/management/grupos', {}], ['POST', `/api/management/inventory/${zero}/separate`, {}],
    ['PATCH', `/api/management/lines/${zero}`, {}], ['PATCH', '/api/management/references/0', {}], ['PATCH', '/api/management/brands/0', {}],
    ['PATCH', `/api/management/grupos/${zero}`, {}], ['DELETE', '/api/management/references/0', undefined],
    ['DELETE', '/api/management/brands/0/aliases/0', undefined]];
  for (const [method, path, data] of refused) {
    const response = await page.request.fetch(path, { method, data, headers: { Origin: origin } });
    expect([400, 404], `${method} ${path}: ${await response.text()}`).toContain(response.status());
    expect(await response.text()).not.toContain('La ruta solicitada no existe');
  }
});
