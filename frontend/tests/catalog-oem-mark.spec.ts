import { expect, Page, test } from '@playwright/test';
import type { ManagedPart } from '../lib/types';
import { noNewProducts } from './new-products-route';

const empty = { count: 0, next: null, previous: null, results: [], pending_count: 0 };
const ids = { disc: '0f100000-0000-4000-8000-000000000001', pump: '0f100000-0000-4000-8000-000000000002', wordy: '0f100000-0000-4000-8000-000000000003',
  fan: '0f100000-0000-4000-8000-000000000004', filter: '0f100000-0000-4000-8000-000000000005', pads: '0f100000-0000-4000-8000-000000000006' };
const compact = (code: string) => code.replace(/[^0-9A-Z]/gi, '').toUpperCase();
const row = (id: string, sku: string, description: string, oem = false): ManagedPart => ({ id, sku, name: sku, description, category: '', subcategory: '', part_type: null,
  is_OEM: oem, active: true, codes: [], stock_record_count: 1, merged_count: 0, images: [],
  identity: { ref_type: oem ? 'oem' : 'unknown', brand: oem ? 'TOYOTA' : '', status: oem ? 'oem' : 'needs_oem' } } as ManagedPart);

// Mirrors the catalog API: the plan lists the OEM list's items with the SKU's code, names the make a description gives and blocks a code
// without digits or (to mark) not in the list; marking, creating and unmarking flip the flag (the alterno and the list are the server's).
async function fixture(page: Page) {
  test.skip(!process.env.E2E_ADMIN_TOKEN, 'A disposable superuser token is required for server-rendered administration.');
  await page.context().addCookies([{ name: 'partsmall_session', value: process.env.E2E_ADMIN_TOKEN!, url: 'http://localhost:8080', httpOnly: true, sameSite: 'Strict' }]);
  const rows: Record<string, ManagedPart> = { [ids.disc]: row(ids.disc, '43512-60150', 'DISCO FRENO DEL TOY LAND CRUISER'),
    [ids.wordy]: row(ids.wordy, 'BOMBA-AGUA', 'BOMBA AGUA'), [ids.pump]: row(ids.pump, '16100-29155', 'BOMBA AGUA TOY 22R', true),
    [ids.fan]: row(ids.fan, '21082-V5000', 'FAN CLUTCH NIS URVAN'), [ids.filter]: row(ids.filter, '90915-YZZD4', 'FILTRO ACEITE TOY HILUX'),
    [ids.pads]: row(ids.pads, '04465-35290', 'PASTILLA FRENO TOY', true) };
  // The OEM list: compact number → the manufacturers that file it.
  const library: Record<string, string[]> = { '4351260150': ['TOYOTA'], '90915YZZD4': ['LEXUS', 'TOYOTA'], '0446535290': ['TOYOTA'] };
  const unexpected: string[] = [], marks: unknown[] = [], creates: unknown[] = [], unmarks: unknown[] = [];
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
  await page.route(/\/api\/management\/catalog\/(mark|create)-oem(?:\?.*)?$/, route => {
    const create = route.request().url().includes('create-oem');
    if (route.request().method() === 'POST') {
      const body = route.request().postDataJSON() as { rows: { id: string; manufacturer: string }[] };
      (create ? creates : marks).push(body);
      for (const entry of body.rows) {
        if (create) library[compact(rows[entry.id].sku)] = [...(library[compact(rows[entry.id].sku)] ?? []), entry.manufacturer];
        rows[entry.id] = { ...rows[entry.id], is_OEM: true, identity: { ref_type: 'oem', brand: entry.manufacturer, status: 'oem' } };
      }
      return route.fulfill({ json: { updated: body.rows.map(entry => rows[entry.id]) } });
    }
    const wanted = new URL(route.request().url()).searchParams.get('ids')!.split(',');
    return route.fulfill({ json: { manufacturers: ['NISSAN', 'TOYOTA'], rows: wanted.map(id => rows[id]).map(part => {
      const make = part.description.includes('TOY') ? 'TOYOTA' : part.description.includes('NIS') ? 'NISSAN' : '';
      const listed = (library[compact(part.sku)] ?? []).map(maker => ({ id: `ref-${part.id}-${maker}`, manufacturer: maker, code: compact(part.sku), status: 'declared', status_label: 'Declarado', description: 'DISCO' }));
      const digits = /\d/.test(part.sku) ? '' : `${part.sku} no parece un número OEM: necesita dígitos y hasta 60 letras o dígitos.`;
      return { id: part.id, sku: part.sku, description: part.description, is_OEM: part.is_OEM, makes: make ? [make] : [],
        suggested: create ? make : (listed.find(item => item.manufacturer === make) ?? listed[0])?.manufacturer ?? '',
        listed, description_for_list: part.description, part_type_for_list: '', warnings: [],
        blocked: digits || (!create && !listed.length ? `${compact(part.sku)} no está en la lista OEM: usa «Crear OEM desde SKU».` : ''),
        marked_alterno: part.is_OEM, oem_brands: part.is_OEM ? ['TOYOTA'] : [] };
    }) } });
  });
  await page.route('**/api/management/catalog/unmark-oem', route => {
    const body = route.request().postDataJSON() as { rows: { id: string }[] };
    unmarks.push(body);
    for (const entry of body.rows) rows[entry.id] = { ...rows[entry.id], is_OEM: false, identity: { ref_type: 'unknown', brand: '', status: 'needs_oem' } };
    return route.fulfill({ json: { updated: body.rows.map(entry => rows[entry.id]) } });
  });
  return { unexpected, marks, creates, unmarks, lists: () => lists };
}

// Any row container: the SKU column is pinned to the left on a wide screen.
const cell = (page: Page, id: string, column = 'description') => page.locator(`.ag-row[row-id="${id}"] .ag-cell[col-id="${column}"]`);
const option = (page: Page, text: string) => page.locator('.ag-menu-option', { hasText: text });

test('Marcar como OEM from the right click ties the SKU to the item the OEM list has with its code', async ({ page }) => {
  const mock = await fixture(page);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto('/administracion?seccion=inventario');
  await cell(page, ids.disc).click({ button: 'right' });
  await option(page, 'Marcar como OEM…').click();
  const dialog = page.getByRole('dialog', { name: 'Marcar como OEM' });
  await expect(dialog.getByText('En la lista OEM: TOYOTA 4351260150 · Declarado · DISCO')).toBeVisible();
  await dialog.getByLabel('Fuente (opcional)').fill('EPC 2026 pág. 4');
  const confirm = dialog.getByRole('button', { name: 'Marcar 1 SKU como OEM' });
  await expect(confirm).toBeDisabled();
  await dialog.getByLabel('Revisé que cada código es el número OEM de su fabricante.').check();
  const before = mock.lists();
  await confirm.click();
  await expect(dialog).toHaveCount(0);
  await expect(page.getByRole('status').filter({ hasText: '1 SKU marcado como OEM: TOYOTA 43512-60150.' })).toBeVisible();
  expect(mock.marks).toEqual([{ rows: [{ id: ids.disc, manufacturer: 'TOYOTA', citation: 'EPC 2026 pág. 4' }] }]);
  await expect(cell(page, ids.disc, 'sku')).toContainText('MAIN OEM');  // the grid read its rows again
  expect(mock.lists()).toBeGreaterThan(before);
  expect(mock.unexpected).toEqual([]);
});

test('a code without digits cannot be marked, one not in the list goes to Crear OEM, and an OEM SKU outside the list can be unmarked', async ({ page }) => {
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

  // A number the list does not have: Marcar sends it to Crear OEM desde SKU, which adds it with the make and marks the SKU.
  await cell(page, ids.fan).click();
  await cell(page, ids.fan).click({ button: 'right' });
  await option(page, 'Marcar como OEM…').click();
  const notListed = page.getByRole('dialog', { name: 'Marcar como OEM' });
  await expect(notListed.getByText('21082V5000 no está en la lista OEM: usa «Crear OEM desde SKU».')).toBeVisible();
  await notListed.getByRole('button', { name: 'Crear OEM desde SKU' }).click();
  const create = page.getByRole('dialog', { name: 'Crear OEM desde SKU' });
  await expect(create.getByLabel('Fabricante de 21082-V5000')).toHaveValue('NISSAN');
  await create.getByLabel('Tipo de pieza de 21082-V5000').fill('FAN CLUTCH');
  await create.getByLabel('Revisé que cada código es el número OEM de su fabricante.').check();
  await create.getByRole('button', { name: 'Crear 1 OEM y marcar' }).click();
  await expect(page.getByRole('status').filter({ hasText: 'Agregado a la Biblioteca OEM y marcado como OEM: NISSAN 21082-V5000.' })).toBeVisible();
  expect(mock.creates).toEqual([{ rows: [{ id: ids.fan, manufacturer: 'NISSAN', citation: '', description: 'FAN CLUTCH NIS URVAN', part_type: 'FAN CLUTCH' }] }]);

  await cell(page, ids.pump).click();  // a new selection: just this row
  await cell(page, ids.pump).click({ button: 'right' });
  await expect(option(page, 'Marcar como OEM…')).toHaveClass(/ag-menu-option-disabled/);
  await option(page, 'Quitar marca OEM…').click();
  const unmark = page.getByRole('dialog', { name: 'Quitar marca OEM' });
  await expect(unmark.getByLabel('Retirar también su alterno OEM TOYOTA 16100-29155')).toBeChecked();  // it was filed by a marking
  await unmark.getByLabel('Revisé qué SKU dejan de ser OEM.').check();
  await unmark.getByRole('button', { name: 'Quitar marca OEM a 1 SKU' }).click();
  await expect(page.getByRole('status').filter({ hasText: '1 SKU ya no es OEM: 16100-29155.' })).toBeVisible();
  expect(mock.unmarks).toEqual([{ rows: [{ id: ids.pump, retire_number: true }] }]);
  expect(mock.marks).toEqual([]);
  expect(mock.unexpected).toEqual([]);
});

test('a number under several makes asks which one, Crear opens from the menu, and a SKU whose code is in the list cannot be unmarked', async ({ page }) => {
  const mock = await fixture(page);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto('/administracion?seccion=inventario');
  await cell(page, ids.filter).click();
  await cell(page, ids.filter).click({ button: 'right' });
  await option(page, 'Marcar como OEM…').click();
  const dialog = page.getByRole('dialog', { name: 'Marcar como OEM' });
  const make = dialog.getByLabel('Fabricante de 90915-YZZD4');
  await expect(make).toHaveValue('TOYOTA');  // the make the description names
  await make.selectOption('LEXUS');
  await dialog.getByLabel('Revisé que cada código es el número OEM de su fabricante.').check();
  await dialog.getByRole('button', { name: 'Marcar 1 SKU como OEM' }).click();
  await expect(dialog).toHaveCount(0);
  await expect.poll(() => mock.marks).toEqual([{ rows: [{ id: ids.filter, manufacturer: 'LEXUS', citation: '' }] }]);

  await cell(page, ids.fan).click();
  await cell(page, ids.fan).click({ button: 'right' });
  await option(page, 'Crear OEM desde SKU…').click();
  await expect(page.getByRole('dialog', { name: 'Crear OEM desde SKU' }).getByLabel('Fabricante de 21082-V5000')).toHaveValue('NISSAN');
  await page.getByRole('dialog').getByRole('button', { name: 'Cerrar ventana' }).click();

  await cell(page, ids.pads).click();
  await cell(page, ids.pads).click({ button: 'right' });
  await option(page, 'Quitar marca OEM…').click();
  const unmark = page.getByRole('dialog', { name: 'Quitar marca OEM' });
  await expect(unmark.getByLabel('Desmarcar 04465-35290')).toBeDisabled();
  await expect(unmark.getByText('Su código es TOYOTA 0446535290 de la lista OEM, así que es OEM')).toBeVisible();
  expect(mock.unmarks).toEqual([]);
  expect(mock.unexpected).toEqual([]);
});
