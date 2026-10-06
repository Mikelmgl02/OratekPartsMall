import { expect, Page, test } from '@playwright/test';
import type { Deal } from '../lib/deal-types';
import type { PriceList, PriceRow } from '../lib/pricing-types';
import type { SupplierRequestLine } from '../lib/request-types';
import { mockQuoteDrafts, suggestion } from './quote-draft-mock';

const supplierId = '22222222-2222-4222-8222-222222222222';
const clientId = '11111111-1111-4111-8111-111111111111';
const itemA = '66666666-6666-4666-8666-666666666661';
const itemB = '66666666-6666-4666-8666-666666666662';
const general = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1';
const mayorista = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2';
const stamp = '2026-10-03T12:00:00Z';
const empty = { count: 0, next: null, previous: null, results: [] };
const any = expect.any(String);
const base = `/api/market/accounts/${supplierId}`;

function priceList(id: string, code: string, isDefault: boolean, priced: number): PriceList {
  return { id, code, name: `LISTA ${code}`, currency: 'USD', is_default: isDefault, active: true, version: 2, priced_count: priced, missing_count: 2 - priced, created_at: stamp, updated_at: stamp };
}
function row(id: string, invent: string, codigo: string, prices: PriceRow['prices'], extra: Partial<PriceRow> = {}): PriceRow {
  return { supplier_item_id: id, supplier_invent_id: invent, codigo, brand: 'KSM', description: `TAMBOR ${invent}`, matching_status: 'matched', discount_group: '',
    floor_price: null, item_pricing_revision: null, prices, updated_at: null, ...extra };
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
const cell = (page: Page, item: string, column: string) => page.locator(`.price-grid .ag-row[row-id="${item}"] .ag-cell[col-id="${column}"]`);
// Like a spreadsheet: selecting a cell never opens it; typing replaces its value and Enter keeps it.
async function type(page: Page, item: string, column: string, value: string) {
  await cell(page, item, column).click();
  await page.keyboard.type(value);
  await page.keyboard.press('Enter');
}

test('suppliers edit and paste private prices, save them with expected revisions and reload what another member changed', async ({ page, context }) => {
  await context.grantPermissions(['clipboard-read', 'clipboard-write']);
  const unexpected = await session(page);
  const writes: Record<string, unknown>[] = [], reads: string[] = [];
  let rows = [row(itemA, 'A-001', '58411-1R000-G', { GENERAL: { unit_price: '15.00', revision: 1, updated_at: stamp } },
    { discount_group: 'FILTROS', floor_price: '12.00', item_pricing_revision: 1, updated_at: stamp }), row(itemB, 'A-002', '58411-1R000-KSM', {})];
  await page.route(`${base}/price-lists`, route => route.fulfill({ json: { can_configure: true, item_count: 2, results: [priceList(general, 'GENERAL', true, 1), priceList(mayorista, 'MAYORISTA', false, 0)] } }));
  await page.route(new RegExp(`${base}/prices(?:\\?.*)?$`), route => {
    if (route.request().method() === 'GET') { reads.push(new URL(route.request().url()).search); return route.fulfill({ json: { ...empty, count: rows.length, results: rows } }); }
    const body = route.request().postDataJSON(); writes.push(body);
    if (writes.length === 1) {
      rows = [row(itemA, 'A-001', '58411-1R000-G', { GENERAL: { unit_price: '15.00', revision: 1, updated_at: stamp }, MAYORISTA: { unit_price: '13.50', revision: 1, updated_at: stamp } },
        { discount_group: 'FILTROS', floor_price: '12.00', item_pricing_revision: 1, updated_at: stamp }),
        row(itemB, 'A-002', '58411-1R000-KSM', { GENERAL: { unit_price: '8.00', revision: 1, updated_at: stamp }, MAYORISTA: { unit_price: '7.50', revision: 1, updated_at: stamp } },
          { floor_price: '5.00', item_pricing_revision: 1, updated_at: stamp })];
      return route.fulfill({ json: { summary: { created: 3, updated: 0, removed: 0, unchanged: 0, floor_updates: 1, line_updates: 0 }, rows } });
    }
    // Another member saved GENERAL for A-001 meanwhile: nothing is written and its current value comes back.
    rows = [{ ...rows[0], prices: { ...rows[0].prices, GENERAL: { unit_price: '15.50', revision: 2, updated_at: stamp } } }, rows[1]];
    return route.fulfill({ status: 409, json: { detail: 'Otro usuario cambió algunos precios.', rows,
      conflicts: [{ supplier_item_id: itemA, list_id: general, current: { unit_price: '15.50', revision: 2 } }] } });
  });
  await page.route(`${base}/prices/${itemA}/history?page=1`, route => route.fulfill({ json: { ...empty, count: 1, results: [{ id: 1, kind: 'list_price', price_list: { id: general, code: 'GENERAL' },
    old_value: '14.00', new_value: '15.00', old_text: '', new_text: '', source: 'manual', reference: 'op:1', actor: { name: 'EMPLEADO' }, created_at: stamp }] } }));
  await page.goto(`/?vista=proveedor&seccion=precios&cuenta=${supplierId}`);
  await expect(page.getByRole('tab', { name: 'Precios', exact: true })).toHaveAttribute('aria-selected', 'true');
  const section = page.getByRole('region', { name: 'Precios del proveedor', exact: true });
  await expect(section.getByRole('note')).toContainText('Estos precios son privados: nunca se muestran en el catálogo');
  await expect(section.locator('.price-list-table tbody tr').first()).toContainText('GENERAL');
  await expect(section.locator('.price-list-table tbody tr').first()).toContainText('Predeterminada');
  await expect(cell(page, itemA, 'price:GENERAL')).toContainText('15.00');
  await expect(cell(page, itemB, 'price:GENERAL')).toHaveText('Sin precio');
  await type(page, itemA, 'price:MAYORISTA', '13,5');
  await expect(cell(page, itemA, 'price:MAYORISTA')).toContainText('13.50');
  const save = section.getByRole('button', { name: /^Guardar cambios/ });
  await expect(save).toHaveText('Guardar cambios (1)');
  // Pasting a row copied from Excel fills the selected cell and the ones to its right.
  await page.evaluate(() => navigator.clipboard.writeText('8.00\t7.50'));
  await cell(page, itemB, 'price:GENERAL').click();
  await page.keyboard.press('ControlOrMeta+v');
  await expect(cell(page, itemB, 'price:MAYORISTA')).toContainText('7.50');
  await expect(cell(page, itemB, 'price:GENERAL')).toContainText('8.00');
  await type(page, itemB, 'floor_price', '-1');
  await expect(section.getByText('Corrige las celdas marcadas para guardar.', { exact: true })).toBeVisible();
  await expect(save).toBeDisabled();
  await type(page, itemB, 'floor_price', '5');
  await expect(save).toHaveText('Guardar cambios (4)');
  await save.click();
  await expect(section.getByRole('status').filter({ hasText: 'Se guardaron 4 cambios.' })).toBeVisible();
  expect(writes[0]).toEqual({
    changes: [{ list_id: mayorista, supplier_item_id: itemA, unit_price: '13.50', expected_revision: null },
      { list_id: general, supplier_item_id: itemB, unit_price: '8.00', expected_revision: null }, { list_id: mayorista, supplier_item_id: itemB, unit_price: '7.50', expected_revision: null }],
    items: [{ supplier_item_id: itemB, floor_price: '5.00', expected_revision: null }] });
  await expect(save).toHaveText('Guardar cambios (0)');
  // A conflict reloads the changed price and keeps the other edit pending.
  await type(page, itemA, 'price:GENERAL', '16');
  await type(page, itemB, 'discount_group', 'pastillas');
  await expect(cell(page, itemB, 'discount_group')).toHaveText('PASTILLAS');
  await save.click();
  await expect(section.getByRole('alert')).toHaveText('Otro usuario cambió 1 precio. Se recargaron sus valores actuales.');
  expect(writes[1]).toEqual({ changes: [{ list_id: general, supplier_item_id: itemA, unit_price: '16.00', expected_revision: 1 }],
    items: [{ supplier_item_id: itemB, discount_group: 'PASTILLAS', expected_revision: 1 }] });
  await expect(cell(page, itemA, 'price:GENERAL')).toContainText('15.50');
  await expect(cell(page, itemB, 'discount_group')).toHaveText('PASTILLAS');
  await expect(save).toHaveText('Guardar cambios (1)');
  await section.getByRole('button', { name: 'Descartar', exact: true }).click();
  await expect(cell(page, itemB, 'discount_group')).toHaveText('');
  await section.getByRole('button', { name: 'Historial de precio de 58411-1R000-G', exact: true }).click();
  const history = page.getByRole('dialog', { name: 'Historial de precio · 58411-1R000-G' });
  await expect(history.getByRole('listitem')).toContainText(['Precio GENERAL']);
  await expect(history.getByRole('listitem')).toContainText(/14.00 → .*15.00/);
  await history.getByRole('button', { name: 'Cerrar ventana' }).click();
  await section.getByRole('group', { name: 'Filtrar precios' }).getByRole('button', { name: 'Bajo el mínimo', exact: true }).click();
  await expect.poll(() => reads.at(-1)).toBe(`?page=1&status=below_floor&list=${general}`);
  // The sub-tabs follow the tabs pattern: arrows move between them and each panel is named by its tab.
  await page.route(new RegExp(`${base}/pricing/history(?:\\?.*)?$`), route => route.fulfill({ json: { ...empty, count: 1, results: [{ id: 9, kind: 'prices_edited',
    actor: { name: 'EMPLEADO' }, client: null, order: null, object_id: 'op:1', payload: { created: 3, updated: 0, removed: 0, floor_updates: 1, line_updates: 0 }, created_at: stamp }] } }));
  const listsTab = section.getByRole('tab', { name: 'Listas de precios', exact: true });
  await listsTab.focus();
  // Arrows cycle through the N sub-sections: left from the first one wraps to Historial, the last.
  await listsTab.press('ArrowLeft');
  await expect(section.getByRole('tab', { name: 'Historial', exact: true })).toBeFocused();
  await expect(section.getByRole('tabpanel', { name: 'Historial', exact: true })).toContainText('3 nuevos · 1 mínimos');
  await page.keyboard.press('Home');
  await expect(listsTab).toHaveAttribute('aria-selected', 'true');
  await expect(section.getByRole('tabpanel', { name: 'Listas de precios', exact: true })).toBeVisible();
  expect(unexpected).toEqual([]);
});

const orderId = '33333333-3333-4333-8333-333333333333';
const lineA = '44444444-4444-4444-8444-444444444441';
const lineB = '44444444-4444-4444-8444-444444444442';
function orderLine(id: string, codigo: string, quantity: number): SupplierRequestLine {
  return { id, part_id: '77777777-7777-4777-8777-777777777777', supplier_item_id: id === lineA ? itemA : itemB, supplier_invent_id: `ID-${codigo}`, sku: '58411-1R000',
    name: 'TAMBOR', codigo, brand: 'KSM', description: `REPUESTO ${codigo}`, quantity, stock: { reported_quantity: 9, reserved_quantity: 0, available_quantity: 9, shortfall: 0, updated_at: stamp } };
}

test('drafts arrive priced from the list, explain each price and apply suggestions only through the server', async ({ page }) => {
  const unexpected = await session(page);
  const order: Deal = { id: orderId, reference: 'ORD-PRECIOS-001', client: { id: clientId, name: 'TALLER CENTRAL' }, supplier: { id: supplierId, name: 'REPUESTOS CENTRAL' },
    status: 'reviewed', version: 3, created_at: stamp, reviewed_at: stamp, handshaked_at: null, line_count: 2, unit_count: 3, notes: '', quotation: null, quotations: [], events: [],
    lines: [orderLine(lineA, '58411-1R000-G', 2), orderLine(lineB, '00700-NP', 1)] };
  await page.route(`${base}/requests/${orderId}`, route => route.fulfill({ json: order }));
  await page.route(`${base}/requests/${orderId}/review`, route => route.fulfill({ json: order }));
  await page.route(/\/api\/market\/accounts\/[^/]+\/deals\/[^/]+\/messages(?:\?.*)?$/, route => route.fulfill({ json: { results: [], cursor: 0, has_more: false, has_earlier: false } }));
  const drafts = await mockQuoteDrafts(page, () => order, { suggestions: { [lineA]: suggestion('15.00', { floor: '12.00' }), [lineB]: suggestion(null) } });
  const content = page.locator('.deal-order-content');
  const quote = (line: string, column: string) => content.locator(`.quotation-grid .ag-row[row-id="${line}"] .ag-cell[col-id="${column}"]`);
  await page.goto(`/proveedor/${supplierId}/ordenes/${orderId}`);
  // Artículos shows the private suggested price with its origin.
  const lines = content.locator('.supplier-request-lines');
  await expect(lines.getByRole('columnheader', { name: 'Precio sugerido' })).toBeVisible();
  await expect(lines.getByRole('row').nth(1)).toContainText('Lista GENERAL');
  await expect(lines.getByRole('row').nth(2)).toContainText('Sin precio en tu lista');
  await content.getByRole('button', { name: 'Cotización', exact: true }).click();
  await expect(quote(lineA, 'unit_price')).toContainText('15.00');
  await expect(quote(lineA, 'price_source')).toHaveText('Lista');
  await expect(quote(lineB, 'price_source')).toHaveText('Sin precio');
  await expect(content.getByRole('button', { name: /Aplicar precios sugeridos/ })).toHaveCount(0);
  await quote(lineA, 'unit_price').click();
  const input = content.getByLabel('Precio de 58411-1R000-G', { exact: true });
  await input.fill('13'); await input.press('Enter');
  await expect(quote(lineA, 'price_source')).toHaveText('Manual');
  await expect.poll(() => drafts.saves.length).toBe(1);
  expect(drafts.saves[0]).toEqual({ save_id: any, expected_draft_version: 0, lines: [{ order_line_id: lineA, unit_price: '13.00' }] });
  await expect(content.getByRole('status', { name: 'Estado del borrador' })).toContainText('Borrador v1');
  // The list gains a price for the second item in another tab; the next save brings it as a suggestion to apply.
  drafts.suggest(lineB, suggestion('4.00'));
  await content.getByLabel('Condiciones de entrega y cotización').fill('retiro en sucursal');
  const apply = content.getByRole('button', { name: 'Aplicar precios sugeridos (1)', exact: true });
  await apply.click();
  await expect(quote(lineB, 'unit_price')).toContainText('4.00');
  await expect(quote(lineB, 'price_source')).toHaveText('Lista');
  await expect(content.getByRole('status').filter({ hasText: '1 precio sugerido aplicado.' })).toBeVisible();
  expect(drafts.reprices).toEqual([{ save_id: any, expected_draft_version: 2, scope: 'blank' }]);
  // The drawer explains the suggestion and can replace the manual price with it.
  await quote(lineA, 'price_source').getByRole('button').click();
  const drawer = page.getByRole('dialog', { name: '¿De dónde sale este precio?' });
  await expect(drawer).toContainText('58411-1R000-G');
  await expect(drawer.getByRole('listitem').first()).toContainText('Lista GENERAL');
  await expect(drawer.getByRole('listitem').first()).toContainText('Revisión 3');
  await expect(drawer).toContainText('Redondeo a centavos');
  await expect(drawer).toContainText('Tu precio mínimo');
  await drawer.getByRole('button', { name: 'Usar precio sugerido', exact: true }).click();
  await expect(drawer).toHaveCount(0);
  await expect(quote(lineA, 'unit_price')).toContainText('15.00');
  await expect(quote(lineA, 'price_source')).toHaveText('Lista');
  expect(drafts.reprices.at(-1)).toEqual({ save_id: any, expected_draft_version: 3, scope: 'lines', order_line_ids: [lineA] });
  expect(drafts.current().lines.map(line => [line.unit_price, line.price_source])).toEqual([['15.00', 'engine'], ['4.00', 'engine']]);
  // Artículos follows the latest saved draft: the new list price shows as the second item's suggestion.
  await content.getByRole('button', { name: 'Artículos', exact: true }).click();
  await expect(lines.getByRole('row').nth(2)).toContainText('Lista GENERAL');
  expect(drafts.saves.map(({ save_id: _, ...save }) => save)).toEqual([{ expected_draft_version: 0, lines: [{ order_line_id: lineA, unit_price: '13.00' }] },
    { expected_draft_version: 1, terms: 'RETIRO EN SUCURSAL' }]);
  expect(unexpected).toEqual([]);
});
