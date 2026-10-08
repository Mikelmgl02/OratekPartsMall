import { expect, Page, Route, test } from '@playwright/test';
import { readFile } from 'node:fs/promises';
import type { PriceImportResult, PriceList, PriceRow } from '../lib/pricing-types';
import { excelFixture } from './excel-fixture';

const supplierId = '22222222-2222-4222-8222-222222222222';
const jobId = '33333333-3333-4333-8333-333333333333';
const itemA = '66666666-6666-4666-8666-666666666661';
const general = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1';
const nivel = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2';
const stamp = '2026-10-03T12:00:00Z';
const empty = { count: 0, next: null, previous: null, results: [] };
const base = `/api/market/accounts/${supplierId}`;
const excelType = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet';
const fixture = excelFixture([['PRECIOS-ERP', 'ARCHIVO DE PRUEBA']]);

function priceList(id: string, code: string, isDefault: boolean, priced: number): PriceList {
  return { id, code, name: code, currency: 'USD', is_default: isDefault, active: true, version: 1, priced_count: priced, missing_count: 1501 - priced, created_at: stamp, updated_at: stamp };
}
function row(prices: PriceRow['prices']): PriceRow {
  return { supplier_item_id: itemA, supplier_invent_id: 'A-001', codigo: '58411-1R000-G', brand: 'KSM', description: 'TAMBOR', matching_status: 'matched', discount_group: '',
    floor_price: null, item_pricing_revision: null, prices, updated_at: null };
}
function importJob(overrides: Partial<PriceImportResult> = {}): PriceImportResult {
  const counts = { new_prices: 1500, increases: 1, decreases: 0, unchanged: 3, removed: 0 };
  return {
    job_id: jobId, supplier_id: supplierId, filename: 'precios-erp.xlsx', status: 'ready', expires_at: '2099-10-01T12:00:00Z', valid: true, imported: false,
    summary: { ...counts, source_rows: 1502, valid_rows: 1501, rejected_rows: 1, big_changes: 1, line_updates: 2, floor_updates: 0,
      lists: { GENERAL: { ...counts, new_prices: 0 }, NIVEL_A: { ...counts, increases: 0, unchanged: 0 } } },
    applied_summary: { created: 0, updated: 0, removed: 0, unchanged: 0, floor_updates: 0, line_updates: 0 },
    id_column: 'ID_INVENTARIO_PROVEEDOR', code_column: 'CODIGO', columns: ['ID_INVENTARIO_PROVEEDOR', 'CODIGO', 'PRECIO', 'PRECIO_NIVEL_A'],
    lists: [{ header: 'PRECIO', code: 'GENERAL', new: false, archived: false, skipped: false }, { header: 'PRECIO_NIVEL_A', code: 'NIVEL_A', new: true, archived: false, skipped: false }],
    lists_to_create: [{ code: 'NIVEL_A', name: 'NIVEL_A', currency: 'USD', is_default: false, header: 'PRECIO_NIVEL_A' }], ignored_columns: ['COSTO', 'UBICACION'],
    preview: [
      { row: 2, supplier_invent_id: 'A-001', codigo: '58411-1R000-G', description: 'TAMBOR', kind: 'list_price', list: 'GENERAL', current: '10.00', new: '16.00',
        change_percent: '60.0', action: 'update', big_change: true },
      { row: 2, supplier_invent_id: 'A-001', codigo: '58411-1R000-G', description: 'TAMBOR', kind: 'list_price', list: 'NIVEL_A', current: null, new: '12.50',
        change_percent: null, action: 'create', big_change: false },
      { row: 3, supplier_invent_id: 'A-002', codigo: '58411-1R000-KSM', description: 'TAMBOR', kind: 'discount_group', list: null, current: null, new: 'FRENOS',
        change_percent: null, action: 'create', big_change: false },
    ], preview_count: 1503,
    errors: [{ row: 9, column: 'ID_INVENTARIO_PROVEEDOR', message: 'Este ID no existe en tu inventario. Carga primero sus existencias.' }], error_count: 1,
    warnings: [{ row: 2, column: 'PRECIO', message: 'Variación mayor al 50 % (+60.0 %): 10.00 → 16.00.' }], warning_count: 1,
    requires: { acknowledge_big_changes: true, confirm_new_lists: true },
    progress: { batch_size: 1000, total_batches: 2, completed_batches: 0, total_rows: 1501, processed_rows: 0, next_batch: 0 },
    ...overrides,
  };
}
async function session(page: Page) {
  const unexpected: string[] = [];
  await page.route('**/api/**', route => { unexpected.push(`${route.request().method()} ${new URL(route.request().url()).pathname}`); return route.fulfill({ status: 404, json: { detail: 'Ruta no prevista' } }); });
  await page.route('**/api/session', route => route.fulfill({ json: { authenticated: true, user: { id: 801, username: 'EMPLEADO', first_name: '', last_name: '', is_superuser: false },
    accounts: { ...empty, count: 1, results: [{ id: supplierId, name: 'REPUESTOS CENTRAL', roles: ['supplier_retail'], capabilities: ['supplier'], permission: 'staff' }] } } }));
  await page.route('**/api/market/analytics/events', route => route.fulfill({ status: 202, json: { recorded: true } }));
  await page.route('**/api/market/wishlist/state', route => route.fulfill({ json: { part_ids: [], count: 0 } }));
  await page.route(/\/api\/market\/catalog(?:\?.*)?$/, route => route.fulfill({ json: empty }));
  return unexpected;
}
async function openImport(page: Page) {
  await page.goto(`/?vista=proveedor&seccion=precios&cuenta=${supplierId}`);
  const section = page.getByRole('region', { name: 'Precios del proveedor', exact: true });
  await section.getByRole('button', { name: 'Importar precios', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Importar precios desde Excel', exact: true });
  await expect(dialog).toBeVisible();
  return { section, dialog };
}
async function download(page: Page, link: ReturnType<Page['getByRole']>) {
  // Chrome's eager download requests bypass page routing; the mocked attachment still drives a real download through this URL.
  await link.evaluate(element => element.removeAttribute('download'));
  const pending = page.waitForEvent('download');
  await link.click();
  return pending;
}

test('suppliers review an ERP price file, confirm big changes and new lists, apply it in batches across a reload and download its errors', async ({ page }) => {
  const unexpected = await session(page);
  let current = importJob(), lists = [priceList(general, 'GENERAL', true, 1)], loseNextResponse = true;
  const uploads: string[] = [], batches: Record<string, unknown>[] = [], downloads: string[] = [], gridReads: number[] = [];
  // Downloads arrive as page navigations once the link loses its download attribute; a context route also covers eager browser downloads.
  const attachment = async (route: Route) => {
    const filename = route.request().url().endsWith('/errors') ? 'errores-precios.xlsx' : 'plantilla-precios.xlsx';
    downloads.push(filename);
    await route.fulfill({ body: fixture, contentType: excelType, headers: { 'Content-Disposition': `attachment; filename="${filename}"` } });
  };
  const downloadPaths = new RegExp(`${base}/prices/import/(?:template|jobs/${jobId}/errors)$`);
  await page.context().route(downloadPaths, attachment);
  await page.route(downloadPaths, attachment);
  await page.route(`${base}/price-lists`, route => route.fulfill({ json: { can_configure: true, item_count: 1501, results: lists } }));
  await page.route(new RegExp(`${base}/prices(?:\\?.*)?$`), route => { gridReads.push(Date.now()); return route.fulfill({ json: { ...empty, count: 1, results: [row({ GENERAL: { unit_price: current.imported ? '16.00' : '10.00', revision: 1, updated_at: stamp } })] } }); });
  await page.route(`${base}/prices/import`, async route => {
    uploads.push(route.request().headers()['content-type'] || '');
    await route.fulfill({ json: current });
  });
  await page.route(`${base}/prices/import/jobs/${jobId}`, async route => {
    if (route.request().method() === 'POST') {
      const body = route.request().postDataJSON();
      batches.push(body);
      const index = body.batch_index as number;
      current = { ...current, status: index === 0 ? 'importing' : 'completed', imported: index === 1, requires: { acknowledge_big_changes: false, confirm_new_lists: false },
        applied_summary: index === 0 ? { created: 999, updated: 1, removed: 0, unchanged: 0, floor_updates: 0, line_updates: 2 } : { created: 1500, updated: 1, removed: 0, unchanged: 3, floor_updates: 0, line_updates: 2 },
        progress: { ...current.progress, completed_batches: index + 1, processed_rows: index === 0 ? 1000 : 1501, next_batch: index === 0 ? 1 : null } };
      if (index === 0) lists = [priceList(general, 'GENERAL', true, 1), priceList(nivel, 'NIVEL_A', false, 999)];
      // The first batch is saved but its response never arrives: the dialog must recover the durable progress.
      if (loseNextResponse) { loseNextResponse = false; await route.abort('failed'); return; }
    }
    await route.fulfill({ json: current });
  });
  const { section, dialog } = await openImport(page);
  const template = await download(page, dialog.getByRole('link', { name: 'Descargar plantilla', exact: true }));
  expect(template.suggestedFilename()).toBe('plantilla-precios.xlsx');
  expect(await readFile((await template.path())!)).toEqual(fixture);
  await dialog.getByLabel('Archivo Excel de precios', { exact: true }).setInputFiles({ name: 'precios-erp.xlsx', mimeType: excelType, buffer: fixture });
  await dialog.getByRole('button', { name: 'Revisar archivo', exact: true }).click();
  await expect(dialog.getByRole('heading', { name: 'Revisa los cambios', exact: true })).toBeVisible();
  expect(uploads).toHaveLength(1);
  expect(uploads[0]).toContain('multipart/form-data');
  const tiles = dialog.locator('.price-import-summary');
  await expect(tiles).toContainText('1,500Precios nuevos');
  await expect(tiles).toContainText('1Cambios grandes');
  await expect(tiles).toContainText('1Errores');
  await expect(dialog.locator('.price-import-notices')).toContainText('Se creará la lista NIVEL_A (USD).');
  await expect(dialog.locator('.price-import-notices')).toContainText('Columnas ignoradas: COSTO, UBICACION.');
  await expect(dialog.locator('.price-import-notices')).toContainText('También cambian 2 líneas y 0 precios mínimos.');
  const big = dialog.getByRole('row').filter({ hasText: 'Lista GENERAL' });
  await expect(big).toContainText('+60.0 %');
  await expect(big).toContainText('Cambio grande');
  await expect(dialog.getByRole('row').filter({ hasText: 'Línea' })).toContainText('FRENOS');
  await expect(dialog.getByText('Se muestran los primeros 3 de 1,503 cambios, empezando por los cambios grandes.')).toBeVisible();
  await dialog.evaluate(element => { element.scrollTop = 0; });
  await page.screenshot({ path: '/tmp/motionpartes-price-import-desktop.png' });
  // Applying needs both explicit confirmations.
  const apply = dialog.getByRole('button', { name: 'Aplicar precios válidos', exact: true });
  await expect(apply).toBeDisabled();
  await dialog.getByLabel('Revisé los cambios mayores al 50 %', { exact: true }).check();
  await expect(apply).toBeDisabled();
  await dialog.getByLabel('Crear las listas nuevas', { exact: true }).check();
  await apply.click();
  await expect(dialog.getByRole('alert')).toContainText('Se perdió la conexión');
  expect(batches).toEqual([{ batch_index: 0, acknowledge_big_changes: true, confirm_new_lists: true }]);
  await page.reload();
  await section.getByRole('button', { name: 'Importar precios', exact: true }).click();
  await expect(dialog.getByText(/1 de 2 lotes/)).toBeVisible();
  await expect(dialog.getByLabel('Crear las listas nuevas', { exact: true })).toHaveCount(0);
  await dialog.getByRole('button', { name: 'Reanudar importación', exact: true }).click();
  await expect(dialog.getByRole('heading', { name: 'Importación completada', exact: true })).toBeVisible();
  await expect(dialog.getByRole('status').filter({ hasText: 'Se aplicaron 1,500 precios nuevos, 1 actualizados y 0 eliminados.' })).toBeVisible();
  expect(batches).toEqual([{ batch_index: 0, acknowledge_big_changes: true, confirm_new_lists: true }, { batch_index: 1, acknowledge_big_changes: false, confirm_new_lists: false }]);
  const errors = await download(page, dialog.getByRole('link', { name: 'Descargar filas con errores', exact: true }));
  expect(errors.suggestedFilename()).toBe('errores-precios.xlsx');
  const reads = gridReads.length;
  await dialog.getByRole('button', { name: 'Cerrar', exact: true }).click();
  await expect(dialog).toHaveCount(0);
  await expect(section.getByRole('status').filter({ hasText: 'Importación de precios completada: 1500 nuevos, 1 actualizados y 0 eliminados. 1 filas pendientes' })).toBeVisible();
  await expect(section.locator('.price-list-table .ag-center-cols-container [role="row"]')).toHaveCount(2);
  await expect(section.locator('.price-list-table')).toContainText('NIVEL_A');
  await expect.poll(() => gridReads.length).toBeGreaterThan(reads);
  await expect(section.locator(`.price-grid .ag-row[row-id="${itemA}"] .ag-cell[col-id="price:GENERAL"]`)).toContainText('16.00');
  expect(downloads).toEqual(['plantilla-precios.xlsx', 'errores-precios.xlsx']);
  expect(unexpected).toEqual([]);
});

test('a price file changed since its review shows the conflict on a phone and asks for a fresh review', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const unexpected = await session(page);
  const current = importJob({ summary: { ...importJob().summary, rejected_rows: 0 }, errors: [], error_count: 0, lists_to_create: [],
    requires: { acknowledge_big_changes: true, confirm_new_lists: false }, progress: { ...importJob().progress, total_batches: 1, total_rows: 2 } });
  const secondJob = '44444444-4444-4444-8444-444444444444';
  let reviews = 0, attempts = 0;
  await page.route(`${base}/price-lists`, route => route.fulfill({ json: { can_configure: true, item_count: 1, results: [priceList(general, 'GENERAL', true, 1)] } }));
  await page.route(new RegExp(`${base}/prices(?:\\?.*)?$`), route => route.fulfill({ json: { ...empty, count: 1, results: [row({})] } }));
  // A fresh review is a new job: its big changes must be confirmed again.
  await page.route(`${base}/prices/import`, route => { reviews++; return route.fulfill({ json: reviews === 1 ? current : { ...current, job_id: secondJob } }); });
  await page.route(`${base}/prices/import/jobs/${jobId}`, route => {
    if (route.request().method() === 'POST') { attempts++; return route.fulfill({ status: 409, json: { detail: 'Los precios cambiaron desde la revisión; vuelve a subir el archivo.' } }); }
    return route.fulfill({ json: current });
  });
  const { dialog } = await openImport(page);
  await dialog.getByLabel('Archivo Excel de precios', { exact: true }).setInputFiles({ name: 'precios-erp.xlsx', mimeType: excelType, buffer: fixture });
  await dialog.getByRole('button', { name: 'Revisar archivo', exact: true }).click();
  const apply = dialog.getByRole('button', { name: 'Aplicar precios', exact: true });
  const reviewed = dialog.getByLabel('Revisé los cambios mayores al 50 %', { exact: true });
  await expect(apply).toBeDisabled();
  await reviewed.check();
  await expect(apply).toBeEnabled();
  expect(await dialog.evaluate(element => element.scrollWidth > element.clientWidth)).toBe(false);
  await page.screenshot({ path: '/tmp/motionpartes-price-import-mobile.png', fullPage: false });
  await apply.click();
  await expect(dialog.getByRole('alert')).toContainText('Los precios cambiaron desde la revisión; vuelve a subir el archivo.');
  await expect(dialog.getByRole('alert')).toContainText('Selecciona el archivo y revísalo de nuevo.');
  await expect(dialog.getByRole('button', { name: 'Reanudar importación', exact: true })).toBeDisabled();
  expect(attempts).toBe(1);
  await dialog.getByLabel('Archivo Excel de precios', { exact: true }).setInputFiles({ name: 'precios-erp.xlsx', mimeType: excelType, buffer: fixture });
  await dialog.getByRole('button', { name: 'Revisar archivo', exact: true }).click();
  await expect(reviewed).not.toBeChecked();
  await expect(apply).toBeDisabled();
  await reviewed.check();
  await expect(apply).toBeEnabled();
  expect(attempts).toBe(1);
  expect(reviews).toBe(2);
  expect(unexpected).toEqual([]);
});
