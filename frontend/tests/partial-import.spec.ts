import { test, expect, Page } from '@playwright/test';
import { excelDate, excelFixture } from './excel-fixture';

async function signIn(page: Page) {
  await page.goto('/');
  await page.getByRole('button', { name: 'Cuenta y listas', exact: true }).click();
  await page.getByRole('button', { name: 'Iniciar sesión', exact: true }).first().click();
  await page.getByLabel('Usuario', { exact: true }).fill(process.env.E2E_ADMIN_USERNAME!);
  await page.getByLabel('Contraseña', { exact: true }).fill(process.env.E2E_ADMIN_PASSWORD!);
  await page.getByRole('dialog').getByRole('button', { name: 'Iniciar sesión', exact: true }).last().click();
  await expect(page.getByText('Cuenta conectada', { exact: true })).toBeVisible();
}

async function upload(page: Page, filename: string, buffer: Buffer) {
  await page.goto('/administracion?seccion=inventario');
  await page.getByRole('button', { name: 'Importar Excel', exact: true }).click();
  await page.getByLabel('Archivo Excel', { exact: true }).setInputFiles({
    name: filename, mimeType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', buffer,
  });
  const response = page.waitForResponse(result => new URL(result.url()).pathname === '/api/management/catalog/import' && result.request().method() === 'POST');
  await page.getByRole('button', { name: 'Revisar archivo', exact: true }).click();
  return (await response).json();
}

test('valid Excel rows import immediately and a date error can be corrected after reload', async ({ page }) => {
  test.skip(!process.env.E2E_ADMIN_USERNAME || !process.env.E2E_ADMIN_PASSWORD, 'A disposable superuser fixture is required.');
  await signIn(page);
  const prefix = `${process.env.E2E_ADMIN_USERNAME}-PARTIAL-${Date.now().toString(36)}`.toUpperCase();
  const validSku = `${prefix}-VALID`;
  const repairedSku = `${prefix}-CORRECTED`;
  const filename = `${prefix}-PENDING.xlsx`;
  const date = excelDate(2026, 6, 12);
  const preview = await upload(page, filename, excelFixture([
    [validSku, 'repuesto válido', '', `${validSku}-ALT`],
    [date, date, `${prefix} ABRAZADERA INOXIDABLE 1/2`],
  ]));
  expect(preview).toMatchObject({ valid: true, rejected_row_count: 1, pending_error_count: 1, preview_count: 1 });
  expect(preview.summary).toMatchObject({ rows: 1, rejected_rows: 1, created_skus: 1 });
  expect(preview.preview[0].sku).toBe(validSku);
  await expect(page.getByRole('button', { name: /^Importar filas válidas(?: sin IA)?$/ })).toBeEnabled();
  await expect(page.getByText('Cargando clasificación…', { exact: true })).not.toBeVisible();
  await page.setViewportSize({ width: 1440, height: 1440 });
  await page.screenshot({ path: '/tmp/motionpartes-partial-import-desktop.png' });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.getByRole('button', { name: /^Importar filas válidas(?: sin IA)?$/ }).click();
  await expect(page.getByRole('dialog')).not.toBeVisible();
  await expect(page.getByText(/1 filas? pendientes? de corregir/)).toBeVisible();
  const imported = await (await page.request.get(`/api/management/catalog?search=${validSku}`)).json();
  expect(imported.count).toBe(1);
  expect(imported.results[0]).toMatchObject({ sku: validSku, name: 'REPUESTO VÁLIDO' });
  const deferred = await (await page.request.get(`/api/management/catalog/import/issues?job=${preview.job_id}`)).json();
  expect(deferred.count).toBe(1);
  expect(deferred.results[0]).toMatchObject({ row: 3, status: 'pending', filename });
  const original = deferred.results[0].original;

  await page.reload();
  await page.getByRole('button', { name: 'Errores de importación', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Errores de importación', exact: true });
  await expect(dialog).toBeVisible();
  const row = dialog.getByRole('row').filter({ hasText: filename });
  await expect(row).toBeVisible();
  await page.screenshot({ path: '/tmp/motionpartes-error-queue-desktop.png' });
  await row.getByRole('button', { name: 'Corregir fila 3', exact: true }).click();
  await dialog.getByLabel('SKU interno', { exact: true }).fill(repairedSku.toLowerCase());
  await expect(dialog.getByLabel('SKU interno', { exact: true })).toHaveValue(repairedSku);
  await expect(dialog.getByLabel('SKU interno', { exact: true })).toHaveAttribute('autocomplete', 'off');
  await dialog.getByLabel('Nombre del repuesto', { exact: true }).fill('abrazadera recuperada');
  await expect(dialog.getByLabel('Nombre del repuesto', { exact: true })).toHaveValue('ABRAZADERA RECUPERADA');
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await dialog.evaluate(element => element.scrollWidth > element.clientWidth)).toBe(false);
  await page.screenshot({ path: '/tmp/motionpartes-error-editor-mobile.png' });
  await dialog.getByRole('button', { name: 'Guardar y reintentar', exact: true }).click();
  await expect(dialog.getByText('Fila 3 corregida y guardada en el inventario interno.', { exact: true })).toBeVisible();
  await expect(dialog.getByRole('button', { name: 'Corregir fila 3', exact: true })).not.toBeVisible();
  const after = await (await page.request.get(`/api/management/catalog/import/issues?job=${preview.job_id}`)).json();
  expect(after.count).toBe(0);
  const repaired = await (await page.request.get(`/api/management/catalog?search=${repairedSku}`)).json();
  expect(repaired.count).toBe(1);
  expect(repaired.results[0]).toMatchObject({ sku: repairedSku, name: 'ABRAZADERA RECUPERADA' });
  const resolved = await (await page.request.get(`/api/management/catalog/import/issues/${deferred.results[0].id}`)).json();
  expect(resolved.status).toBe('resolved');
  expect(resolved.original).toEqual(original);
});

test('an entirely invalid file still leaves its rows in the persistent correction queue', async ({ page }) => {
  test.skip(!process.env.E2E_ADMIN_USERNAME || !process.env.E2E_ADMIN_PASSWORD, 'A disposable superuser fixture is required.');
  await signIn(page);
  const sku = `${process.env.E2E_ADMIN_USERNAME}-ALL-INVALID-${Date.now().toString(36)}`.toUpperCase();
  const filename = `${sku}.xlsx`;
  const preview = await upload(page, filename, excelFixture([[sku, 'repuesto por corregir', '', '', '', 'MAYBE']]));
  expect(preview).toMatchObject({ valid: false, status: 'review', rejected_row_count: 1, pending_error_count: 1 });
  expect(preview.summary.rows).toBe(0);
  expect(preview.progress.total_batches).toBe(0);
  const initial = await (await page.request.get(`/api/management/catalog?search=${sku}`)).json();
  expect(initial.count).toBe(0);
  await page.getByRole('dialog').getByRole('button', { name: 'Cerrar ventana', exact: true }).click();
  await page.reload();
  await page.getByRole('button', { name: 'Errores de importación', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Errores de importación', exact: true });
  await dialog.getByRole('row').filter({ hasText: filename }).getByRole('button', { name: 'Corregir fila 2', exact: true }).click();
  await expect(dialog.getByLabel('SKU interno', { exact: true })).toHaveValue(sku);
  await dialog.getByRole('combobox', { name: 'Estado del repuesto', exact: true }).selectOption('SI');
  await dialog.getByRole('button', { name: 'Guardar y reintentar', exact: true }).click();
  await expect(dialog.getByText('Fila 2 corregida y guardada en el inventario interno.', { exact: true })).toBeVisible();
  await expect(dialog.getByRole('row').filter({ hasText: filename })).not.toBeVisible();
  const after = await (await page.request.get(`/api/management/catalog?search=${sku}`)).json();
  expect(after.count).toBe(1);
  expect(after.results[0]).toMatchObject({ sku, name: 'REPUESTO POR CORREGIR', active: true });
  const pending = await (await page.request.get(`/api/management/catalog/import/issues?job=${preview.job_id}`)).json();
  expect(pending.count).toBe(0);
});
