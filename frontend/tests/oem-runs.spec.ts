import { expect, Page, test } from '@playwright/test';
import type { OEMHalt, OEMPendingPage, OEMRun, OEMRunPage, OEMSpotCheck, OEMSpotRow } from '../lib/oem-finder-types';

const stamp = '2026-10-06T12:00:00Z';
const empty = { count: 0, next: null, previous: null, results: [], pending_count: 0 };

function spotRow(change: number, patch: Partial<OEMSpotRow> = {}): OEMSpotRow {
  return { run: 12, change, batch: 1, tier: 'AUTO_FLAG_CURRENT', method: 'flag_current', part_id: `00000000-0000-4000-8000-0000000000${String(change).padStart(2, '0')}`,
    previous_sku: '48654-30030', sku: '48654-30030', current_sku: '48654-30030', code: '48654-30030', brand: 'TOYOTA', description: 'BASE AMORT TOY COROLLA',
    system: 'TOYOTA', grade: 'F3', lexicon: 'agree_strong', lexicon_share: 1, lexicon_keys: 7, class_heads: 'BASE|AMORT (7)', units: 'lexicon_agree',
    attributions: 'BARE', suppliers: 1, available_quantity: 4, reference_source: 'algo:oem-finder:v2:current:12', reasons: 'formato OEM de TOYOTA (F3)',
    reverted_at: '', verdict: '', evidence: { tier: 'AUTO_FLAG_CURRENT' }, ...patch };
}

// Mirrors the server: run history with the halt rule, the spot-check sample, per-SKU and whole-run undo (idempotent) and halt/resume.
async function fixture(page: Page) {
  test.skip(!process.env.E2E_ADMIN_TOKEN, 'A disposable superuser token is required for server-rendered administration.');
  await page.context().addCookies([{ name: 'partsmall_session', value: process.env.E2E_ADMIN_TOKEN!, url: 'http://localhost:8080', httpOnly: true, sameSite: 'Strict' }]);
  const rows = [spotRow(1), spotRow(2, { previous_sku: '54830-2H000-MOBIS', sku: '54830-2H000', current_sku: '54830-2H000', code: '54830-2H000', brand: 'HYUNDAI',
    tier: 'AUTO_RENAME_BASE', method: 'rename_base', description: 'TERM ESTAB HYU ELANTRA', system: 'HMG', grade: 'F4' })];
  const run: OEMRun = { id: 12, mode: 'apply_auto', status: 'completed', rules_version: 'oem-finder-2', suffix_table_version: 'db:7',
    scope: { tier: null, limit: null, canary: 50, in_stock_first: true, batch_size: 100, stage: 'command' }, tiers: { AUTO_FLAG_CURRENT: [4249, 3130] },
    applied: { applied: 48, conflicts: 1, skipped: { snapshot_changed: 1 }, errors: 0, tiers: { AUTO_FLAG_CURRENT: 47, AUTO_RENAME_BASE: 1 },
      batches: [{ index: 1, applied: 48, conflicts: 1, skipped: 1, errors: 0 }], stopped: null, excluded: {}, selected: 50, spot_check: [1, 2] },
    errors: [], actor: 'motionpartes-oem-finder-service', started_at: stamp, finished_at: stamp, changes: 48, reverted: 0 };
  let halt: OEMHalt | null = null;
  const unexpected: string[] = [], reverts: Record<string, unknown>[] = [], halts: Record<string, unknown>[] = [], lists: string[] = [];
  await page.route('**/api/**', route => { unexpected.push(`${route.request().method()} ${new URL(route.request().url()).pathname}`); return route.fulfill({ status: 404, json: { detail: 'Ruta no prevista' } }); });
  await page.route('**/api/market/analytics/events', route => route.fulfill({ status: 202, json: { recorded: true } }));
  await page.route(/\/api\/management\/(roles|catalog\/import\/issues|part-types)(?:\?.*)?$/, route => route.fulfill({ json: route.request().url().includes('/roles') ? [] : empty }));
  await page.route(/\/api\/management\/catalog(?:\?.*)?$/, route => route.fulfill({ json: empty }));
  const idle = { pending: 0, in_stock: 0, canary_done: true };
  const pending: OEMPendingPage = { count: 0, next: null, previous: null, results: [], tiers: {}, apply_tiers: {}, statuses: { applied: 48 }, facets: { chains: [], makes: [], systems: [] },
    apply: { AUTO_FLAG_CURRENT: idle, AUTO_RENAME_BASE: idle, STRONG_PENDING_OWNER_TAGS: idle }, sizes: { canary: 50, batch: 100 }, halt: null, busy: { lock: false, running: null }, last_refresh: null };
  await page.route(/\/api\/management\/oem-finder\/pending(?:\?.*)?$/, route => route.fulfill({ json: pending }));
  await page.route(/\/api\/management\/oem-finder\/runs(?:\?.*)?$/, route => {
    lists.push(new URL(route.request().url()).search);
    const body: OEMRunPage = { count: 1, next: null, previous: null, results: [run], halt };
    return route.fulfill({ json: body });
  });
  await page.route('**/api/management/oem-finder/runs/12/spot-check', route => {
    const body: OEMSpotCheck = { run: 12, columns: ['ejecucion', 'sku_anterior', 'correcto_si_no'], rows, csv: 'ejecucion,sku_anterior,correcto_si_no\n12,48654-30030,\n' };
    return route.fulfill({ json: body });
  });
  await page.route('**/api/management/oem-finder/runs/12/revert', route => {
    const body = route.request().postDataJSON() as Record<string, unknown>;
    reverts.push(body);
    const targets = body.part ? rows.filter(row => row.part_id === body.part) : rows;
    const fresh = targets.filter(row => !row.reverted_at);
    fresh.forEach(row => { row.reverted_at = stamp; });
    const reverted = body.part ? fresh.length : run.changes - run.reverted;
    run.reverted += reverted;
    return route.fulfill({ json: { run: 12, reverted, already_reverted: body.part ? targets.length - fresh.length : 0, skipped: [] } });
  });
  await page.route('**/api/management/oem-finder/halt', route => {
    const body = route.request().postDataJSON() as Record<string, unknown>;
    halts.push(body);
    halt = body.action === 'halt' ? { reason: String(body.reason), run: null, created_by: 'ADMIN', created_at: stamp } : null;
    return route.fulfill({ json: { halt } });
  });
  return { run, rows, unexpected, reverts, halts, lists };
}

async function openRuns(page: Page, width: number) {
  await page.setViewportSize({ width, height: 1000 });
  await page.goto('/administracion?seccion=inventario');
  await page.getByRole('button', { name: 'Aplicación OEM', exact: true }).click();
  return page.getByRole('dialog', { name: 'Aplicación OEM automática' });
}

for (const width of [1440, 390]) {
  test(`OEM auto-apply history: spot-check, download, undo and halt at ${width}px`, async ({ page }) => {
    const mock = await fixture(page);
    const dialog = await openRuns(page, width);
    await expect(dialog.getByText('#12', { exact: true })).toBeVisible();
    await expect(dialog.getByText('TODOS LOS NIVELES AUTOMÁTICOS · CANARIO 50')).toBeVisible();
    await expect(dialog.getByText('48 APLICADOS', { exact: true })).toBeVisible();
    await expect(dialog.getByText('1 A REVISIÓN POR CONFLICTO', { exact: true })).toBeVisible();
    await expect(dialog.getByText('1 OMITIDOS: CAMBIÓ DESDE EL ANÁLISIS', { exact: true })).toBeVisible();
    expect(mock.lists[0]).toContain('mode=apply_auto');

    await dialog.getByRole('button', { name: 'Muestra de control de la ejecución 12' }).click();
    await expect(dialog.getByRole('heading', { name: 'Muestra de control · ejecución #12' })).toBeVisible();
    await expect(dialog.getByText('54830-2H000-MOBIS → 54830-2H000', { exact: true })).toBeVisible();
    const file = page.waitForEvent('download');
    await dialog.getByRole('button', { name: 'Descargar CSV' }).click();
    expect((await file).suggestedFilename()).toBe('oem-run-12-spot-check.csv');
    await dialog.getByRole('button', { name: 'Deshacer 54830-2H000', exact: true }).click();
    expect(mock.reverts).toEqual([]);
    await dialog.getByRole('button', { name: 'Confirmar deshacer 54830-2H000', exact: true }).click();
    await expect(dialog.getByRole('status')).toContainText('1 cambio revertido');
    expect(mock.reverts).toEqual([{ part: mock.rows[1].part_id }]);
    await expect(dialog.getByText('REVERTIDO', { exact: true })).toBeVisible();

    await dialog.getByRole('button', { name: 'Volver a las ejecuciones' }).click();
    await expect(dialog.getByText('1 / 48', { exact: true })).toBeVisible();
    await dialog.getByRole('button', { name: 'Deshacer la ejecución 12' }).click();
    await dialog.getByRole('button', { name: 'Confirmar deshacer la ejecución 12' }).click();
    await expect(dialog.getByRole('status')).toContainText('47 cambios revertidos');
    expect(mock.reverts[1]).toEqual({});
    await expect(dialog.getByRole('button', { name: 'Deshacer la ejecución 12' })).toBeDisabled();

    await dialog.getByLabel('Motivo para detener').fill('2 FILAS INCORRECTAS');
    await dialog.getByRole('button', { name: 'Detener aplicación automática' }).click();
    await expect(dialog.getByRole('alert')).toContainText('APLICACIÓN DETENIDA: 2 FILAS INCORRECTAS');
    await dialog.getByRole('button', { name: 'Levantar detención' }).click();
    await expect(dialog.getByRole('status')).toContainText('Detención levantada.');
    expect(mock.halts).toEqual([{ action: 'halt', reason: '2 FILAS INCORRECTAS' }, { action: 'resume' }]);
    expect(mock.unexpected).toEqual([]);
  });
}

test('the proxy only forwards the OEM run routes it allows', async ({ page }) => {
  test.skip(!process.env.E2E_ADMIN_TOKEN, 'A disposable superuser token is required for server-rendered administration.');
  await page.context().addCookies([{ name: 'partsmall_session', value: process.env.E2E_ADMIN_TOKEN!, url: 'http://localhost:8080', httpOnly: true, sameSite: 'Strict' }]);
  await page.goto('/');
  const status = await page.evaluate(async () => (await fetch('/api/management/oem-finder/runs/12/apply', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })).status);
  expect(status).toBe(404);
});
