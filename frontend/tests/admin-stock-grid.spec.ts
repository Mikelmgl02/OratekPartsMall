import { expect, Page, test } from '@playwright/test';
import type { ManagedStockItem } from '../lib/types';
import { noNewProducts } from './new-products-route';

// Existencias por proveedor (S6): an item on a line that is not publicada (a Productos nuevos line in preparación) says so, and a code
// change waiting for review shows under the code the item still sells under. The backend is mocked in the browser.
const empty = { count: 0, next: null, previous: null, results: [], pending_count: 0 };
const supplier = '0a000000-0000-4000-8000-000000000001';
const id = (n: number) => `0b000000-0000-4000-8000-${String(n).padStart(12, '0')}`;
const stock = (n: number, extra: Partial<ManagedStockItem>): ManagedStockItem => ({ id: id(n), supplier_invent_id: `SUP-${n}`, part: null, codigo: '', brand: '',
  description: '', references: [], matching_status: 'pending', source: 'upload', reported_quantity: 5, reserved_quantity: 0, available_quantity: 5,
  updated_at: '2026-10-14T12:00:00Z', supplier, supplier_name: 'REPUESTOS DEL ISTMO', part_name: null, part_sku: null, line_estado: null, reference: null,
  codigo_pendiente: null, brand_pendiente: null, ...extra });
const rows = [
  stock(1, { codigo: '58411-1R000', brand: 'HYUNDAI', part: id(101), part_sku: '58411-1R000', part_name: '58411-1R000', line_estado: 'publicada', matching_status: 'matched', reference: 11 }),
  stock(2, { codigo: 'EC01', brand: '', part: id(102), part_sku: 'EC01', part_name: 'EC01', line_estado: 'preparacion', reference: 12 }),
  stock(3, { codigo: '43512-60150', brand: 'TOYOTA', part: id(103), part_sku: '43512-60150', part_name: '43512-60150', line_estado: 'publicada', matching_status: 'review', reference: 13,
    codigo_pendiente: '43512-60151', brand_pendiente: null }),
  stock(4, { codigo: 'SIN-LINEA', brand: 'GMB' }),
];

async function fixture(page: Page) {
  test.skip(!process.env.E2E_ADMIN_TOKEN, 'A disposable superuser token is required for server-rendered administration.');
  await page.context().addCookies([{ name: 'partsmall_session', value: process.env.E2E_ADMIN_TOKEN!, url: 'http://localhost:8080', httpOnly: true, sameSite: 'Strict' }]);
  const unexpected: string[] = [];
  await page.route('**/api/**', route => { if (noNewProducts(route)) return; unexpected.push(`${route.request().method()} ${new URL(route.request().url()).pathname}`); return route.fulfill({ status: 404, json: { detail: 'Ruta no prevista' } }); });
  await page.route('**/api/market/analytics/events', route => route.fulfill({ status: 202, json: { recorded: true } }));
  await page.route(/\/api\/management\/(roles|catalog|catalog\/import\/issues)(?:\?.*)?$/, route => route.fulfill({ json: route.request().url().includes('/roles') ? [] : empty }));
  await page.route(/\/api\/management\/inventory(?:\?.*)?$/, route => route.fulfill({ json: { count: rows.length, next: null, previous: null, results: rows } }));
  return { unexpected };
}
const cell = (page: Page, n: number, column: string) => page.locator(`.ag-center-cols-container .ag-row[row-id="${id(n)}"] .ag-cell[col-id="${column}"]`);

test('Existencias por proveedor marks an item on a line in preparación and a code change waiting for review', async ({ page }) => {
  const mock = await fixture(page);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto('/administracion?seccion=inventario');
  await page.getByRole('button', { name: 'Existencias por proveedor', exact: true }).click();
  await expect(cell(page, 2, 'part')).toContainText('EC01');
  await expect(cell(page, 2, 'part').getByText('EN PREPARACIÓN', { exact: true })).toBeVisible();
  for (const n of [1, 3, 4]) await expect(cell(page, n, 'part').getByText('EN PREPARACIÓN', { exact: true })).toHaveCount(0);
  await expect(cell(page, 4, 'part')).toHaveText('Sin vincular');
  const pending = cell(page, 3, 'codigo').getByText('EN REVISIÓN: 43512-60151', { exact: true });
  await expect(pending).toBeVisible();
  await expect(pending).toHaveAttribute('title', 'Código nuevo en revisión (Casos de coincidencia): 43512-60151');
  await expect(cell(page, 1, 'codigo')).toHaveText(/^58411-1R000\s*HYUNDAI$/);  // no change waiting: the brand, as before
  await expect(cell(page, 3, 'match')).toContainText('Por revisar');
  await page.screenshot({ path: '/tmp/motionpartes-admin-stock-grid.png' });
  expect(mock.unexpected).toEqual([]);
});
