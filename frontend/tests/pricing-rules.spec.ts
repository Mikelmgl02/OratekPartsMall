import { expect, Page, test } from '@playwright/test';
import type { Deal } from '../lib/deal-types';
import type { ClientProfile, DraftProfile, DraftSuggestion, PriceList, PriceRow, PricingRule, SimulationResult } from '../lib/pricing-types';
import type { SupplierRequestLine } from '../lib/request-types';
import { generalList, mockQuoteDrafts, suggestion } from './quote-draft-mock';

const supplierId = '22222222-2222-4222-8222-222222222222';
const clientId = '11111111-1111-4111-8111-111111111111';
const otherId = '11111111-1111-4111-8111-111111111112';
const general = generalList.id;
const mayorista = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2';
const itemA = '66666666-6666-4666-8666-666666666661';
const itemB = '66666666-6666-4666-8666-666666666662';
const stamp = '2026-10-03T12:00:00Z';
const empty = { count: 0, next: null, previous: null, results: [] };
const any = expect.any(String);
const base = `/api/market/accounts/${supplierId}`;
const route = (path: string) => new RegExp(`${base}/${path}(?:\\?.*)?$`);

async function session(page: Page) {
  const unexpected: string[] = [];
  await page.route('**/api/**', request => { unexpected.push(`${request.request().method()} ${new URL(request.request().url()).pathname}`); return request.fulfill({ status: 404, json: { detail: 'Ruta no prevista' } }); });
  await page.route('**/api/session', request => request.fulfill({ json: { authenticated: true, user: { id: 801, username: 'EMPLEADO', first_name: '', last_name: '', is_superuser: false },
    accounts: { ...empty, count: 1, results: [{ id: supplierId, name: 'REPUESTOS CENTRAL', roles: ['supplier_retail'], capabilities: ['supplier'], permission: 'staff' }] } } }));
  await page.route('**/api/market/analytics/events', request => request.fulfill({ status: 202, json: { recorded: true } }));
  await page.route('**/api/market/wishlist/state', request => request.fulfill({ json: { part_ids: [], count: 0 } }));
  await page.route(/\/api\/market\/catalog(?:\?.*)?$/, request => request.fulfill({ json: empty }));
  await page.route(`${base}/price-lists`, request => request.fulfill({ json: { can_configure: true, item_count: 2, results: [priceList(general, 'GENERAL', true), priceList(mayorista, 'MAYORISTA', false)] } }));
  return unexpected;
}
function priceList(id: string, code: string, isDefault: boolean): PriceList {
  return { id, code, name: `LISTA ${code}`, currency: 'USD', is_default: isDefault, active: true, version: 1, priced_count: 1, missing_count: 1, created_at: stamp, updated_at: stamp };
}
const generalRef = { ...generalList, active: true };
function profile(overrides: Partial<ClientProfile> = {}): ClientProfile {
  return { client: { id: clientId, name: 'TALLER CENTRAL' }, exists: false, id: null, version: 0, price_list_id: null, price_list: null, fallback_to_default: true,
    discount_percent: '0.00', preferred_currency: '', default_terms: '', internal_notes: '', customer_code: '', effective_list: generalRef, effective_currency: 'USD',
    order_count: 2, last_order_at: stamp, updated_at: null, updated_by: null, can_configure: true, ...overrides };
}
function rule(overrides: Partial<PricingRule>): PricingRule {
  return { id: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1', name: 'PROMO KSM OCTUBRE', scope: 'all', client: null, target: 'brand', target_value: 'KSM', item: null, kind: 'discount',
    value: '15.00', currency: '', min_quantity: 1, valid_from: '2026-10-01', valid_until: '2026-10-31', active: true, validity: 'active', note: '', revision: 1,
    created_by: { name: 'EMPLEADO' }, updated_by: { name: 'EMPLEADO' }, created_at: stamp, updated_at: stamp, ...overrides };
}
function item(id: string, invent: string, codigo: string, brand: string, group = ''): PriceRow {
  return { supplier_item_id: id, supplier_invent_id: invent, codigo, brand, description: `REPUESTO ${codigo}`, matching_status: 'matched', discount_group: group,
    floor_price: null, item_pricing_revision: null, prices: {}, updated_at: null };
}
const client = (id: string, name: string, profileSummary: Partial<ClientProfile> = {}) => ({ id, name, order_count: 2, last_order_at: stamp, profile: { exists: false, version: 0,
  customer_code: '', price_list: null, discount_percent: '0.00', preferred_currency: '', rule_count: 0, ...profileSummary } });
const promo = rule({});
// Worked example 1 (§3.4): the client's line agreement wins over its general discount and the brand promotion; 10 units is the next break.
function agreement(price: string): DraftSuggestion {
  const value = suggestion(price, { revision: 7 });
  return { ...value, explanation: { ...value.explanation, profile: { id: 'cccccccc-cccc-4ccc-8ccc-ccccccccccc1', version: 1, discount_percent: '5.00', archived_list: null },
    base: { ...value.explanation.base!, unit_price: '15.00' },
    steps: [{ kind: 'rule', rule_id: 'dddddddd-dddd-4ddd-8ddd-ddddddddddd1', rule_revision: 2, name: 'FILTROS TALLER CENTRAL', scope: 'client', target: 'line', target_value: 'FILTROS',
      min_quantity: 1, action: 'discount', value: '10.00', before: '15.00', after: price }],
    discarded: [{ rule_id: 'synthetic:client_discount', name: 'DESCUENTO GENERAL DEL CLIENTE', reason: 'menos_especifica' }, { rule_id: promo.id, name: promo.name, reason: 'menos_especifica' }],
    next_breaks: [{ min_quantity: 10, unit_price: '12.75', rule_name: 'FILTROS VOLUMEN 10+' }] } };
}

test('a supplier configures a client private profile and its own discount from Precios › Clientes', async ({ page }) => {
  const unexpected = await session(page);
  let current = profile(), own: PricingRule[] = [];
  const writes: Record<string, unknown>[] = [], ruleWrites: Record<string, unknown>[] = [];
  await page.route(route('clients'), request => request.fulfill({ json: { ...empty, count: 2, default_list: generalRef,
    results: [client(clientId, 'TALLER CENTRAL', current.exists ? { exists: true, version: current.version, customer_code: current.customer_code, discount_percent: current.discount_percent,
      price_list: { ...priceList(mayorista, 'MAYORISTA', false), active: true } } : {}), client(otherId, 'TIENDA LA ROTONDA')] } }));
  await page.route(`${base}/clients/${clientId}/profile`, request => {
    if (request.request().method() === 'GET') return request.fulfill({ json: current });
    const body = request.request().postDataJSON(); writes.push(body);
    current = profile({ exists: true, id: 'cccccccc-cccc-4ccc-8ccc-ccccccccccc1', version: current.version + 1, price_list_id: body.price_list_id, price_list: { ...priceList(mayorista, 'MAYORISTA', false), active: true },
      effective_list: { ...priceList(mayorista, 'MAYORISTA', false), active: true }, discount_percent: '5.50', customer_code: body.customer_code, default_terms: body.default_terms,
      internal_notes: body.internal_notes, updated_at: stamp, updated_by: { name: 'EMPLEADO' } });
    return request.fulfill({ json: current });
  });
  await page.route(route('pricing-rules'), request => {
    const query = new URL(request.request().url()).searchParams;
    if (request.request().method() === 'GET') return request.fulfill({ json: { ...empty, can_configure: true, ...(query.get('client') === clientId ? { count: own.length, results: own } : { count: 1, results: [promo] }) } });
    const body = request.request().postDataJSON(); ruleWrites.push(body);
    own = [rule({ id: 'dddddddd-dddd-4ddd-8ddd-ddddddddddd1', name: body.name, scope: 'client', client: { id: clientId, name: 'TALLER CENTRAL' }, target: 'line', target_value: body.target_value,
      value: '10.00', valid_from: null, valid_until: null })];
    return request.fulfill({ status: 201, json: own[0] });
  });
  await page.goto(`/?vista=proveedor&seccion=precios&cuenta=${supplierId}&pestana=clientes`);
  const section = page.getByRole('region', { name: 'Precios del proveedor', exact: true });
  await expect(section.getByRole('tab', { name: 'Clientes', exact: true })).toHaveAttribute('aria-selected', 'true');
  const table = section.getByRole('table', { name: 'Clientes', exact: true });
  await expect(table.getByRole('row').nth(1)).toContainText('TALLER CENTRAL');
  await expect(table.getByRole('row').nth(1)).toContainText('Sin perfil comercial');
  await expect(table.getByRole('row').nth(1)).toContainText('GENERAL (predeterminada)');
  await section.getByRole('button', { name: 'Abrir perfil comercial de TALLER CENTRAL', exact: true }).click();
  const profileSection = section.getByRole('region', { name: 'Perfil comercial · TALLER CENTRAL', exact: true });
  await expect(profileSection.getByRole('heading', { name: 'Perfil comercial · TALLER CENTRAL' })).toBeVisible();
  await expect(profileSection.getByRole('note')).toContainText('Privado entre tu empresa y este cliente.');
  await expect(profileSection).toContainText('Este cliente aún no tiene perfil comercial');
  await profileSection.getByLabel('Lista de precios').selectOption(mayorista);
  await profileSection.getByLabel('Descuento general (%)').fill('5,5');
  await profileSection.getByLabel('Código del cliente en tu sistema').fill(' C-00123 ');
  await profileSection.getByLabel('Condiciones predeterminadas para sus cotizaciones').fill('precios no incluyen itbms');
  await profileSection.getByLabel('Notas internas (solo tu equipo)').fill('paga a 30 días');
  await profileSection.getByRole('button', { name: 'Crear perfil', exact: true }).click();
  await expect(profileSection.getByRole('status').filter({ hasText: 'Perfil guardado.' })).toBeVisible();
  // Only the changed fields travel, with the version the supplier saw and a retry key.
  expect(writes).toEqual([{ operation_id: any, expected_version: 0, price_list_id: mayorista, discount_percent: '5.5', customer_code: 'C-00123',
    default_terms: 'PRECIOS NO INCLUYEN ITBMS', internal_notes: 'PAGA A 30 DÍAS' }]);
  await expect(profileSection.getByRole('button', { name: 'Guardar perfil', exact: true })).toBeVisible();
  // The client's own discount on its LINEA.
  await profileSection.getByRole('button', { name: 'Agregar descuento', exact: true }).click();
  const form = page.getByRole('dialog', { name: 'Agregar descuento', exact: true });
  await form.getByLabel('Nombre').fill('filtros taller');
  await expect(form.getByLabel('Aplica a')).toBeDisabled();
  await form.getByLabel('Artículos').selectOption('line');
  await form.getByLabel('Línea', { exact: true }).fill('filtros');
  await form.getByLabel('Descuento (%)').fill('10');
  await expect(form.getByRole('status')).toContainText('queda en');
  await expect(form.getByRole('status')).toContainText('90.00');
  await form.getByRole('button', { name: 'Crear regla', exact: true }).click();
  await expect(form).toHaveCount(0);
  expect(ruleWrites).toEqual([{ operation_id: any, name: 'FILTROS TALLER', scope: 'client', client_id: clientId, target: 'line', target_value: 'FILTROS', item_id: null,
    kind: 'discount', value: '10.00', currency: '', min_quantity: 1, valid_from: null, valid_until: null, note: '' }]);
  const ownRules = profileSection.getByRole('table', { name: 'Reglas de este cliente', exact: true });
  await expect(ownRules.getByRole('row').nth(1)).toContainText('FILTROS TALLER');
  await expect(ownRules.getByRole('row').nth(1)).toContainText('Línea FILTROS');
  await expect(ownRules.getByRole('row').nth(1)).toContainText('Descuento 10 %');
  await profileSection.getByText('Reglas generales que también aplican (1)').click();
  await expect(profileSection.getByRole('table', { name: 'Reglas generales', exact: true })).toContainText('PROMO KSM OCTUBRE');
  await profileSection.getByRole('button', { name: 'Volver a clientes', exact: true }).click();
  await expect(table.getByRole('row').nth(1)).toContainText('C-00123');
  await expect(table.getByRole('row').nth(1)).toContainText('MAYORISTA');
  // The order page links straight to this profile.
  await page.goto(`/?vista=proveedor&seccion=precios&cuenta=${supplierId}&pestana=clientes&cliente=${clientId}`);
  await expect(section.getByRole('heading', { name: 'Perfil comercial · TALLER CENTRAL' })).toBeVisible();
  await expect(section.getByLabel('Código del cliente en tu sistema')).toHaveValue('C-00123');
  expect(unexpected).toEqual([]);
});

test('a first client rule creates the profile without losing what the supplier was typing in the profile form', async ({ page }) => {
  const unexpected = await session(page);
  let current = profile(), own: PricingRule[] = [];
  const writes: Record<string, unknown>[] = [];
  await page.route(`${base}/clients/${clientId}/profile`, request => {
    if (request.request().method() === 'GET') return request.fulfill({ json: current });
    const body = request.request().postDataJSON(); writes.push(body);
    current = profile({ exists: true, id: 'cccccccc-cccc-4ccc-8ccc-ccccccccccc1', version: current.version + 1, internal_notes: body.internal_notes, updated_at: stamp, updated_by: { name: 'EMPLEADO' } });
    return request.fulfill({ json: current });
  });
  await page.route(route('pricing-rules'), request => {
    const query = new URL(request.request().url()).searchParams;
    if (request.request().method() === 'GET') return request.fulfill({ json: { ...empty, can_configure: true, ...(query.get('client') === clientId ? { count: own.length, results: own } : {}) } });
    // The server creates the pair's profile (version 1, default values) with the first client rule.
    current = profile({ exists: true, id: 'cccccccc-cccc-4ccc-8ccc-ccccccccccc1', version: 1 });
    own = [rule({ id: 'dddddddd-dddd-4ddd-8ddd-ddddddddddd2', name: 'TODO TALLER', scope: 'client', client: { id: clientId, name: 'TALLER CENTRAL' }, target: 'all', target_value: '',
      value: '3.00', valid_from: null, valid_until: null })];
    return request.fulfill({ status: 201, json: own[0] });
  });
  await page.goto(`/?vista=proveedor&seccion=precios&cuenta=${supplierId}&pestana=clientes&cliente=${clientId}`);
  const profileSection = page.getByRole('region', { name: 'Perfil comercial · TALLER CENTRAL', exact: true });
  await profileSection.getByLabel('Notas internas (solo tu equipo)').fill('llamar antes');
  await profileSection.getByRole('button', { name: 'Agregar descuento', exact: true }).click();
  const form = page.getByRole('dialog', { name: 'Agregar descuento', exact: true });
  await form.getByLabel('Nombre').fill('todo taller');
  await form.getByLabel('Descuento (%)').fill('3');
  await form.getByRole('button', { name: 'Crear regla', exact: true }).click();
  await expect(form).toHaveCount(0);
  await expect(profileSection.getByRole('button', { name: 'Guardar perfil', exact: true })).toBeVisible();
  await expect(profileSection.getByLabel('Notas internas (solo tu equipo)')).toHaveValue('LLAMAR ANTES');
  await profileSection.getByRole('button', { name: 'Guardar perfil', exact: true }).click();
  await expect(profileSection.getByRole('status').filter({ hasText: 'Perfil guardado.' })).toBeVisible();
  expect(writes).toEqual([{ operation_id: any, expected_version: 1, internal_notes: 'LLAMAR ANTES' }]);
  expect(unexpected).toEqual([]);
});

test('rules: a net price for one item with its example, an edit that reloads on conflict and an archive with confirmation', async ({ page }) => {
  const unexpected = await session(page);
  let rules = [promo];
  const writes: { path: string; body: Record<string, unknown> }[] = [];
  await page.route(route('pricing-rules'), request => {
    if (request.request().method() === 'GET') return request.fulfill({ json: { ...empty, count: rules.length, results: rules, can_configure: true } });
    const body = request.request().postDataJSON(); writes.push({ path: 'create', body });
    const created = rule({ id: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb2', name: body.name, target: 'item', target_value: '', kind: 'net_price', value: '16.80', currency: 'USD', min_quantity: 10,
      item: { id: itemB, supplier_invent_id: 'A-778', codigo: 'BP-778', brand: 'BOSCH', description: 'PASTILLAS DE FRENO' }, valid_from: null, valid_until: null });
    rules = [created, ...rules];
    return request.fulfill({ status: 201, json: created });
  });
  await page.route(`${base}/pricing-rules/${promo.id}`, request => {
    const body = request.request().postDataJSON(); writes.push({ path: 'update', body });
    // Another member saved first: the 409 carries the rule as it is now.
    if (body.expected_version === 1) return request.fulfill({ status: 409, json: { detail: 'Otro usuario actualizó esta regla. Revisa sus valores actuales.', rule: { ...promo, value: '12.00', revision: 2 } } });
    rules = rules.map(value => value.id === promo.id ? { ...promo, value: body.value as string, revision: 3 } : value);
    return request.fulfill({ json: rules.find(value => value.id === promo.id) });
  });
  await page.route(`${base}/pricing-rules/${promo.id}/archive`, request => {
    writes.push({ path: 'archive', body: request.request().postDataJSON() });
    rules = rules.map(value => value.id === promo.id ? { ...value, active: false, validity: 'archived', revision: 4 } : value);
    return request.fulfill({ json: rules.find(value => value.id === promo.id) });
  });
  await page.route(route('prices'), request => request.fulfill({ json: { ...empty, count: 1, results: [item(itemB, 'A-778', 'BP-778', 'BOSCH')] } }));
  await page.goto(`/?vista=proveedor&seccion=precios&cuenta=${supplierId}&pestana=reglas`);
  const section = page.getByRole('region', { name: 'Precios del proveedor', exact: true });
  const panel = section.getByRole('tabpanel', { name: 'Reglas', exact: true });
  await expect(panel).toContainText('Gana la regla más específica: cliente › todos; artículo › línea › marca › todos; mayor cantidad mínima.');
  const table = panel.getByRole('table', { name: 'Reglas comerciales', exact: true });
  await expect(table.getByRole('row').nth(1)).toContainText('PROMO KSM OCTUBRE');
  await expect(table.getByRole('row').nth(1)).toContainText('Todos tus clientes');
  await expect(table.getByRole('row').nth(1)).toContainText('Vigente');
  await panel.getByRole('button', { name: 'Nueva regla', exact: true }).click();
  const form = page.getByRole('dialog', { name: 'Nueva regla', exact: true });
  await form.getByLabel('Nombre').fill('neto pastillas');
  await form.getByLabel('Tipo').selectOption('net_price');
  await expect(form.getByLabel('Artículos')).toHaveValue('item');
  await form.getByLabel('Artículo', { exact: true }).fill('BP');
  await form.getByRole('button', { name: /BP-778/ }).click();
  await expect(form.getByText('A-778 · BOSCH · REPUESTO BP-778')).toBeVisible();
  await form.getByLabel('Precio neto', { exact: true }).fill('16.8');
  await form.getByLabel('Moneda').selectOption('USD');
  await form.getByLabel('Desde (unidades)').fill('10');
  await expect(form.getByRole('status')).toContainText('Tus clientes pagarán');
  await expect(form.getByRole('status')).toContainText('16.80 por unidad desde 10 unidades, sin importar la lista.');
  await form.getByRole('button', { name: 'Crear regla', exact: true }).click();
  await expect(form).toHaveCount(0);
  await expect(panel.getByRole('status').filter({ hasText: 'Regla NETO PASTILLAS creada.' })).toBeVisible();
  expect(writes[0]).toEqual({ path: 'create', body: { operation_id: any, name: 'NETO PASTILLAS', scope: 'all', client_id: null, target: 'item', target_value: '', item_id: itemB,
    kind: 'net_price', value: '16.80', currency: 'USD', min_quantity: 10, valid_from: null, valid_until: null, note: '' } });
  await expect(table.getByRole('row').nth(1)).toContainText('Artículo BP-778');
  await expect(table.getByRole('row').nth(1)).toContainText('Precio neto');
  // Editing on a stale revision reloads the current values instead of overwriting them.
  await table.getByRole('button', { name: 'Editar regla PROMO KSM OCTUBRE', exact: true }).click();
  const edit = page.getByRole('dialog', { name: 'Regla · PROMO KSM OCTUBRE', exact: true });
  await edit.getByLabel('Descuento (%)').fill('14');
  await edit.getByRole('button', { name: 'Guardar regla', exact: true }).click();
  await expect(edit.getByRole('alert')).toContainText('Otro usuario actualizó esta regla. Se cargaron sus valores actuales');
  await expect(edit.getByLabel('Descuento (%)')).toHaveValue('12.00');
  await edit.getByLabel('Descuento (%)').fill('13');
  await edit.getByRole('button', { name: 'Guardar regla', exact: true }).click();
  await expect(edit).toHaveCount(0);
  expect(writes.slice(1).map(write => [write.path, write.body.expected_version, write.body.value])).toEqual([['update', 1, '14.00'], ['update', 2, '13.00']]);
  // Archiving asks first and never deletes: the rule leaves the active list.
  await table.getByRole('button', { name: 'Archivar regla PROMO KSM OCTUBRE', exact: true }).click();
  await table.getByRole('button', { name: 'Sí, archivar', exact: true }).click();
  await expect(panel.getByRole('status').filter({ hasText: 'Regla PROMO KSM OCTUBRE archivada.' })).toBeVisible();
  expect(writes.at(-1)).toEqual({ path: 'archive', body: { expected_version: 3 } });
  await expect(table.getByRole('row').nth(2)).toContainText('Archivada');
  expect(unexpected).toEqual([]);
});

test('the simulator explains the winning rule, the discarded ones and the next volume break without saving anything', async ({ page }) => {
  const unexpected = await session(page);
  const bodies: Record<string, unknown>[] = [];
  await page.route(route('clients'), request => request.fulfill({ json: { ...empty, count: 1, default_list: generalRef, results: [client(clientId, 'TALLER CENTRAL', { exists: true, version: 1 })] } }));
  await page.route(route('prices'), request => request.fulfill({ json: { ...empty, count: 1, results: [item(itemA, 'A-123', 'KSM-123', 'KSM', 'FILTROS')] } }));
  await page.route(`${base}/pricing/simulate`, request => {
    bodies.push(request.request().postDataJSON());
    const explanation = { ...agreement('13.50').explanation, currency: 'PAB' as const, quantity_basis: 4 };
    explanation.steps = [...explanation.steps, { kind: 'parity', from: 'USD', to: 'PAB', rate: '1' }];
    const result: SimulationResult = { client: { id: clientId, name: 'TALLER CENTRAL' }, profile: { exists: true, version: 1, discount_percent: '5.00' }, price_list: generalRef,
      currency: 'PAB', on_date: '2026-10-05', engine: 'pricing-v2', lines: [{ supplier_item_id: itemA, supplier_invent_id: 'A-123', codigo: 'KSM-123', brand: 'KSM', description: 'FILTRO DE ACEITE',
        discount_group: 'FILTROS', quantity: 4, unit_price: '13.50', line_total: '54.00', status: 'priced', explanation }] };
    return request.fulfill({ json: result });
  });
  await page.goto(`/?vista=proveedor&seccion=precios&cuenta=${supplierId}&pestana=simulador`);
  const panel = page.getByRole('tabpanel', { name: 'Simulador', exact: true });
  await expect(panel.getByLabel('Cliente', { exact: true }).locator('option', { hasText: 'TALLER CENTRAL' })).toHaveCount(1);
  await panel.getByLabel('Cliente', { exact: true }).selectOption(clientId);
  await panel.getByLabel('Moneda').selectOption('PAB');
  await panel.getByLabel('Artículo(s)').fill('KSM');
  await panel.getByRole('button', { name: /KSM-123/ }).click();
  await panel.getByLabel('Cantidad de KSM-123').fill('4');
  await panel.getByRole('button', { name: 'Calcular precio', exact: true }).click();
  const outcome = panel.getByRole('region', { name: 'Resultado de la simulación', exact: true });
  await expect(outcome).toContainText('TALLER CENTRAL · perfil v1 · descuento general 5 %');
  await expect(outcome).toContainText('importe');
  await expect(outcome.getByRole('listitem').nth(0)).toContainText('Lista GENERAL');
  await expect(outcome.getByRole('listitem').nth(1)).toContainText('Regla FILTROS TALLER CENTRAL: −10 % →');
  await expect(outcome.getByRole('listitem').nth(1)).toContainText('Acuerdo de este cliente');
  await expect(outcome.getByRole('listitem').nth(2)).toContainText('Paridad USD/PAB 1:1');
  await expect(outcome).toContainText('No aplicadas: Descuento general del cliente (menos específica) · PROMO KSM OCTUBRE (menos específica)');
  await expect(outcome).toContainText('A partir de 10 unidades:');
  expect(bodies).toEqual([{ client_id: clientId, currency: 'PAB', items: [{ supplier_item_id: itemA, quantity: 4 }] }]);
  expect(unexpected).toEqual([]);
});

const orderId = '33333333-3333-4333-8333-333333333333';
const lineA = '44444444-4444-4444-8444-444444444441';
const lineB = '44444444-4444-4444-8444-444444444442';
function orderLine(id: string, codigo: string, quantity: number): SupplierRequestLine {
  return { id, part_id: '77777777-7777-4777-8777-777777777777', supplier_item_id: id === lineA ? itemA : itemB, supplier_invent_id: `ID-${codigo}`, sku: '58411-1R000',
    name: 'FILTRO', codigo, brand: 'KSM', description: `REPUESTO ${codigo}`, quantity, stock: { reported_quantity: 9, reserved_quantity: 0, available_quantity: 9, shortfall: 0, updated_at: stamp } };
}

test('the order header shows the pair profile; configuring the client makes the draft offer to recalculate and explain the rule', async ({ page }) => {
  const unexpected = await session(page);
  const order: Deal = { id: orderId, reference: 'ORD-PERFIL-001', client: { id: clientId, name: 'TALLER CENTRAL' }, supplier: { id: supplierId, name: 'REPUESTOS CENTRAL' },
    status: 'reviewed', version: 3, created_at: stamp, reviewed_at: stamp, handshaked_at: null, line_count: 2, unit_count: 5, notes: '', quotation: null, quotations: [], events: [],
    lines: [orderLine(lineA, 'KSM-123', 4), orderLine(lineB, 'KSM-999', 1)] };
  await page.route(`${base}/requests/${orderId}`, request => request.fulfill({ json: order }));
  await page.route(`${base}/requests/${orderId}/review`, request => request.fulfill({ json: order }));
  await page.route(/\/api\/market\/accounts\/[^/]+\/deals\/[^/]+\/messages(?:\?.*)?$/, request => request.fulfill({ json: { results: [], cursor: 0, has_more: false, has_earlier: false } }));
  const noProfile: DraftProfile = { exists: false, version: 0, discount_percent: '0.00', customer_code: '', internal_notes: '' };
  const drafts = await mockQuoteDrafts(page, () => order, { suggestions: { [lineA]: suggestion('15.00'), [lineB]: suggestion('4.00') }, profile: noProfile });
  let current = profile();
  const writes: Record<string, unknown>[] = [];
  await page.route(`${base}/clients/${clientId}/profile`, request => {
    if (request.request().method() === 'GET') return request.fulfill({ json: current });
    writes.push(request.request().postDataJSON());
    current = profile({ exists: true, id: 'cccccccc-cccc-4ccc-8ccc-ccccccccccc1', version: 1, discount_percent: '5.00', customer_code: 'C-00123', internal_notes: 'PAGA A 30 DÍAS' });
    // The server now prices the pair with its agreement: the saved engine price stays and becomes stale.
    drafts.setProfile({ exists: true, version: 1, discount_percent: '5.00', customer_code: 'C-00123', internal_notes: 'PAGA A 30 DÍAS' });
    drafts.suggest(lineA, agreement('13.50'));
    return request.fulfill({ json: current });
  });
  await page.route(route('pricing-rules'), request => request.fulfill({ json: { ...empty, can_configure: true } }));
  const content = page.locator('.deal-order-content');
  const quote = (line: string, column: string) => content.locator(`.quotation-grid .ag-row[row-id="${line}"] .ag-cell[col-id="${column}"]`);
  await page.goto(`/proveedor/${supplierId}/ordenes/${orderId}`);
  const chip = content.getByLabel('Perfil comercial del cliente');
  await expect(chip).toContainText('Sin perfil comercial · se usa la lista GENERAL');
  await content.getByRole('button', { name: 'Cotización', exact: true }).click();
  await expect(quote(lineA, 'unit_price')).toContainText('15.00');
  // A saved draft keeps its engine prices: the profile change below only marks them stale.
  await content.getByLabel('Condiciones de entrega y cotización').fill('retiro en sucursal');
  await expect(content.getByRole('status', { name: 'Estado del borrador' })).toContainText('Borrador v1');
  await chip.getByRole('button', { name: 'Configurar cliente', exact: true }).click();
  const modal = page.getByRole('dialog', { name: 'Perfil comercial · TALLER CENTRAL', exact: true });
  await modal.getByLabel('Descuento general (%)').fill('5');
  await modal.getByLabel('Código del cliente en tu sistema').fill('C-00123');
  await modal.getByLabel('Notas internas (solo tu equipo)').fill('paga a 30 días');
  await modal.getByRole('button', { name: 'Crear perfil', exact: true }).click();
  await expect(modal.getByRole('status').filter({ hasText: 'Perfil guardado.' })).toBeVisible();
  expect(writes).toEqual([{ operation_id: any, expected_version: 0, discount_percent: '5', customer_code: 'C-00123', internal_notes: 'PAGA A 30 DÍAS' }]);
  await expect(modal.getByRole('link', { name: 'Precios › Clientes', exact: true })).toHaveAttribute('href', `/?vista=proveedor&seccion=precios&cuenta=${supplierId}&pestana=clientes&cliente=${clientId}`);
  await modal.getByRole('button', { name: 'Cerrar ventana', exact: true }).click();
  await expect(chip).toContainText('Lista GENERAL · Desc. 5 % · Código C-00123');
  await expect(chip).toContainText('Notas internas: PAGA A 30 DÍAS');
  await expect(quote(lineA, 'unit_price')).toContainText('15.00');
  const recalculate = content.getByRole('button', { name: 'Recalcular precios sugeridos (1)', exact: true });
  await recalculate.click();
  await expect(quote(lineA, 'unit_price')).toContainText('13.50');
  await expect(quote(lineA, 'price_source')).toHaveText('Regla');
  await expect(content.getByRole('status').filter({ hasText: '1 precio recalculado con tu lista y tus reglas: KSM-123 (FILTROS TALLER CENTRAL).' })).toBeVisible();
  expect(drafts.reprices).toEqual([{ save_id: any, expected_draft_version: 1, scope: 'engine' }]);
  await quote(lineA, 'price_source').getByRole('button').click();
  const drawer = page.getByRole('dialog', { name: '¿De dónde sale este precio?' });
  await expect(drawer.getByRole('listitem').nth(1)).toContainText('Regla FILTROS TALLER CENTRAL: −10 % →');
  await expect(drawer).toContainText('No aplicadas: Descuento general del cliente (menos específica) · PROMO KSM OCTUBRE (menos específica)');
  await expect(drawer).toContainText('A partir de 10 unidades:');
  expect(unexpected).toEqual([]);
});
