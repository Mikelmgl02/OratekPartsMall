import { expect, test } from '@playwright/test';
import { signedOutInbox } from './inbox-route';
import type { ApiagConnection, ManagedAccount } from '../lib/types';

// Cuentas → apiag-cloud, for superusers: server-rendered administration needs a session token; the screen's API calls are mocked.
const supplierId = '22222222-2222-4222-8222-222222222222';
const clientId = '11111111-1111-4111-8111-111111111111';
const secret = 'apiagTok3n-For-MotionPartes-0001';
const roles = [{ code: 'supplier_wholesale', name: 'Proveedor mayorista', capability: 'supplier' }, { code: 'client_business', name: 'Cliente empresarial', capability: 'client' }];
const saved: ApiagConnection = { server_url: 'https://empresa.apiag.cloud', token_hint: '0001', token_readable: true, updated_at: '2026-10-10T15:00:00Z', updated_by: 'root' };

test('superusers connect a supplier account to its apiag-cloud without the token ever coming back', async ({ page, baseURL }) => {
  test.skip(!process.env.E2E_ADMIN_TOKEN, 'A disposable superuser token is required for server-rendered administration.');
  await page.context().addCookies([{ name: 'partsmall_session', value: process.env.E2E_ADMIN_TOKEN!, url: baseURL || 'http://localhost:8080', httpOnly: true, sameSite: 'Strict' }]);
  let connection: ApiagConnection | null = null;
  const posts: unknown[] = [], deletes: string[] = [], unexpected: string[] = [];
  let refuse: Record<string, string[]> | null = null;
  const accounts = (): ManagedAccount[] => [
    { id: supplierId, name: 'REPUESTOS CENTRAL', active: true, roles: ['supplier_wholesale'], member_count: 2, members: [], apiag: connection },
    { id: clientId, name: 'TALLER CENTRAL', active: true, roles: ['client_business'], member_count: 1, members: [], apiag: null }];
  await page.route('**/api/**', route => { if (signedOutInbox(route)) return; unexpected.push(`${route.request().method()} ${new URL(route.request().url()).pathname}`); return route.fulfill({ status: 404, json: { detail: 'Ruta no prevista' } }); });
  await page.route('**/api/market/analytics/events', route => route.fulfill({ status: 202, json: { recorded: true } }));
  await page.route(/\/api\/management\/roles(?:\?.*)?$/, route => route.fulfill({ json: roles }));
  await page.route(/\/api\/management\/accounts(?:\?.*)?$/, route => route.fulfill({ json: { count: 2, next: null, previous: null, results: accounts() } }));
  await page.route(`**/api/management/accounts/${supplierId}/apiag`, route => {
    if (route.request().method() === 'DELETE') { deletes.push(supplierId); connection = null; return route.fulfill({ status: 204 }); }
    const body = route.request().postDataJSON();
    posts.push(body);
    if (refuse) return route.fulfill({ status: 400, json: refuse });
    connection = { ...saved, server_url: body.server_url, token_readable: true };
    return route.fulfill({ status: 201, json: connection });
  });

  await page.goto('/administracion?seccion=cuentas');
  const clientRow = page.getByRole('row').filter({ hasText: 'TALLER CENTRAL' });
  await expect(clientRow.getByRole('button', { name: 'Editar cuenta TALLER CENTRAL' })).toBeVisible();
  await expect(clientRow.getByRole('button', { name: /apiag-cloud/ })).toHaveCount(0);

  await page.getByRole('button', { name: 'Conexión con apiag-cloud de REPUESTOS CENTRAL', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'apiag-cloud · REPUESTOS CENTRAL' });
  // Masked, but not a password field that browsers offer to save as a MotionPartes password; no length cap that cuts a long paste.
  await expect(dialog.getByLabel('Token de acceso')).not.toHaveAttribute('type', 'password');
  await expect(dialog.getByLabel('Token de acceso')).not.toHaveAttribute('maxlength', /.*/);
  expect(await dialog.getByLabel('Token de acceso').evaluate(input => getComputedStyle(input).getPropertyValue('-webkit-text-security'))).toBe('disc');
  await dialog.getByLabel('URL del servidor').fill('http://empresa.apiag.cloud');
  await dialog.getByLabel('Token de acceso').fill(secret);
  refuse = { server_url: ['Usa una dirección https:// para que el token viaje cifrado.'] };
  await dialog.getByRole('button', { name: 'Guardar conexión' }).click();
  await expect(dialog.getByRole('alert')).toHaveText('URL del servidor: Usa una dirección https:// para que el token viaje cifrado.');
  refuse = { access_token: ['Pega solo el token, sin «Token» ni «Bearer» delante: entre 16 y 512 caracteres, sin espacios.'] };
  await dialog.getByRole('button', { name: 'Guardar conexión' }).click();
  await expect(dialog.getByRole('alert')).toHaveText('Token de acceso: Pega solo el token, sin «Token» ni «Bearer» delante: entre 16 y 512 caracteres, sin espacios.');
  refuse = null;
  await dialog.getByLabel('URL del servidor').fill('https://empresa.apiag.cloud');
  await dialog.getByRole('button', { name: 'Guardar conexión' }).click();
  await expect(page.getByText('Conexión con apiag-cloud guardada.')).toBeVisible();
  expect(posts).toEqual([{ server_url: 'http://empresa.apiag.cloud', access_token: secret }, { server_url: 'http://empresa.apiag.cloud', access_token: secret }, { server_url: 'https://empresa.apiag.cloud', access_token: secret }]);
  await expect(page.getByRole('row').filter({ hasText: 'REPUESTOS CENTRAL' }).getByText('Conectada a apiag-cloud')).toBeVisible();

  // Reopened: the address comes back, the token never does; a blank token keeps the saved one.
  await page.getByRole('button', { name: 'Conexión con apiag-cloud de REPUESTOS CENTRAL', exact: true }).click();
  await expect(dialog.getByLabel('URL del servidor')).toHaveValue('https://empresa.apiag.cloud');
  await page.screenshot({ path: '/tmp/motionpartes-apiag-desktop.png' });
  await expect(dialog.getByLabel('Token de acceso')).toHaveValue('');
  await expect(dialog.getByLabel('Token de acceso')).toHaveAttribute('placeholder', 'Déjalo vacío para conservar el token guardado');
  await expect(dialog.getByText(/Token guardado: termina en 0001\. Por seguridad no se vuelve a mostrar\. Conexión actualizada el .* por root\./)).toBeVisible();
  expect(await page.content()).not.toContain(secret);
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await dialog.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: '/tmp/motionpartes-apiag-mobile.png' });
  await dialog.getByRole('button', { name: 'Guardar conexión' }).click();
  await expect(dialog).toBeHidden();
  expect(posts.at(-1)).toEqual({ server_url: 'https://empresa.apiag.cloud', access_token: '' });

  // A token the server can no longer read (its key changed) has to be pasted again.
  connection = { ...saved, token_readable: false };
  await page.reload();
  await expect(page.getByRole('row').filter({ hasText: 'REPUESTOS CENTRAL' }).getByText('apiag-cloud: vuelve a ingresar el token')).toBeVisible();
  await page.getByRole('button', { name: 'Conexión con apiag-cloud de REPUESTOS CENTRAL', exact: true }).click();
  await expect(dialog.getByRole('alert')).toContainText('no se puede leer con la clave actual');
  await expect(dialog.getByLabel('Token de acceso')).toHaveAttribute('required', '');

  await dialog.getByRole('button', { name: 'Quitar conexión' }).click();
  await dialog.getByRole('button', { name: 'Confirmar: quitar conexión' }).click();
  await expect(page.getByText('Conexión con apiag-cloud eliminada.')).toBeVisible();
  expect(deletes).toEqual([supplierId]);
  await expect(page.getByRole('row').filter({ hasText: 'REPUESTOS CENTRAL' }).getByText(/Conectada a apiag-cloud|vuelve a ingresar el token/)).toHaveCount(0);
  expect(unexpected).toEqual([]);
});
