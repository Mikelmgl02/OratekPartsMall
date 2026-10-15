import { expect, Page, test } from '@playwright/test';
import type { ManagedPart } from '../lib/types';
import type { MergedSku, SplitPlan } from '../lib/catalog-split-types';
import { noNewProducts } from './new-products-route';
import { routeLinesFrom } from './lines-route';

const empty = { count: 0, next: null, previous: null, results: [], pending_count: 0 };
const ids = { parent: '0d000000-0000-4000-8000-000000000001', k: '0d000000-0000-4000-8000-000000000002', np: '0d000000-0000-4000-8000-000000000003',
  raz: '0d000000-0000-4000-8000-000000000004', other: '0d000000-0000-4000-8000-000000000005' };
const items = { k: 'aa000000-0000-4000-8000-000000000001', np: 'aa000000-0000-4000-8000-000000000002' };
const description = 'TAMBOR KIA PICANTO 04-11';
const row = (id: string, sku: string, merged = 0): ManagedPart => ({ id, sku, name: sku, description, category: 'FRENOS', subcategory: 'TAMBORES DE FRENO', part_type: null,
  is_OEM: false, active: true, codes: [], stock_record_count: 1, merged_count: merged, images: [], identity: { ref_type: 'unknown', brand: '' } } as ManagedPart);
const child = (id: string, sku: string, extra: Partial<SplitPlan['children'][number]>) => ({ id, sku, name: sku, description, category: 'FRENOS', subcategory: 'TAMBORES DE FRENO',
  is_OEM: false, active: false, merged_at: '2026-10-07T08:30:00Z', merged_by: 'motionpartes-matching-service', automatic: true, method: 'Mismo código base y la misma descripción', codes: [], ...extra });
const plan: SplitPlan = {
  target: { ...row(ids.parent, '58411-08000'), created_by_merge: true } as unknown as SplitPlan['target'],
  children: [child(ids.k, '58411-08000-K', { codes: [{ brand: 'KOREA', code: 'KX-100' }] }), child(ids.np, '58411-08000-NP', {}),
    child(ids.raz, '58411-08000-RAZ', { automatic: false, merged_by: 'miguel', method: 'Agrupar SKU', codes: null })],
  codes: [
    { id: 11, brand: 'KOREA', code: 'KX-100', ref_type: 'company', reference_source: '', suggested: ids.k, alias_of: '', alias_returns: false },
    { id: 12, brand: '', code: '58411-08000-K', ref_type: 'unknown', reference_source: '', suggested: ids.k, alias_of: ids.k, alias_returns: false },
    { id: 13, brand: '', code: '58411-08000-NP', ref_type: 'unknown', reference_source: '', suggested: ids.np, alias_of: ids.np, alias_returns: false },
    { id: 14, brand: 'GMB', code: 'GT-77', ref_type: 'company', reference_source: '', suggested: '', alias_of: '', alias_returns: false },
  ],
  items: [
    { id: items.k, supplier_name: 'GRUPO RAZOR', supplier_invent_id: 'INV-1', codigo: '58411-08000-K', brand: '', description, available_quantity: 5, suggested: ids.k },
    { id: items.np, supplier_name: 'GRUPO RAZOR', supplier_invent_id: 'INV-2', codigo: '58411-08000-NP', brand: '', description, available_quantity: 2, suggested: ids.np },
  ],
  images: [],
  warnings: ['58411-08000 es un SKU padre que se creó al agrupar: si separas todos sus SKU y no le queda nada, quedará inactivo.'],
};
const merged: MergedSku = { id: ids.parent, sku: '58411-08000', description, active: true, merged_count: 3, last_merged_at: '2026-10-07T08:30:00Z', automatic: true,
  merged: plan.children.map(({ sku }) => ({ sku, description })) };

// Mirrors Separar SKU's API: the list of SKUs holding grouped SKUs, a SKU's plan and the split, which brings the K SKU back.
async function fixture(page: Page) {
  test.skip(!process.env.E2E_ADMIN_TOKEN, 'A disposable superuser token is required for server-rendered administration.');
  await page.context().addCookies([{ name: 'partsmall_session', value: process.env.E2E_ADMIN_TOKEN!, url: 'http://localhost:8080', httpOnly: true, sameSite: 'Strict' }]);
  const rows: Record<string, ManagedPart> = { [ids.parent]: row(ids.parent, '58411-08000', 3), [ids.other]: row(ids.other, 'OTRO-1') };
  const unexpected: string[] = [], splits: unknown[] = [], lists: string[] = [];
  await page.route('**/api/**', route => { if (noNewProducts(route)) return; unexpected.push(`${route.request().method()} ${new URL(route.request().url()).pathname}`); return route.fulfill({ status: 404, json: { detail: 'Ruta no prevista' } }); });
  await page.route('**/api/market/analytics/events', route => route.fulfill({ status: 202, json: { recorded: true } }));
  await routeLinesFrom(page, () => Object.values(rows));  // the Inventario grid reads OEM lines (S8)
  await page.route(/\/api\/management\/(roles|catalog\/import\/issues)(?:\?.*)?$/, route => route.fulfill({ json: route.request().url().includes('/roles') ? [] : empty }));
  await page.route(/\/api\/management\/catalog(?:\?.*)?$/, route => {
    const wanted = new URL(route.request().url()).searchParams.get('ids');
    const results = wanted ? wanted.split(',').map(id => rows[id]).filter(Boolean) : Object.values(rows);
    return route.fulfill({ json: { count: results.length, next: null, previous: null, results } });
  });
  await page.route(/\/api\/management\/catalog\/merged(?:\?.*)?$/, route => { lists.push(new URL(route.request().url()).search); return route.fulfill({ json: { count: 1, next: null, previous: null, results: [merged] } }); });
  await page.route(`**/api/management/catalog/${ids.parent}/split`, route => {
    if (route.request().method() === 'GET') return route.fulfill({ json: plan });
    splits.push(route.request().postDataJSON());
    rows[ids.parent] = { ...rows[ids.parent], merged_count: 2 };
    rows[ids.k] = row(ids.k, '58411-08000-K');
    return route.fulfill({ json: { split_id: '0e000000-0000-4000-8000-000000000001', target: rows[ids.parent], restored: [rows[ids.k]],
      summary: { restored_skus: 1, codes_moved: 2, aliases_removed: 1, items_moved: 1, images_moved: 0, links_moved: 0, links_shared: 0, target_retired: false } } });
  });
  return { unexpected, splits, lists };
}

async function chooseK(page: Page) {
  const dialog = page.getByRole('dialog', { name: 'Separar SKU' });
  await expect(dialog.getByText('58411-08000 es un SKU padre que se creó al agrupar')).toBeVisible();
  await expect(dialog.getByText('No quedó registro de sus alternos al agruparse.')).toBeVisible();  // RAZ, grouped by hand
  await dialog.getByLabel('Separar 58411-08000-K').check();
  return dialog;
}

test('Separar SKU lists the grouped SKUs, suggests where each row goes and brings the chosen SKU back', async ({ page }) => {
  const mock = await fixture(page);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto('/administracion?seccion=inventario');
  await page.getByRole('button', { name: 'Separar SKU', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Separar SKU' });
  await expect(dialog.getByRole('heading', { name: '58411-08000' })).toBeVisible();
  const day = await page.evaluate(() => new Date('2026-10-07T08:30:00Z').toLocaleDateString('es-PA', { timeZone: 'America/Panama', dateStyle: 'medium' }));
  await expect(dialog.getByText(`3 SKU agrupados · ${day} · Agrupación automática`)).toBeVisible();
  await dialog.getByLabel('Agrupado por').selectOption('automatic');
  await expect.poll(() => mock.lists.at(-1)).toContain('origin=automatic');
  await dialog.getByRole('button', { name: 'Revisar 58411-08000' }).click();
  await chooseK(page);

  // The evidence decides the defaults: KX-100 and the K stock row came with K; GT-77 and NP's row stay.
  await expect(dialog.getByLabel('Destino del alterno KX-100')).toHaveValue(ids.k);
  await expect(dialog.getByLabel('Destino del alterno GT-77')).toHaveValue('');
  await expect(dialog.getByText('Es el código de 58411-08000-K: se retira, el SKU ya lo identifica.')).toBeVisible();
  await expect(dialog.getByText('Es el código de 58411-08000-NP, que sigue agrupado: se queda.')).toBeVisible();
  // A supplier's stock follows its Reference (S8): its destination is shown, not chosen.
  await expect(dialog.getByLabel('Destino de GRUPO RAZOR 58411-08000-K')).toHaveText('Pasa a 58411-08000-K');
  await expect(dialog.getByLabel('Destino de GRUPO RAZOR 58411-08000-NP')).toHaveText('Se queda en 58411-08000');
  await expect(dialog.getByRole('combobox', { name: /^Destino de GRUPO RAZOR/ })).toHaveCount(0);
  await dialog.getByLabel('Destino del alterno GT-77').selectOption({ label: 'Pasa a 58411-08000-K' });
  await expect(dialog.getByText('58411-08000-K vuelve a estar activo con 2 alternos, 1 existencia y 0 imágenes; lo demás se queda en 58411-08000.')).toBeVisible();
  const confirm = dialog.getByRole('button', { name: 'Separar 1 SKU' });
  await expect(confirm).toBeDisabled();
  await dialog.getByLabel('Revisé a qué SKU va cada alterno, existencia e imagen.').check();
  await page.screenshot({ path: '/tmp/motionpartes-catalog-split.png', fullPage: true });
  await confirm.click();

  await expect(dialog.getByRole('status').filter({ hasText: '1 SKU separado de 58411-08000: 58411-08000-K.' })).toBeVisible();  // back on the list, for the next one
  await expect(dialog.getByLabel('Agrupado por')).toHaveValue('automatic');  // where it was
  expect(mock.splits).toEqual([{ restore: [{ id: ids.k, codes: [11, 14], items: [items.k], images: [] }] }]);
  await expect.poll(() => mock.lists.length).toBeGreaterThan(2);  // the list reads again
  expect(mock.unexpected).toEqual([]);
});

test('the badge on a grouped SKU opens its split, which closes when done', async ({ page }) => {
  const mock = await fixture(page);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto('/administracion?seccion=inventario');
  await page.getByRole('button', { name: '3 SKU agrupados en 58411-08000: separar' }).click();
  const dialog = await chooseK(page);
  await expect(dialog.getByRole('button', { name: 'Volver a la lista' })).toHaveCount(0);
  await dialog.getByLabel('Revisé a qué SKU va cada alterno, existencia e imagen.').check();
  await dialog.getByRole('button', { name: 'Separar 1 SKU' }).click();
  await expect(dialog).toHaveCount(0);
  await expect(page.getByRole('status').filter({ hasText: '1 SKU separado de 58411-08000' })).toBeVisible();
  await expect(page.getByRole('button', { name: '2 SKU agrupados en 58411-08000: separar' })).toBeVisible();  // the grid read the rows again
  expect(mock.splits).toEqual([{ restore: [{ id: ids.k, codes: [11], items: [items.k], images: [] }] }]);
  expect(mock.lists).toEqual([]);
  expect(mock.unexpected).toEqual([]);
});

test('the split dialog fits a phone screen', async ({ page }) => {
  await fixture(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/administracion?seccion=inventario');
  await page.getByRole('button', { name: '3 SKU agrupados en 58411-08000: separar' }).click();
  await chooseK(page);
  await expect(page.getByLabel('Destino del alterno KX-100')).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)).toBe(false);
  const dialog = (await page.getByRole('dialog', { name: 'Separar SKU' }).boundingBox())!;
  expect(dialog.x + dialog.width).toBeLessThanOrEqual(390);
});
