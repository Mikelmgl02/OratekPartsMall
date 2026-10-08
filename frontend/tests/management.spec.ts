import { test, expect, Page } from '@playwright/test';
import { excelFixture } from './excel-fixture';

async function signIn(page: Page, username: string, password: string) {
  await page.goto('/');
  await page.getByRole('button', { name: 'Cuenta y listas', exact: true }).click();
  await page.getByRole('button', { name: 'Iniciar sesión', exact: true }).first().click();
  await page.getByLabel('Usuario', { exact: true }).fill(username);
  await page.getByLabel('Contraseña', { exact: true }).fill(password);
  await page.getByRole('dialog').getByRole('button', { name: 'Iniciar sesión', exact: true }).last().click();
  await expect(page.getByText('Cuenta conectada', { exact: true })).toBeVisible();
}

test('administration requires sign-in and management endpoints reject anonymous requests', async ({ page, request }) => {
  await page.goto('/administracion');
  await expect(page.getByRole('heading', { name: 'Acceso exclusivo para superusuarios.' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Crear cuenta', exact: true })).not.toBeVisible();
  expect((await request.get('/api/management/users')).status()).toBe(401);
  expect((await request.post('/api/management/accounts', { data: { name: 'Denied' } })).status()).toBe(401);
  expect((await request.post('/api/management/catalog', { data: { name: 'Denied', brand: 'TEST' } })).status()).toBe(401);
  expect((await request.post('/api/management/catalog/import', { data: {} })).status()).toBe(401);
  const job = '/api/management/catalog/import/jobs/00000000-0000-0000-0000-000000000000';
  expect((await request.get(job)).status()).toBe(401);
  expect((await request.post(job, { data: { batch_index: 0 } })).status()).toBe(401);
  expect((await request.get(`${job}/classification`)).status()).toBe(401);
  expect((await request.post(`${job}/classification`, { data: { mode: 'classify', batch_index: 0 } })).status()).toBe(401);
});

test('ordinary signed-in user cannot open or write to administration', async ({ page }) => {
  test.skip(!process.env.E2E_USERNAME || !process.env.E2E_PASSWORD, 'A disposable ordinary-user fixture is required.');
  await signIn(page, process.env.E2E_USERNAME!, process.env.E2E_PASSWORD!);
  await expect(page.getByRole('link', { name: 'Administración', exact: true })).not.toBeVisible();
  await page.goto('/administracion');
  await expect(page.getByRole('heading', { name: 'Acceso exclusivo para superusuarios.' })).toBeVisible();
  expect((await page.request.get('/api/management/users')).status()).toBe(403);
  const blocked = await page.request.post('/api/management/accounts', { headers: { Origin: new URL(page.url()).origin }, data: { name: 'Denied' } });
  expect(blocked.status()).toBe(403);
  for (const route of ['catalog', 'alternates', 'catalog/import']) {
    expect((await page.request.post(`/api/management/${route}`, { headers: { Origin: new URL(page.url()).origin }, data: { name: 'Denied', brand: 'TEST' } })).status()).toBe(403);
  }
  const job = '/api/management/catalog/import/jobs/00000000-0000-0000-0000-000000000000';
  expect((await page.request.get(job)).status()).toBe(403);
  expect((await page.request.post(job, { headers: { Origin: new URL(page.url()).origin }, data: { batch_index: 0 } })).status()).toBe(403);
  expect((await page.request.get(`${job}/classification`)).status()).toBe(403);
  expect((await page.request.post(`${job}/classification`, { headers: { Origin: new URL(page.url()).origin }, data: { mode: 'classify', batch_index: 0 } })).status()).toBe(403);
});

test('superuser creates catalog codes, reviews stock, and manages alternos inside the panel', async ({ page, browser }) => {
  test.skip(!process.env.E2E_ADMIN_USERNAME || !process.env.E2E_ADMIN_PASSWORD || !process.env.E2E_USERNAME || !process.env.E2E_PASSWORD, 'Disposable superuser and supplier fixtures are required.');
  const partName = `${process.env.E2E_ADMIN_USERNAME}-catálogo`.toUpperCase();
  const code = `${process.env.E2E_ADMIN_USERNAME}-native`.toUpperCase();
  const alternateCode = `${code}-ALT`;
  const editedAlternateCode = `${code}-ALT-EDIT`;
  const stableId = `${process.env.E2E_ADMIN_USERNAME}-stable`;
  await signIn(page, process.env.E2E_ADMIN_USERNAME!, process.env.E2E_ADMIN_PASSWORD!);
  const origin = new URL(page.url()).origin;
  const supplierContext = await browser.newContext({ baseURL: origin });
  let itemId: string; let accountId: string; let ledgerBefore: unknown;
  try {
    const supplierPage = await supplierContext.newPage();
    await signIn(supplierPage, process.env.E2E_USERNAME!, process.env.E2E_PASSWORD!);
    const accounts = await (await supplierContext.request.get('/api/market/accounts')).json();
    accountId = accounts.results.find((account: { name: string }) => account.name === process.env.E2E_USERNAME).id;
    const stock = await supplierContext.request.post(`/api/market/accounts/${accountId}/inventory/ingest`, { headers: { Origin: origin }, data: { update_id: stableId, supplier_invent_id: stableId, codigo: code, brand: 'NATIVE-QA', source: 'upload', quantity: 7 } });
    expect(stock.status()).toBe(200);
    const item = await stock.json(); itemId = item.id;
    expect(item.matching_status).toBe('pending');
    ledgerBefore = await (await supplierContext.request.get(`/api/market/accounts/${accountId}/inventory/${itemId}/ledger`)).json();

    await page.goto('/administracion?seccion=inventario');
    expect(await page.locator('a[href^="/admin/"]').count()).toBe(0);
    expect((await page.request.post('/api/management/catalog', { headers: { Origin: 'https://unrelated.example' }, data: { name: 'Denied', brand: 'TEST' } })).status()).toBe(403);
    await page.getByRole('button', { name: 'Crear SKU', exact: true }).click();
    await page.screenshot({ path: '/tmp/motionpartes-native-catalog-desktop.png' });
    await page.getByLabel('SKU interno', { exact: true }).fill(partName.toLowerCase());
    await expect(page.getByLabel('Marca del repuesto', { exact: true })).not.toBeVisible();
    await page.getByLabel('Descripción', { exact: true }).fill('Filtro creado desde el panel');
    await page.getByLabel('Código 1', { exact: true }).fill(code.toLowerCase());
    await expect(page.getByLabel('SKU interno', { exact: true })).toHaveValue(partName);
    await expect(page.getByLabel('Descripción', { exact: true })).toHaveValue('FILTRO CREADO DESDE EL PANEL');
    await expect(page.getByLabel('Código 1', { exact: true })).toHaveValue(code);
    await page.getByRole('button', { name: 'Agregar código', exact: true }).click();
    await page.getByLabel('Marca del código 2', { exact: true }).fill('OTHER-QA');
    await page.getByLabel('Código 2', { exact: true }).fill('OTHER-CODE');
    await page.getByRole('button', { name: 'Guardar SKU', exact: true }).click();
    await expect(page.getByText('SKU y alternos guardados.', { exact: true })).toBeVisible();
    await page.getByLabel('Buscar repuestos').fill(partName);
    await page.getByRole('button', { name: `Editar SKU ${partName}`, exact: true }).click();
    await expect(page.getByLabel('Marca del código 1', { exact: true })).toHaveValue('');
    await expect(page.getByLabel('Código 1', { exact: true })).toHaveValue(code);
    await expect(page.getByLabel('Descripción', { exact: true })).toHaveValue('FILTRO CREADO DESDE EL PANEL');
    await page.getByRole('button', { name: 'Quitar código 2', exact: true }).click();
    await page.getByLabel('Descripción', { exact: true }).fill('Descripción actualizada');
    await page.getByRole('button', { name: 'Guardar SKU', exact: true }).click();
    await expect(page.getByRole('dialog')).not.toBeVisible();
    await page.reload();
    await expect(page.getByRole('region', { name: 'Inventario interno' }).getByText(partName, { exact: true })).toBeVisible();

    await page.getByRole('button', { name: 'Crear SKU', exact: true }).click();
    await page.getByLabel('SKU interno', { exact: true }).fill('Duplicate should not be saved');
    await page.getByLabel('Código 1', { exact: true }).fill(code);
    await page.getByRole('button', { name: 'Guardar SKU', exact: true }).click();
    await expect(page.getByRole('dialog').getByRole('alert')).toContainText('ya pertenece a otro SKU');
    await page.getByRole('button', { name: 'Cerrar ventana', exact: true }).click();

    await page.getByRole('button', { name: 'Existencias por proveedor', exact: true }).click();
    await page.getByLabel('Buscar existencias').fill(stableId);
    await page.getByRole('button', { name: `Revisar coincidencia ${process.env.E2E_USERNAME} ${stableId}`, exact: true }).click();
    await page.getByLabel('Buscar SKU del catálogo', { exact: true }).fill(partName);
    await expect(page.getByRole('combobox', { name: 'SKU del catálogo', exact: true }).locator('option')).toHaveCount(2);
    await page.getByRole('combobox', { name: 'SKU del catálogo', exact: true }).selectOption({ label: partName });
    await page.getByRole('combobox', { name: 'Estado de coincidencia', exact: true }).selectOption('matched');
    await page.getByRole('button', { name: 'Guardar coincidencia', exact: true }).click();
    const stockRow = page.getByRole('row').filter({ has: page.getByRole('gridcell', { name: stableId, exact: true }) });
    await expect(stockRow.getByText('Vinculado', { exact: true })).toBeVisible();
    await expect(stockRow.getByText(partName, { exact: true })).toBeVisible();
    const currentStock = await (await supplierContext.request.get(`/api/market/accounts/${accountId}/inventory?search=${stableId}`)).json();
    expect(currentStock.results[0].id).toBe(itemId);
    expect(currentStock.results[0].reported_quantity).toBe(7);
    expect(await (await supplierContext.request.get(`/api/market/accounts/${accountId}/inventory/${itemId}/ledger`)).json()).toEqual(ledgerBefore);

    await page.getByRole('link', { name: 'Alternos', exact: true }).click();
    await page.getByLabel('Buscar alternos').fill(partName);
    // The code created in the SKU editor is already in the alternos library.
    await expect(page.getByRole('region', { name: 'Alternos' }).getByText(code, { exact: true })).toBeVisible();
    await page.getByRole('button', { name: 'Crear alterno', exact: true }).click();
    await page.getByLabel('Buscar SKU interno', { exact: true }).fill(partName);
    await expect(page.getByRole('combobox', { name: 'SKU interno', exact: true }).locator('option')).toHaveCount(2);
    await page.getByRole('combobox', { name: 'SKU interno', exact: true }).selectOption({ label: partName });
    await page.getByLabel('Código alterno', { exact: true }).fill(alternateCode.toLowerCase());
    await expect(page.getByLabel('Código alterno', { exact: true })).toHaveValue(alternateCode);
    await expect(page.getByLabel('Código alterno', { exact: true })).toHaveAttribute('autocomplete', 'off');
    await expect(page.getByRole('combobox', { name: 'SKU alterno', exact: true })).not.toBeVisible();
    await page.screenshot({ path: '/tmp/motionpartes-alterno-editor-desktop.png' });
    await page.getByRole('button', { name: 'Guardar alterno', exact: true }).click();
    await page.getByRole('button', { name: `Editar alterno ${alternateCode}`, exact: true }).click();
    await expect(page.getByLabel('Marca del código (opcional)', { exact: true })).toHaveValue('');
    await page.getByLabel('Código alterno', { exact: true }).fill(editedAlternateCode.toLowerCase());
    await page.getByLabel('Marca del código (opcional)', { exact: true }).fill('other-qa');
    await page.getByRole('button', { name: 'Guardar alterno', exact: true }).click();
    await expect(page.getByRole('region', { name: 'Alternos' }).getByText(editedAlternateCode, { exact: true })).toBeVisible();
    await page.getByRole('link', { name: 'Inventario', exact: true }).click();
    await page.getByLabel('Buscar repuestos').fill(partName);
    await page.getByRole('button', { name: `Editar SKU ${partName}`, exact: true }).click();
    await expect(page.getByLabel('Código 2', { exact: true })).toHaveValue(editedAlternateCode);
    await expect(page.getByLabel('Marca del código 2', { exact: true })).toHaveValue('OTHER-QA');
    await page.getByRole('button', { name: 'Cerrar ventana', exact: true }).click();
    const alternateStock = await supplierContext.request.post(`/api/market/accounts/${accountId}/inventory/ingest`, { headers: { Origin: origin }, data: { update_id: `${stableId}-alt`, supplier_invent_id: `${stableId}-alt`, codigo: editedAlternateCode, brand: 'OTHER-QA', source: 'upload', quantity: 2 } });
    expect(alternateStock.status()).toBe(200);
    const linkedStock = await alternateStock.json();
    expect(linkedStock.part).toBe(currentStock.results[0].part);
    expect(linkedStock.matching_status).toBe('matched');
    await page.getByRole('link', { name: 'Alternos', exact: true }).click();
    await page.getByLabel('Buscar alternos').fill(editedAlternateCode);
    await page.getByRole('button', { name: `Editar alterno ${editedAlternateCode}`, exact: true }).click();
    await page.setViewportSize({ width: 390, height: 844 });
    expect(await page.getByRole('dialog').evaluate(dialog => dialog.scrollWidth > dialog.clientWidth)).toBe(false);
    await page.screenshot({ path: '/tmp/motionpartes-alterno-editor-mobile.png' });
    await page.getByRole('button', { name: 'Cerrar ventana', exact: true }).click();
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.getByRole('button', { name: `Retirar alterno ${editedAlternateCode}`, exact: true }).click();
    await page.getByRole('button', { name: 'Confirmar retiro', exact: true }).click();
    await expect(page.getByRole('button', { name: `Editar alterno ${editedAlternateCode}`, exact: true })).not.toBeVisible();
    expect(await (await supplierContext.request.get(`/api/market/accounts/${accountId}/inventory/${itemId}/ledger`)).json()).toEqual(ledgerBefore);

    await page.getByRole('link', { name: 'Inventario', exact: true }).click();
    await page.getByLabel('Buscar repuestos').fill(partName);
    await page.setViewportSize({ width: 390, height: 844 });
    await page.getByRole('button', { name: `Editar SKU ${partName}`, exact: true }).click();
    expect(await page.getByRole('dialog').evaluate(dialog => dialog.scrollWidth > dialog.clientWidth)).toBe(false);
    await page.screenshot({ path: '/tmp/motionpartes-native-catalog-mobile.png' });
    await page.getByLabel('SKU activo', { exact: true }).uncheck();
    await page.getByRole('button', { name: 'Guardar SKU', exact: true }).click();
    // The search leaves only this SKU; its pinned SKU column and its Estado cell render as separate grid row parts.
    await expect(page.getByRole('region', { name: 'Inventario interno' }).getByText('Inactivo', { exact: true })).toBeVisible();
  } finally { await supplierContext.close(); }
});

test('superuser previews Excel imports, fixes errors, and imports grouped SKUs and alternos', async ({ page }) => {
  test.skip(!process.env.E2E_ADMIN_USERNAME || !process.env.E2E_ADMIN_PASSWORD, 'A disposable superuser fixture is required.');
  await signIn(page, process.env.E2E_ADMIN_USERNAME!, process.env.E2E_ADMIN_PASSWORD!);
  const origin = new URL(page.url()).origin;
  const existingSku = `${process.env.E2E_ADMIN_USERNAME}-EXCEL-EXISTING`.toUpperCase();
  const newSku = `${process.env.E2E_ADMIN_USERNAME}-EXCEL-NEW`.toUpperCase();
  const created = await page.request.post('/api/management/catalog', { headers: { Origin: origin }, data: { sku: existingSku, name: 'ORIGINAL', description: 'CONSERVAR', codes: [{ code: `${existingSku}-OLD` }] } });
  expect(created.status()).toBe(201);
  const existing = await created.json();
  expect((await page.request.post('/api/management/catalog/import', { headers: { Origin: 'https://unrelated.example' }, data: {} })).status()).toBe(403);
  await page.goto('/administracion?seccion=inventario');
  await page.getByRole('button', { name: 'Importar Excel', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Seleccionar archivo', exact: true })).toBeVisible();
  const download = page.waitForEvent('download');
  await page.getByRole('link', { name: 'Descargar plantilla Excel', exact: true }).click();
  expect((await download).suggestedFilename()).toBe('inventario-interno.xlsx');
  const invalid = excelFixture([[newSku, '', '', existingSku]]);
  await page.getByLabel('Archivo Excel', { exact: true }).setInputFiles({ name: 'errores.xlsx', mimeType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', buffer: invalid });
  await page.getByRole('button', { name: 'Revisar archivo', exact: true }).click();
  await expect(page.getByRole('dialog').getByRole('status').filter({ hasText: '1 fila con errores guardada' })).toBeVisible();
  await expect(page.getByRole('button', { name: /^Importar filas válidas(?: sin IA)?$/ })).toBeDisabled();
  await page.getByRole('button', { name: 'Seleccionar otro archivo', exact: true }).click();
  const valid = excelFixture([[newSku.toLowerCase(), 'repuesto importado', '', `${newSku}-G`], [newSku, '', '', `${newSku}-SHORT`, 'oem'], [existingSku, 'nombre actualizado']]);
  await page.getByLabel('Archivo Excel', { exact: true }).setInputFiles({ name: 'inventario.xlsx', mimeType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', buffer: valid });
  await page.getByRole('button', { name: 'Revisar archivo', exact: true }).click();
  await expect(page.getByRole('button', { name: /^(Confirmar importación|Importar sin IA)$/ })).toBeEnabled();
  const before = await (await page.request.get(`/api/management/catalog?search=${newSku}`)).json();
  expect(before.count).toBe(0);
  await page.screenshot({ path: '/tmp/motionpartes-excel-import-desktop.png' });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.getByRole('dialog').evaluate(dialog => dialog.scrollWidth > dialog.clientWidth)).toBe(false);
  await page.screenshot({ path: '/tmp/motionpartes-excel-import-mobile.png' });
  await page.getByRole('button', { name: /^(Confirmar importación|Importar sin IA)$/ }).click();
  await expect(page.getByText('Importación completada: 1 SKU nuevos, 1 SKU actualizados y 2 alternos nuevos.', { exact: true })).toBeVisible();
  const after = await (await page.request.get(`/api/management/catalog/${existing.id}`)).json();
  expect(after.id).toBe(existing.id); expect(after.name).toBe('NOMBRE ACTUALIZADO'); expect(after.description).toBe('CONSERVAR');
  expect(after.codes).toEqual([{ code: `${existingSku}-OLD`, brand: '' }]);
  await page.getByLabel('Buscar repuestos', { exact: true }).fill(newSku);
  await expect(page.getByRole('region', { name: 'Inventario interno' }).getByText(newSku, { exact: true })).toBeVisible();
  await expect(page.getByRole('region', { name: 'Inventario interno' }).getByText('REPUESTO IMPORTADO', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Importar Excel', exact: true }).click();
  await page.getByLabel('Archivo Excel', { exact: true }).setInputFiles({ name: 'inventario.xlsx', mimeType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', buffer: valid });
  await page.getByRole('button', { name: 'Revisar archivo', exact: true }).click();
  await expect(page.getByText('Las filas válidas ya están registradas. No hay cambios por importar.', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: /^(Confirmar importación|Importar sin IA)$/ })).toBeDisabled();
});

test('superuser reviews numeric Excel code conversions and imports them as text', async ({ page }) => {
  test.skip(!process.env.E2E_ADMIN_USERNAME || !process.env.E2E_ADMIN_PASSWORD, 'A disposable superuser fixture is required.');
  await signIn(page, process.env.E2E_ADMIN_USERNAME!, process.env.E2E_ADMIN_PASSWORD!);
  const sku = `${process.env.E2E_ADMIN_USERNAME}-EXCEL-NUMERIC`.toUpperCase();
  const numericCode = 9000000000 + Math.floor(Math.random() * 100000000);
  const workbook = excelFixture([[sku, 'código numérico', '', numericCode]]);
  await page.goto('/administracion?seccion=inventario');
  await page.getByRole('button', { name: 'Importar Excel', exact: true }).click();
  await page.getByLabel('Archivo Excel', { exact: true }).setInputFiles({ name: 'codigos-numericos.xlsx', mimeType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', buffer: workbook });
  const previewResponse = page.waitForResponse(response => new URL(response.url()).pathname === '/api/management/catalog/import' && response.request().method() === 'POST');
  await page.getByRole('button', { name: 'Revisar archivo', exact: true }).click();
  const preview = await (await previewResponse).json();
  expect(preview).toMatchObject({ valid: true, error_count: 0, warning_count: 1 });
  expect(preview.warnings).toMatchObject([{ row: 2, column: 'CODIGO_ALTERNO' }]);
  await expect(page.getByRole('dialog').getByRole('status').filter({ hasText: '1 código numérico convertido a texto' })).toBeVisible();
  await page.getByText('Ver conversiones a texto', { exact: true }).click();
  const conversions = page.getByRole('table').filter({ has: page.getByRole('columnheader', { name: 'Conversión', exact: true }) });
  await expect(conversions.getByRole('cell', { name: 'CODIGO_ALTERNO', exact: true })).toBeVisible();
  await expect(conversions.locator('tbody tr')).toHaveCount(1);
  const before = await (await page.request.get(`/api/management/catalog?search=${sku}`)).json();
  expect(before.count).toBe(0);
  await expect(page.getByRole('button', { name: /^(Confirmar importación|Importar sin IA)$/ })).toBeEnabled();
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.getByRole('dialog').evaluate(dialog => dialog.scrollWidth > dialog.clientWidth)).toBe(false);
  await page.getByRole('button', { name: /^(Confirmar importación|Importar sin IA)$/ }).click();
  await expect(page.getByText('Importación completada: 1 SKU nuevos, 0 SKU actualizados y 1 alternos nuevos.', { exact: true })).toBeVisible();
  const after = await (await page.request.get(`/api/management/catalog?search=${sku}`)).json();
  expect(after.count).toBe(1);
  expect(after.results[0]).toMatchObject({ sku, name: 'CÓDIGO NUMÉRICO', codes: [{ code: String(numericCode), brand: '' }] });
});

test('superuser pauses a committed Excel batch, resumes after reload, and safely retries an acknowledged batch', async ({ page }) => {
  test.skip(!process.env.E2E_ADMIN_USERNAME || !process.env.E2E_ADMIN_PASSWORD, 'A disposable superuser fixture is required.');
  test.setTimeout(60000);
  await signIn(page, process.env.E2E_ADMIN_USERNAME!, process.env.E2E_ADMIN_PASSWORD!);
  const origin = new URL(page.url()).origin;
  const prefix = `${process.env.E2E_ADMIN_USERNAME}-EXCEL-RESUME`.toUpperCase();
  const workbook = excelFixture(Array.from({ length: 501 }, (_, index) => [`${prefix}-${String(index).padStart(4, '0')}`, `REPUESTO ${index}`]));
  await page.goto('/administracion?seccion=inventario');
  await page.getByRole('button', { name: 'Importar Excel', exact: true }).click();
  await page.getByLabel('Archivo Excel', { exact: true }).setInputFiles({ name: 'inventario-501.xlsx', mimeType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', buffer: workbook });
  const previewResponse = page.waitForResponse(response => new URL(response.url()).pathname === '/api/management/catalog/import' && response.request().method() === 'POST');
  await page.getByRole('button', { name: 'Revisar archivo', exact: true }).click();
  const preview = await (await previewResponse).json();
  expect(preview.progress).toMatchObject({ total_batches: 2, completed_batches: 0, total_skus: 501, batch_size: 500 });
  await expect(page.getByRole('button', { name: /^(Confirmar importación|Importar sin IA)$/ })).toBeEnabled();
  const jobPath = `/api/management/catalog/import/jobs/${preview.job_id}`;
  const jobUrl = `${origin}${jobPath}`;
  let releaseResponse!: () => void;
  const release = new Promise<void>(resolve => { releaseResponse = resolve; });
  let committed!: () => void;
  const firstCommitted = new Promise<void>(resolve => { committed = resolve; });
  let postedBatches = 0;
  await page.route(jobUrl, async route => {
    if (route.request().method() !== 'POST') { await route.continue(); return; }
    postedBatches++;
    const response = await route.fetch();
    if (postedBatches === 1) {
      expect(response.status()).toBe(200);
      expect((await response.json()).progress).toMatchObject({ completed_batches: 1, processed_skus: 500 });
      committed();
      // Hold the response after the real server commit so pause timing is deterministic.
      await release;
    }
    await route.fulfill({ response });
  });
  try {
    await page.getByRole('button', { name: /^(Confirmar importación|Importar sin IA)$/ }).click();
    await firstCommitted;
    await page.getByRole('button', { name: 'Pausar importación', exact: true }).click();
    await expect(page.getByRole('button', { name: 'Pausando al terminar el lote…', exact: true })).toBeDisabled();
    releaseResponse();
    await expect(page.getByRole('button', { name: 'Reanudar importación', exact: true })).toBeEnabled();
    await expect(page.getByRole('dialog').getByRole('status')).toContainText('500 de 501 SKU procesados · 1 de 2 lotes guardados · En pausa');
    expect(postedBatches).toBe(1);
    const saved = await (await page.request.get(jobPath)).json();
    expect(saved.progress).toMatchObject({ completed_batches: 1, processed_skus: 500 });
    expect((await (await page.request.get(`/api/management/catalog?search=${prefix}`)).json()).count).toBe(500);
    const retry = await page.request.post(jobPath, { headers: { Origin: origin }, data: { batch_index: 0 } });
    expect(retry.status()).toBe(200);
    expect((await retry.json()).progress).toMatchObject({ completed_batches: 1, processed_skus: 500 });
    await page.reload();
    await page.getByRole('button', { name: 'Importar Excel', exact: true }).click();
    await expect(page.getByRole('button', { name: 'Reanudar importación', exact: true })).toBeEnabled();
    await expect(page.getByRole('dialog').getByRole('status')).toContainText('500 de 501 SKU procesados · 1 de 2 lotes guardados · En pausa');
    await expect(page.getByLabel('Archivo Excel', { exact: true })).not.toBeVisible();
    await page.getByRole('button', { name: 'Reanudar importación', exact: true }).click();
    await expect(page.getByText('Importación completada: 501 SKU nuevos, 0 SKU actualizados y 0 alternos nuevos.', { exact: true })).toBeVisible();
    expect((await (await page.request.get(`/api/management/catalog?search=${prefix}`)).json()).count).toBe(501);
    const completedRetry = await page.request.post(jobPath, { headers: { Origin: origin }, data: { batch_index: 1 } });
    expect(completedRetry.status()).toBe(200);
    expect(await completedRetry.json()).toMatchObject({ imported: true, status: 'completed', progress: { completed_batches: 2, processed_skus: 501 } });
    expect(postedBatches).toBe(2);
    expect(await page.evaluate(() => localStorage.getItem('motionpartes.catalog-import-job'))).toBeNull();
  } finally {
    releaseResponse();
    await page.unroute(jobUrl);
  }
});

test('unconfigured AI allows import and a mocked provider failure blocks confirmation only while classification is pending', async ({ page }) => {
  test.skip(!process.env.E2E_ADMIN_USERNAME || !process.env.E2E_ADMIN_PASSWORD, 'A disposable superuser fixture is required.');
  await signIn(page, process.env.E2E_ADMIN_USERNAME!, process.env.E2E_ADMIN_PASSWORD!);
  const sku = `${process.env.E2E_ADMIN_USERNAME}-EXCEL-AI-FAILURE`.toUpperCase();
  const classificationUrl = /\/api\/management\/catalog\/import\/jobs\/[^/]+\/classification\/?(?:\?.*)?$/;
  let configured = false;
  let releaseProvider!: () => void;
  const release = new Promise<void>(resolve => { releaseProvider = resolve; });
  let providerStarted!: () => void;
  const started = new Promise<void>(resolve => { providerStarted = resolve; });
  let providerCalls = 0;
  await page.route(classificationUrl, async route => {
    if (route.request().method() === 'GET') {
      await route.fulfill({ json: { configured, model: 'MOCK-PROVIDER', batch_size: 50, completed_batches: 0, total_batches: 1, status: 'ready', count: 0, offset: 0, next_offset: null, results: [] } });
      return;
    }
    expect(route.request().postDataJSON()).toEqual({ mode: 'classify', batch_index: 0 });
    providerCalls++;
    providerStarted();
    await release;
    await route.fulfill({ status: 503, json: { detail: 'La IA no está configurada en el servidor.' } });
  });
  try {
    await page.goto('/administracion?seccion=inventario');
    await page.getByRole('button', { name: 'Importar Excel', exact: true }).click();
    await page.getByLabel('Archivo Excel', { exact: true }).setInputFiles({ name: 'ia-configuracion.xlsx', mimeType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', buffer: excelFixture([[sku, 'PASTILLAS DE FRENO']]) });
    await page.getByRole('button', { name: 'Revisar archivo', exact: true }).click();
    await expect(page.getByText('La IA aún no está configurada.', { exact: false })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Clasificar con IA', exact: true })).not.toBeVisible();
    await expect(page.getByRole('button', { name: /^(Confirmar importación|Importar sin IA)$/ })).toBeEnabled();
    configured = true;
    await page.reload();
    await page.getByRole('button', { name: 'Importar Excel', exact: true }).click();
    await expect(page.getByRole('button', { name: 'Clasificar con IA', exact: true })).toBeEnabled();
    await expect(page.getByText('Agrupación y categorías: IA sin ejecutar.', { exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Importar sin IA', exact: true })).toBeEnabled();
    await page.getByRole('button', { name: 'Clasificar con IA', exact: true }).click();
    await started;
    await expect(page.getByRole('button', { name: /^(Confirmar importación|Importar sin IA)$/ })).toBeDisabled();
    await expect(page.getByRole('button', { name: 'Seleccionar otro archivo', exact: true })).toBeDisabled();
    await page.getByRole('button', { name: 'Cerrar ventana', exact: true }).click();
    await expect(page.getByRole('dialog')).toBeVisible();
    expect((await (await page.request.get(`/api/management/catalog?search=${sku}`)).json()).count).toBe(0);
    releaseProvider();
    await expect(page.getByRole('dialog').getByRole('alert')).toContainText('La IA no está configurada en el servidor.');
    await expect(page.getByRole('button', { name: /^(Confirmar importación|Importar sin IA)$/ })).toBeEnabled();
    expect(providerCalls).toBe(1);
    expect((await (await page.request.get(`/api/management/catalog?search=${sku}`)).json()).count).toBe(0);
    await page.getByRole('button', { name: /^(Confirmar importación|Importar sin IA)$/ }).click();
    await expect(page.getByText('Importación completada: 1 SKU nuevos, 0 SKU actualizados y 0 alternos nuevos.', { exact: true })).toBeVisible();
    expect((await (await page.request.get(`/api/management/catalog?search=${sku}`)).json()).count).toBe(1);
  } finally {
    releaseProvider();
    await page.unroute(classificationUrl);
  }
});

test('mocked AI proposals require explicit row review and allow uppercase corrections before applying the preview', async ({ page }) => {
  test.skip(!process.env.E2E_ADMIN_USERNAME || !process.env.E2E_ADMIN_PASSWORD, 'A disposable superuser fixture is required.');
  await signIn(page, process.env.E2E_ADMIN_USERNAME!, process.env.E2E_ADMIN_PASSWORD!);
  const sku = `${process.env.E2E_ADMIN_USERNAME}-EXCEL-AI-REVIEW`.toUpperCase();
  const classificationUrl = /\/api\/management\/catalog\/import\/jobs\/[^/]+\/classification\/?(?:\?.*)?$/;
  let classified = false;
  let applied = false;
  let preview: { preview: Array<{ sku: string; category: string; subcategory: string }> };
  let submitted: unknown;
  const result = () => ({ configured: true, model: 'MOCK-PROVIDER', batch_size: 50, completed_batches: classified ? 1 : 0, total_batches: 1, status: classified ? 'completed' : 'ready', count: classified ? 1 : 0, offset: 0, next_offset: null, results: classified ? [{ sku, row: 2, target_sku: sku, category: 'SUSPENSION', subcategory: 'AMORTIGUADORES', reason: 'Propuesta simulada para comprobar la revisión.', confidence: 0.55, needs_review: true, evidence: { source_reference: '', target_reference: '' }, candidate_skus: [], applied, applied_decision: applied ? { target_sku: sku, category: 'FRENOS', subcategory: 'PASTILLAS' } : {} }] : [] });
  await page.route(classificationUrl, async route => {
    if (route.request().method() === 'GET') { await route.fulfill({ json: result() }); return; }
    const data = route.request().postDataJSON();
    if (data.mode === 'classify') {
      expect(data).toEqual({ mode: 'classify', batch_index: 0 });
      classified = true;
      await route.fulfill({ json: result() });
    } else {
      submitted = data;
      applied = true;
      await route.fulfill({ json: { ...preview, preview: preview.preview.map(item => ({ ...item, category: 'FRENOS', subcategory: 'PASTILLAS' })) } });
    }
  });
  try {
    await page.goto('/administracion?seccion=inventario');
    await page.getByRole('button', { name: 'Importar Excel', exact: true }).click();
    await page.getByLabel('Archivo Excel', { exact: true }).setInputFiles({ name: 'ia-revision.xlsx', mimeType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', buffer: excelFixture([[sku, 'PASTILLAS DE FRENO']]) });
    const response = page.waitForResponse(response => new URL(response.url()).pathname === '/api/management/catalog/import' && response.request().method() === 'POST');
    await page.getByRole('button', { name: 'Revisar archivo', exact: true }).click();
    preview = await (await response).json();
    await page.getByRole('button', { name: 'Clasificar con IA', exact: true }).click();
    await expect(page.getByText('Clasificación terminada. Revisa las propuestas antes de aplicarlas.', { exact: true })).toBeVisible();
    await expect(page.getByLabel(`Revisión confirmada para ${sku}`, { exact: true })).not.toBeChecked();
    await expect(page.getByRole('button', { name: 'Importar con revisión parcial', exact: true })).toBeEnabled();
    await expect(page.getByRole('button', { name: 'Aplicar 0 propuestas revisadas', exact: true })).toBeDisabled();
    expect((await (await page.request.get(`/api/management/catalog?search=${sku}`)).json()).count).toBe(0);
    await page.getByLabel(`Categoría para ${sku}`, { exact: true }).fill('frenos');
    await page.getByLabel(`Subcategoría para ${sku}`, { exact: true }).fill('pastillas');
    await expect(page.getByLabel(`Categoría para ${sku}`, { exact: true })).toHaveValue('FRENOS');
    await expect(page.getByLabel(`Subcategoría para ${sku}`, { exact: true })).toHaveValue('PASTILLAS');
    await page.getByLabel(`Revisión confirmada para ${sku}`, { exact: true }).check();
    await page.getByRole('button', { name: 'Aplicar 1 propuestas revisadas', exact: true }).click();
    await expect(page.getByText('Revisión aplicada a la vista previa. Confirma la importación para guardar los SKU y alternos.', { exact: true })).toBeVisible();
    expect(submitted).toEqual({ mode: 'apply', decisions: [{ sku, target_sku: sku, category: 'FRENOS', subcategory: 'PASTILLAS' }] });
    await expect(page.getByText('FRENOS / PASTILLAS', { exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: /^(Confirmar importación|Importar sin IA)$/ })).toBeEnabled();
    expect((await (await page.request.get(`/api/management/catalog?search=${sku}`)).json()).count).toBe(0);
  } finally { await page.unroute(classificationUrl); }
});

test('one brand-neutral SKU shows all equivalent supplier items and keeps basket choices separate', async ({ page, browser }) => {
  test.skip(!process.env.E2E_ADMIN_USERNAME || !process.env.E2E_ADMIN_PASSWORD || !process.env.E2E_USERNAME || !process.env.E2E_PASSWORD, 'Disposable superuser and supplier fixtures are required.');
  await signIn(page, process.env.E2E_ADMIN_USERNAME!, process.env.E2E_ADMIN_PASSWORD!);
  const origin = new URL(page.url()).origin;
  const sku = `${process.env.E2E_ADMIN_USERNAME}-GRUPO`.toUpperCase();
  const codes = [`${sku}-G`, `${sku}-SHORT`, `${sku}-ALT`];
  const brands = ['GENERIC-QA', 'OTHER-QA', 'OEM-QA'];
  const created = await page.request.post('/api/management/catalog', { headers: { Origin: origin }, data: { sku, codes: codes.map(code => ({ code })) } });
  expect(created.status()).toBe(201);
  const part = await created.json();
  expect(part.brand).toBeUndefined();
  const supplierContext = await browser.newContext({ baseURL: origin });
  try {
    const clientPage = await supplierContext.newPage();
    await signIn(clientPage, process.env.E2E_USERNAME!, process.env.E2E_PASSWORD!);
    const accounts = await (await supplierContext.request.get('/api/market/accounts')).json();
    const account = accounts.results.find((account: { name: string }) => account.name === process.env.E2E_USERNAME);
    for (let index = 0; index < codes.length; index++) {
      const stock = await supplierContext.request.post(`/api/market/accounts/${account.id}/inventory/ingest`, { headers: { Origin: origin }, data: { update_id: `${sku}-${index}`, supplier_invent_id: `${sku}-${index}`, codigo: codes[index], brand: brands[index], source: 'upload', quantity: 5, description: `Artículo equivalente ${index + 1}` } });
      expect(stock.status()).toBe(200);
      expect((await stock.json()).part).toBe(part.id);
    }
    await clientPage.getByLabel('Buscar repuestos, marcas o códigos', { exact: true }).fill(codes[1]);
    await clientPage.getByRole('button', { name: `Ver ${sku}`, exact: true }).click();
    const dialog = clientPage.getByRole('dialog');
    await expect(dialog.getByRole('heading', { name: sku, exact: true })).toBeVisible();
    for (let index = 0; index < codes.length; index++) {
      await expect(dialog.getByRole('group', { name: `Artículo ${codes[index]} · ${brands[index]}`, exact: true })).toBeVisible();
    }
    await clientPage.screenshot({ path: '/tmp/motionpartes-sku-options.png' });
    for (const code of codes.slice(0, 2)) {
      await dialog.getByRole('button', { name: `Agregar ${code} de ${account.name} a la cesta`, exact: true }).click();
    }
    await dialog.getByRole('button', { name: 'Cerrar ventana', exact: true }).click();
    await clientPage.getByRole('button', { name: 'Abrir cesta, 2 artículos', exact: true }).click();
    for (let index = 0; index < 2; index++) {
      await expect(clientPage.getByRole('main', { name: 'Cesta de solicitudes', exact: true }).getByText(`${codes[index]} · ${brands[index]}`, { exact: true })).toBeVisible();
    }
    await clientPage.reload();
    await expect(clientPage.getByRole('main', { name: 'Cesta de solicitudes', exact: true }).getByText(`${codes[1]} · ${brands[1]}`, { exact: true })).toBeVisible();
    await clientPage.getByRole('button', { name: 'Volver al catálogo', exact: true }).click();
    await clientPage.setViewportSize({ width: 390, height: 844 });
    await clientPage.getByLabel('Buscar repuestos, marcas o códigos', { exact: true }).fill(sku);
    await clientPage.getByRole('button', { name: `Ver ${sku}`, exact: true }).click();
    await expect(clientPage.getByRole('group', { name: `Artículo ${codes[2]} · ${brands[2]}`, exact: true })).toBeVisible();
    expect(await clientPage.getByRole('dialog').evaluate(dialog => dialog.scrollWidth > dialog.clientWidth)).toBe(false);
  } finally { await supplierContext.close(); }
});

test('superuser manages accounts, employee access, users, and invitations', async ({ page }) => {
  test.skip(!process.env.E2E_ADMIN_USERNAME || !process.env.E2E_ADMIN_PASSWORD || !process.env.E2E_USERNAME, 'Disposable superuser and employee fixtures are required.');
  await signIn(page, process.env.E2E_ADMIN_USERNAME!, process.env.E2E_ADMIN_PASSWORD!);
  await page.getByRole('link', { name: 'Administración', exact: true }).first().click();
  await expect(page.getByRole('heading', { name: 'Administración.' })).toBeVisible();
  await expect(page.getByText('Solo superusuarios')).toBeVisible();
  await page.getByRole('navigation', { name: 'Gestión administrativa' }).getByRole('link', { name: 'Cuentas', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Crear cuenta', exact: true })).toBeVisible();
  expect((await page.request.post('/api/management/accounts', { headers: { Origin: 'https://unrelated.example' }, data: { name: 'Denied' } })).status()).toBe(403);

  const accountName = `${process.env.E2E_ADMIN_USERNAME}-empresa`;
  await page.getByRole('button', { name: 'Crear cuenta', exact: true }).click();
  await page.getByLabel('Nombre de la cuenta').fill(accountName);
  await page.getByRole('dialog').getByLabel('Proveedor mayorista').check();
  await page.getByRole('dialog').getByLabel('Cliente empresarial').check();
  await page.getByRole('button', { name: 'Guardar cuenta', exact: true }).click();
  await expect(page.getByRole('cell', { name: accountName, exact: true })).toBeVisible();
  const accountRow = page.getByRole('row').filter({ has: page.getByRole('cell', { name: accountName, exact: true }) });
  await expect(accountRow.getByText('Proveedor mayorista', { exact: true })).toBeVisible();
  await expect(accountRow.getByText('Cliente empresarial', { exact: true })).toBeVisible();

  await page.getByRole('button', { name: `Gestionar usuarios de ${accountName}`, exact: true }).click();
  await page.getByLabel('Buscar usuario', { exact: true }).fill(process.env.E2E_USERNAME!);
  const userOption = page.getByLabel('Usuario para agregar');
  await expect(userOption.locator('option')).toHaveCount(2);
  await userOption.selectOption({ label: `${process.env.E2E_USERNAME} · ${process.env.E2E_USERNAME}@example.invalid` });
  await page.getByLabel('Permiso en la cuenta', { exact: true }).selectOption('manager');
  await page.getByRole('button', { name: 'Agregar a la cuenta', exact: true }).click();
  await expect(page.getByLabel(`Permiso de ${process.env.E2E_USERNAME}`)).toHaveValue('manager');
  await page.getByRole('button', { name: `Retirar acceso de ${process.env.E2E_USERNAME}` }).click();
  await page.getByRole('button', { name: 'Confirmar retiro', exact: true }).click();
  await expect(page.getByLabel(`Permiso de ${process.env.E2E_USERNAME}`)).not.toBeVisible();
  await page.getByRole('button', { name: 'Cerrar ventana', exact: true }).click();

  await page.getByRole('navigation', { name: 'Gestión administrativa' }).getByRole('link', { name: 'Usuarios', exact: true }).click();
  await page.getByLabel('Buscar usuarios').fill(process.env.E2E_USERNAME!);
  await page.getByRole('button', { name: `Asignar cuenta a ${process.env.E2E_USERNAME}` }).click();
  await page.getByLabel('Buscar cuenta', { exact: true }).fill(accountName);
  await expect(page.getByLabel('Cuenta para asignar').locator('option')).toHaveCount(2);
  await page.getByLabel('Cuenta para asignar').selectOption({ label: accountName });
  await page.getByRole('button', { name: 'Asignar cuenta', exact: true }).click();
  await expect(page.getByText('Cuenta asignada al usuario.', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: `Editar usuario ${process.env.E2E_USERNAME}` }).click();
  await page.getByLabel('Nombre', { exact: true }).fill('Ana');
  await page.getByLabel('Apellido', { exact: true }).fill('Pérez');
  await page.getByRole('button', { name: 'Guardar usuario', exact: true }).click();
  await expect(page.getByText('Ana Pérez', { exact: true })).toBeVisible();

  await page.getByRole('navigation', { name: 'Gestión administrativa' }).getByRole('link', { name: 'Invitaciones', exact: true }).click();
  const email = `${process.env.E2E_ADMIN_USERNAME}@invited.example.invalid`;
  await page.getByRole('button', { name: 'Invitar usuario', exact: true }).click();
  await page.getByLabel('Correo electrónico del invitado').fill(email);
  await page.getByRole('button', { name: 'Crear invitación', exact: true }).click();
  await expect(page.getByLabel('Código de registro')).toHaveValue(/[a-f0-9-]{36}/);
  await page.getByRole('button', { name: 'Cerrar ventana', exact: true }).click();
  await page.getByRole('button', { name: `Revocar invitación de ${email}`, exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: 'Revocar invitación', exact: true }).click();
  const invitationRow = page.getByRole('row').filter({ has: page.getByRole('cell', { name: email, exact: true }) });
  await expect(invitationRow.getByText('Vencida', { exact: true })).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)).toBe(false);
  await page.getByRole('button', { name: /Menú administrativo/ }).click();
  await page.getByRole('navigation', { name: 'Gestión administrativa' }).getByRole('link', { name: 'Cuentas', exact: true }).click();
  await page.getByLabel('Buscar cuentas').fill(accountName);
  await page.getByRole('button', { name: `Editar cuenta ${accountName}`, exact: true }).click();
  await page.getByLabel('Cuenta activa', { exact: true }).uncheck();
  await page.getByRole('button', { name: 'Guardar cuenta', exact: true }).click();
  await expect(page.getByRole('row').filter({ has: page.getByRole('cell', { name: accountName, exact: true }) }).getByText('Inactiva', { exact: true })).toBeVisible();
});


test('sidebar opens catalog and alternos, preserves links on reload, and collapses on mobile', async ({ page }) => {
  test.skip(!process.env.E2E_ADMIN_USERNAME || !process.env.E2E_ADMIN_PASSWORD || !process.env.E2E_USERNAME, 'Disposable superuser and catalog fixtures are required.');
  await signIn(page, process.env.E2E_ADMIN_USERNAME!, process.env.E2E_ADMIN_PASSWORD!);
  await page.getByRole('link', { name: 'Administración', exact: true }).first().click();
  const navigation = page.getByRole('navigation', { name: 'Gestión administrativa' });
  for (const name of ['Inventario', 'Alternos', 'Cuentas', 'Usuarios', 'Invitaciones']) {
    await expect(navigation.getByRole('link', { name, exact: true })).toBeVisible();
  }
  await navigation.getByRole('link', { name: 'Inventario', exact: true }).click();
  await expect(page).toHaveURL(/seccion=inventario/);
  await expect(page.getByRole('heading', { name: 'Inventario', exact: true })).toBeVisible();
  await page.getByLabel('Buscar repuestos', { exact: true }).fill(process.env.E2E_USERNAME!);
  await expect(page.getByRole('region', { name: 'Inventario interno' }).getByText(process.env.E2E_USERNAME!.toUpperCase(), { exact: true }).first()).toBeVisible();
  await page.getByRole('button', { name: 'Existencias por proveedor', exact: true }).click();
  await expect(page.getByLabel('Buscar existencias')).toBeVisible();
  await navigation.getByRole('link', { name: 'Alternos', exact: true }).click();
  await expect(page).toHaveURL(/seccion=alternos/);
  await expect(page.getByRole('heading', { name: 'Alternos', exact: true })).toBeVisible();
  await page.reload();
  await expect(navigation.getByRole('link', { name: 'Alternos', exact: true })).toHaveAttribute('aria-current', 'page');
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByRole('button', { name: /Menú administrativo/ })).toHaveAttribute('aria-expanded', 'false');
  await expect(navigation).not.toBeVisible();
  await page.getByRole('button', { name: /Menú administrativo/ }).click();
  await expect(navigation).toBeVisible();
  await navigation.getByRole('link', { name: 'Usuarios', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Usuarios', exact: true })).toBeVisible();
  await expect(navigation).not.toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)).toBe(false);
});
