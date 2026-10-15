import { expect, type Page, test } from '@playwright/test';
import { makeLine, makeReference, mockLines } from './lines-route';

// The line panel's References editor (plan S8.3), which replaces the Alternos tab, "Equivalencias por número OEM" and the "EL SKU PRINCIPAL
// ES OEM" checkbox: each Reference with its tipo, estado, brand and sources, written one row at a time with its own version; the main
// chosen by its radio (the SKU and the line's tipo follow it); a retired Reference kept in the history; the OEM lookup adding OEM ones.
const line = makeLine(1, 'RN11002V', 'DISCO DELANTERO HYUNDAI ACCENT 256 MM', { variant_count: 2, main: { id: 100, code: 'RN11002V', brand: 'RENEW', tipo: 'aftermarket', estado: 'declarado' } });
const oem = makeReference(101, line, '51712-1R000', { brand: 'HYUNDAI', tipo: 'oem', estado: 'verificado', variant_count: 1,
  sources: [{ kind: 'catalogo', citation: 'ASVA CV 2024 p. 12', marks: ['*'], retirada: false, created_at: '2026-10-01T00:00:00Z' }, { kind: 'proveedor', citation: 'REPUESTOS DEL ISTMO', marks: [], retirada: false, created_at: '2026-10-02T00:00:00Z' }] });
const interno = makeReference(102, line, 'RDI-4471', { tipo: 'interno', supplier: { id: 'acc-1', name: 'REPUESTOS DEL ISTMO' } });
const gone = makeReference(103, line, '51712-1R001', { brand: 'HYUNDAI', tipo: 'oem', estado: 'retirada' });

async function open(page: Page, width: number) {
  const mock = await mockLines(page, { lines: [line], references: [oem, interno, gone] });
  await page.route('**/api/management/catalog/company-suffixes', route => route.fulfill({ json: { suffixes: [] } }));
  await page.setViewportSize({ width, height: 1000 });
  await page.goto('/administracion?seccion=inventario');
  await page.getByRole('button', { name: `Editar línea OEM ${line.sku}`, exact: true }).click();
  const panel = page.getByRole('dialog', { name: `Línea OEM · ${line.sku}` });
  await panel.getByRole('tab', { name: /^Referencias/ }).click();
  return { mock, panel };
}

for (const width of [1440, 390]) {
  test(`each Reference shows its tipo, estado, brand and sources, and a retired one stays in the history at ${width}px`, async ({ page }) => {
    const { mock, panel } = await open(page, width);
    const list = panel.getByRole('list', { name: 'Referencias de la línea OEM' });
    const main = list.getByRole('listitem', { name: `Referencia ${line.sku}` });
    await expect(main).toContainText('PRINCIPAL');
    await expect(main).toContainText('AFTERMARKET · RENEW');
    await expect(main.getByRole('radio')).toBeChecked();
    await expect(main.getByRole('button', { name: `Retirar referencia ${line.sku}` })).toHaveCount(0);  // the main is never retired here
    const number = list.getByRole('listitem', { name: 'Referencia 51712-1R000' });
    await expect(number).toContainText('OEM · HYUNDAI');
    await expect(number).toContainText('VERIFICADA');
    await expect(number).toContainText('CATÁLOGO DE MARCA · ASVA CV 2024 p. 12');
    await expect(number).toContainText('MARCADO CON *');
    await expect(number).toContainText('PROVEEDOR · REPUESTOS DEL ISTMO');
    await expect(number).toContainText('1 artículo');
    const own = list.getByRole('listitem', { name: 'Referencia RDI-4471' });
    await expect(own).toContainText('INTERNO DE REPUESTOS DEL ISTMO');
    await expect(own.getByRole('radio')).toBeDisabled();  // a supplier's interno code never names the line
    await expect(list.getByRole('listitem', { name: 'Referencia 51712-1R001' })).toHaveCount(0);
    await panel.getByText('Retiradas (1)').click();
    await expect(panel.getByRole('listitem', { name: 'Referencia retirada 51712-1R001' })).toContainText('RETIRADA');
    await expect(panel.getByRole('checkbox', { name: 'EL SKU PRINCIPAL ES OEM' })).toHaveCount(0);
    await expect(panel.getByText('Equivalencias por número OEM')).toHaveCount(0);
    expect(await panel.evaluate(element => element.scrollWidth > element.clientWidth)).toBe(false);
    await page.screenshot({ path: `/tmp/motionpartes-references-${width}.png` });
    expect(mock.unexpected).toEqual([]);
  });
}

test('a Reference is added, edited with its version, made main and retired, one row at a time', async ({ page }) => {
  const { mock, panel } = await open(page, 1440);
  const writes = () => mock.calls.filter(call => !call.startsWith('GET'));
  await panel.getByLabel('Código de la nueva referencia').fill('51712-0u000');
  await panel.getByLabel('Marca de la nueva referencia').fill('kia');
  await panel.getByLabel('Tipo de la nueva referencia').selectOption('oem');
  await panel.getByLabel('Fuente de la nueva referencia').fill('https://example.invalid/Catalog');
  await panel.getByRole('button', { name: 'Agregar referencia', exact: true }).click();
  await expect(panel.getByRole('status')).toHaveText('51712-0U000 agregada.');
  await expect(panel.getByRole('listitem', { name: 'Referencia 51712-0U000' })).toContainText('ADMINISTRADOR · https://example.invalid/Catalog');
  expect(writes()).toEqual([`POST lines/${line.id}/references {"code":"51712-0U000","brand":"KIA","tipo":"oem","estado":"declarado","source":"https://example.invalid/Catalog"}`]);
  await expect(panel.getByRole('tab', { name: /^Referencias/ })).toContainText('4');  // the line was read again: its count follows

  // Edit: only what changed is sent, with the Reference's own version; disputa needs its reason.
  await panel.getByRole('button', { name: 'Editar referencia 51712-1R000' }).click();
  await panel.getByLabel('Estado de 51712-1R000').selectOption('disputa');
  await expect(panel.getByRole('button', { name: 'Guardar referencia' })).toBeDisabled();
  await panel.getByLabel('Motivo de la disputa de 51712-1R000').fill('El catálogo lo da para el modelo 2012');
  await panel.getByRole('button', { name: 'Guardar referencia' }).click();
  await expect(panel.getByRole('listitem', { name: 'Referencia 51712-1R000' })).toContainText('EN DISPUTA: El catálogo lo da para el modelo 2012');
  expect(writes()[1]).toBe('PATCH references/101 {"estado":"disputa","dispute_note":"El catálogo lo da para el modelo 2012","expected_version":1}');

  // Someone changed it meanwhile: refused, and the list shows it as it is now.
  mock.refs.get(101)!.version = 5;
  mock.refs.get(101)!.notes = 'REVISADO POR OTRA PERSONA';
  await panel.getByRole('button', { name: 'Editar referencia 51712-1R000' }).click();
  await panel.getByLabel('Notas de 51712-1R000').fill('mi nota');
  await panel.getByRole('button', { name: 'Guardar referencia' }).click();
  await expect(panel.getByRole('alert')).toHaveText('La referencia cambió mientras la editabas: vuelve a cargarla.');
  await expect(panel.getByRole('listitem', { name: 'Referencia 51712-1R000' })).toContainText('REVISADO POR OTRA PERSONA');

  // The main: the SKU and the line's tipo follow it.
  await panel.getByRole('radio', { name: 'Referencia principal 51712-0U000' }).click();
  const renamed = page.getByRole('dialog', { name: 'Línea OEM · 51712-0U000' });
  await expect(renamed.getByRole('status')).toContainText('51712-0U000 es ahora la referencia principal: el SKU de la línea OEM pasa a ser 51712-0U000.');
  await expect(renamed.getByRole('radio', { name: 'Referencia principal 51712-0U000' })).toBeChecked();
  expect(writes()).toContain(`POST lines/${line.id}/main {"reference":9001,"expected_version":1}`);
  await expect(page.locator(`.ag-row[row-id="${line.id}"] .ag-cell[col-id="sku"]`).first()).toContainText('OEM · KIA');

  // Retire: asked first, then kept in the history.
  await renamed.getByRole('button', { name: `Retirar referencia ${line.sku}` }).click();
  await expect(renamed.getByText(`¿Retirar ${line.sku}?`)).toBeVisible();
  await renamed.getByRole('button', { name: 'Confirmar retiro' }).click();
  await expect(renamed.getByRole('status')).toHaveText(`Referencia ${line.sku} retirada. Se conserva en el historial de la línea OEM.`);
  await expect(renamed.getByText('Retiradas (2)')).toBeVisible();
  expect(writes().at(-1)).toBe('DELETE references/100');
  expect(mock.unexpected).toEqual([]);
});

test('the OEM lookup adds the reviewed numbers as OEM References of the line', async ({ page }) => {
  const { mock, panel } = await open(page, 1440);
  await page.route(`**/api/management/catalog/${line.id}/oem-lookup`, route => route.fulfill({ json: { cached: true, metrics: { total_tokens: 0 }, note: 'REVISAR FUENTES',
    sources: [{ url: 'https://example.com/catalog', title: 'CATÁLOGO' }], search_suggestions: '', references: [
      { brand: 'HYUNDAI', code: '51712-1R050', relationship: 'equivalent', source_url: 'https://example.com/catalog', reason: 'Mismo disco', reference_source: 'https://example.com/catalog' },
      { brand: 'HYUNDAI', code: '51712-ASSY', relationship: 'component', source_url: 'https://example.com/catalog', reason: 'Conjunto', reference_source: 'https://example.com/catalog' }] } }));
  await panel.getByText('Buscar referencias OEM con IA', { exact: true }).click();
  await panel.getByRole('button', { name: 'Buscar OEM', exact: true }).click();
  await expect(panel.getByText('BÚSQUEDA GUARDADA · SIN NUEVA LLAMADA A IA')).toBeVisible();
  await expect(panel.getByRole('checkbox', { name: '51712-ASSY · HYUNDAI', exact: true })).toBeDisabled();
  await panel.getByRole('checkbox', { name: '51712-1R050 · HYUNDAI', exact: true }).check();
  await panel.getByRole('checkbox', { name: /Verifiqué en las fuentes/ }).check();
  await panel.getByRole('button', { name: /Agregar .* referencias revisadas al formulario/ }).click();
  await expect(panel.getByRole('listitem', { name: 'Referencia 51712-1R050' })).toContainText('OEM · HYUNDAI');
  expect(mock.calls).toContain(`POST lines/${line.id}/references {"code":"51712-1R050","brand":"HYUNDAI","tipo":"oem","estado":"declarado","source":"https://example.com/catalog"}`);
  expect(mock.unexpected).toEqual([]);
});
