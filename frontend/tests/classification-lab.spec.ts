import { expect, test } from '@playwright/test';
import { makeLine, mockLines } from './lines-route';

test('classification research is reviewable and cannot write references', async ({ page }) => {
  const line = makeLine(1, '006Y0-11-SJ0-K', 'AXIALES KIA K2700');
  const mock = await mockLines(page, { lines: [line] });
  await page.route('**/api/management/catalog/company-suffixes', route => route.fulfill({ json: { suffixes: {} } }));
  await page.route(`**/api/management/catalog/${line.id}/oem-lookup`, route => route.fulfill({ json: {
    references: [{ code: 'TEST-OEM', brand: 'KIA', relationship: 'uncertain', source_url: 'https://example.com/catalog', reason: 'EVIDENCIA DE PRUEBA' }],
    note: 'RESULTADO SIMULADO', cached: true, metrics: {}, sources: [], search_suggestions: '',
  } }));
  await page.goto('/administracion?seccion=inventario');
  await page.locator(`[row-id="${line.id}"] [col-id="tipo"]`).hover();
  await page.getByRole('button', { name: `Clasificar con IA ${line.sku}`, exact: true }).click();
  const panel = page.getByRole('dialog', { name: 'Clasificación', exact: true });
  await expect(panel).toContainText('Desconocido significa aún no confirmado.');
  await expect(panel.getByLabel('Código a investigar')).toHaveValue(line.sku);
  await expect(panel.getByLabel('Código a investigar')).toHaveValue(line.sku);
  await panel.getByRole('button', { name: 'Buscar OEM', exact: true }).click();
  await expect(panel).toContainText('EVIDENCIA DE PRUEBA');
  await expect(panel.getByRole('checkbox', { name: 'Incluir OEM TEST-OEM', exact: true })).toBeDisabled();
  await expect(panel.getByRole('checkbox', { name: 'Seleccionar todo', exact: true })).toBeDisabled();
  await expect(panel.getByRole('button', { name: /Agregar.*referencias/ })).toHaveCount(0);
  expect(mock.calls.filter(call => !call.startsWith('GET'))).toEqual([]);
  expect(mock.unexpected).toEqual([]);
});

for (const merge of [false, true]) {
  test(`apply an explicitly reviewed OEM with merge=${merge}`, async ({ page }) => {
    const line = makeLine(2, 'CODE-K', 'AXIAL KIA', { tipo: 'AFTERMARKET', main: { id: 200, code: 'CODE-K', brand: 'KFM', tipo: 'aftermarket', estado: 'verificado' } });
    await mockLines(page, { lines: [line] });
    const finalCode = merge ? 'EXISTING-MAIN' : 'OEM-TEST';
    const actionName = 'Guardar clasificación';
    const owners = merge ? [{ id: makeLine(3, '', '').id, sku: finalCode, description: 'AXIAL EXISTENTE', estado: 'publicada', main: { code: finalCode, brand: 'KIA', tipo: 'oem', estado: 'verificado' }, references: [{ code: 'OEM-TEST', brand: 'KIA', is_main: false }] }] : [];
    await page.route('**/api/management/catalog/company-suffixes', route => route.fulfill({ json: { suffixes: {} } }));
    await page.route(`**/api/management/catalog/${line.id}/oem-lookup`, route => route.fulfill({ json: {
      references: [{ code: 'OEM-TEST', brand: 'KIA', relationship: 'equivalent', source_url: 'https://example.com/catalog', reason: 'MISMA PIEZA · RESULTADO SIMULADO', catalog_matches: owners }, { code: 'OEM-SECOND', brand: 'KIA', relationship: 'equivalent', source_url: 'https://example.com/second', reason: 'SEGUNDA EQUIVALENCIA VERIFICADA', catalog_matches: [] }],
      research_token: 'test-research', note: 'RESULTADO SIMULADO', cached: true, metrics: {}, sources: [], search_suggestions: '',
    } }));
    const applications: object[] = [];
    const updatedLine = { ...line, id: merge ? makeLine(3, '', '').id : line.id, sku: finalCode, tipo: 'OEM' as const, version: line.version + 1,
      main: { id: 9000, code: finalCode, brand: 'KIA', tipo: 'oem' as const, estado: 'verificado' as const } };
    await page.route('**/api/management/lines?**', route => applications.length
      ? route.fulfill({ json: { count: 1, results: [updatedLine], next: null, previous: null } }) : route.fallback());
    await page.route(`**/api/management/lines/${line.id}/oem-classification`, async route => {
      const body = route.request().postDataJSON();
      if (body.preview_token) {
        applications.push(body);
        await route.fulfill({ json: { applied: true, line: updatedLine } });
      } else await route.fulfill({ json: { preview_token: 'test-preview', merge, oem: finalCode, brand: 'KIA', preserve_main: merge, selected_oem: 'OEM-TEST', selected_brand: 'KIA', selected_oems: [{ code: 'OEM-TEST', brand: 'KIA', source_url: 'https://example.com/catalog', already_registered: merge }, { code: 'OEM-SECOND', brand: 'KIA', source_url: 'https://example.com/second', already_registered: false }], matched_equivalences: [{ code: 'OEM-TEST', brand: 'KIA', catalog_matches: owners }], previous_code: line.sku,
        source: { id: line.id, sku: line.sku, description: line.description, items: 2, images: 1 },
        target: { id: merge ? 'other' : line.id, sku: merge ? 'DESTINO-TEST' : line.sku, description: 'DESTINO AXIAL', items: 3, images: 2 } } });
    });
    await page.goto('/administracion?seccion=inventario');
    await page.locator(`[row-id="${line.id}"] [col-id="tipo"]`).hover();
  await page.getByRole('button', { name: `Clasificar con IA ${line.sku}`, exact: true }).click();
    const panel = page.getByRole('dialog', { name: 'Clasificación', exact: true });
    await expect(panel.getByLabel('Código a investigar')).toHaveValue(line.sku);
    await panel.getByRole('button', { name: 'Buscar OEM', exact: true }).click();
    await expect(panel.getByRole('button', { name: 'Revisar selección', exact: true })).toBeDisabled();
    await panel.getByRole('checkbox', { name: 'Incluir OEM OEM-TEST', exact: true }).check();
    await panel.getByRole('checkbox', { name: 'Incluir OEM OEM-SECOND', exact: true }).check();
    if (!merge) await panel.getByRole('radio', { name: 'Usar como principal', exact: true }).first().check();
    if (merge) await expect(panel).toContainText('Ya registrado · EXISTING-MAIN');
    expect(await panel.evaluate(element => element.scrollWidth > element.clientWidth)).toBe(false);
    await panel.screenshot({ path: `/tmp/oratek-classification-results-${merge}.png` });
    await panel.getByRole('button', { name: 'Revisar selección', exact: true }).click();
    const review = panel.getByRole('region', { name: 'Aplicar clasificación OEM' });
    await expect(review.getByRole('button', { name: actionName })).toBeDisabled();
    if (merge) await expect(review).toContainText('Se unificarán dos fichas.');
    expect(applications).toEqual([]);
    await review.getByRole('checkbox').check();
    await review.getByRole('button', { name: actionName }).click();
    await expect(panel.getByRole('status').filter({ hasText: 'Clasificación guardada' })).toBeVisible();
    expect(applications).toHaveLength(1);
    await panel.getByRole('button', { name: 'Cerrar ventana' }).click();
    await expect(page.getByRole('button', { name: `Editar línea OEM ${finalCode}`, exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: `Editar línea OEM ${line.sku}`, exact: true })).toHaveCount(0);
    await expect(page.getByRole('button', { name: `Clasificar con IA ${finalCode}` })).toHaveCount(0);
    expect(applications[0]).toMatchObject({ selections: [{ code: 'OEM-TEST', brand: 'KIA' }, { code: 'OEM-SECOND', brand: 'KIA' }], research_token: 'test-research', preview_token: 'test-preview', confirmed: true, code: 'OEM-TEST', expected_version: line.version });
  });
}

for (const tipo of ['UNKNOWN', 'AFTERMARKET'] as const) for (const touch of [false, true]) {
  test(`contextual entry opens the selected row without AI calls, tipo=${tipo}, touch=${touch}`, async ({ page }) => {
    await page.setViewportSize({ width: touch ? 390 : 1440, height: 1000 });
    const line = makeLine(4, 'CONTEXT-TEST', 'PIEZA PARA CLASIFICAR', { tipo, main: { id: 400, code: 'CONTEXT-TEST', brand: '', tipo: tipo === 'AFTERMARKET' ? 'aftermarket' : 'otro', estado: 'declarado' } });
    await mockLines(page, { lines: [line] });
    let lookups = 0;
    await page.route('**/api/management/catalog/*/oem-lookup', route => { lookups++; return route.fulfill({ json: {} }); });
    await page.goto('/administracion?seccion=inventario');
    await expect(page.getByRole('button', { name: 'Clasificación', exact: true })).toHaveCount(0);
    const cell = page.locator(`[row-id="${line.id}"] [col-id="tipo"]`);
    const button = page.getByRole('button', { name: `Clasificar con IA ${line.sku}`, exact: true });
    await expect(button).toBeAttached();
    if (touch) {
      await button.scrollIntoViewIfNeeded();
      await expect(button).toHaveCSS('opacity', '1');
      await button.click();
    } else {
      await page.getByRole('heading', { name: 'Inventario', exact: true }).hover();
      await expect(button).toHaveCSS('opacity', '0');
      await cell.hover(); await expect(button).toHaveCSS('opacity', '1');
      await cell.focus(); await expect(button).toHaveCSS('opacity', '1');
      await page.keyboard.press('Enter');
    }
    const panel = page.getByRole('dialog', { name: 'Clasificación', exact: true });
    await expect(panel.getByRole('heading', { name: line.sku, exact: true })).toBeVisible();
    await expect(panel.getByLabel('Código a investigar')).toHaveValue(line.sku);
    expect(lookups).toBe(0);
    await expect(page.getByRole('dialog')).toHaveCount(1);
  });
}

test('conflicting live catalog owners are shown and block reviewing proposals', async ({ page }) => {
  const line = makeLine(8, 'CONFLICT-TEST', 'PIEZA PARA REVISAR');
  await mockLines(page, { lines: [line] });
  await page.route('**/api/management/catalog/company-suffixes', route => route.fulfill({ json: { suffixes: {} } }));
  await page.route(`**/api/management/catalog/${line.id}/oem-lookup`, route => route.fulfill({ json: {
    catalog_conflict: true, research_token: 'test-research', cached: true, note: '', metrics: {}, sources: [], search_suggestions: '',
    references: ['OEM-A', 'OEM-B'].map((code, index) => ({ code, brand: 'KIA', relationship: 'equivalent', reason: 'EVIDENCIA SIMULADA', source_url: 'https://example.com/catalog',
      catalog_matches: [{ id: String(index), sku: code, description: `FICHA ${index}`, estado: 'publicada', main: { code, brand: 'KIA', tipo: 'oem', estado: 'verificado' }, references: [{ code, brand: 'KIA', is_main: true }] }] })),
  } }));
  await page.goto('/administracion?seccion=inventario');
  await page.locator(`[row-id="${line.id}"] [col-id="tipo"]`).hover();
  await page.getByRole('button', { name: `Clasificar con IA ${line.sku}`, exact: true }).click();
  const panel = page.getByRole('dialog', { name: 'Clasificación', exact: true });
  await panel.getByRole('button', { name: 'Buscar OEM', exact: true }).click();
  await expect(panel.getByRole('alert')).toContainText('fichas distintas');
  await expect(panel).toContainText('Ya registrado · OEM-A');
  await expect(panel).toContainText('Ya registrado · OEM-B');
  await expect(panel.getByRole('checkbox', { name: 'Seleccionar todo', exact: true })).toBeDisabled();
  await expect(panel.getByRole('checkbox', { name: 'Incluir OEM OEM-A', exact: true })).toBeDisabled();
  await expect(panel.getByRole('checkbox', { name: 'Incluir OEM OEM-B', exact: true })).toBeDisabled();
  await expect(panel.getByRole('button', { name: 'Revisar selección', exact: true })).toBeDisabled();
  await expect(panel.getByRole('region', { name: 'Aplicar clasificación OEM' })).toHaveCount(0);
});

for (const width of [1440, 390]) {
  test(`OEM and aftermarket stay in distinct panels at ${width}px`, async ({ page }) => {
    const { makeReference } = await import('./lines-route');
    await page.setViewportSize({ width, height: width === 1440 ? 1400 : 1000 });
    const line = makeLine(9, 'AFTERMARKET-ORIGEN', 'AXIAL KIA · FICHA DE PRUEBA', { tipo: 'AFTERMARKET', main: { id: 900, code: 'AFTERMARKET-ORIGEN', brand: 'KFM', tipo: 'aftermarket', estado: 'verificado' } });
    await mockLines(page, { lines: [line], references: [
      makeReference(901, line, 'OEM-ALTERNO', { brand: 'KIA', tipo: 'oem', estado: 'verificado' }),
      makeReference(902, line, 'OTRO-AFTERMARKET', { brand: 'GMB', tipo: 'aftermarket', estado: 'verificado' }),
    ] });
    await page.goto('/administracion?seccion=inventario');
    const button = page.getByRole('button', { name: `Clasificar con IA ${line.sku}`, exact: true });
    await button.scrollIntoViewIfNeeded();
    await page.locator(`[row-id="${line.id}"] [col-id="tipo"]`).hover();
    await button.click();
    const dialog = page.getByRole('dialog', { name: 'Clasificación', exact: true });
    const oem = dialog.getByRole('region', { name: 'OEM', exact: true });
    const aftermarket = dialog.getByRole('region', { name: 'Referencias aftermarket', exact: true });
    await expect(oem.getByRole('table', { name: 'Referencias OEM', exact: true })).toBeVisible();
    await expect(aftermarket.getByRole('table', { name: 'Referencias aftermarket', exact: true })).toBeVisible();
    await expect(oem).toContainText('OEM-ALTERNO');
    await expect(aftermarket).toContainText('AFTERMARKET-ORIGEN');
    await expect(aftermarket).toContainText('OTRO-AFTERMARKET');
    await expect(aftermarket).not.toContainText('OEM-ALTERNO');
    await expect(oem).not.toContainText('OTRO-AFTERMARKET');
    const a = await oem.boundingBox(), b = await aftermarket.boundingBox();
    if (width === 1440) { expect(a!.x).toBeLessThan(b!.x); expect(Math.abs(a!.y - b!.y)).toBeLessThan(2); }
    else { expect(b!.y).toBeGreaterThan(a!.y); expect(Math.abs(a!.x - b!.x)).toBeLessThan(2); }
    await dialog.screenshot({ path: `/tmp/oratek-classification-panels-${width}.png` });
    expect(await dialog.evaluate(element => element.scrollWidth > element.clientWidth)).toBe(false);
  });
}

test('select all only includes eligible OEMs, reflects partial selection and resets review', async ({ page }) => {
  const line = makeLine(11, 'SELECT-ALL-TEST', 'PIEZA DE PRUEBA');
  await mockLines(page, { lines: [line] });
  let searches = 0, previews = 0, writes = 0;
  await page.route(`**/api/management/catalog/${line.id}/oem-lookup`, route => {
    searches++;
    return route.fulfill({ json: { research_token: 'test-research', cached: true, note: '', sources: [], references:
      ['equivalent', 'equivalent', 'component', 'uncertain'].map((relationship, i) => ({ code: `OEM-${i}`, brand: 'KIA', relationship, source_url: 'https://example.com/catalog', reason: 'EVIDENCIA SIMULADA' })) } });
  });
  await page.route(`**/api/management/lines/${line.id}/oem-classification`, route => {
    const body = route.request().postDataJSON();
    if (body.preview_token) writes++; else previews++;
    expect(body.selections).toEqual([{ code: 'OEM-0', brand: 'KIA' }, { code: 'OEM-1', brand: 'KIA' }]);
    expect(body.code).toBe('OEM-1');
    return route.fulfill({ json: { preview_token: 'test-preview', merge: false, oem: 'OEM-1', brand: 'KIA', previous_code: line.sku, source: { items: 0, images: 0 }, target: {} } });
  });
  await page.goto('/administracion?seccion=inventario');
  await page.locator(`[row-id="${line.id}"] [col-id="tipo"]`).hover();
  await page.getByRole('button', { name: `Clasificar con IA ${line.sku}`, exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Clasificación', exact: true });
  const all = dialog.getByRole('checkbox', { name: 'Seleccionar todo', exact: true });
  await expect(all).toBeDisabled();
  await dialog.getByRole('button', { name: 'Buscar OEM', exact: true }).click();
  await all.check();
  await expect(all).toBeChecked();
  await expect(dialog.getByRole('checkbox', { name: 'Incluir OEM OEM-2', exact: true })).not.toBeChecked();
  await expect(dialog.getByRole('checkbox', { name: 'Incluir OEM OEM-2', exact: true })).toBeDisabled();
  await expect(dialog.getByRole('checkbox', { name: 'Incluir OEM OEM-3', exact: true })).not.toBeChecked();
  await expect(dialog.getByRole('button', { name: 'Revisar selección', exact: true })).toBeDisabled();
  await dialog.getByRole('checkbox', { name: 'Incluir OEM OEM-0', exact: true }).uncheck();
  await expect(all).toBeChecked({ indeterminate: true });
  await dialog.getByRole('radio', { name: 'Usar como principal', exact: true }).check();
  await all.check();
  await expect(dialog.getByRole('radio', { name: 'Usar como principal', exact: true }).nth(1)).toBeChecked();
  expect(searches).toBe(1); expect(previews).toBe(0); expect(writes).toBe(0);
  await dialog.getByRole('button', { name: 'Revisar selección', exact: true }).click();
  await expect(dialog.getByRole('region', { name: 'Aplicar clasificación OEM' }).getByRole('button', { name: 'Guardar clasificación', exact: true })).toBeVisible();
  await all.uncheck();
  await expect(dialog.getByRole('region', { name: 'Aplicar clasificación OEM' })).toHaveCount(0);
  await expect(dialog).toContainText('0 OEM seleccionadas');
  await all.check();
  await expect(dialog.getByRole('radio', { name: 'Usar como principal', exact: true }).first()).not.toBeChecked();
  await expect(dialog.getByRole('radio', { name: 'Usar como principal', exact: true }).nth(1)).not.toBeChecked();
  expect(searches).toBe(1); expect(previews).toBe(1); expect(writes).toBe(0);
});
