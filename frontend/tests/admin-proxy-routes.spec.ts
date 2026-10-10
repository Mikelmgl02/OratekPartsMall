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
    ...(first ? [`/api/management/catalog/${first}/oem-equivalents`, `/api/management/catalog/${first}/split`, `/api/management/catalog/mark-oem?ids=${first}`] : [])];
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
  for (const path of ['/api/management/catalog/bulk-edit', '/api/management/catalog/mark-oem', '/api/management/catalog/unmark-oem', '/api/management/oem-references/bulk-edit']) {
    const batch = await page.request.post(path, { data: { rows: [] }, headers: { Origin: origin } });
    expect(batch.status(), `${path}: ${await batch.text()}`).toBe(400);
    expect(await batch.text()).not.toContain('La ruta solicitada no existe');
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
