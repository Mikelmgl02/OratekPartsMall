import { expect, Page, test } from '@playwright/test';
import type { ManagedPart, PartReference } from '../lib/types';

const empty = { count: 0, next: null, previous: null, results: [], pending_count: 0 };
const ids = { pump: '0c000000-0000-4000-8000-000000000001', disc: '0c000000-0000-4000-8000-000000000002' };
const part = (id: string, sku: string, description: string, codes: PartReference[] = []): ManagedPart => ({ id, sku, name: sku, description, category: '', subcategory: '',
  part_type: null, is_OEM: false, active: true, codes, stock_record_count: 2, images: [], identity: { ref_type: 'unknown', brand: '', status: 'needs_oem' } } as unknown as ManagedPart);

// Mirrors the catalog API: the batch saves every row or none (the disc's subgroup is refused: it has technical data), alternos are
// created and retired one by one, and ?ids= re-reads rows.
async function fixture(page: Page) {
  test.skip(!process.env.E2E_ADMIN_TOKEN, 'A disposable superuser token is required for server-rendered administration.');
  await page.context().addCookies([{ name: 'partsmall_session', value: process.env.E2E_ADMIN_TOKEN!, url: 'http://localhost:8080', httpOnly: true, sameSite: 'Strict' }]);
  const rows: Record<string, ManagedPart> = { [ids.pump]: part(ids.pump, 'BOMBA-1', 'BOMBA AGUA', [{ id: 7, brand: 'GMB', code: 'GWT-41A', ref_type: 'company' }]),
    [ids.disc]: part(ids.disc, 'DISCO-1', 'DISCO FRENO DEL') };
  let nextCode = 100;
  const unexpected: string[] = [], batches: Record<string, unknown>[][] = [], alternates: string[] = [], rereads: string[] = [];
  await page.route('**/api/**', route => { unexpected.push(`${route.request().method()} ${new URL(route.request().url()).pathname}`); return route.fulfill({ status: 404, json: { detail: 'Ruta no prevista' } }); });
  await page.route('**/api/market/analytics/events', route => route.fulfill({ status: 202, json: { recorded: true } }));
  await page.route(/\/api\/management\/(roles|catalog\/import\/issues)(?:\?.*)?$/, route => route.fulfill({ json: route.request().url().includes('/roles') ? [] : empty }));
  await page.route(/\/api\/management\/catalog(?:\?.*)?$/, route => {
    const wanted = new URL(route.request().url()).searchParams.get('ids');
    if (wanted) rereads.push(wanted);
    const results = wanted ? wanted.split(',').map(id => rows[id]).filter(Boolean) : Object.values(rows);
    return route.fulfill({ json: { count: results.length, next: null, previous: null, results } });
  });
  await page.route('**/api/management/catalog/taxonomy', route => route.fulfill({ json: { results: [{ category: 'FRENOS', subcategory: 'DISCOS DE FRENO' }, { category: 'REFRIGERACIÓN', subcategory: 'BOMBAS DE AGUA' }] } }));
  await page.route('**/api/management/catalog/bulk-edit', route => {
    const batch = (route.request().postDataJSON() as { rows: (Record<string, unknown> & { id: string })[] }).rows;
    batches.push(batch);
    if (batch.some(row => row.id === ids.disc && 'subcategory' in row)) return route.fulfill({ status: 400, json: { detail: 'No se guardó ningún cambio: DISCO-1: Este SKU tiene datos técnicos.', errors: [] } });
    for (const row of batch) rows[row.id] = { ...rows[row.id], ...Object.fromEntries(Object.entries(row).filter(([key]) => key !== 'id').map(([key, value]) => [key, typeof value === 'string' ? value.toUpperCase() : value])) };
    return route.fulfill({ json: { updated: batch.map(row => rows[row.id]) } });
  });
  await page.route(/\/api\/management\/alternates(?:\/\d+)?$/, route => {
    const method = route.request().method();
    if (method === 'POST') {
      const body = route.request().postDataJSON() as { part: string; code: string; brand: string; ref_type: PartReference['ref_type'] };
      alternates.push(`POST ${body.code}`);
      const saved = { id: ++nextCode, code: body.code.toUpperCase(), brand: body.brand.toUpperCase(), ref_type: body.ref_type };
      rows[body.part] = { ...rows[body.part], codes: [...rows[body.part].codes, saved] };
      return route.fulfill({ status: 201, json: { ...saved, part: body.part } });
    }
    const id = Number(new URL(route.request().url()).pathname.split('/').pop());
    alternates.push(`DELETE ${id}`);
    for (const key of Object.keys(rows)) rows[key] = { ...rows[key], codes: rows[key].codes.filter(code => code.id !== id) };
    return route.fulfill({ status: 204, body: '' });
  });
  return { rows, unexpected, batches, alternates, rereads };
}

const cell = (page: Page, id: string, column: string) => page.locator(`.ag-center-cols-container .ag-row[row-id="${id}"] .ag-cell[col-id="${column}"]`);

async function paste(page: Page, text: string, id: string, column: string) {
  await page.evaluate(value => navigator.clipboard.writeText(value), text);
  await cell(page, id, column).click();
  await page.keyboard.press('ControlOrMeta+V');
}

test('edits and pastes save in batches, flash what the server kept and put back every cell of a refused batch', async ({ page, context }) => {
  await context.grantPermissions(['clipboard-read', 'clipboard-write']);
  const mock = await fixture(page);
  await page.setViewportSize({ width: 1600, height: 1000 });
  await page.goto('/administracion?seccion=inventario');
  await expect(cell(page, ids.pump, 'description')).toHaveText('BOMBA AGUA');
  await cell(page, ids.pump, 'description').dblclick();
  await expect(page.locator('.ag-cell-inline-editing')).toHaveCount(0);  // read-only until Editar celdas
  await page.getByRole('button', { name: 'Editar celdas', exact: true }).click();
  await expect(page.getByText(/se guardan juntos al soltar/)).toBeVisible();
  await expect(cell(page, ids.pump, 'description')).toHaveClass(/editable-cell-active/);

  // Two descriptions pasted from Excel: one request for both rows, which come back as the server keeps them.
  await paste(page, 'bomba agua toy 22r\ndisco freno del toy', ids.pump, 'description');
  await expect(page.getByRole('status').filter({ hasText: '2 SKU guardados.' })).toBeVisible();
  await expect(cell(page, ids.disc, 'description')).toHaveText('DISCO FRENO DEL TOY');
  await expect(cell(page, ids.pump, 'description')).toHaveText('BOMBA AGUA TOY 22R');
  expect(mock.batches).toEqual([[{ id: ids.pump, description: 'bomba agua toy 22r' }, { id: ids.disc, description: 'disco freno del toy' }]]);

  // The status and subgroup editors: an edit does not flash, the saved cell does.
  await cell(page, ids.pump, 'active').click();
  await page.keyboard.press('Enter');
  await page.getByRole('option', { name: 'INACTIVO', exact: true }).click();
  await expect(cell(page, ids.pump, 'active')).toHaveText('Inactivo');
  await expect(cell(page, ids.pump, 'active')).toHaveClass(/ag-cell-data-changed/);
  await cell(page, ids.pump, 'subgroup').click();
  await page.keyboard.press('Enter');
  await page.keyboard.type('bombas');
  await page.getByRole('option', { name: 'REFRIGERACIÓN / BOMBAS DE AGUA' }).click();
  await expect(cell(page, ids.pump, 'subgroup')).toHaveClass(/ag-cell-data-changed/);
  expect(mock.batches.slice(1)).toEqual([[{ id: ids.pump, active: false }], [{ id: ids.pump, category: 'REFRIGERACIÓN', subcategory: 'BOMBAS DE AGUA' }]]);

  // A block pasted over status and subgroup of both rows: the disc's subgroup is refused, so neither row keeps anything.
  await paste(page, 'ACTIVO\tREFRIGERACIÓN / BOMBAS DE AGUA\nINACTIVO\tFRENOS / DISCOS DE FRENO', ids.pump, 'active');
  await expect(page.getByRole('alert').filter({ hasText: 'DISCO-1' })).toContainText('No se guardó ningún cambio');
  await expect(cell(page, ids.pump, 'active')).toHaveText('Inactivo');
  await expect(cell(page, ids.disc, 'active')).toHaveText('Activo');
  await expect(cell(page, ids.disc, 'subgroup')).toHaveText('—');
  expect(mock.batches[3]).toEqual([{ id: ids.pump, active: true }, { id: ids.disc, active: false, category: 'FRENOS', subcategory: 'DISCOS DE FRENO' }]);
  await page.screenshot({ path: '/tmp/motionpartes-inline-catalog-edit.png' });

  await page.getByRole('button', { name: 'Editando celdas', exact: true }).click();
  await cell(page, ids.disc, 'description').dblclick();
  await expect(page.locator('.ag-cell-inline-editing')).toHaveCount(0);
  expect(mock.batches).toHaveLength(4);
  expect(mock.unexpected).toEqual([]);
});

test('the Alternos popup reads and copies without Editar celdas, adds and retires codes with it, and takes a paste', async ({ page, context }) => {
  await context.grantPermissions(['clipboard-read', 'clipboard-write']);
  const mock = await fixture(page);
  await page.setViewportSize({ width: 1600, height: 1000 });
  await page.goto('/administracion?seccion=inventario');
  await paste(page, 'ZZ-1', ids.pump, 'codes');  // ignored: read-only
  await cell(page, ids.pump, 'codes').dblclick();
  const popup = page.getByRole('dialog', { name: 'Alternos de BOMBA-1' });
  await expect(popup).toContainText('Solo lectura · clic en un código para copiarlo');
  await popup.getByRole('button', { name: /^GWT-41A/ }).click();
  await expect(popup.getByRole('status')).toHaveText('Copiado: GWT-41A');
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe('GWT-41A');
  await expect(popup.getByLabel('Nuevo código alterno')).toHaveCount(0);
  await expect(popup.getByRole('button', { name: 'Retirar alterno GWT-41A' })).toHaveCount(0);
  await page.keyboard.press('Escape');
  await expect(popup).toHaveCount(0);

  await page.getByRole('button', { name: 'Editar celdas', exact: true }).click();
  await expect(page.getByText(/se guardan juntos al soltar/)).toBeVisible();
  await cell(page, ids.pump, 'codes').dblclick();
  await popup.getByLabel('Nuevo código alterno').fill('wpt-111');
  await page.keyboard.press('Tab');  // through the popup's fields, not to the next cell
  await expect(popup.getByLabel('Marca del código')).toBeFocused();
  await page.keyboard.type('aisin');
  await page.keyboard.press('Enter');  // Enter adds, the popup stays open
  await expect(popup.getByRole('button', { name: /^WPT-111/ })).toBeVisible();
  await expect(popup.getByLabel('Nuevo código alterno')).toHaveValue('');
  await popup.getByRole('button', { name: 'Retirar alterno GWT-41A', exact: true }).click();
  await expect(popup.getByRole('button', { name: /^GWT-41A/ })).toHaveCount(0);
  await popup.getByRole('button', { name: 'Cerrar alternos', exact: true }).click();
  await expect(popup).toHaveCount(0);
  await expect(cell(page, ids.pump, 'codes')).toContainText('WPT-111');  // the row was re-read when the popup closed
  await expect(cell(page, ids.pump, 'codes')).not.toContainText('GWT-41A');
  await expect(cell(page, ids.pump, 'codes')).toHaveClass(/ag-cell-data-changed/);
  expect(mock.alternates).toEqual(['POST WPT-111', 'DELETE 7']);
  expect(mock.rereads).toContain(ids.pump);

  // Pasted codes: the ones the SKU already has are skipped, each new one is saved.
  await paste(page, 'ab-1, ab-2; WPT-111', ids.pump, 'codes');
  await expect(page.getByRole('status').filter({ hasText: 'BOMBA-1: 2 alternos agregados.' })).toBeVisible();
  await expect(cell(page, ids.pump, 'codes')).toContainText('AB-1');
  expect(mock.alternates.slice(2)).toEqual(['POST AB-1', 'POST AB-2']);
  expect(mock.unexpected).toEqual([]);
});

test('changes made elsewhere reach the rows on screen and flash', async ({ page }) => {
  await page.clock.install();
  const mock = await fixture(page);
  await page.setViewportSize({ width: 1600, height: 1000 });
  await page.goto('/administracion?seccion=inventario');
  await expect(cell(page, ids.pump, 'stock')).toHaveText('2');
  mock.rows[ids.pump] = { ...mock.rows[ids.pump], stock_record_count: 5, description: 'BOMBA AGUA TOY 2KD' };  // another user, a supplier feed
  await page.clock.fastForward('00:16');
  await expect(cell(page, ids.pump, 'stock')).toHaveText('5');
  await expect(cell(page, ids.pump, 'description')).toHaveText('BOMBA AGUA TOY 2KD');
  await expect(cell(page, ids.pump, 'stock')).toHaveClass(/ag-cell-data-changed/);
  await expect(cell(page, ids.disc, 'stock')).not.toHaveClass(/ag-cell-data-changed/);  // unchanged rows stay still
});

test('the edit toggle and the Alternos popup fit a phone screen', async ({ page }) => {
  await fixture(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/administracion?seccion=inventario');
  await page.getByRole('button', { name: 'Editar celdas', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Editando celdas', exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)).toBe(false);
  await cell(page, ids.pump, 'codes').dblclick();
  const popup = page.getByRole('dialog', { name: 'Alternos de BOMBA-1' });
  await expect(popup.getByRole('button', { name: 'Agregar' })).toBeVisible();
  const grid = (await page.getByRole('region', { name: 'Inventario interno' }).boundingBox())!, box = (await popup.boundingBox())!;
  expect(box.x).toBeGreaterThanOrEqual(grid.x);  // a grid popup is clipped by the grid: it must fit inside it
  expect(box.x + box.width).toBeLessThanOrEqual(grid.x + grid.width);
});
