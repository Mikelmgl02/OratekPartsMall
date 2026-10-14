import { expect, Page, test } from '@playwright/test';
import type { ManagedPart } from '../lib/types';
import type { SplitPlan } from '../lib/catalog-split-types';
import { noNewProducts } from './new-products-route';

const empty = { count: 0, next: null, previous: null, results: [], pending_count: 0 };
const ids = { parent: '0f000000-0000-4000-8000-000000000001', disc: '0f000000-0000-4000-8000-000000000002', oem: '0f000000-0000-4000-8000-000000000003',
  pump: '0f000000-0000-4000-8000-000000000004' };
const row = (id: string, sku: string, description: string, extra: Partial<ManagedPart> = {}): ManagedPart => ({ id, sku, name: sku, description, category: 'FRENOS',
  subcategory: 'DISCOS DE FRENO', part_type: null, is_OEM: false, active: true, codes: [], stock_record_count: 1, merged_count: 0, images: [],
  identity: { ref_type: 'unknown', brand: '' }, ...extra } as ManagedPart);
const disc = 'DISCO FRENO DEL TOY LAND CRUISER';

// The catalog list (with its search), a split plan for the grouped SKU and the merge, which refuses when told to.
async function fixture(page: Page, { refuse = false } = {}) {
  test.skip(!process.env.E2E_ADMIN_TOKEN, 'A disposable superuser token is required for server-rendered administration.');
  await page.context().addCookies([{ name: 'partsmall_session', value: process.env.E2E_ADMIN_TOKEN!, url: 'http://localhost:8080', httpOnly: true, sameSite: 'Strict' }]);
  const rows: Record<string, ManagedPart> = {
    [ids.parent]: row(ids.parent, '58411-08000', 'TAMBOR KIA PICANTO 04-11', { merged_count: 3 }),
    [ids.disc]: row(ids.disc, 'DF-4512', disc),
    [ids.oem]: row(ids.oem, '43512-60150', disc, { is_OEM: true, identity: { ref_type: 'oem', brand: 'TOYOTA' } }),
    [ids.pump]: row(ids.pump, 'BOMBA-9', 'BOMBA AGUA TOY 22R', { active: false }),
  };
  const unexpected: string[] = [], merges: unknown[] = [];
  await page.route('**/api/**', route => { if (noNewProducts(route)) return; unexpected.push(`${route.request().method()} ${new URL(route.request().url()).pathname}`); return route.fulfill({ status: 404, json: { detail: 'Ruta no prevista' } }); });
  await page.route('**/api/market/analytics/events', route => route.fulfill({ status: 202, json: { recorded: true } }));
  await page.route(/\/api\/management\/(roles|catalog\/import\/issues)(?:\?.*)?$/, route => route.fulfill({ json: route.request().url().includes('/roles') ? [] : empty }));
  await page.route(/\/api\/management\/catalog(?:\?.*)?$/, route => {
    const params = new URL(route.request().url()).searchParams, wanted = params.get('ids'), search = (params.get('search') || '').toUpperCase();
    const results = wanted ? wanted.split(',').map(id => rows[id]).filter(Boolean) : Object.values(rows).filter(part => !search || `${part.sku} ${part.description}`.includes(search));
    return route.fulfill({ json: { count: results.length, next: null, previous: null, results } });
  });
  await page.route(`**/api/management/catalog/${ids.parent}/split`, route => route.fulfill({ json: {
    target: { ...rows[ids.parent], created_by_merge: true }, children: [], codes: [], items: [], images: [], warnings: [] } as unknown as SplitPlan }));
  await page.route('**/api/management/catalog/grouping/merge', route => {
    const body = route.request().postDataJSON() as { target_sku: string; source_skus: string[] };
    merges.push(body);
    if (refuse) return route.fulfill({ status: 400, json: { detail: 'Los SKU de origen tienen fichas técnicas o aplicaciones. Revisa y conserva esos datos en el SKU principal antes de vaciar las fichas de origen y unificarlos.' } });
    return route.fulfill({ json: { target: rows[ids.oem], merged_skus: body.source_skus, already_merged_skus: [], summary: {}, merge_id: '0e000000-0000-4000-8000-000000000009', retry: false } });
  });
  return { unexpected, merges };
}

const cell = (page: Page, id: string, column = 'description') => page.locator(`.ag-center-cols-container .ag-row[row-id="${id}"] .ag-cell[col-id="${column}"]`);
const option = (page: Page, text: string) => page.locator('.ag-menu-option', { hasText: text });

test('the right click offers to group a SKU or split the SKUs grouped into it, before the grid\'s own items', async ({ page }) => {
  const mock = await fixture(page);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto('/administracion?seccion=inventario');
  await cell(page, ids.parent).click({ button: 'right' });
  await expect(option(page, 'Agrupar 58411-08000 con…')).toBeVisible();
  await expect(option(page, 'Copiar').first()).toBeVisible();
  await expect(page.locator('.ag-menu-option', { hasText: /OEM/ })).toHaveCount(0);  // no Marcar como OEM / Crear OEM desde SKU / Quitar marca OEM
  await option(page, 'Separar SKU agrupados (3)').click();
  const dialog = page.getByRole('dialog', { name: 'Separar SKU' });
  await expect(dialog.getByRole('region', { name: 'Separación de 58411-08000' })).toBeVisible();
  await dialog.getByRole('button', { name: 'Cerrar ventana' }).click();
  await cell(page, ids.disc).click({ button: 'right' });
  await expect(option(page, 'Separar SKU agrupados')).toHaveClass(/ag-menu-option-disabled/);  // nothing grouped into it
  await page.keyboard.press('Escape');
  expect(mock.unexpected).toEqual([]);
});

test('Agrupar con… adds a SKU by search, suggests the OEM as principal and merges into the one chosen', async ({ page }) => {
  const mock = await fixture(page);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto('/administracion?seccion=inventario');
  await cell(page, ids.disc).click({ button: 'right' });
  await option(page, 'Agrupar DF-4512 con…').click();
  const dialog = page.getByRole('dialog', { name: 'Agrupar SKU' });
  await expect(dialog.getByRole('button', { name: 'Agrega al menos otro SKU' })).toBeDisabled();
  await dialog.getByLabel('Buscar SKU para agrupar').fill('bomba');
  await expect(dialog.getByRole('button', { name: 'Agregar BOMBA-9' })).toBeDisabled();  // inactive
  await dialog.getByLabel('Buscar SKU para agrupar').fill('43512');
  await dialog.getByRole('button', { name: 'Agregar 43512-60150' }).click();
  await expect(dialog.getByText('43512-60150 es OEM: normalmente el SKU OEM queda como principal.')).toBeVisible();
  await dialog.getByLabel('43512-60150 como SKU principal').check();
  await expect(dialog.getByText('DF-4512 deja de aparecer separado: sus alternos, existencias e imágenes pasan a 43512-60150.')).toBeVisible();
  const confirm = dialog.getByRole('button', { name: 'Agrupar 1 SKU en 43512-60150' });
  await expect(confirm).toBeDisabled();
  await dialog.getByLabel('Revisé que son el mismo repuesto.').check();
  await page.screenshot({ path: '/tmp/motionpartes-catalog-merge.png' });
  await confirm.click();
  await expect(dialog).toHaveCount(0);
  await expect(page.getByRole('status').filter({ hasText: '1 SKU agrupado en 43512-60150: DF-4512. Si fue un error, Separar SKU lo deshace.' })).toBeVisible();
  expect(mock.merges).toEqual([{ target_sku: '43512-60150', source_skus: ['DF-4512'] }]);
  expect(mock.unexpected).toEqual([]);
});

test('rows selected together merge from the right click, and a refused merge says why', async ({ page }) => {
  const mock = await fixture(page, { refuse: true });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto('/administracion?seccion=inventario');
  await cell(page, ids.disc).click();
  await cell(page, ids.oem).click({ modifiers: ['Shift'] });
  await cell(page, ids.oem).click({ button: 'right' });
  await option(page, 'Agrupar 2 SKU seleccionados…').click();
  const dialog = page.getByRole('dialog', { name: 'Agrupar SKU' });
  await expect(dialog.getByLabel('43512-60150 como SKU principal')).toBeChecked();  // the OEM, by default
  await dialog.getByLabel('Revisé que son el mismo repuesto.').check();
  await dialog.getByRole('button', { name: 'Agrupar 1 SKU en 43512-60150' }).click();
  await expect(dialog.getByRole('alert')).toContainText('fichas técnicas o aplicaciones');
  await expect(dialog).toBeVisible();
  expect(mock.merges).toEqual([{ target_sku: '43512-60150', source_skus: ['DF-4512'] }]);
  expect(mock.unexpected).toEqual([]);
});

test('the merge dialog fits a phone screen', async ({ page }) => {
  await fixture(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/administracion?seccion=inventario');
  await cell(page, ids.disc, 'sku').click({ button: 'right' });
  await option(page, 'Agrupar DF-4512 con…').click();
  const dialog = page.getByRole('dialog', { name: 'Agrupar SKU' });
  await dialog.getByLabel('Buscar SKU para agrupar').fill('43512');
  await dialog.getByRole('button', { name: 'Agregar 43512-60150' }).click();
  expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)).toBe(false);
  const box = (await dialog.boundingBox())!;
  expect(box.x + box.width).toBeLessThanOrEqual(390);
});
