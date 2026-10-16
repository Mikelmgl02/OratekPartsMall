import { expect, Page, test } from '@playwright/test';
import { grupos, makeLine, mockLines } from './lines-route';

// The OEM line's side panel over the Inventario grid: General, Referencias and Ficha técnica (plan S8.2–3), the grid behind it unchanged.
const disc = makeLine(1, 'DF-4512', 'DISCO FRENO DEL TOY LAND CRUISER', { grupo: { id: grupos[0].id, nombre: 'FRENOS' }, subgrupo: { id: grupos[0].subgrupos[0].id, name: 'DISCOS DE FRENO' } });
const pump = makeLine(2, 'BOMBA-9', 'BOMBA AGUA TOY 22R');
const partType = '0f300000-0000-4000-8000-000000000001';

// The lines and what the panel reads when it opens (the technical sheet, applications, suffixes for the OEM lookup).
async function fixture(page: Page) {
  const mock = await mockLines(page, { lines: [disc, pump] });
  const sheets: unknown[] = [];
  const field = { key: 'diameter', label: 'DIÁMETRO', section: 'MEDIDAS', kind: 'number', unit: 'mm', options: [], required: true, position: 0 };
  const sheet = (revision: number, values = {}) => ({ part: disc.id, part_type: { id: partType, category: 'FRENOS', name: 'DISCOS DE FRENO' },
    revision, template: { part_type: partType, revision: 2, fields: [field] }, values, applications: [], missing_required: [] });
  await page.route('**/api/management/catalog/company-suffixes', route => route.fulfill({ json: { suffixes: [] } }));
  await page.route(/\/api\/management\/applications(?:\?.*)?$/, route => route.fulfill({ json: { count: 0, next: null, previous: null, results: [] } }));
  await page.route(/\/api\/management\/catalog\/[a-f0-9-]{36}\/technical$/, route => {
    if (route.request().method() !== 'PATCH') return route.fulfill({ json: sheet(3) });
    const body = route.request().postDataJSON() as { values: Record<string, unknown> };
    sheets.push(body);
    return route.fulfill({ json: sheet(4, body.values) });
  });
  return { ...mock, sheets, lineLists: () => mock.queries().length };
}

const skuCell = (page: Page, id: string) => page.locator(`.ag-row[row-id="${id}"] .ag-cell[col-id="sku"]`);

test('the SKU code opens the line panel over the grid, which stays as it was', async ({ page }) => {
  const mock = await fixture(page);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto('/administracion?seccion=inventario');
  await expect(skuCell(page, disc.id)).toContainText('DF-4512');
  const lists = mock.lineLists();
  await skuCell(page, disc.id).getByRole('button', { name: 'DF-4512' }).click();
  const editor = page.getByRole('dialog', { name: 'Línea OEM · DF-4512' });
  await expect(editor).toBeVisible();
  await expect(editor).toHaveClass(/drawer/);
  const box = (await editor.boundingBox())!;
  expect(Math.round(box.x + box.width)).toBe(1440);  // docked to the right edge
  await page.screenshot({ path: '/tmp/motionpartes-sku-editor-panel.png' });
  await editor.getByRole('button', { name: 'Cerrar ventana' }).click();
  await expect(editor).toHaveCount(0);
  expect(mock.lineLists()).toBe(lists);  // the grid never reloaded: same rows, same scroll position

  await skuCell(page, pump.id).locator('.sku-identity').dblclick();  // a double click anywhere in the cell opens it too
  await expect(page.getByRole('dialog', { name: 'Línea OEM · BOMBA-9' })).toBeVisible();
  await page.keyboard.press('Escape');
  await skuCell(page, disc.id).getByRole('button', { name: 'DF-4512' }).dblclick();  // its second click lands on the backdrop
  await expect(editor).toBeVisible();
  expect(mock.unexpected).toEqual([]);
});

test('on a phone the line panel takes the screen width', async ({ page }) => {
  await fixture(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/administracion?seccion=inventario');
  await skuCell(page, disc.id).getByRole('button', { name: 'DF-4512' }).click();
  const editor = page.getByRole('dialog', { name: 'Línea OEM · DF-4512' });
  await expect(editor).toBeVisible();
  const box = (await editor.boundingBox())!;
  expect(box.x).toBeGreaterThanOrEqual(0);
  expect(box.x + box.width).toBeLessThanOrEqual(390);
  await editor.getByRole('tab', { name: /^Referencias/ }).click();
  await expect(editor.getByRole('list', { name: 'Referencias de la línea OEM' })).toContainText('DF-4512');
  expect(await editor.evaluate(element => element.scrollWidth > element.clientWidth)).toBe(false);
});

test('the line panel has General, Referencias and Ficha técnica tabs, and its Ficha técnica saves without closing the panel', async ({ page }) => {
  const mock = await fixture(page);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto('/administracion?seccion=inventario');
  await skuCell(page, disc.id).getByRole('button', { name: 'DF-4512' }).click();
  const editor = page.getByRole('dialog', { name: 'Línea OEM · DF-4512' });
  await expect(editor.getByRole('tab', { name: 'General', exact: true })).toHaveAttribute('aria-selected', 'true');
  await expect(editor.getByLabel('Descripción', { exact: true })).toBeVisible();
  await expect(editor.getByLabel('Subgrupo', { exact: true })).toHaveValue(grupos[0].subgrupos[0].id);
  await editor.getByRole('tab', { name: /^Referencias/ }).click();
  await expect(editor.getByRole('form', { name: 'Agregar referencia' })).toBeVisible();
  await expect(editor.getByLabel('Descripción', { exact: true })).toBeHidden();

  await editor.getByRole('tab', { name: 'Ficha técnica', exact: true }).click();
  await expect(editor.getByRole('button', { name: 'Guardar línea OEM', exact: true })).toBeHidden();  // the ficha saves with its own button
  await editor.getByLabel('DIÁMETRO (mm) · NECESARIO', { exact: true }).fill('280');
  await editor.getByRole('button', { name: 'Guardar ficha técnica', exact: true }).click();
  await expect(editor.getByRole('status').filter({ hasText: 'Ficha técnica y aplicaciones guardadas.' })).toBeVisible();
  await expect(editor).toBeVisible();
  expect(mock.sheets).toEqual([{ revision: 3, part_type: partType, template_revision: 2, values: { diameter: { value: '280' } }, applications: [] }]);
  await editor.getByRole('tab', { name: 'General', exact: true }).click();  // back and forth keeps what was typed
  await editor.getByRole('tab', { name: 'Ficha técnica', exact: true }).click();
  await expect(editor.getByLabel('DIÁMETRO (mm) · NECESARIO', { exact: true })).toHaveValue('280');
  await page.screenshot({ path: '/tmp/motionpartes-sku-editor-tabs.png' });
  await editor.getByRole('button', { name: 'Cerrar ventana' }).click();

  await page.getByRole('button', { name: 'Ficha técnica de BOMBA-9', exact: true }).click();  // the grid's Ficha opens the same panel on its tab
  const opened = page.getByRole('dialog', { name: 'Línea OEM · BOMBA-9' });
  await expect(opened.getByRole('tab', { name: 'Ficha técnica', exact: true })).toHaveAttribute('aria-selected', 'true');
  expect(mock.unexpected).toEqual([]);
});

test('Crear SKU has no References or Ficha técnica yet, and needs its codigo', async ({ page }) => {
  const mock = await fixture(page);
  const creates: unknown[] = [];
  await page.route(/\/api\/management\/catalog(?:\?.*)?$/, route => { creates.push(route.request().postDataJSON()); return route.fulfill({ status: 400, json: { sku: ['Este SKU o código ya identifica otro repuesto del catálogo.'] } }); });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto('/administracion?seccion=inventario');
  await page.getByRole('button', { name: 'Crear SKU', exact: true }).click();
  const editor = page.getByRole('dialog', { name: 'Crear SKU' });
  await expect(editor.getByRole('tab', { name: 'Ficha técnica', exact: true })).toBeDisabled();
  await expect(editor.getByRole('tab', { name: 'Referencias', exact: true })).toBeDisabled();
  await editor.getByRole('button', { name: 'Crear SKU', exact: true }).click();  // the browser asks for the codigo first
  expect(creates).toEqual([]);
  await editor.getByLabel('SKU principal (MAIN)').fill('df-4512');
  await editor.getByRole('button', { name: 'Crear SKU', exact: true }).click();
  await expect(editor.getByRole('alert')).toHaveText('SKU interno: Este SKU o código ya identifica otro repuesto del catálogo.');
  expect(creates).toEqual([{ sku: 'DF-4512', description: '', active: true, codes: [] }]);  // S10: no name (a line's name is its description)
  expect(mock.unexpected).toEqual([]);
});
