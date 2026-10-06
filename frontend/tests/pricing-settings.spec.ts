import { expect, Page, test } from '@playwright/test';
import type { MembershipPermission } from '../lib/types';
import type { PricingSettings } from '../lib/pricing-types';

const supplierId = '22222222-2222-4222-8222-222222222222';
const stamp = '2026-10-05T15:00:00Z';
const empty = { count: 0, next: null, previous: null, results: [] };
const settingsUrl = `/api/market/accounts/${supplierId}/pricing/settings`;

function settings(permission: MembershipPermission, extra: Partial<PricingSettings> = {}): PricingSettings {
  const values = { default_currency: 'USD', usd_pab_parity: true, config_min_permission: 'staff', publish_min_permission: 'staff', over_request_policy: 'confirm',
    over_stock_policy: 'confirm', accept_shortfall_policy: 'block', prefill_quantity: 'requested', assistant_enabled: false, version: 1, updated_at: null, updated_by: null,
    ...extra } as PricingSettings;
  const rank = { staff: 0, manager: 1, owner: 2 };
  return { ...values, permission, can_configure: rank[permission] >= rank[values.config_min_permission], can_manage_permissions: permission === 'owner' };
}
// Server-like settings: only changed fields count, owner-only fields need the owner and a stale version returns 409 with the current values.
async function open(page: Page, permission: MembershipPermission, initial: Partial<PricingSettings> = {}) {
  const unexpected: string[] = [], writes: Record<string, unknown>[] = [];
  let current = settings(permission, initial);
  await page.route('**/api/**', route => { unexpected.push(`${route.request().method()} ${new URL(route.request().url()).pathname}`); return route.fulfill({ status: 404, json: { detail: 'Ruta no prevista' } }); });
  await page.route('**/api/session', route => route.fulfill({ json: { authenticated: true, user: { id: 801, username: 'EMPLEADO', first_name: '', last_name: '', is_superuser: false },
    accounts: { ...empty, count: 1, results: [{ id: supplierId, name: 'REPUESTOS CENTRAL', roles: ['supplier_retail'], capabilities: ['supplier'], permission }] } } }));
  await page.route('**/api/market/analytics/events', route => route.fulfill({ status: 202, json: { recorded: true } }));
  await page.route('**/api/market/wishlist/state', route => route.fulfill({ json: { part_ids: [], count: 0 } }));
  await page.route(/\/api\/market\/catalog(?:\?.*)?$/, route => route.fulfill({ json: empty }));
  // The Precios section always reads the supplier's lists, whichever sub-section opens.
  await page.route(`/api/market/accounts/${supplierId}/price-lists`, route => route.fulfill({ json: { can_configure: permission !== 'staff', item_count: 0, results: [] } }));
  await page.route(settingsUrl, route => {
    if (route.request().method() === 'GET') return route.fulfill({ json: current });
    const body = route.request().postDataJSON() as Record<string, unknown>; writes.push(body);
    if (!current.can_configure)
      return route.fulfill({ status: 403, json: { detail: 'Tu permiso en la cuenta no permite cambiar esta configuración. Pide a un administrador de tu cuenta que la cambie.' } });
    const changes = Object.entries(body).filter(([key, value]) => key !== 'expected_version' && current[key as keyof PricingSettings] !== value);
    if (permission !== 'owner' && changes.some(([key]) => ['config_min_permission', 'publish_min_permission', 'assistant_enabled'].includes(key)))
      return route.fulfill({ status: 403, json: { detail: 'Solo el propietario de la cuenta puede cambiar quién configura precios, quién envía cotizaciones y el asistente de IA.' } });
    if (changes.length && body.expected_version !== current.version)
      return route.fulfill({ status: 409, json: { detail: 'Otro miembro de tu equipo cambió esta configuración. Revisa los valores actuales.', settings: current } });
    if (changes.length) current = { ...current, ...Object.fromEntries(changes), version: current.version + 1, updated_at: stamp, updated_by: { name: 'EMPLEADO' } };
    return route.fulfill({ json: current });
  });
  await page.goto(`/?vista=proveedor&seccion=precios&cuenta=${supplierId}&pestana=configuracion`);
  const panel = page.getByRole('tabpanel', { name: 'Configuración', exact: true });
  await expect(page.getByRole('tab', { name: 'Configuración', exact: true })).toHaveAttribute('aria-selected', 'true');
  await expect(panel.getByLabel('Quién puede enviar cotizaciones')).toBeVisible();
  return { panel, writes, unexpected, change: (values: Partial<PricingSettings>) => { current = settings(permission, { ...current, ...values, version: current.version + 1 }); }, current: () => current };
}

test('the owner sets who sends quotations and the policies; only the changed fields travel', async ({ page }) => {
  const state = await open(page, 'owner');
  const { panel } = state;
  for (const label of ['Quién puede configurar precios', 'Quién puede enviar cotizaciones', 'Ofrecer más de lo disponible', 'Ofrecer más de lo solicitado', 'Cantidad sugerida',
    'Moneda predeterminada', 'Si el cliente confirma y faltan existencias']) await expect(panel.getByLabel(label)).toBeEnabled();
  await expect(panel.getByLabel('Tratar USD y PAB como equivalentes')).toBeChecked();
  await expect(panel.getByLabel('Usar el asistente de cotización')).not.toBeChecked();
  await expect(panel).toContainText('El texto de la orden y de la conversación se envía a Google Gemini para interpretarlo. Nunca se envían precios ni existencias.');
  await expect(panel.getByText('Solo el propietario de la cuenta cambia los permisos y el asistente de IA.')).toHaveCount(0);
  await panel.getByRole('button', { name: 'Guardar configuración', exact: true }).click();
  await expect(panel.getByRole('status')).toHaveText('No hay cambios por guardar.');
  expect(state.writes).toEqual([]);
  await panel.getByLabel('Quién puede enviar cotizaciones').selectOption('manager');
  await panel.getByLabel('Ofrecer más de lo disponible').selectOption('block');
  await panel.getByLabel('Si el cliente confirma y faltan existencias').selectOption('allow');
  await panel.getByLabel('Tratar USD y PAB como equivalentes').uncheck();
  await panel.getByRole('button', { name: 'Guardar configuración (4)', exact: true }).click();
  await expect(panel.getByRole('status')).toHaveText('Configuración guardada.');
  expect(state.writes).toEqual([{ expected_version: 1, publish_min_permission: 'manager', over_stock_policy: 'block', accept_shortfall_policy: 'allow', usd_pab_parity: false }]);
  await expect(panel.getByLabel('Quién puede enviar cotizaciones')).toHaveValue('manager');
  await expect(panel).toContainText('Última actualización por EMPLEADO');
  await expect(panel.getByRole('button', { name: 'Guardar configuración', exact: true })).toBeVisible();
  expect(state.unexpected).toEqual([]);
});

test('a manager cannot touch owner-only fields, and a conflict keeps the manager\'s own edit over the other member\'s values', async ({ page }) => {
  const state = await open(page, 'manager', { publish_min_permission: 'manager' });
  const { panel } = state;
  await expect(panel.getByText('Solo el propietario de la cuenta cambia los permisos y el asistente de IA.')).toBeVisible();
  for (const label of ['Quién puede configurar precios', 'Quién puede enviar cotizaciones', 'Usar el asistente de cotización']) await expect(panel.getByLabel(label)).toBeDisabled();
  await expect(panel.getByLabel('Quién puede enviar cotizaciones')).toHaveValue('manager');
  await panel.getByLabel('Cantidad sugerida').selectOption('available');
  // Meanwhile the owner raises the publish permission and another member blocks over-stock offers.
  state.change({ publish_min_permission: 'owner', over_stock_policy: 'block' });
  await panel.getByRole('button', { name: 'Guardar configuración (1)', exact: true }).click();
  await expect(panel.getByRole('alert')).toHaveText('Otro miembro de tu equipo cambió esta configuración. Se cargaron sus valores; revisa tus cambios y vuelve a guardar.');
  await expect(panel.getByLabel('Quién puede enviar cotizaciones')).toHaveValue('owner');
  await expect(panel.getByLabel('Ofrecer más de lo disponible')).toHaveValue('block');
  await expect(panel.getByLabel('Cantidad sugerida')).toHaveValue('available');
  await panel.getByRole('button', { name: 'Guardar configuración (1)', exact: true }).click();
  await expect(panel.getByRole('status')).toHaveText('Configuración guardada.');
  // Never a full form: a stale owner-only value would be refused with 403 before the version check.
  expect(state.writes).toEqual([{ expected_version: 1, prefill_quantity: 'available' }, { expected_version: 2, prefill_quantity: 'available' }]);
  expect(state.current()).toMatchObject({ publish_min_permission: 'owner', over_stock_policy: 'block', prefill_quantity: 'available', version: 3 });
  expect(state.unexpected).toEqual([]);
});

test('a member below the configuration permission reads every setting without being able to change it', async ({ page }) => {
  const state = await open(page, 'staff', { config_min_permission: 'manager', publish_min_permission: 'manager', assistant_enabled: true,
    updated_at: stamp, updated_by: { name: 'PROPIETARIA' } });
  const { panel } = state;
  await expect(panel.getByText('Pide a un administrador de tu cuenta que cambie esta configuración.')).toBeVisible();
  for (const label of ['Quién puede configurar precios', 'Quién puede enviar cotizaciones', 'Ofrecer más de lo disponible', 'Ofrecer más de lo solicitado', 'Cantidad sugerida',
    'Moneda predeterminada', 'Si el cliente confirma y faltan existencias', 'Tratar USD y PAB como equivalentes', 'Usar el asistente de cotización'])
    await expect(panel.getByLabel(label)).toBeDisabled();
  await expect(panel.getByLabel('Quién puede configurar precios')).toHaveValue('manager');
  await expect(panel.getByLabel('Usar el asistente de cotización')).toBeChecked();
  await expect(panel.getByRole('button', { name: /Guardar configuración/ })).toHaveCount(0);
  await expect(panel).toContainText('Última actualización por PROPIETARIA');
  expect(state.writes).toEqual([]);
  expect(state.unexpected).toEqual([]);
});

test('a member whose configuration permission was raised meanwhile gets the refusal and a read-only form, not a save that can never pass', async ({ page }) => {
  const state = await open(page, 'manager');
  const { panel } = state;
  await panel.getByLabel('Cantidad sugerida').selectOption('available');
  // Meanwhile the owner keeps price configuration to himself.
  state.change({ config_min_permission: 'owner' });
  await panel.getByRole('button', { name: 'Guardar configuración (1)', exact: true }).click();
  await expect(panel.getByRole('alert')).toHaveText('Tu permiso en la cuenta no permite cambiar esta configuración. Pide a un administrador de tu cuenta que la cambie.');
  await expect(panel.getByText('Pide a un administrador de tu cuenta que cambie esta configuración.')).toBeVisible();
  await expect(panel.getByLabel('Quién puede configurar precios')).toHaveValue('owner');
  await expect(panel.getByLabel('Cantidad sugerida')).toBeDisabled();
  await expect(panel.getByLabel('Cantidad sugerida')).toHaveValue('requested');
  await expect(panel.getByRole('button', { name: /Guardar configuración/ })).toHaveCount(0);
  expect(state.writes).toEqual([{ expected_version: 1, prefill_quantity: 'available' }]);
  expect(state.unexpected).toEqual([]);
});
