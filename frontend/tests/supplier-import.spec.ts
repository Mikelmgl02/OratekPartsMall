import { expect, Page, test } from '@playwright/test';
import { readFile } from 'node:fs/promises';
import { excelFixture } from './excel-fixture';

const accountId = '11111111-1111-4111-8111-111111111111';
const secondAccountId = '22222222-2222-4222-8222-222222222222';
const jobId = '33333333-3333-4333-8333-333333333333';
const existingItemId = '44444444-4444-4444-8444-444444444444';
const newItemId = '55555555-5555-4555-8555-555555555555';
const root = `/api/market/accounts/${accountId}/inventory`;
const excelType = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet';
const fixture = excelFixture([['SUPPLIER-IMPORT-TEST', 'REPUESTO DE PRUEBA']]);
const emptyPage = { count: 0, next: null, previous: null, results: [] };

function stockItem(id = existingItemId, supplierInventoryId = 'supplier-id-lowercase', quantity = 10) {
  return { id, supplier_invent_id: supplierInventoryId, codigo: id === existingItemId ? 'FE-ONE' : 'FE-TWO',
    brand: 'ACME', description: 'REPUESTO DE PRUEBA', part: null, source: 'upload', matching_status: 'pending',
    reported_quantity: quantity, reserved_quantity: id === existingItemId ? 2 : 0,
    available_quantity: Math.max(0, quantity - (id === existingItemId ? 2 : 0)), updated_at: '2026-10-01T12:00:00Z' };
}

function importJob({ batches = 1, validRows = 2, rejectedRows = 1 } = {}) {
  const summary = { source_rows: validRows + rejectedRows, valid_rows: validRows, rejected_rows: rejectedRows,
    created_items: validRows - 1, updated_items: 1, unchanged_items: 0, credit_units: 5, debit_units: 7 };
  return { job_id: jobId, supplier_id: accountId, filename: 'existencias-prueba.xlsx', expires_at: '2099-10-01T12:00:00Z',
    status: 'ready', valid: true, imported: false, summary,
    applied_summary: { ...summary, source_rows: 0, valid_rows: 0, created_items: 0, updated_items: 0, credit_units: 0, debit_units: 0 },
    preview: [
      { row: 2, supplier_invent_id: 'supplier-id-lowercase', codigo: 'FE-ONE', brand: 'ACME', description: 'REPUESTO DE PRUEBA',
        current_quantity: 10, uploaded_quantity: 3, quantity: 3, new_quantity: 3, movement_quantity: 7, direction: 'debit',
        matching_status: 'pending', part_id: null, part_sku: null, action: 'update' },
      { row: 3, supplier_invent_id: '00045', codigo: 'FE-TWO', brand: 'ACME', description: 'REPUESTO NUEVO',
        current_quantity: 0, uploaded_quantity: 5, quantity: 5, new_quantity: 5, movement_quantity: 5, direction: 'credit',
        matching_status: 'pending', part_id: null, part_sku: null, action: 'create' },
    ], preview_count: validRows,
    errors: rejectedRows ? [{ row: 4, column: 'EXISTENCIAS', message: 'Una celda vacía no significa cero.' }] : [],
    error_count: rejectedRows, warnings: [], warning_count: 0,
    progress: { batch_size: 500, total_batches: batches, completed_batches: 0, total_rows: validRows, processed_rows: 0, next_batch: 0 as number | null } };
}

async function supplier(page: Page, bothAccounts = false) {
  const accounts = [{ id: accountId, name: 'PROVEEDOR DE PRUEBA', roles: ['supplier_retail'], capabilities: ['supplier'] },
    ...(bothAccounts ? [{ id: secondAccountId, name: 'SEGUNDO PROVEEDOR', roles: ['supplier_wholesale'], capabilities: ['supplier'] }] : [])];
  await page.route('**/api/session', route => route.fulfill({ json: { authenticated: true,
    user: { id: 7, username: 'PROVEEDOR', first_name: '', last_name: '', is_superuser: false },
    accounts: { ...emptyPage, count: accounts.length, results: accounts } } }));
  await page.route(/\/api\/market\/catalog(?:\?.*)?$/, route => route.fulfill({ json: emptyPage }));
}

async function openPanel(page: Page) {
  await page.goto('/');
  await expect(page.getByText('Cuenta conectada', { exact: true })).toBeVisible();
  await expect(page.getByRole('link', { name: 'Administración', exact: true })).not.toBeVisible();
  if (page.viewportSize()!.width < 760) await page.getByRole('button', { name: 'Abrir tu panel', exact: true }).click();
  else await page.getByRole('button', { name: 'Para proveedores', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Importar Excel', exact: true })).toBeVisible();
}

async function upload(page: Page, downloadTemplate = false) {
  await page.getByRole('button', { name: 'Importar Excel', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Importar existencias desde Excel', exact: true });
  await expect(dialog).toBeVisible();
  if (downloadTemplate) {
    // Chrome's eager download requests bypass Playwright routing. The mocked
    // attachment response still drives a real download through this same URL.
    await dialog.getByRole('link', { name: 'Descargar plantilla', exact: true }).evaluate(element => element.removeAttribute('download'));
    const downloadPromise = page.waitForEvent('download');
    await dialog.getByRole('link', { name: 'Descargar plantilla', exact: true }).click();
    const download = await downloadPromise;
    expect(download.suggestedFilename(), download.url()).toBe('inventario-proveedor.xlsx');
    expect(await readFile((await download.path())!)).toEqual(fixture);
  }
  await dialog.getByLabel('Archivo Excel de existencias', { exact: true }).setInputFiles({
    name: 'existencias-prueba.xlsx', mimeType: excelType, buffer: fixture,
  });
  await dialog.getByRole('button', { name: 'Revisar archivo', exact: true }).click();
  return dialog;
}

test('ordinary supplier imports valid balances, sees positive debit and credit movements, and retrieves rejected rows after reload', async ({ page }) => {
  await supplier(page);
  let current = importJob();
  let items = [stockItem()];
  const savedBatches: number[] = [];
  const downloaded: string[] = [];
  // Browser downloads have no page-owned navigation and need a context route.
  await page.context().route(new RegExp(`${root}/import/(?:template|jobs/${jobId}/errors)$`), async route => {
    const filename = new URL(route.request().url()).pathname.endsWith('/errors') ? 'errores-inventario-proveedor.xlsx' : 'inventario-proveedor.xlsx';
    downloaded.push(filename);
    await route.fulfill({ body: fixture, contentType: excelType, headers: { 'Content-Disposition': `attachment; filename="${filename}"` } });
  });
  await page.route(`${root}/**`, async route => {
    const pathname = new URL(route.request().url()).pathname;
    if (pathname === `${root}/import` && route.request().method() === 'POST') { await route.fulfill({ json: current }); return; }
    if (pathname === `${root}/import/jobs/${jobId}/errors` || pathname === `${root}/import/template`) {
      const filename = pathname.endsWith('/errors') ? 'errores-inventario-proveedor.xlsx' : 'inventario-proveedor.xlsx';
      downloaded.push(filename); await route.fulfill({ body: fixture, contentType: excelType, headers: { 'Content-Disposition': `attachment; filename="${filename}"` } }); return;
    }
    if (pathname === `${root}/import/jobs/${jobId}`) {
      if (route.request().method() === 'POST') {
        savedBatches.push(route.request().postDataJSON().batch_index);
        current = { ...current, status: 'completed', imported: true, applied_summary: { ...current.summary },
          progress: { ...current.progress, completed_batches: 1, processed_rows: 2, next_batch: null } };
        items = [stockItem(existingItemId, 'supplier-id-lowercase', 3), stockItem(newItemId, '00045', 5)];
      }
      await route.fulfill({ json: current }); return;
    }
    if (pathname === `${root}/${existingItemId}/ledger`) {
      await route.fulfill({ json: { ...emptyPage, count: 1, results: [{ id: 1, item: existingItemId, kind: 'sync',
        quantity: 7, direction: 'debit', balance_type: 'stock', reserved_delta: 0, reserved_direction: '',
        reported_after: 3, reserved_after: 2, reference: `excel-${jobId}-2`, created_at: '2026-10-01T12:00:00Z' }] } }); return;
    }
    if (pathname === `${root}/${newItemId}/ledger`) {
      await route.fulfill({ json: { ...emptyPage, count: 1, results: [{ id: 2, item: newItemId, kind: 'sync',
        quantity: 5, direction: 'credit', balance_type: 'stock', reserved_delta: 0, reserved_direction: '',
        reported_after: 5, reserved_after: 0, reference: `excel-${jobId}-3`, created_at: '2026-10-01T12:00:00Z' }] } }); return;
    }
    await route.fulfill({ status: 404, json: { detail: 'Ruta de prueba desconocida.' } });
  });
  await page.route(new RegExp(`${root}(?:\\?.*)?$`), route => route.fulfill({ json: { ...emptyPage, count: items.length, results: items } }));
  await openPanel(page);
  const dialog = await upload(page, true);
  await expect(dialog.getByRole('button', { name: 'Importar filas válidas', exact: true })).toBeEnabled();
  const debit = dialog.getByRole('row').filter({ hasText: 'supplier-id-lowercase' });
  await expect(debit).toContainText('Débito');
  await expect(debit.getByRole('cell').nth(5)).toHaveText('7');
  const credit = dialog.getByRole('row').filter({ hasText: '00045' });
  await expect(credit).toContainText('Crédito');
  await expect(credit.getByRole('cell').nth(5)).toHaveText('5');
  await expect(dialog.getByRole('link', { name: 'Descargar filas con errores', exact: true })).toBeVisible();
  await dialog.evaluate(element => { element.scrollTop = 0; });
  await page.screenshot({ path: '/tmp/motionpartes-supplier-import-desktop.png' });
  await dialog.getByRole('button', { name: 'Importar filas válidas', exact: true }).click();
  await expect(dialog.getByRole('heading', { name: 'Importación completada', exact: true })).toBeVisible();
  await dialog.getByRole('button', { name: 'Cerrar', exact: true }).click();
  await expect(dialog).not.toBeVisible();
  expect(savedBatches).toEqual([0]);
  await expect(page.getByRole('gridcell', { name: 'supplier-id-lowercase', exact: true })).toBeVisible();
  const updatedRow = page.getByRole('row').filter({ hasText: 'supplier-id-lowercase' });
  await expect(updatedRow.getByRole('gridcell').nth(3)).toHaveText('1');
  await expect(updatedRow.getByRole('gridcell').nth(4)).toHaveText('2');
  await expect(page.getByRole('gridcell', { name: '00045', exact: true })).toBeVisible();

  await page.getByRole('button', { name: 'Ver movimientos de FE-ONE', exact: true }).click();
  const history = page.getByRole('dialog', { name: 'Movimientos de existencias · FE-ONE', exact: true });
  await expect(history).toContainText('Débito');
  await expect(history.getByText('7', { exact: true })).toBeVisible();
  await expect(history.getByText('-7', { exact: true })).not.toBeVisible();
  await page.screenshot({ path: '/tmp/motionpartes-supplier-ledger.png' });
  await history.getByRole('button', { name: 'Cerrar ventana', exact: true }).click();
  await page.getByRole('button', { name: 'Ver movimientos de FE-TWO', exact: true }).click();
  const newHistory = page.getByRole('dialog', { name: 'Movimientos de existencias · FE-TWO', exact: true });
  await expect(newHistory).toContainText('Crédito');
  await expect(newHistory.getByText('5', { exact: true })).toBeVisible();
  await newHistory.getByRole('button', { name: 'Cerrar ventana', exact: true }).click();

  await page.reload();
  await page.getByRole('button', { name: 'Para proveedores', exact: true }).click();
  await page.getByRole('button', { name: 'Importar Excel', exact: true }).click();
  const recovered = page.getByRole('dialog', { name: 'Importar existencias desde Excel', exact: true });
  await recovered.getByRole('link', { name: 'Descargar filas con errores', exact: true }).evaluate(element => element.removeAttribute('download'));
  const downloadPromise = page.waitForEvent('download');
  await recovered.getByRole('link', { name: 'Descargar filas con errores', exact: true }).click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toBe('errores-inventario-proveedor.xlsx');
  expect(await readFile((await download.path())!)).toEqual(fixture);
  expect(downloaded).toContain('errores-inventario-proveedor.xlsx');
  expect(downloaded).toContain('inventario-proveedor.xlsx');
  expect(savedBatches).toEqual([0]);
});

test('lost batch response is recovered after reload and only the remaining batch is submitted', async ({ page }) => {
  await supplier(page, true);
  let current = importJob({ batches: 2, validRows: 501, rejectedRows: 0 });
  const submitted: number[] = [];
  let loseFirstResponse = true;
  await page.route(`${root}/**`, async route => {
    const pathname = new URL(route.request().url()).pathname;
    if (pathname === `${root}/import`) { await route.fulfill({ json: current }); return; }
    if (pathname === `${root}/import/jobs/${jobId}`) {
      if (route.request().method() === 'POST') {
        const index = route.request().postDataJSON().batch_index;
        submitted.push(index);
        current = { ...current, status: index === 0 ? 'importing' : 'completed', imported: index === 1,
          applied_summary: index === 1 ? { ...current.summary } : { ...current.applied_summary, valid_rows: 500, created_items: 499, updated_items: 1 },
          progress: { ...current.progress, completed_batches: index + 1, processed_rows: index === 0 ? 500 : 501, next_batch: index === 0 ? 1 : null } };
        if (loseFirstResponse) { loseFirstResponse = false; await route.abort('failed'); return; }
      }
      await route.fulfill({ json: current }); return;
    }
    await route.fulfill({ status: 404, json: { detail: 'Ruta de prueba desconocida.' } });
  });
  await page.route(new RegExp(`/api/market/accounts/(?:${accountId}|${secondAccountId})/inventory(?:\\?.*)?$`), route => route.fulfill({ json: emptyPage }));
  await openPanel(page);
  const dialog = await upload(page);
  await dialog.getByRole('button', { name: 'Confirmar importación', exact: true }).click();
  await expect(dialog.getByRole('alert')).toBeVisible();
  expect(submitted).toEqual([0]);
  await page.reload();
  await page.getByRole('button', { name: 'Para proveedores', exact: true }).click();
  await page.getByRole('button', { name: 'Importar Excel', exact: true }).click();
  const resumed = page.getByRole('dialog', { name: 'Importar existencias desde Excel', exact: true });
  await expect(resumed.getByText(/500 de 501/)).toBeVisible();
  await resumed.getByRole('button', { name: 'Reanudar importación', exact: true }).click();
  await expect(resumed.getByRole('heading', { name: 'Importación completada', exact: true })).toBeVisible();
  await resumed.getByRole('button', { name: 'Cerrar', exact: true }).click();
  await expect(resumed).not.toBeVisible();
  expect(submitted).toEqual([0, 1]);

  await page.getByLabel('Cuenta activa', { exact: true }).selectOption(secondAccountId);
  await page.getByRole('button', { name: 'Para proveedores', exact: true }).click();
  await page.getByRole('button', { name: 'Importar Excel', exact: true }).click();
  const separate = page.getByRole('dialog', { name: 'Importar existencias desde Excel', exact: true });
  await expect(separate.getByLabel('Archivo Excel de existencias', { exact: true })).toBeAttached();
  await expect(separate.getByLabel('Archivo Excel de existencias', { exact: true })).toBeEnabled();
  await expect(separate.getByText(/500 de 501/)).not.toBeVisible();
  expect(submitted).toEqual([0, 1]);
});

test('stale stock preview shows the conflict without claiming completion and allows a fresh review', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await supplier(page);
  const current = importJob({ rejectedRows: 0 });
  let reviews = 0;
  let attemptedBatches = 0;
  await page.route(`${root}/**`, async route => {
    const pathname = new URL(route.request().url()).pathname;
    if (pathname === `${root}/import`) { reviews++; await route.fulfill({ json: current }); return; }
    if (pathname === `${root}/import/jobs/${jobId}`) {
      if (route.request().method() === 'POST') {
        attemptedBatches++;
        await route.fulfill({ status: 409, json: { detail: 'Las existencias cambiaron desde la vista previa. Revisa el archivo otra vez.' } }); return;
      }
      await route.fulfill({ json: current }); return;
    }
    await route.fulfill({ status: 404, json: { detail: 'Ruta de prueba desconocida.' } });
  });
  await page.route(new RegExp(`${root}(?:\\?.*)?$`), route => route.fulfill({ json: { ...emptyPage, count: 1, results: [stockItem()] } }));
  await openPanel(page);
  const dialog = await upload(page);
  expect(await dialog.evaluate(element => element.scrollWidth > element.clientWidth)).toBe(false);
  await dialog.evaluate(element => { element.scrollTop = 0; });
  await page.screenshot({ path: '/tmp/motionpartes-supplier-import-mobile.png' });
  await dialog.getByRole('button', { name: 'Confirmar importación', exact: true }).click();
  await expect(dialog.getByRole('alert')).toContainText('Las existencias cambiaron desde la vista previa.');
  await expect(dialog.getByText(/0 de 2/)).toBeVisible();
  expect(attemptedBatches).toBe(1);
  await expect(dialog.getByRole('button', { name: 'Elegir otro archivo', exact: true })).toBeVisible();
  await dialog.getByLabel('Archivo Excel de existencias', { exact: true }).setInputFiles({ name: 'existencias-prueba.xlsx', mimeType: excelType, buffer: fixture });
  await dialog.getByRole('button', { name: 'Revisar archivo', exact: true }).click();
  await expect(dialog.getByRole('button', { name: 'Confirmar importación', exact: true })).toBeEnabled();
  expect(reviews).toBe(2);
  expect(attemptedBatches).toBe(1);
});

test('manual balance retries reuse the update ID while edited payloads receive a new ID and accept zero', async ({ page }) => {
  await supplier(page);
  const payloads: Record<string, unknown>[] = [];
  let current = stockItem();
  await page.route(`${root}/ingest`, async route => {
    const payload = route.request().postDataJSON();
    payloads.push(payload);
    if (payloads.length < 3) { await route.abort('failed'); return; }
    current = stockItem(existingItemId, String(payload.supplier_invent_id), Number(payload.quantity));
    await route.fulfill({ json: current });
  });
  await page.route(new RegExp(`${root}(?:\\?.*)?$`), route => route.fulfill({ json: { ...emptyPage, count: 1, results: [current] } }));
  await openPanel(page);
  await page.getByRole('button', { name: 'Actualizar existencias', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Actualizar un registro de existencias', exact: true });
  await dialog.getByLabel('ID de inventario del proveedor', { exact: true }).fill('supplier-id-lowercase');
  await expect(dialog.getByLabel('ID de inventario del proveedor', { exact: true })).toHaveValue('supplier-id-lowercase');
  await dialog.getByLabel('Código del repuesto', { exact: true }).fill('fe-one');
  await dialog.getByLabel('Marca', { exact: true }).fill('acme');
  await dialog.getByLabel('Descripción', { exact: true }).fill('descripción corregida');
  for (const [label, value] of [['Código del repuesto', 'FE-ONE'], ['Marca', 'ACME'], ['Descripción', 'DESCRIPCIÓN CORREGIDA']]) {
    await expect(dialog.getByLabel(label, { exact: true })).toHaveValue(value);
    await expect(dialog.getByLabel(label, { exact: true })).toHaveAttribute('autocomplete', 'off');
  }
  await dialog.getByLabel('Cantidad de existencias reportadas', { exact: true }).fill('3');
  const save = dialog.getByRole('button', { name: 'Guardar saldo de existencias', exact: true });
  await save.click();
  await expect(dialog.getByRole('alert')).toBeVisible();
  expect(payloads).toHaveLength(1);
  await save.click();
  await expect.poll(() => payloads.length).toBe(2);
  await expect(dialog.getByRole('alert')).toBeVisible();
  expect(payloads[1]).toEqual(payloads[0]);
  await dialog.getByLabel('Cantidad de existencias reportadas', { exact: true }).fill('0');
  await save.click();
  await expect(dialog).not.toBeVisible();
  expect(payloads).toHaveLength(3);
  expect(payloads[2]).toMatchObject({ supplier_invent_id: 'supplier-id-lowercase', codigo: 'FE-ONE', brand: 'ACME', description: 'DESCRIPCIÓN CORREGIDA', quantity: 0, source: 'upload' });
  expect(payloads[2].update_id).not.toBe(payloads[0].update_id);
});
