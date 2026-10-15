import { expect, type Page, test } from '@playwright/test';
import { grupos, makeLine, makeReference, mockLines, uid } from './lines-route';

// Inventario on OEM lines (plan S8.2): estado tabs over management/lines, the Tipo and Marca columns, the Tipo filter, Marcar referencia
// principal from the right click, and the line's side panel: General saved with the version it was read at, its estado moves, and
// Crear SKU opening on Referencias. The backend is mocked in the browser (tests/lines-route.ts).
const cell = (page: Page, id: string, column: string) => page.locator(`.ag-center-cols-container .ag-row[row-id="${id}"] .ag-cell[col-id="${column}"], .ag-pinned-left-cols-container .ag-row[row-id="${id}"] .ag-cell[col-id="${column}"]`);
const filter = makeLine(1, '90915-YZZE1', 'FILTRO ACEITE TOY HILUX', { tipo: 'OEM', brand: 'TOYOTA', main: { id: 100, code: '90915-YZZE1', brand: 'TOYOTA', tipo: 'oem', estado: 'verificado' },
  grupo: { id: grupos[0].id, nombre: 'FRENOS' }, subgrupo: { id: grupos[0].subgrupos[0].id, name: 'DISCOS DE FRENO' }, variant_count: 3 });
const pump = makeLine(2, 'GWP-41A', 'BOMBA AGUA TOY 22R', { tipo: 'AFTERMARKET', brand: 'GMB', main: { id: 200, code: 'GWP-41A', brand: 'GMB', tipo: 'aftermarket', estado: 'declarado' } });
const retired = makeLine(3, 'RET-77', 'RETEN CIGUEÑAL', { estado: 'retirada' });
const fresh = makeLine(4, 'EC01', 'KIT EMBRAGUE HYUNDAI H100', { estado: 'preparacion', origen: 'proveedor', variant_count: 1 });
const merged = makeLine(5, '04111-22152-AS', 'JUEGO EMPAQUES MOTOR', { estado: 'unificada', merged_into: uid(5, 1), main: null, reference_count: 0 });
const library = makeLine(6, '16100-39315', 'BOMBA DE AGUA', { estado: 'referencia', origen: 'biblioteca', tipo: 'OEM', brand: 'TOYOTA', main: { id: 600, code: '16100-39315', brand: 'TOYOTA', tipo: 'oem', estado: 'declarado' } });
const references = [makeReference(201, pump, '16100-39316', { brand: 'TOYOTA', tipo: 'oem', estado: 'verificado' }), makeReference(202, pump, 'WPT-111', { brand: 'AISIN', tipo: 'aftermarket' })];
const lines = [filter, pump, retired, fresh, merged, library];

for (const width of [1440, 390]) {
  test(`estado tabs list the OEM lines of each estado with their tipo and brand at ${width}px`, async ({ page }) => {
    const mock = await mockLines(page, { lines, references });
    await page.setViewportSize({ width, height: 1000 });
    await page.goto('/administracion?seccion=inventario');
    const tabs = page.getByRole('group', { name: 'Estado de la línea OEM' });
    await expect(tabs.getByRole('button', { name: 'Publicadas y retiradas', exact: true })).toHaveAttribute('aria-pressed', 'true');
    await expect(cell(page, filter.id, 'sku')).toContainText('90915-YZZE1');
    await expect(cell(page, filter.id, 'sku')).toContainText('OEM · TOYOTA');
    await expect(cell(page, retired.id, 'estado')).toHaveText('RETIRADA');
    await expect(page.locator(`.ag-row[row-id="${fresh.id}"]`)).toHaveCount(0);
    expect(mock.lists[0].get('estado')).toBe('publicada,retirada');
    if (width >= 900) {
      await expect(cell(page, pump.id, 'tipo')).toHaveText('AFTERMARKET');
      await expect(cell(page, pump.id, 'brand')).toHaveText('GMB');
      await expect(cell(page, pump.id, 'codes')).toContainText('16100-39316');
      await expect(cell(page, pump.id, 'codes')).toContainText('WPT-111');
      await expect(cell(page, filter.id, 'stock')).toHaveText('3');
      await expect(cell(page, filter.id, 'subgroup')).toHaveText('FRENOS / DISCOS DE FRENO');
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)).toBe(false);

    await page.getByRole('combobox', { name: 'Tipo de línea OEM', exact: true }).selectOption('OEM');
    await expect.poll(() => mock.queries().at(-1)?.get('tipo')).toBe('OEM');
    await expect(cell(page, filter.id, 'sku')).toContainText('90915-YZZE1');
    await expect(page.locator(`.ag-row[row-id="${pump.id}"]`)).toHaveCount(0);
    await page.getByRole('combobox', { name: 'Tipo de línea OEM', exact: true }).selectOption('');

    await tabs.getByRole('button', { name: 'En preparación', exact: true }).click();
    await expect(cell(page, fresh.id, 'sku')).toContainText('EC01');
    await expect(cell(page, fresh.id, 'estado')).toHaveText('EN PREPARACIÓN');
    await expect(page.locator(`.ag-row[row-id="${filter.id}"]`)).toHaveCount(0);
    expect(new URL(page.url()).searchParams.get('estado')).toBe('preparacion');
    expect(mock.queries().at(-1)?.get('estado')).toBe('preparacion');
    await tabs.getByRole('button', { name: 'Referencia', exact: true }).click();
    await expect(cell(page, library.id, 'sku')).toContainText('16100-39315');
    await tabs.getByRole('button', { name: 'Unificadas', exact: true }).click();
    await expect(cell(page, merged.id, 'sku').getByRole('button', { name: 'Separar de su línea OEM' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Editar celdas', exact: true })).toHaveCount(0);  // a unificada line is read only
    await page.screenshot({ path: `/tmp/motionpartes-oem-lines-${width}.png` });
    expect(mock.unexpected).toEqual([]);
  });
}

test('a deep link opens its estado tab', async ({ page }) => {
  const mock = await mockLines(page, { lines });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto('/administracion?seccion=inventario&estado=rechazada');
  await expect(page.getByRole('group', { name: 'Estado de la línea OEM' }).getByRole('button', { name: 'Rechazadas', exact: true })).toHaveAttribute('aria-pressed', 'true');
  await expect.poll(() => mock.queries().at(-1)?.get('estado')).toBe('rechazada');
  await expect(page.getByText('No hay líneas OEM rechazadas.')).toBeVisible();
});

test('Marcar referencia principal makes another Reference the main: the SKU follows its code', async ({ page }) => {
  const mock = await mockLines(page, { lines, references });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto('/administracion?seccion=inventario');
  await cell(page, pump.id, 'description').click({ button: 'right' });
  await page.getByText('Marcar referencia principal…', { exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Referencia principal · GWP-41A' });
  await expect(dialog.getByRole('radio', { name: /GWP-41A/ })).toBeChecked();
  await expect(dialog.getByRole('button', { name: 'Hacer principal' })).toBeDisabled();
  await dialog.getByRole('radio', { name: /16100-39316/ }).check();
  await dialog.getByRole('button', { name: 'Hacer principal' }).click();
  await expect(page.getByRole('status').filter({ hasText: '16100-39316 es ahora la referencia principal de la línea OEM: su SKU pasó de GWP-41A a 16100-39316.' })).toBeVisible();
  await expect(cell(page, pump.id, 'sku')).toContainText('16100-39316');
  await expect(cell(page, pump.id, 'sku')).toContainText('OEM · TOYOTA');
  expect(mock.calls).toContain(`POST lines/${pump.id}/main {"reference":201,"expected_version":1}`);
  // A line with only its main has nothing to choose from.
  await cell(page, retired.id, 'description').click({ button: 'right' });
  await expect(page.locator('.ag-menu-option-disabled').filter({ hasText: 'Marcar referencia principal…' })).toBeVisible();
  expect(mock.unexpected).toEqual([]);
});

test('the line panel saves General with its version, recovers from a conflict and moves the estado', async ({ page }) => {
  const mock = await mockLines(page, { lines, references });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto('/administracion?seccion=inventario');
  await cell(page, pump.id, 'sku').getByRole('button', { name: 'GWP-41A' }).click();
  const panel = page.getByRole('dialog', { name: 'Línea OEM · GWP-41A' });
  await expect(panel).toHaveClass(/drawer/);
  await expect(panel.getByRole('region', { name: 'Estado de la línea OEM' })).toContainText('PUBLICADA');
  await expect(panel.getByRole('region', { name: 'Estado de la línea OEM' })).toContainText('AFTERMARKET · GMB');
  await expect(panel.getByRole('checkbox', { name: 'EL SKU PRINCIPAL ES OEM' })).toHaveCount(0);  // the main Reference's tipo decides
  await panel.getByLabel('Descripción', { exact: true }).fill('bomba agua toy 22r 4runner');
  await panel.getByLabel('Subgrupo', { exact: true }).selectOption({ label: 'REFRIGERACIÓN / BOMBAS DE AGUA' });
  await panel.getByRole('button', { name: 'Guardar línea OEM' }).click();
  await expect(panel.getByRole('status').filter({ hasText: 'Línea OEM GWP-41A guardada.' })).toBeVisible();
  expect(mock.calls).toContain(`PATCH lines/${pump.id} {"description":"BOMBA AGUA TOY 22R 4RUNNER","subgrupo":"${grupos[1].subgrupos[0].id}","expected_version":1}`);
  await expect(cell(page, pump.id, 'description')).toHaveText('BOMBA AGUA TOY 22R 4RUNNER');

  // Someone else saved the line meanwhile: the save is refused and the panel shows the line as it is now.
  const current = mock.lines.get(pump.id)!;
  Object.assign(current, { version: 7, description: 'BOMBA DE AGUA' });
  await panel.getByLabel('Aplicaciones', { exact: true }).fill('toyota hilux 1990-1997');
  await panel.getByRole('button', { name: 'Guardar línea OEM' }).click();
  await expect(panel.getByRole('alert')).toContainText('La línea cambió mientras la editabas');
  await expect(panel.getByLabel('Descripción', { exact: true })).toHaveValue('BOMBA DE AGUA');

  await panel.getByRole('button', { name: 'Retirar', exact: true }).click();
  await expect(panel.getByRole('region', { name: 'Estado de la línea OEM' })).toContainText('RETIRADA');
  expect(mock.calls).toContain(`POST lines/${pump.id}/transition {"estado":"retirada","expected_version":7}`);
  await expect(panel.getByRole('button', { name: 'Publicar', exact: true })).toBeVisible();
  expect(await panel.evaluate(element => element.scrollWidth > element.clientWidth)).toBe(false);
  expect(mock.unexpected).toEqual([]);
});

test('a line in preparación is rejected with its reason from the panel', async ({ page }) => {
  const mock = await mockLines(page, { lines });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto('/administracion?seccion=inventario&estado=preparacion');
  await cell(page, fresh.id, 'sku').getByRole('button', { name: 'EC01' }).click();
  const panel = page.getByRole('dialog', { name: 'Línea OEM · EC01' });
  await expect(panel.getByRole('region', { name: 'Estado de la línea OEM' })).toContainText('ORIGEN: PROVEEDOR');
  await panel.getByRole('button', { name: 'Rechazar', exact: true }).click();
  await panel.getByLabel('Motivo del rechazo').selectOption('not_a_part');
  await panel.getByLabel('Nota del rechazo').fill('Es una herramienta');
  await panel.getByRole('button', { name: 'Confirmar rechazo' }).click();
  await expect(panel.getByRole('region', { name: 'Estado de la línea OEM' })).toContainText('Rechazada: No es un repuesto · Es una herramienta');
  expect(mock.calls).toContain(`POST lines/${fresh.id}/transition {"estado":"rechazada","expected_version":1,"reason":"not_a_part","note":"Es una herramienta"}`);
  await expect(panel.getByRole('button', { name: 'Volver a preparación', exact: true })).toBeVisible();
});

test('Crear SKU creates the line from its codigo and opens its References', async ({ page }) => {
  const mock = await mockLines(page, { lines });
  const created = makeLine(9, 'NEW-100', 'SENSOR OXIGENO');
  const posts: unknown[] = [];
  await page.route(/\/api\/management\/catalog(?:\?.*)?$/, route => {
    if (route.request().method() !== 'POST') return route.fallback();
    posts.push(route.request().postDataJSON());
    mock.addLine(created);
    return route.fulfill({ status: 201, json: { id: created.id, sku: created.sku } });
  });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto('/administracion?seccion=inventario');
  await page.getByRole('button', { name: 'Crear SKU', exact: true }).click();
  const panel = page.getByRole('dialog', { name: 'Crear SKU' });
  await expect(panel.getByRole('tab', { name: 'Referencias' })).toBeDisabled();
  await panel.getByLabel('SKU principal (MAIN)').fill('new-100');
  await panel.getByLabel('Descripción', { exact: true }).fill('sensor oxigeno');
  await panel.getByLabel('Subgrupo', { exact: true }).selectOption({ label: 'FRENOS / PASTILLAS DE FRENO' });
  await panel.getByRole('button', { name: 'Crear SKU', exact: true }).click();
  const opened = page.getByRole('dialog', { name: 'Línea OEM · NEW-100' });
  await expect(opened.getByRole('tab', { name: /^Referencias/ })).toHaveAttribute('aria-selected', 'true');
  await expect(opened.getByRole('list', { name: 'Referencias de la línea OEM' })).toContainText('NEW-100');
  expect(posts).toEqual([{ sku: 'NEW-100', name: '', description: 'SENSOR OXIGENO', active: true, codes: [], part_type: grupos[0].subgrupos[1].id }]);
  expect(mock.unexpected).toEqual([]);
});
