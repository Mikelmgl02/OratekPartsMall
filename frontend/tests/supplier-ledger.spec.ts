import { expect, test } from '@playwright/test';
import { signedOutInbox } from './inbox-route';
import { noNewProducts } from './new-products-route';
import type { LedgerChange, LedgerRow, LedgerRun } from '../lib/ledger-types';

// Libro de movimientos del proveedor: the supplier's own panel (Inventario → Libro de movimientos / Cargas y sincronizaciones) and
// Administración → Inventario → Libro de movimientos. Every API call is mocked.
const supplierId = '22222222-2222-4222-8222-222222222222';
const orderId = '44444444-4444-4444-8444-444444444444';
const runId = '55555555-5555-4555-8555-555555555555';
const stamp = '2026-10-12T15:00:00Z';
const empty = { count: 0, next: null, previous: null, results: [] };
const page = <T,>(results: T[]) => ({ count: results.length, next: null, previous: null, results });
const item = { id: '66666666-6666-4666-8666-666666666661', supplier_invent_id: 'A-001', codigo: '58411-1R000-G', brand: 'KSM', description: 'TAMBOR', source: 'upload' };
const rows: LedgerRow[] = [
  { id: 3, created_at: stamp, kind: 'reserve', kind_label: 'Reserva', origin: 'quotation', origin_label: 'Cotización confirmada', item, stock_delta: 0, reserved_delta: 3,
    available_delta: -3, reported_after: 10, reserved_after: 3, available_after: 7, reference: 'ORD-20261012-AAAA1111', actor: 'COMPRADOR',
    order: { id: orderId, reference: 'ORD-20261012-AAAA1111', client_name: 'TALLER CENTRAL', status: 'handshaked', status_label: 'Acuerdo confirmado', revision: 2 }, run: null },
  { id: 2, created_at: stamp, kind: 'sync', kind_label: 'Ajuste de existencias', origin: 'excel', origin_label: 'Carga Excel', item, stock_delta: 5, reserved_delta: 0,
    available_delta: 5, reported_after: 10, reserved_after: 0, available_after: 10, reference: `excel-${runId}-2`, actor: 'EMPLEADO', order: null,
    run: { id: runId, label: `excel-${runId}`, name: 'EXISTENCIAS.xlsx', origin: 'excel' } },
];
const run: LedgerRun = { id: runId, origin: 'excel', origin_label: 'Carga Excel', label: `excel-${runId}`, name: 'EXISTENCIAS.xlsx', filename: 'EXISTENCIAS.xlsx', mode: '', mode_label: '', started_at: stamp,
  finished_at: stamp, actor: 'EMPLEADO', rebuilt: false, import_job: null, counts: { created: 1, updated: 1, stock: 0, withdrawn: 0, credit_units: 7, debit_units: 0 } };
const changes: LedgerChange[] = [
  { id: 1, action: 'updated', action_label: 'Modificado', created_at: stamp, item, fields: [{ field: 'description', label: 'Descripción', before: 'TAMBOR', after: 'TAMBOR TRASERO' }],
    reason: null, reason_label: '', quantity_before: 5, quantity_after: 10, delta: 5, branches: [{ sucursal: '1', nombre: 'MATRIZ', before: 4, after: 1 }] },
];

async function ledgerRoutes(context: import('@playwright/test').Page, prefix: string) {
  const queries: URLSearchParams[] = [];
  await context.route(new RegExp(`${prefix}/ledger(\\?.*)?$`), route => { queries.push(new URL(route.request().url()).searchParams); return route.fulfill({ json: page(rows) }); });
  await context.route(new RegExp(`${prefix}/ledger/runs(\\?.*)?$`), route => route.fulfill({ json: page([run]) }));
  await context.route(new RegExp(`${prefix}/ledger/runs/${runId}$`), route => route.fulfill({ json: run }));
  await context.route(new RegExp(`${prefix}/ledger/runs/${runId}/changes(\\?.*)?$`), route => route.fulfill({ json: page(changes) }));
  return queries;
}

test('the supplier reads its ledger: what confirmed quotations moved, what each load changed, and no Excel import while synced', async ({ page: browser }) => {
  await browser.route('**/api/**', route => { if (signedOutInbox(route)) return; return route.fulfill({ json: empty }); });
  await browser.route('**/api/session', route => route.fulfill({ json: { authenticated: true, user: { id: 801, username: 'EMPLEADO', first_name: '', last_name: '', is_superuser: false },
    accounts: { ...empty, count: 1, results: [{ id: supplierId, name: 'MOTION MOTORS', roles: ['supplier_wholesale'], capabilities: ['supplier'], permission: 'staff', apiag_connected: true }] } } }));
  await browser.route(/\/api\/market\/accounts\/[^/]+\/apiag$/, route => route.fulfill({ json: { connected: false } }));
  const queries = await ledgerRoutes(browser, `/api/market/accounts/${supplierId}`);
  await browser.goto(`/?vista=proveedor&seccion=inventario&cuenta=${supplierId}&pestana=movimientos`);
  await expect(browser.getByRole('tab', { name: 'Libro de movimientos' })).toHaveAttribute('aria-selected', 'true');
  await expect(browser.getByRole('button', { name: 'Importar Excel' })).toHaveCount(0);   // synced with apiag-cloud
  await expect(browser.getByRole('button', { name: 'Actualizar existencias' })).toBeVisible();
  const order = browser.getByRole('link', { name: 'ORD-20261012-AAAA1111 · TALLER CENTRAL' });
  await expect(order).toHaveAttribute('href', `/proveedor/${supplierId}/ordenes/${orderId}`);
  await expect(browser.getByText('Cotización v2 · Acuerdo confirmado')).toBeVisible();
  await expect(browser.locator('.ag-row[row-id="3"] .ag-cell[col-id="reserved"]').first()).toHaveText('+3');
  await expect(browser.locator('.ag-row[row-id="3"] .ag-cell[col-id="available"]').first()).toHaveText('−3');
  await browser.screenshot({ path: '/tmp/motionpartes-supplier-ledger.png' });

  await browser.getByLabel('Origen').selectOption('quotation');
  await expect.poll(() => queries.at(-1)?.get('origin')).toBe('quotation');
  await browser.getByLabel('Desde').fill('2026-10-01');
  await browser.getByLabel('Hasta').fill('2026-10-12');
  await expect.poll(() => [queries.at(-1)?.get('after'), queries.at(-1)?.get('before')]).toEqual(['2026-10-01', '2026-10-12']);

  await browser.getByRole('button', { name: 'Ver qué cambió en EXISTENCIAS.xlsx' }).click();
  const drawer = browser.getByRole('dialog', { name: 'Carga Excel · EXISTENCIAS.xlsx' });
  await expect(drawer.getByText('Descripción: TAMBOR → TAMBOR TRASERO')).toBeVisible();
  await expect(drawer.getByText('MATRIZ: 4 → 1')).toBeVisible();
  await expect(drawer.locator('.ag-cell[col-id="quantity"]').first()).toHaveText('5 → 10 (+5)');
  await drawer.screenshot({ path: '/tmp/motionpartes-supplier-ledger-run.png' });
  await drawer.getByRole('button', { name: 'Cerrar ventana' }).click();
  await browser.mouse.move(400, 600);  // the close button sits over the account menu, which opens on hover
  await expect(browser.getByRole('button', { name: 'Cuenta y listas' })).toHaveAttribute('aria-expanded', 'false');

  await browser.getByRole('tab', { name: 'Cargas y sincronizaciones' }).click();
  await expect(browser.getByRole('button', { name: 'Ver qué cambió en EXISTENCIAS.xlsx' })).toBeVisible();
  await expect(browser.locator('.ag-cell[col-id="units"]').first()).toHaveText('+7 / −0');
  await browser.setViewportSize({ width: 390, height: 844 });
  expect(await browser.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

test('Administración opens any supplier ledger, units only', async ({ page: browser, baseURL }) => {
  test.skip(!process.env.E2E_ADMIN_TOKEN, 'A disposable superuser token is required for server-rendered administration.');
  await browser.context().addCookies([{ name: 'partsmall_session', value: process.env.E2E_ADMIN_TOKEN!, url: baseURL || 'http://localhost:8080', httpOnly: true, sameSite: 'Strict' }]);
  await browser.route('**/api/**', route => { if (signedOutInbox(route) || noNewProducts(route)) return; return route.fulfill({ json: empty }); });
  await browser.route(/\/api\/management\/roles$/, route => route.fulfill({ json: [{ code: 'supplier_wholesale', name: 'MAYORISTA', capability: 'supplier' }, { code: 'client_business', name: 'TALLER', capability: 'client' }] }));
  await browser.route(/\/api\/management\/accounts(\?.*)?$/, route => route.fulfill({ json: page([
    { id: supplierId, name: 'MOTION MOTORS', active: true, roles: ['supplier_wholesale'], apiag: { server_url: 'https://mm.oratek.info' } },
    { id: '77777777-7777-4777-8777-777777777777', name: 'TALLER CENTRAL', active: true, roles: ['client_business'], apiag: null }]) }));
  const queries = await ledgerRoutes(browser, `/api/management/accounts/${supplierId}`);
  await browser.goto(`/administracion?seccion=inventario&vista=movimientos&proveedor=${supplierId}`);
  await expect(browser.getByRole('button', { name: 'Libro de movimientos' })).toHaveAttribute('aria-pressed', 'true');
  await expect(browser.getByLabel('Proveedor', { exact: true })).toHaveValue(supplierId);
  await expect(browser.getByLabel('Proveedor', { exact: true }).locator('option')).toHaveCount(1);   // only supplier accounts
  await expect(browser.getByText('Sincronizada con apiag-cloud (https://mm.oratek.info): la carga Excel no está disponible.')).toBeVisible();
  await expect(browser.getByText('ORD-20261012-AAAA1111 · TALLER CENTRAL')).toBeVisible();
  await expect.poll(() => queries.length).toBeGreaterThan(0);
  await browser.getByRole('tab', { name: 'Cargas y sincronizaciones' }).click();
  await expect(browser.getByRole('button', { name: 'Ver qué cambió en EXISTENCIAS.xlsx' })).toBeVisible();
});
