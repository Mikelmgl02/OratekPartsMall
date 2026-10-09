import { expect, Page, test } from '@playwright/test';
import type { ManagedPart } from '../lib/types';

const empty = { count: 0, next: null, previous: null, results: [], pending_count: 0 };
const ids = { disc: '0f200000-0000-4000-8000-000000000001', pump: '0f200000-0000-4000-8000-000000000002' };
const row = (id: string, sku: string, description: string): ManagedPart => ({ id, sku, name: sku, description, category: 'FRENOS', subcategory: 'DISCOS DE FRENO',
  part_type: null, is_OEM: false, active: true, codes: [], stock_record_count: 1, merged_count: 0, images: [],
  identity: { ref_type: 'unknown', brand: '', status: 'needs_oem' } } as ManagedPart);

// The catalog list and what the SKU editor reads when it opens (part types, suffixes, the SKU's OEM numbers).
async function fixture(page: Page) {
  test.skip(!process.env.E2E_ADMIN_TOKEN, 'A disposable superuser token is required for server-rendered administration.');
  await page.context().addCookies([{ name: 'partsmall_session', value: process.env.E2E_ADMIN_TOKEN!, url: 'http://localhost:8080', httpOnly: true, sameSite: 'Strict' }]);
  const rows = [row(ids.disc, 'DF-4512', 'DISCO FRENO DEL TOY LAND CRUISER'), row(ids.pump, 'BOMBA-9', 'BOMBA AGUA TOY 22R')];
  const unexpected: string[] = [];
  let lists = 0;
  await page.route('**/api/**', route => { unexpected.push(`${route.request().method()} ${new URL(route.request().url()).pathname}`); return route.fulfill({ status: 404, json: { detail: 'Ruta no prevista' } }); });
  await page.route('**/api/market/analytics/events', route => route.fulfill({ status: 202, json: { recorded: true } }));
  await page.route(/\/api\/management\/(roles|catalog\/import\/issues|part-types)(?:\?.*)?$/, route => route.fulfill({ json: route.request().url().includes('/roles') ? [] : empty }));
  await page.route('**/api/management/catalog/company-suffixes', route => route.fulfill({ json: { suffixes: [] } }));
  await page.route(/\/api\/management\/catalog\/[a-f0-9-]{36}\/oem-equivalents$/, route => route.fulfill({ json: { numbers: [], cross_references: [] } }));
  await page.route(/\/api\/management\/catalog(?:\?.*)?$/, route => { lists += 1; return route.fulfill({ json: { count: rows.length, next: null, previous: null, results: rows } }); });
  return { unexpected, lists: () => lists };
}

const skuCell = (page: Page, id: string) => page.locator(`.ag-row[row-id="${id}"] .ag-cell[col-id="sku"]`);

test('the SKU code opens the editor in a side panel over the grid, which stays as it was', async ({ page }) => {
  const mock = await fixture(page);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto('/administracion?seccion=inventario');
  await expect(skuCell(page, ids.disc)).toContainText('DF-4512');
  const lists = mock.lists();
  await skuCell(page, ids.disc).getByRole('button', { name: 'DF-4512' }).click();
  const editor = page.getByRole('dialog', { name: 'Editar SKU · DF-4512' });
  await expect(editor).toBeVisible();
  await expect(editor).toHaveClass(/drawer/);
  const box = (await editor.boundingBox())!;
  expect(Math.round(box.x + box.width)).toBe(1440);  // docked to the right edge
  await page.screenshot({ path: '/tmp/motionpartes-sku-editor-panel.png' });
  await editor.getByRole('button', { name: 'Cerrar ventana' }).click();
  await expect(editor).toHaveCount(0);
  expect(mock.lists()).toBe(lists);  // the grid never reloaded: same rows, same scroll position

  await skuCell(page, ids.pump).locator('.sku-identity').dblclick();  // a double click anywhere in the cell opens it too
  await expect(page.getByRole('dialog', { name: 'Editar SKU · BOMBA-9' })).toBeVisible();
  await page.keyboard.press('Escape');
  await skuCell(page, ids.disc).getByRole('button', { name: 'DF-4512' }).dblclick();  // its second click lands on the backdrop
  await expect(editor).toBeVisible();
  expect(mock.unexpected).toEqual([]);
});

test('on a phone the SKU editor panel takes the screen width', async ({ page }) => {
  await fixture(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/administracion?seccion=inventario');
  await skuCell(page, ids.disc).getByRole('button', { name: 'DF-4512' }).click();
  const editor = page.getByRole('dialog', { name: 'Editar SKU · DF-4512' });
  await expect(editor).toBeVisible();
  const box = (await editor.boundingBox())!;
  expect(box.x).toBeGreaterThanOrEqual(0);
  expect(box.x + box.width).toBeLessThanOrEqual(390);
});
