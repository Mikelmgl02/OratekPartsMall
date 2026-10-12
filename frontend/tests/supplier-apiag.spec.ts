import { expect, Page, test } from '@playwright/test';
import { expectSignedIn } from './header';
import type { ApiagSyncStatus, LedgerEntry, StockItem } from '../lib/types';

// The supplier panel's apiag-cloud card and per-sucursal stock. Everything the page asks the API is mocked.
const accountId = '11111111-1111-4111-8111-111111111111';
const itemId = '44444444-4444-4444-8444-444444444444';
const root = `/api/market/accounts/${accountId}`;
const emptyPage = { count: 0, next: null, previous: null, results: [] };
const stamp = '2026-10-11T15:00:00Z';

function status(overrides: Partial<Extract<ApiagSyncStatus, { connected: true }>> = {}): ApiagSyncStatus {
  return { connected: true, server: 'mm.oratek.info', token_readable: true, state: 'completed', can_sync: true, interval_minutes: 5,
    last_started_at: stamp, last_finished_at: stamp, last_success_at: stamp, next_run_at: stamp, error: null, last_mode: 'full',
    counts: { received: 79, created: 79, updated: 0, withdrawn: 0, pages: 1 }, samples: [], progress: {},
    sucursales: [{ id: '1', nombre: 'BODEGA PRINCIPAL' }, { id: '4', nombre: 'RICARDO J ALFARO' }], ...overrides };
}

const apiagItem: StockItem = { id: itemId, supplier_invent_id: '3', part: null, codigo: '015301960AA', brand: 'KAIYI',
  description: 'INTERRUPTOR LUZ DE REVERSA', matching_status: 'matched', source: 'apiag', reported_quantity: 5, reserved_quantity: 1,
  available_quantity: 4, updated_at: stamp, references: [],
  branch_stock: [{ sucursal: '1', nombre: 'BODEGA PRINCIPAL', disponible: 3 }, { sucursal: '4', nombre: 'RICARDO J ALFARO', disponible: 1 },
    { sucursal: '5', nombre: 'SANTA MARIA', disponible: 1 }] };
const manualItem: StockItem = { ...apiagItem, id: '55555555-5555-4555-8555-555555555555', supplier_invent_id: 'M-1', codigo: 'MANUAL-1',
  source: 'upload', branch_stock: [] };

async function openPanel(page: Page, apiag: (method: string) => ApiagSyncStatus | null, items: StockItem[] = []) {
  const calls = { inventory: 0, posts: [] as string[] };
  await page.route('**/api/session', route => route.fulfill({ json: { authenticated: true,
    user: { id: 7, username: 'PROVEEDOR', first_name: '', last_name: '', is_superuser: false },
    accounts: { ...emptyPage, count: 1, results: [{ id: accountId, name: 'MOTION MOTORS', roles: ['supplier_wholesale'], capabilities: ['supplier'] }] } } }));
  await page.route(/\/api\/market\/catalog(?:\?.*)?$/, route => route.fulfill({ json: emptyPage }));
  await page.route(new RegExp(`${root}/inventory(?:\\?.*)?$`), route => {
    calls.inventory += 1;
    const unmatched = new URL(route.request().url()).searchParams.get('matching_status') === 'unmatched';
    return route.fulfill({ json: unmatched ? emptyPage : { ...emptyPage, count: items.length, results: items } });
  });
  await page.route(`${root}/inventory/${itemId}/ledger?page=1`, route => route.fulfill({ json: { ...emptyPage, count: 1, results: [
    { id: 1, kind: 'sync', quantity: 5, direction: 'credit', balance_type: 'stock', reserved_delta: 0, reserved_direction: '', reported_after: 5,
      reserved_after: 0, reference: 'apiag-cloud 2026-10-11 15:00 #ab12 · 3', created_at: stamp } satisfies LedgerEntry] } }));
  await page.route(new RegExp(`${root}/apiag(?:/sync)?$`), route => {
    const method = route.request().method();
    if (method === 'POST') calls.posts.push(route.request().postData() || '');
    const answer = apiag(method);
    return answer ? route.fulfill({ status: method === 'POST' ? 202 : 200, json: answer }) : route.fulfill({ status: 404, json: { detail: 'No encontrado.' } });
  });
  await page.goto('/');
  await expectSignedIn(page);
  if (page.viewportSize()!.width < 760) await page.getByRole('button', { name: 'Abrir tu panel', exact: true }).click();
  else await page.getByRole('button', { name: 'Para proveedores', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Actualizar existencias', exact: true })).toBeVisible();
  return calls;
}

test('a connected supplier sees the last pull and asks for one now, and the inventory reloads when it ends', async ({ page }) => {
  let current = status();
  const calls = await openPanel(page, method => {
    if (method === 'POST') current = status({ state: 'queued', can_sync: false });
    return current;
  });
  const card = page.getByRole('region', { name: 'Conexión con apiag-cloud' });
  await expect(card.getByText('Sincronizada', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Importar Excel', exact: true })).toHaveCount(0);  // its stock loads from apiag-cloud
  await expect(card.getByText(/^mm\.oratek\.info · Última sincronización: /)).toBeVisible();
  await expect(card.getByText('Sincronización completa: 79 nuevos · 0 actualizados · 0 retirados.')).toBeVisible();
  await expect(card.getByText('Sucursales publicadas: BODEGA PRINCIPAL, RICARDO J ALFARO.')).toBeVisible();
  await expect(page.getByText('Importa tus saldos desde Excel o actualiza registros de carga manual.', { exact: false })).toHaveCount(0);  // the static note gives way

  const before = calls.inventory;
  await card.getByRole('button', { name: 'Sincronizar ahora' }).click();
  expect(calls.posts).toEqual(['{}']);
  await expect(card.getByRole('button', { name: 'En cola…' })).toBeDisabled();
  await expect(card.getByRole('status')).toHaveText('Sincronización en cola. Continúa aunque cierres esta página.');
  current = status({ state: 'running', can_sync: false, progress: { received: 500, mode: 'changes' } });
  await expect(card.getByText('500 artículos recibidos hasta ahora.')).toBeVisible({ timeout: 12000 });
  current = status({ last_finished_at: '2026-10-11T15:05:00Z', last_success_at: '2026-10-11T15:05:00Z', last_mode: 'changes',
    counts: { received: 3, created: 1, updated: 2, withdrawn: 1 } });
  await expect(card.getByText('Sincronización de cambios: 1 nuevos · 2 actualizados · 1 retirados.')).toBeVisible({ timeout: 12000 });
  await expect.poll(() => calls.inventory).toBeGreaterThan(before);   // the grid and totals read the new inventory
  await expect(card.getByRole('button', { name: 'Sincronizar ahora' })).toBeEnabled();
});

test('errors, an unreadable token, refused items and no published sucursal are spelled out', async ({ page }) => {
  await openPanel(page, () => status({ state: 'paused', token_readable: false, sucursales: [],
    error: { code: 'token_rejected', message: 'Ese apiag-cloud rechazó el token: no coincide con motion_partes_access_token de su .env.' },
    counts: { received: 2, created: 0, updated: 0, withdrawn: 0, refused: 1, conflicts: 1 },
    samples: ['MANUAL (10) ya existe como carga manual: no se toca.', '11: {"codigo": ["Este campo no puede estar en blanco."]}'] }));
  const card = page.getByRole('region', { name: 'Conexión con apiag-cloud' });
  await expect(card.getByText('En pausa', { exact: true })).toBeVisible();
  await expect(card.getByRole('alert').filter({ hasText: 'rechazó el token' })).toBeVisible();
  await expect(card.getByRole('alert').filter({ hasText: 'ya no se puede leer' })).toBeVisible();
  await expect(card.getByText('2 no aplicados', { exact: false })).toBeVisible();
  await expect(card.getByText('Ninguna sucursal publicada', { exact: false })).toBeVisible();
  await card.screenshot({ path: '/tmp/motionpartes-apiag-card-errors.png' });
  await card.getByText('Artículos no aplicados en la última sincronización').click();
  await expect(card.getByText('MANUAL (10) ya existe como carga manual: no se toca.')).toBeVisible();
});

test('apiag-cloud items show their units per sucursal in the grid and in Movimientos', async ({ page }) => {
  await openPanel(page, () => status(), [apiagItem, manualItem]);
  await expect(page.getByRole('gridcell', { name: 'BODEGA PRINCIPAL 3 · RICARDO J ALFARO 1 · +1', exact: true })).toBeVisible();
  await expect(page.getByRole('row').filter({ hasText: 'MANUAL-1' }).getByRole('gridcell', { name: '—', exact: true })).toBeVisible();
  await expect(page.getByRole('columnheader', { name: 'Por sucursal' })).toBeVisible();
  await page.getByRole('button', { name: 'Ver movimientos de 015301960AA' }).click();
  const dialog = page.getByRole('dialog', { name: 'Movimientos de existencias · 015301960AA' });
  await expect(dialog.getByRole('heading', { name: 'Existencias por sucursal' })).toBeVisible();
  await expect(dialog.getByRole('row', { name: 'SANTA MARIA 1' })).toBeVisible();
  await expect(dialog.getByText(/^Sincronización con apiag-cloud · /)).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

test('without a connection the panel keeps its static note and shows no card', async ({ page }) => {
  await openPanel(page, () => ({ connected: false }));
  await expect(page.getByRole('region', { name: 'Conexión con apiag-cloud' })).toHaveCount(0);
  await expect(page.getByText('Importa tus saldos desde Excel o actualiza registros de carga manual.', { exact: false })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Importar Excel', exact: true })).toBeVisible();
});

test('the card fits a phone', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await openPanel(page, () => status());
  const card = page.getByRole('region', { name: 'Conexión con apiag-cloud' });
  await expect(card.getByRole('button', { name: 'Sincronizar ahora' })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await card.screenshot({ path: '/tmp/motionpartes-apiag-card-mobile.png' });
});
