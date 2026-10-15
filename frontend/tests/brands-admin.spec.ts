import { test, expect, type Page } from '@playwright/test';
import type { Brand } from '../lib/line-types';
import { noNewProducts } from './new-products-route';

// Marcas (plan S8.5): the brands with their aliases, tipo and familia; a brand created, renamed (its codes follow, the old spelling stays as
// an alias), given a familia, an alias added and removed, and one brand merged into another. management/brands, mocked in the browser.
const brand = (id: number, nombre: string, extra: Partial<Brand> = {}): Brand => ({ id, nombre, tipo: '', familia: null, aliases: [], miembros: [], notas: '', reference_count: 0,
  created_at: '2026-10-14T00:00:00Z', updated_at: '2026-10-14T00:00:00Z', ...extra });

async function mockBrands(page: Page) {
  test.skip(!process.env.E2E_ADMIN_TOKEN, 'Disposable superuser token required.');
  await page.context().addCookies([{ name: 'partsmall_session', value: process.env.E2E_ADMIN_TOKEN!, url: 'http://localhost:8080', httpOnly: true, sameSite: 'Strict' }]);
  const rows = new Map<number, Brand>([
    [1, brand(1, 'TOYOTA', { tipo: 'vehiculo', aliases: [{ id: 11, alias: 'TOY' }], reference_count: 4200, miembros: [{ id: 3, nombre: 'LEXUS' }] })],
    [2, brand(2, 'CHERY', { tipo: 'vehiculo', reference_count: 310 })],
    [3, brand(3, 'LEXUS', { tipo: 'vehiculo', familia: { id: 1, nombre: 'TOYOTA' }, reference_count: 85 })],
    [4, brand(4, 'KAYI', { reference_count: 12 })],
    [5, brand(5, 'GMB', { tipo: 'repuesto', reference_count: 990 })],
  ]);
  const writes: string[] = [], unexpected: string[] = [], lists: string[] = [];
  let next = 100;
  await page.route('**/api/**', route => { if (noNewProducts(route)) return; unexpected.push(`${route.request().method()} ${new URL(route.request().url()).pathname}`); return route.fulfill({ status: 404, json: { detail: 'Ruta no prevista' } }); });
  await page.route(/\/api\/management\/roles$/, route => route.fulfill({ json: [] }));
  await page.route(/\/api\/management\/brands(\/.*)?(\?.*)?$/, route => {
    const request = route.request(), url = new URL(request.url()), parts = url.pathname.replace('/api/management/', '').split('/'), method = request.method();
    const body = method === 'GET' || method === 'DELETE' ? null : request.postDataJSON() as Record<string, unknown>;
    if (method !== 'GET') writes.push(`${method} ${parts.join('/')}${body ? ` ${JSON.stringify(body)}` : ''}`);
    if (parts.length === 1 && method === 'GET') {
      lists.push(url.searchParams.toString());
      const search = (url.searchParams.get('search') || '').toUpperCase(), tipo = url.searchParams.get('tipo');
      const results = [...rows.values()].filter(row => (!search || row.nombre.includes(search) || row.aliases.some(alias => alias.alias.includes(search)))
        && (!tipo || (tipo === 'none' ? !row.tipo : row.tipo === tipo)));
      return route.fulfill({ json: { count: results.length, next: null, previous: null, results } });
    }
    if (parts.length === 1) {
      if ([...rows.values()].some(row => row.nombre === String(body!.nombre).toUpperCase())) return route.fulfill({ status: 400, json: { nombre: [`${String(body!.nombre).toUpperCase()} ya es una marca o un alias de una.`] } });
      const created = brand(++next, String(body!.nombre).toUpperCase(), { tipo: body!.tipo as Brand['tipo'] });
      rows.set(created.id, created);
      return route.fulfill({ status: 201, json: created });
    }
    const row = rows.get(Number(parts[1]))!;
    if (parts[2] === 'aliases' && method === 'POST') {
      const alias = { id: ++next, alias: String(body!.alias).toUpperCase() };
      row.aliases = [...row.aliases, alias];
      return route.fulfill({ status: 201, json: { alias, moved: 7, cases: ['0e000000-0000-4000-8000-000000000001'], brand: row } });
    }
    if (parts[2] === 'aliases') { row.aliases = row.aliases.filter(alias => alias.id !== Number(parts[3])); return route.fulfill({ status: 204, body: '' }); }
    if (body?.merge_into) { const kept = rows.get(Number(body.merge_into))!; rows.delete(row.id); kept.aliases = [...kept.aliases, { id: ++next, alias: row.nombre }]; return route.fulfill({ json: kept }); }
    if (body?.nombre) { row.aliases = [...row.aliases, { id: ++next, alias: row.nombre }]; row.nombre = String(body.nombre).toUpperCase(); }
    if (body && 'tipo' in body) row.tipo = body.tipo as Brand['tipo'];
    if (body && 'familia' in body) row.familia = body.familia ? { id: Number(body.familia), nombre: rows.get(Number(body.familia))!.nombre } : null;
    if (body && 'notas' in body) row.notas = String(body.notas);
    return route.fulfill({ json: row });
  });
  return { rows, writes, unexpected, lists };
}

for (const width of [1440, 390]) {
  test(`the brands list shows aliases, tipo and familia, and filters by name, alias and tipo at ${width}px`, async ({ page }) => {
    const mock = await mockBrands(page);
    await page.setViewportSize({ width, height: 1000 });
    await page.goto('/administracion?seccion=marcas');
    const screen = page.getByRole('region', { name: 'Marcas' });
    const toyota = screen.getByRole('row', { name: /^TOYOTA/ });
    await expect(toyota).toContainText('MARCA DE VEHÍCULO');
    await expect(toyota).toContainText('TOY');
    await expect(toyota).toContainText('Encabeza: LEXUS');
    await expect(toyota).toContainText('4,200');
    await expect(screen.getByRole('row', { name: /^LEXUS/ })).toContainText('De la familia TOYOTA');
    expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)).toBe(false);
    await page.screenshot({ path: `/tmp/motionpartes-brands-${width}.png` });
    await screen.getByLabel('Buscar marcas').fill('toy');
    await expect(screen.getByRole('row', { name: /^CHERY/ })).toHaveCount(0);
    await expect(screen.getByRole('row', { name: /^TOYOTA/ })).toBeVisible();
    await screen.getByLabel('Buscar marcas').fill('');
    await screen.getByLabel('Tipo de marca').selectOption('repuesto');
    await expect(screen.getByRole('row', { name: /^GMB/ })).toBeVisible();
    await expect(screen.getByRole('row', { name: /^TOYOTA/ })).toHaveCount(0);
    expect(mock.lists.at(-1)).toBe('page=1&tipo=repuesto');
    expect(mock.unexpected).toEqual([]);
  });
}

test('a brand is created, renamed into its familia, given and stripped of an alias, and merged into another', async ({ page }) => {
  const mock = await mockBrands(page);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto('/administracion?seccion=marcas');
  const screen = page.getByRole('region', { name: 'Marcas' });

  await screen.getByRole('button', { name: 'Crear marca' }).click();
  let dialog = page.getByRole('dialog', { name: 'Crear marca' });
  await dialog.getByLabel('Nombre de la marca').fill('toyota');
  await dialog.getByRole('button', { name: 'Crear marca' }).click();
  await expect(dialog.getByRole('alert')).toHaveText('Nombre: TOYOTA ya es una marca o un alias de una.');
  await dialog.getByLabel('Nombre de la marca').fill('baic');
  await dialog.getByLabel('Tipo', { exact: true }).selectOption('vehiculo');
  await dialog.getByRole('button', { name: 'Crear marca' }).click();
  await expect(screen.getByRole('status')).toHaveText('Marca BAIC creada.');

  await screen.getByRole('button', { name: 'Editar marca KAYI' }).click();
  dialog = page.getByRole('dialog', { name: 'Marca · KAYI' });
  await dialog.getByLabel('Nombre de la marca').fill('kaiyi');
  await dialog.getByLabel('Buscar familia').fill('che');
  await dialog.getByRole('button', { name: 'CHERY' }).click();
  await expect(dialog.getByText('CHERY', { exact: true })).toBeVisible();
  await dialog.getByRole('button', { name: 'Guardar marca' }).click();
  await expect(screen.getByRole('status')).toHaveText('Marca renombrada a KAIYI: sus códigos la siguen y KAYI queda como alias.');
  expect(mock.writes.at(-1)).toBe('PATCH brands/4 {"nombre":"KAIYI","familia":2}');
  await expect(screen.getByRole('row', { name: /^KAIYI/ })).toContainText('De la familia CHERY');

  await screen.getByRole('button', { name: 'Editar marca TOYOTA' }).click();
  dialog = page.getByRole('dialog', { name: 'Marca · TOYOTA' });
  await dialog.getByLabel('Nuevo alias').fill('toyta');
  await dialog.getByRole('button', { name: 'Agregar alias' }).click();
  await expect(dialog.getByRole('status')).toHaveText('Alias TOYTA agregado. 7 códigos pasaron a TOYOTA. 1 caso de unificación abierto por códigos repetidos.');
  await dialog.getByRole('button', { name: 'Quitar alias TOY', exact: true }).click();
  await expect(dialog.getByRole('status')).toHaveText('Alias TOY quitado.');
  expect(mock.writes.slice(-2)).toEqual(['POST brands/1/aliases {"alias":"TOYTA"}', 'DELETE brands/1/aliases/11']);
  await dialog.getByRole('button', { name: 'Cerrar ventana' }).click();
  // the aliases saved in the editor show in the list once it closes
  await expect(screen.getByRole('row', { name: /^TOYOTA/ }).locator('.brand-aliases')).toHaveText('TOYTA');

  await screen.getByRole('button', { name: 'Editar marca GMB' }).click();
  dialog = page.getByRole('dialog', { name: 'Marca · GMB' });
  await expect(dialog.getByRole('button', { name: /^Unir GMB/ })).toBeDisabled();
  await dialog.getByLabel('Buscar marca que queda').fill('toy');
  await dialog.getByRole('button', { name: /^TOYOTA/ }).click();
  await dialog.getByRole('button', { name: 'Unir GMB con TOYOTA' }).click();
  // a merge removes the brand: it asks first, and Cancelar writes nothing
  const confirm = dialog.getByRole('group', { name: 'Confirmar unión' });
  await expect(confirm).toContainText('¿Unir GMB con TOYOTA? GMB desaparece y sus 990 referencias pasan a TOYOTA.');
  const writes = mock.writes.length;
  await confirm.getByRole('button', { name: 'Cancelar' }).click();
  expect(mock.writes.length).toBe(writes);
  await dialog.getByRole('button', { name: 'Unir GMB con TOYOTA' }).click();
  await confirm.getByRole('button', { name: 'Confirmar unión' }).click();
  await expect(screen.getByRole('status')).toHaveText('GMB se unió a TOYOTA: sus códigos y alias pasaron a TOYOTA.');
  expect(mock.writes.at(-1)).toBe('PATCH brands/5 {"merge_into":1}');
  await expect(screen.getByRole('row', { name: /^GMB/ })).toHaveCount(0);
  expect(mock.unexpected).toEqual([]);
});
