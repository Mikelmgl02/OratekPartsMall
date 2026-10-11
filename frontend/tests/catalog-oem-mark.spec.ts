import { expect, Page, test } from '@playwright/test';
import type { ManagedPart } from '../lib/types';
import { noNewProducts } from './new-products-route';

const empty = { count: 0, next: null, previous: null, results: [], pending_count: 0 };
const ids = { disc: '0f100000-0000-4000-8000-000000000001', pump: '0f100000-0000-4000-8000-000000000002', wordy: '0f100000-0000-4000-8000-000000000003' };
const row = (id: string, sku: string, description: string, oem = false): ManagedPart => ({ id, sku, name: sku, description, category: '', subcategory: '', part_type: null,
  is_OEM: oem, active: true, codes: [], stock_record_count: 1, merged_count: 0, images: [],
  identity: { ref_type: oem ? 'oem' : 'unknown', brand: oem ? 'TOYOTA' : '', status: oem ? 'oem' : 'needs_oem' } } as ManagedPart);

// Mirrors the catalog API: the plan names the make a description gives and blocks a code without digits; marking and unmarking flip
// the flag (the alterno and the library entry are the server's business).
async function fixture(page: Page) {
  test.skip(!process.env.E2E_ADMIN_TOKEN, 'A disposable superuser token is required for server-rendered administration.');
  await page.context().addCookies([{ name: 'partsmall_session', value: process.env.E2E_ADMIN_TOKEN!, url: 'http://localhost:8080', httpOnly: true, sameSite: 'Strict' }]);
  const rows: Record<string, ManagedPart> = { [ids.disc]: row(ids.disc, '43512-60150', 'DISCO FRENO DEL TOY LAND CRUISER'),
    [ids.wordy]: row(ids.wordy, 'BOMBA-AGUA', 'BOMBA AGUA'), [ids.pump]: row(ids.pump, '16100-29155', 'BOMBA AGUA TOY 22R', true) };
  const unexpected: string[] = [], marks: unknown[] = [], unmarks: unknown[] = [];
  let lists = 0;
  await page.route('**/api/**', route => { if (noNewProducts(route)) return; unexpected.push(`${route.request().method()} ${new URL(route.request().url()).pathname}`); return route.fulfill({ status: 404, json: { detail: 'Ruta no prevista' } }); });
  await page.route('**/api/market/analytics/events', route => route.fulfill({ status: 202, json: { recorded: true } }));
  await page.route(/\/api\/management\/(roles|catalog\/import\/issues)(?:\?.*)?$/, route => route.fulfill({ json: route.request().url().includes('/roles') ? [] : empty }));
  await page.route(/\/api\/management\/catalog(?:\?.*)?$/, route => {
    lists += 1;
    const wanted = new URL(route.request().url()).searchParams.get('ids');
    const results = wanted ? wanted.split(',').map(id => rows[id]).filter(Boolean) : Object.values(rows);
    return route.fulfill({ json: { count: results.length, next: null, previous: null, results } });
  });
  await page.route(/\/api\/management\/catalog\/mark-oem(?:\?.*)?$/, route => {
    if (route.request().method() === 'POST') {
      const body = route.request().postDataJSON() as { rows: { id: string }[] };
      marks.push(body);
      for (const entry of body.rows) rows[entry.id] = { ...rows[entry.id], is_OEM: true, identity: { ref_type: 'oem', brand: 'TOYOTA', status: 'oem' } };
      return route.fulfill({ json: { updated: body.rows.map(entry => rows[entry.id]) } });
    }
    const wanted = new URL(route.request().url()).searchParams.get('ids')!.split(',');
    return route.fulfill({ json: { manufacturers: ['NISSAN', 'TOYOTA'], rows: wanted.map(id => rows[id]).map(part => ({ id: part.id, sku: part.sku, description: part.description,
      is_OEM: part.is_OEM, makes: part.description.includes('TOY') ? ['TOYOTA'] : [], suggested: part.description.includes('TOY') ? 'TOYOTA' : '', warnings: [],
      blocked: /\d/.test(part.sku) ? '' : `${part.sku} no parece un número OEM: necesita dígitos y hasta 60 letras o dígitos.`,
      marked_alterno: part.is_OEM, oem_brands: part.is_OEM ? ['TOYOTA'] : [] })) } });
  });
  await page.route('**/api/management/catalog/unmark-oem', route => {
    const body = route.request().postDataJSON() as { rows: { id: string }[] };
    unmarks.push(body);
    for (const entry of body.rows) rows[entry.id] = { ...rows[entry.id], is_OEM: false, identity: { ref_type: 'unknown', brand: '', status: 'needs_oem' } };
    return route.fulfill({ json: { updated: body.rows.map(entry => rows[entry.id]) } });
  });
  return { unexpected, marks, unmarks, lists: () => lists };
}

// Any row container: the SKU column is pinned to the left on a wide screen.
const cell = (page: Page, id: string, column = 'description') => page.locator(`.ag-row[row-id="${id}"] .ag-cell[col-id="${column}"]`);
const option = (page: Page, text: string) => page.locator('.ag-menu-option', { hasText: text });

test('Marcar como OEM from the right click names the make the description gives and files the SKU as MAIN OEM', async ({ page }) => {
  const mock = await fixture(page);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto('/administracion?seccion=inventario');
  await cell(page, ids.disc).click({ button: 'right' });
  await option(page, 'Marcar como OEM…').click();
  const dialog = page.getByRole('dialog', { name: 'Marcar como OEM' });
  await expect(dialog.getByLabel('Fabricante de 43512-60150')).toHaveValue('TOYOTA');
  await dialog.getByLabel('Fuente (opcional)').fill('EPC 2026 pág. 4');
  const confirm = dialog.getByRole('button', { name: 'Marcar 1 SKU como OEM' });
  await expect(confirm).toBeDisabled();
  await dialog.getByLabel('Revisé que cada código es el número OEM de su fabricante.').check();
  const before = mock.lists();
  await confirm.click();
  await expect(dialog).toHaveCount(0);
  await expect(page.getByRole('status').filter({ hasText: '1 SKU marcado como OEM: TOYOTA 43512-60150. Ya están en la Biblioteca OEM.' })).toBeVisible();
  expect(mock.marks).toEqual([{ rows: [{ id: ids.disc, manufacturer: 'TOYOTA', citation: 'EPC 2026 pág. 4' }] }]);
  await expect(cell(page, ids.disc, 'sku')).toContainText('MAIN OEM');  // the grid read its rows again
  expect(mock.lists()).toBeGreaterThan(before);
  expect(mock.unexpected).toEqual([]);
});

test('a code without digits cannot be marked, and a SKU already MAIN OEM offers to take the mark off', async ({ page }) => {
  const mock = await fixture(page);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto('/administracion?seccion=inventario');
  await cell(page, ids.disc).click();
  await cell(page, ids.wordy).click({ modifiers: ['Shift'] });
  await cell(page, ids.wordy).click({ button: 'right' });
  await option(page, 'Marcar 2 SKU como OEM…').click();
  const dialog = page.getByRole('dialog', { name: 'Marcar como OEM' });
  await expect(dialog.getByText('BOMBA-AGUA no parece un número OEM')).toBeVisible();
  await expect(dialog.getByLabel('Marcar BOMBA-AGUA')).toBeDisabled();
  await expect(dialog.getByLabel('Marcar 43512-60150')).toBeChecked();
  await dialog.getByRole('button', { name: 'Cerrar ventana' }).click();

  await cell(page, ids.pump).click();  // a new selection: just this row
  await cell(page, ids.pump).click({ button: 'right' });
  await expect(option(page, 'Marcar como OEM…')).toHaveClass(/ag-menu-option-disabled/);
  await option(page, 'Quitar marca OEM…').click();
  const unmark = page.getByRole('dialog', { name: 'Quitar marca OEM' });
  await expect(unmark.getByLabel('Retirar también el alterno OEM TOYOTA 16100-29155')).toBeChecked();  // it was filed by a marking
  await unmark.getByLabel('Revisé qué SKU dejan de ser MAIN OEM.').check();
  await unmark.getByRole('button', { name: 'Quitar marca OEM a 1 SKU' }).click();
  await expect(page.getByRole('status').filter({ hasText: '1 SKU ya no es MAIN OEM: 16100-29155.' })).toBeVisible();
  expect(mock.unmarks).toEqual([{ rows: [{ id: ids.pump, retire_number: true }] }]);
  expect(mock.marks).toEqual([]);
  expect(mock.unexpected).toEqual([]);
});
