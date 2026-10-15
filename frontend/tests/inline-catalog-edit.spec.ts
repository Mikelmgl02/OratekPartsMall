import { expect, Page, test } from '@playwright/test';
import { grupos, makeLine, makeReference, mockLines } from './lines-route';

// Editar celdas on the OEM lines (plan S8.2): edits and pastes collect into one batch; each row saves with the version it was read at
// (management/lines/<id>, its estado through /transition), so one changed meanwhile is refused and its cells go back; references are
// added and retired one by one (management/lines/<id>/references, management/references/<id>), and ?ids= re-reads rows.
const pump = makeLine(1, 'BOMBA-1', 'BOMBA AGUA', { variant_count: 2 });
const disc = makeLine(2, 'DISCO-1', 'DISCO FRENO DEL', { variant_count: 2 });
const gwt = makeReference(7, pump, 'GWT-41A', { brand: 'GMB', tipo: 'aftermarket' });
const subgrupo = (n: number, m: number) => grupos[n].subgrupos[m].id;

async function fixture(page: Page) {
  return mockLines(page, { lines: [pump, disc], references: [gwt],
    // The disc has technical data for its subgrupo: changing it is refused.
    refuse: (method, path, body) => method === 'PATCH' && path === `lines/${disc.id}` && 'subgrupo' in body ? { status: 400, json: { detail: 'Esta línea OEM tiene datos técnicos de su subgrupo.' } } : null });
}
const cell = (page: Page, id: string, column: string) => page.locator(`.ag-center-cols-container .ag-row[row-id="${id}"] .ag-cell[col-id="${column}"]`);
const writes = (calls: string[]) => calls.filter(call => !call.startsWith('GET'));

async function paste(page: Page, text: string, id: string, column: string) {
  await page.evaluate(value => navigator.clipboard.writeText(value), text);
  await cell(page, id, column).click();
  await page.keyboard.press('ControlOrMeta+V');
}

test('edits and pastes save each row with its version, flash what the server kept and put back the cells of a refused row', async ({ page, context }) => {
  await context.grantPermissions(['clipboard-read', 'clipboard-write']);
  const mock = await fixture(page);
  await page.setViewportSize({ width: 1600, height: 1000 });
  await page.goto('/administracion?seccion=inventario');
  await expect(cell(page, pump.id, 'description')).toHaveText('BOMBA AGUA');
  await cell(page, pump.id, 'description').dblclick();
  await expect(page.locator('.ag-cell-inline-editing')).toHaveCount(0);  // read-only until Editar celdas
  await page.getByRole('button', { name: 'Editar celdas', exact: true }).click();
  await expect(page.getByText(/se guardan juntos al soltar/)).toBeVisible();
  await expect(cell(page, pump.id, 'description')).toHaveClass(/editable-cell-active/);

  // Two descriptions pasted from Excel: one batch, each row with the version it was read at, back as the server keeps them.
  await paste(page, 'bomba agua toy 22r\ndisco freno del toy', pump.id, 'description');
  await expect(page.getByRole('status').filter({ hasText: '2 SKU guardados.' })).toBeVisible();
  await expect(cell(page, disc.id, 'description')).toHaveText('DISCO FRENO DEL TOY');
  await expect(cell(page, pump.id, 'description')).toHaveText('BOMBA AGUA TOY 22R');
  expect(writes(mock.calls)).toEqual([`PATCH lines/${pump.id} {"description":"bomba agua toy 22r","expected_version":1}`,
    `PATCH lines/${disc.id} {"description":"disco freno del toy","expected_version":1}`]);

  // The estado and subgroup editors: an edit does not flash, the saved cell does.
  await cell(page, pump.id, 'estado').click();
  await page.keyboard.press('Enter');
  await page.getByRole('option', { name: 'RETIRADA', exact: true }).click();
  await expect(cell(page, pump.id, 'estado')).toHaveText('RETIRADA');
  await expect(cell(page, pump.id, 'estado')).toHaveClass(/ag-cell-data-changed/);
  await cell(page, pump.id, 'subgroup').click();
  await page.keyboard.press('Enter');
  await page.keyboard.type('bombas');
  await page.getByRole('option', { name: 'REFRIGERACIÓN / BOMBAS DE AGUA' }).click();
  await expect(cell(page, pump.id, 'subgroup')).toHaveClass(/ag-cell-data-changed/);
  expect(writes(mock.calls).slice(2)).toEqual([`POST lines/${pump.id}/transition {"estado":"retirada","expected_version":2}`,
    `PATCH lines/${pump.id} {"subgrupo":"${subgrupo(1, 0)}","expected_version":3}`]);

  // A block pasted over estado and subgroup of both rows: the pump is published again, the disc's subgroup is refused, so the disc keeps
  // what it had and the message names it.
  await paste(page, 'PUBLICADA\tREFRIGERACIÓN / BOMBAS DE AGUA\nRETIRADA\tFRENOS / DISCOS DE FRENO', pump.id, 'estado');
  await expect(page.getByRole('alert').filter({ hasText: 'DISCO-1' })).toContainText('Se guardaron 1 SKU; DISCO-1: Esta línea OEM tiene datos técnicos de su subgrupo.');
  await expect(cell(page, pump.id, 'estado')).toHaveText('PUBLICADA');
  await expect(cell(page, disc.id, 'estado')).toHaveText('PUBLICADA');
  await expect(cell(page, disc.id, 'subgroup')).toHaveText('—');
  expect(writes(mock.calls).slice(4)).toEqual([`POST lines/${pump.id}/transition {"estado":"publicada","expected_version":4}`,
    `PATCH lines/${disc.id} {"subgrupo":"${subgrupo(0, 0)}","expected_version":2}`]);

  // Another person saved the disc meanwhile: its row is refused with the 409 and comes back as the server has it.
  mock.lines.get(disc.id)!.version = 9;
  mock.lines.get(disc.id)!.name = 'DISCO VENTILADO';
  await cell(page, disc.id, 'description').dblclick();
  await page.keyboard.press('ControlOrMeta+A');
  await page.keyboard.type('disco freno trasero');
  await page.keyboard.press('Enter');
  await expect(page.getByRole('alert').filter({ hasText: 'DISCO-1' })).toContainText('No se guardó ningún cambio: DISCO-1: La línea cambió mientras la editabas');
  await expect(cell(page, disc.id, 'description')).toHaveText('DISCO FRENO DEL TOY');
  await page.screenshot({ path: '/tmp/motionpartes-inline-catalog-edit.png' });

  await page.getByRole('button', { name: 'Editando celdas', exact: true }).click();
  await cell(page, disc.id, 'description').dblclick();
  await expect(page.locator('.ag-cell-inline-editing')).toHaveCount(0);
  expect(mock.unexpected).toEqual([]);
});

test('the Referencias popup reads and copies without Editar celdas, adds and retires References with it, and takes a paste', async ({ page, context }) => {
  await context.grantPermissions(['clipboard-read', 'clipboard-write']);
  const mock = await fixture(page);
  await page.setViewportSize({ width: 1600, height: 1000 });
  await page.goto('/administracion?seccion=inventario');
  await paste(page, 'ZZ-1', pump.id, 'codes');  // ignored: read-only
  await cell(page, pump.id, 'codes').dblclick();
  const popup = page.getByRole('dialog', { name: 'Referencias de BOMBA-1' });
  await expect(popup).toContainText('Solo lectura · clic en un código para copiarlo');
  await expect(popup).toContainText('AFTERMARKET · GMB');
  await popup.getByRole('button', { name: /^GWT-41A/ }).click();
  await expect(popup.getByRole('status')).toHaveText('Copiado: GWT-41A');
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe('GWT-41A');
  await expect(popup.getByLabel('Nueva referencia')).toHaveCount(0);
  await expect(popup.getByRole('button', { name: 'Retirar referencia GWT-41A' })).toHaveCount(0);
  await page.keyboard.press('Escape');
  await expect(popup).toHaveCount(0);

  await page.getByRole('button', { name: 'Editar celdas', exact: true }).click();
  await expect(page.getByText(/se guardan juntos al soltar/)).toBeVisible();
  await cell(page, pump.id, 'codes').dblclick();
  await popup.getByLabel('Nueva referencia').fill('wpt-111');
  await page.keyboard.press('Tab');  // through the popup's fields, not to the next cell
  await expect(popup.getByLabel('Marca de la referencia')).toBeFocused();
  await page.keyboard.type('aisin');
  await popup.getByLabel('Tipo de referencia').selectOption('aftermarket');
  await popup.getByLabel('Nueva referencia').press('Enter');  // Enter adds, the popup stays open
  await expect(popup.getByRole('button', { name: /^WPT-111/ })).toBeVisible();
  await expect(popup.getByLabel('Nueva referencia')).toHaveValue('');
  await popup.getByRole('button', { name: 'Retirar referencia GWT-41A', exact: true }).click();
  await expect(popup.getByRole('button', { name: /^GWT-41A/ })).toHaveCount(0);
  await popup.getByRole('button', { name: 'Cerrar referencias', exact: true }).click();
  await expect(popup).toHaveCount(0);
  await expect(cell(page, pump.id, 'codes')).toContainText('WPT-111');  // the row was re-read when the popup closed
  await expect(cell(page, pump.id, 'codes')).not.toContainText('GWT-41A');
  await expect(cell(page, pump.id, 'codes')).toHaveClass(/ag-cell-data-changed/);
  expect(writes(mock.calls)).toEqual([`POST lines/${pump.id}/references {"code":"WPT-111","brand":"AISIN","tipo":"aftermarket"}`, 'DELETE references/7']);
  expect(mock.calls.some(call => call.startsWith('GET lines?') && call.includes(`ids=${pump.id}`))).toBe(true);

  // Pasted codes: the ones the line already has are skipped, each new one is added as its own Reference.
  await paste(page, 'ab-1, ab-2; WPT-111', pump.id, 'codes');
  await expect(page.getByRole('status').filter({ hasText: 'BOMBA-1: 2 referencias agregadas.' })).toBeVisible();
  await expect(cell(page, pump.id, 'codes')).toContainText('AB-1');
  expect(writes(mock.calls).slice(2)).toEqual([`POST lines/${pump.id}/references {"code":"AB-1","brand":"","tipo":"otro"}`, `POST lines/${pump.id}/references {"code":"AB-2","brand":"","tipo":"otro"}`]);
  expect(mock.unexpected).toEqual([]);
});

test('changes made elsewhere reach the rows on screen and flash', async ({ page }) => {
  await page.clock.install();
  const mock = await fixture(page);
  await page.setViewportSize({ width: 1600, height: 1000 });
  await page.goto('/administracion?seccion=inventario');
  await expect(cell(page, pump.id, 'stock')).toHaveText('2');
  Object.assign(mock.lines.get(pump.id)!, { variant_count: 5, description: 'BOMBA AGUA TOY 2KD' });  // another user, a supplier feed
  await page.clock.fastForward('00:16');
  await expect(cell(page, pump.id, 'stock')).toHaveText('5');
  await expect(cell(page, pump.id, 'description')).toHaveText('BOMBA AGUA TOY 2KD');
  await expect(cell(page, pump.id, 'stock')).toHaveClass(/ag-cell-data-changed/);
  await expect(cell(page, disc.id, 'stock')).not.toHaveClass(/ag-cell-data-changed/);  // unchanged rows stay still
});

test('the edit toggle and the Referencias popup fit a phone screen', async ({ page }) => {
  await fixture(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/administracion?seccion=inventario');
  await page.getByRole('button', { name: 'Editar celdas', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Editando celdas', exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)).toBe(false);
  await cell(page, pump.id, 'codes').dblclick();
  const popup = page.getByRole('dialog', { name: 'Referencias de BOMBA-1' });
  await expect(popup.getByRole('button', { name: 'Agregar' })).toBeVisible();
  const box = (await popup.boundingBox())!;  // popups open on the page: whole on screen, without widening it
  expect(box.x).toBeGreaterThanOrEqual(0);
  expect(box.x + box.width).toBeLessThanOrEqual(390);
  expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)).toBe(false);
});
