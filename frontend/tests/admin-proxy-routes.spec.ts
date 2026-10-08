import { expect, test } from '@playwright/test';

// The management proxy forwards only allowlisted routes ("La ruta solicitada no existe." otherwise). The screen specs mock these
// routes in the browser, before the proxy, so this one calls the real proxy and backend: GETs, and a batch the backend refuses unread.
const origin = process.env.E2E_BASE_URL || 'http://localhost:8080';

test('the proxy forwards the routes of Editar celdas, Por revisar and the SKU editor', async ({ page }) => {
  test.skip(!process.env.E2E_ADMIN_TOKEN, 'A disposable superuser token is required.');
  await page.context().addCookies([{ name: 'partsmall_session', value: process.env.E2E_ADMIN_TOKEN!, url: origin, httpOnly: true, sameSite: 'Strict' }]);
  const catalog = await page.request.get('/api/management/catalog?page=1');
  expect(catalog.status()).toBe(200);
  const first = (await catalog.json()).results[0]?.id;
  const paths = ['/api/management/catalog/taxonomy', '/api/management/alternate-review', ...(first ? [`/api/management/catalog/${first}/oem-equivalents`] : [])];
  for (const path of paths) {
    const response = await page.request.get(path);
    expect(response.status(), `${path}: ${await response.text()}`).toBe(200);
  }
  if (first) {  // the rows Editar celdas re-reads after a save, a popup or a poll
    const reread = await page.request.get(`/api/management/catalog?ids=${first}`);
    expect((await reread.json()).results.map((row: { id: string }) => row.id)).toEqual([first]);
  }
  // An empty batch reaches the backend and is refused there, so nothing is written.
  const batch = await page.request.post('/api/management/catalog/bulk-edit', { data: { rows: [] }, headers: { Origin: origin } });
  expect(batch.status(), await batch.text()).toBe(400);
  expect(await batch.text()).not.toContain('La ruta solicitada no existe');
});
