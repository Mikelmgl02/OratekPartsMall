import { existsSync } from 'node:fs';
import { expect, Page, test } from '@playwright/test';
import { excelDate, excelFixture } from './excel-fixture';

const workbookType = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet';

async function signIn(page: Page) {
  await page.goto('/');
  await page.getByRole('button', { name: 'Cuenta y listas', exact: true }).click();
  await page.getByRole('button', { name: 'Iniciar sesión', exact: true }).first().click();
  await page.getByLabel('Usuario', { exact: true }).fill(process.env.E2E_ADMIN_USERNAME!);
  await page.getByLabel('Contraseña', { exact: true }).fill(process.env.E2E_ADMIN_PASSWORD!);
  await page.getByRole('dialog').getByRole('button', { name: 'Iniciar sesión', exact: true }).last().click();
  await expect(page.getByText('Cuenta conectada', { exact: true })).toBeVisible();
  await page.goto('/administracion?seccion=inventario');
  await page.getByRole('button', { name: 'Importar Excel', exact: true }).click();
}

async function review(page: Page, button = 'Revisar archivo') {
  const response = page.waitForResponse(item => new URL(item.url()).pathname === '/api/management/catalog/import' && item.request().method() === 'POST');
  await page.getByRole('button', { name: button, exact: true }).click();
  const received = await response;
  expect(received.status()).toBe(200);
  return received.json();
}

function fixtureSku(suffix: string) {
  return `${process.env.E2E_ADMIN_USERNAME}-RECOVERY-${suffix}`.toUpperCase();
}

test.beforeEach(async ({ page }) => {
  test.skip(!process.env.E2E_ADMIN_USERNAME || !process.env.E2E_ADMIN_PASSWORD, 'A disposable superuser fixture is required.');
  await signIn(page);
});

test('date recovery shows ambiguous codes first and keeps automatic recoveries collapsed', async ({ page }) => {
  const sku = fixtureSku('AUTOMATIC');
  const ambiguous = excelDate(2026, 6, 12);
  const workbook = excelFixture([
    [sku, '', 'BALINERA 10-3042', excelDate(3042, 10, 1, 'mmm-yy')],
    [ambiguous, ambiguous, 'ABRAZADERA INOXIDABLE 1/2'],
  ]);
  await page.getByLabel('Archivo Excel', { exact: true }).setInputFiles({ name: 'recuperaciones.xlsx', mimeType: workbookType, buffer: workbook });
  const preview = await review(page);
  expect(preview).toMatchObject({ valid: true, error_count: 1, date_recovery_count: 1, source_rows: 2, rejected_row_count: 1, pending_error_count: 1, summary: { rows: 1 } });
  expect(preview.recovery_rows).toEqual(expect.arrayContaining([
    expect.objectContaining({ row: 2, column: 'CODIGO_ALTERNO', automatic: true, resolved: true, value: '10-3042' }),
    expect.objectContaining({ row: 3, column: 'SKU', automatic: false, resolved: false }),
  ]));
  const recovery = page.locator('details[aria-label="Recuperación de códigos"]');
  await expect(recovery).not.toHaveAttribute('open');
  await expect(page.getByRole('button', { name: /^Importar filas válidas(?: sin IA)?$/ })).toBeEnabled();
  await recovery.locator(':scope > summary').click();
  await expect(recovery.locator('.import-code-row').first().getByLabel('Código recuperado fila 3 SKU', { exact: true })).toBeVisible();
  await expect(page.getByLabel('Código recuperado fila 3 SKU', { exact: true })).toHaveValue('6-12');
  await expect(page.getByLabel('Código recuperado fila 2 CODIGO_ALTERNO', { exact: true })).not.toBeVisible();
  await expect(recovery.locator('.import-automatic-recovery')).not.toHaveAttribute('open');
  await page.getByText('Ver 1 código recuperado automáticamente', { exact: true }).click();
  await expect(page.getByLabel('Código recuperado fila 2 CODIGO_ALTERNO', { exact: true })).toHaveValue('10-3042');
  await expect(page.getByRole('button', { name: /^Importar filas válidas(?: sin IA)?$/ })).toBeEnabled();
  const unchanged = await (await page.request.get(`/api/management/catalog?search=${sku}`)).json();
  expect(unchanged.count).toBe(0);
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.getByRole('dialog').evaluate(dialog => dialog.scrollWidth > dialog.clientWidth)).toBe(false);
  await page.screenshot({ path: '/tmp/motionpartes-date-recovery-mobile.png' });
});

test('reviewed uppercase date correction survives reload and saves the recovered SKU and name', async ({ page }) => {
  const sku = fixtureSku('MANUAL');
  const original = excelDate(2026, 6, 12);
  const workbook = excelFixture([[original, original, 'ABRAZADERA DE PRUEBA']]);
  await page.getByLabel('Archivo Excel', { exact: true }).setInputFiles({ name: 'corregir-fecha.xlsx', mimeType: workbookType, buffer: workbook });
  const initial = await review(page);
  expect(initial).toMatchObject({ valid: false, status: 'review', rejected_row_count: 1, pending_error_count: 1 });
  expect(initial.job_id).toBeTruthy();
  expect(initial.recovery_rows[0]).toMatchObject({ row: 2, column: 'SKU', resolved: false, suggested_code: '6-12' });
  await page.locator('details[aria-label="Recuperación de códigos"] > summary').click();
  const recovered = page.getByLabel('Código recuperado fila 2 SKU', { exact: true });
  await expect(recovered).toHaveAttribute('autocomplete', 'off');
  await expect(recovered).toHaveValue('6-12');
  await recovered.fill(sku.toLowerCase());
  await expect(recovered).toHaveValue(sku);
  await expect(page.getByRole('button', { name: /^Importar filas válidas(?: sin IA)?$/ })).toBeDisabled();
  const corrected = await review(page, 'Aplicar correcciones y revisar');
  expect(corrected).toMatchObject({ valid: true, error_count: 0, date_recovery_count: 1, summary: { rows: 1 } });
  expect(corrected.job_id).toBe(initial.job_id);
  expect(corrected.pending_error_count).toBe(0);
  expect(corrected.preview[0]).toMatchObject({ sku, name: sku, description: 'ABRAZADERA DE PRUEBA' });
  expect(corrected.recovery_rows[0]).toMatchObject({ automatic: false, resolved: true, skipped: false, value: sku });
  expect((await (await page.request.get(`/api/management/catalog?search=${sku}`)).json()).count).toBe(0);
  await expect(page.getByRole('button', { name: /^(Confirmar importación|Importar sin IA)$/ })).toBeEnabled();

  await page.reload();
  await page.getByRole('button', { name: 'Importar Excel', exact: true }).click();
  await page.locator('details[aria-label="Recuperación de códigos"] > summary').click();
  const restored = page.getByLabel('Código recuperado fila 2 SKU', { exact: true });
  await expect(restored).toHaveValue(sku);
  await expect(restored).toBeDisabled();
  await expect(restored).toHaveAttribute('autocomplete', 'off');
  await expect(page.getByLabel('Excluir fila 2 SKU', { exact: true })).toBeDisabled();
  await expect(page.getByRole('button', { name: 'Aplicar correcciones y revisar', exact: true })).not.toBeVisible();
  await expect(page.getByText('Esta vista conserva las decisiones guardadas. Para cambiar los códigos, selecciona de nuevo el archivo Excel.', { exact: true })).toBeVisible();
  const persisted = await (await page.request.get(`/api/management/catalog/import/jobs/${corrected.job_id}`)).json();
  expect(persisted.recovery_rows).toEqual(corrected.recovery_rows);
  await page.getByRole('button', { name: /^(Confirmar importación|Importar sin IA)$/ }).click();
  await expect(page.getByText('Importación completada: 1 SKU nuevos, 0 SKU actualizados y 0 alternos nuevos.', { exact: true })).toBeVisible();
  const saved = await (await page.request.get(`/api/management/catalog?search=${sku}`)).json();
  expect(saved.count).toBe(1);
  expect(saved.results[0]).toMatchObject({ sku, name: sku, description: 'ABRAZADERA DE PRUEBA', codes: [] });
  expect(await page.evaluate(() => localStorage.getItem('motionpartes.catalog-import-job'))).toBeNull();
});

test('explicitly excluded date row stays deferred after reload and is reported on completion', async ({ page }) => {
  const sku = fixtureSku(`KEEP-${Date.now().toString(36)}`);
  const workbook = excelFixture([[sku, 'REPUESTO VÁLIDO'], [excelDate(2026, 6, 12), '', 'FILA AMBIGUA']]);
  await page.getByLabel('Archivo Excel', { exact: true }).setInputFiles({ name: 'excluir-fecha.xlsx', mimeType: workbookType, buffer: workbook });
  const initial = await review(page);
  expect(initial).toMatchObject({ valid: true, rejected_row_count: 1, pending_error_count: 1 });
  await page.locator('details[aria-label="Recuperación de códigos"] > summary').click();
  await page.getByLabel('Excluir fila 3 SKU', { exact: true }).check();
  await expect(page.getByLabel('Código recuperado fila 3 SKU', { exact: true })).toBeDisabled();
  await expect(page.getByRole('button', { name: /^Importar filas válidas(?: sin IA)?$/ })).toBeDisabled();
  const reviewed = await review(page, 'Aplicar correcciones y revisar');
  expect(reviewed).toMatchObject({ valid: true, source_rows: 2, skipped_rows: [3], skipped_row_count: 1, rejected_row_count: 1, pending_error_count: 1, summary: { rows: 1, skipped_rows: 1 } });
  expect(reviewed.job_id).toBe(initial.job_id);
  expect(reviewed.recovery_rows[0]).toMatchObject({ row: 3, skipped: true });
  await expect(page.getByRole('dialog').getByRole('status').filter({ hasText: 'Se excluirá 1 fila de esta importación. 1 de 2 filas del archivo se incluyen en la vista previa.' })).toBeVisible();

  await page.reload();
  await page.getByRole('button', { name: 'Importar Excel', exact: true }).click();
  await page.locator('details[aria-label="Recuperación de códigos"] > summary').click();
  await expect(page.getByLabel('Excluir fila 3 SKU', { exact: true })).toBeChecked();
  await expect(page.getByLabel('Excluir fila 3 SKU', { exact: true })).toBeDisabled();
  const restored = await (await page.request.get(`/api/management/catalog/import/jobs/${reviewed.job_id}`)).json();
  expect(restored.skipped_rows).toEqual([3]);
  expect(restored.recovery_rows).toEqual(reviewed.recovery_rows);
  await page.getByRole('button', { name: /^Importar filas válidas(?: sin IA)?$/ }).click();
  await expect(page.getByText(/Importación completada: 1 SKU nuevos, 0 SKU actualizados y 0 alternos nuevos\. 1 fila pendiente de corregir en «Errores de importación»\./)).toBeVisible();
  const completed = await (await page.request.get(`/api/management/catalog/import/jobs/${reviewed.job_id}`)).json();
  expect(completed).toMatchObject({ imported: true, status: 'completed', skipped_rows: [3], skipped_row_count: 1, pending_error_count: 1 });
  const issues = await (await page.request.get(`/api/management/catalog/import/issues?job=${reviewed.job_id}`)).json();
  expect(issues.count).toBe(1);
  const saved = await (await page.request.get(`/api/management/catalog?search=${sku}`)).json();
  expect(saved.count).toBe(1);
  expect(saved.results[0]).toMatchObject({ sku, name: 'REPUESTO VÁLIDO' });
});

test('large supplied workbook offers recoveries and inline review without creating catalog items', async ({ page }) => {
  const source = process.env.E2E_RECOVERY_WORKBOOK;
  test.skip(!source || !existsSync(source), 'Set E2E_RECOVERY_WORKBOOK to an existing local workbook for this optional integration check.');
  test.setTimeout(120000);
  const before = await (await page.request.get('/api/management/catalog')).json();
  await page.getByLabel('Archivo Excel', { exact: true }).setInputFiles(source!);
  const preview = await review(page);
  expect(preview).toMatchObject({ valid: true, rejected_row_count: 8, pending_error_count: 8, summary: { rows: 33312 } });
  expect(preview.source_rows).toBe(33320);
  expect(preview.recovery_count).toBe(49);
  expect(preview.date_recovery_count).toBe(41);
  expect(preview.numeric_warning_count).toBe(760);
  expect(preview.error_count).toBe(8);
  expect(preview.recovery_rows.filter((item: { automatic: boolean; resolved: boolean }) => item.automatic && item.resolved)).toHaveLength(41);
  expect(preview.recovery_rows.filter((item: { resolved: boolean; skipped: boolean }) => !item.resolved && !item.skipped)).toHaveLength(8);
  await expect(page.getByRole('button', { name: /^Importar filas válidas(?: sin IA)?$/ })).toBeEnabled();
  await page.locator('details[aria-label="Recuperación de códigos"] > summary').click();
  await expect(page.getByRole('heading', { name: 'Códigos que Excel convirtió en fechas', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Aplicar correcciones y revisar', exact: true })).toBeVisible();
  await expect(page.getByRole('dialog').getByRole('status').filter({ hasText: '41 códigos recuperados automáticamente · 8 por revisar' })).toBeVisible();
  await page.getByRole('heading', { name: 'Códigos que Excel convirtió en fechas', exact: true }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: '/tmp/motionpartes-recovery-desktop.png' });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.getByRole('dialog').evaluate(dialog => dialog.scrollWidth > dialog.clientWidth)).toBe(false);
  await page.getByRole('heading', { name: 'Códigos que Excel convirtió en fechas', exact: true }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: '/tmp/motionpartes-recovery-mobile.png' });
  await page.setViewportSize({ width: 1440, height: 1000 });
  // This explicitly reviews the suggestions for this disposable job, and only
  // stages the data. The real user still reviews their own eight uncertain codes.
  const staged = await review(page, 'Aplicar correcciones y revisar');
  expect(staged).toMatchObject({ valid: true, error_count: 0, source_rows: 33320, recovery_count: 49, summary: { rows: 33320 } });
  expect(staged.job_id).toBe(preview.job_id);
  expect(staged.pending_error_count).toBe(0);
  const issues = await (await page.request.get(`/api/management/catalog/import/issues?job=${staged.job_id}`)).json();
  expect(issues.count).toBe(0);
  await expect(page.getByRole('button', { name: /^(Confirmar importación|Importar sin IA)$/ })).toBeEnabled();
  const after = await (await page.request.get('/api/management/catalog')).json();
  expect(after.count).toBe(before.count);
});
