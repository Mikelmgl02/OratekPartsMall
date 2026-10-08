import { expect, Page, test } from '@playwright/test';
import type { ManagedPart } from '../lib/types';

const empty = { count: 0, next: null, previous: null, results: [], pending_count: 0 };
const part = (id: string, sku: string, description: string): ManagedPart => ({ id, sku, name: sku, description, category: '', subcategory: '', part_type: null,
  is_OEM: false, active: true, codes: [], stock_record_count: 2, images: [], identity: { ref_type: 'unknown', brand: '', status: 'needs_oem' } } as unknown as ManagedPart);
const ids = { pump: '0c000000-0000-4000-8000-000000000001', disc: '0c000000-0000-4000-8000-000000000002' };

// Mirrors the catalog PATCH: uppercase text, the subgroup refused for the disc (it has technical data).
async function fixture(page: Page) {
  test.skip(!process.env.E2E_ADMIN_TOKEN, 'A disposable superuser token is required for server-rendered administration.');
  await page.context().addCookies([{ name: 'partsmall_session', value: process.env.E2E_ADMIN_TOKEN!, url: 'http://localhost:8080', httpOnly: true, sameSite: 'Strict' }]);
  const rows: Record<string, ManagedPart> = { [ids.pump]: part(ids.pump, 'BOMBA-1', 'BOMBA AGUA'), [ids.disc]: part(ids.disc, 'DISCO-1', 'DISCO FRENO DEL') };
  const unexpected: string[] = [], patches: { id: string; body: Record<string, unknown> }[] = [];
  await page.route('**/api/**', route => { unexpected.push(`${route.request().method()} ${new URL(route.request().url()).pathname}`); return route.fulfill({ status: 404, json: { detail: 'Ruta no prevista' } }); });
  await page.route('**/api/market/analytics/events', route => route.fulfill({ status: 202, json: { recorded: true } }));
  await page.route(/\/api\/management\/(roles|catalog\/import\/issues)(?:\?.*)?$/, route => route.fulfill({ json: route.request().url().includes('/roles') ? [] : empty }));
  await page.route(/\/api\/management\/catalog(?:\?.*)?$/, route => route.fulfill({ json: { count: 2, next: null, previous: null, results: Object.values(rows) } }));
  await page.route('**/api/management/catalog/taxonomy', route => route.fulfill({ json: { results: [{ category: 'FRENOS', subcategory: 'DISCOS DE FRENO' }, { category: 'REFRIGERACIÓN', subcategory: 'BOMBAS DE AGUA' }] } }));
  await page.route(/\/api\/management\/catalog\/0c000000-[0-9a-f-]+$/, route => {
    const id = new URL(route.request().url()).pathname.split('/').pop()!, body = route.request().postDataJSON() as Record<string, unknown>;
    patches.push({ id, body });
    if (id === ids.disc && 'subcategory' in body) return route.fulfill({ status: 400, json: ['Este SKU tiene datos técnicos. Vacía su ficha técnica antes de cambiar el subgrupo para no asignar medidas a otra plantilla.'] });
    rows[id] = { ...rows[id], ...Object.fromEntries(Object.entries(body).filter(([key]) => key !== 'is_OEM').map(([key, value]) => [key, typeof value === 'string' ? value.toUpperCase() : value])) };
    return route.fulfill({ json: rows[id] });
  });
  return { unexpected, patches, rows };
}

const cell = (page: Page, id: string, column: string) => page.locator(`.ag-center-cols-container .ag-row[row-id="${id}"] .ag-cell[col-id="${column}"]`);

test('Editar celdas saves a description, a status and a subgroup in place and reverts what the server refuses', async ({ page }) => {
  const mock = await fixture(page);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto('/administracion?seccion=inventario');
  await expect(cell(page, ids.pump, 'description')).toHaveText('BOMBA AGUA');
  await cell(page, ids.pump, 'description').dblclick();
  await expect(page.locator('.ag-cell-inline-editing')).toHaveCount(0);  // read-only until Editar celdas
  const toggle = page.getByRole('button', { name: 'Editar celdas', exact: true });
  await toggle.click();
  await expect(page.getByRole('button', { name: 'Editando celdas', exact: true })).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByText(/Enter guarda, Esc cancela y Ctrl\+Z deshace/)).toBeVisible();

  await cell(page, ids.pump, 'description').click();
  await page.keyboard.type('bomba agua toy 22r');
  await page.keyboard.press('Enter');
  await expect(page.getByRole('status').filter({ hasText: 'BOMBA-1 · Descripción guardado.' })).toBeVisible();
  await expect(cell(page, ids.pump, 'description')).toHaveText('BOMBA AGUA TOY 22R');

  await cell(page, ids.pump, 'active').click();
  await page.keyboard.press('Enter');
  await page.getByRole('option', { name: 'INACTIVO', exact: true }).click();
  await expect(cell(page, ids.pump, 'active')).toHaveText('Inactivo');

  await cell(page, ids.pump, 'subgroup').click();
  await page.keyboard.press('Enter');
  await page.keyboard.type('bombas');
  await page.getByRole('option', { name: 'REFRIGERACIÓN / BOMBAS DE AGUA' }).click();
  await expect(cell(page, ids.pump, 'subgroup')).toHaveText('REFRIGERACIÓN / BOMBAS DE AGUA');

  await cell(page, ids.disc, 'subgroup').click();
  await page.keyboard.press('Enter');
  await page.getByRole('option', { name: 'FRENOS / DISCOS DE FRENO' }).click();
  await expect(page.getByRole('alert').filter({ hasText: 'DISCO-1' })).toContainText('DISCO-1: Este SKU tiene datos técnicos.');
  await expect(cell(page, ids.disc, 'subgroup')).toHaveText('—');  // reverted

  expect(mock.patches).toEqual([
    { id: ids.pump, body: { description: 'bomba agua toy 22r', is_OEM: false } },
    { id: ids.pump, body: { active: false, is_OEM: false } },
    { id: ids.pump, body: { category: 'REFRIGERACIÓN', subcategory: 'BOMBAS DE AGUA', is_OEM: false } },
    { id: ids.disc, body: { category: 'FRENOS', subcategory: 'DISCOS DE FRENO', is_OEM: false } },
  ]);
  await page.screenshot({ path: '/tmp/motionpartes-inline-catalog-edit.png' });
  await page.getByRole('button', { name: 'Editando celdas', exact: true }).click();
  await cell(page, ids.disc, 'description').dblclick();
  await expect(page.locator('.ag-cell-inline-editing')).toHaveCount(0);
  expect(mock.unexpected).toEqual([]);
});

test('the edit toggle fits a phone screen', async ({ page }) => {
  await fixture(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/administracion?seccion=inventario');
  await page.getByRole('button', { name: 'Editar celdas', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Editando celdas', exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)).toBe(false);
});
