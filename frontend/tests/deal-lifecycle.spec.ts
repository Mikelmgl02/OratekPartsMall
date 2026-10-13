import { expect, Page, test } from '@playwright/test';
import { signedOutInbox } from './inbox-route';
import { fakeServerBasket } from './server-basket';
import type { Deal, DealShortfall } from '../lib/deal-types';

const clientId = '11111111-1111-4111-8111-111111111111';
const supplierId = '22222222-2222-4222-8222-222222222222';
const orderId = '33333333-3333-4333-8333-333333333333';
const lineA = '44444444-4444-4444-8444-444444444441';
const lineB = '44444444-4444-4444-8444-444444444442';
const quoteId = '88888888-8888-4888-8888-000000000001';
const stamp = '2026-10-03T12:00:00Z';
const empty = { count: 0, results: [], next: null, previous: null };
const accounts = [{ id: clientId, name: 'TALLER CENTRAL', roles: ['client_business'], capabilities: ['client'], permission: 'owner' },
  { id: supplierId, name: 'REPUESTOS CENTRAL', roles: ['supplier_retail'], capabilities: ['supplier'], permission: 'owner' }];
const quoteLine = (id: string, codigo: string, quantity: number) => ({ order_line_id: id, codigo, sku: `SKU-${codigo}`, description: `TAMBOR ${codigo}`, quantity,
  unit_price: '12.35', total: (quantity * 12.35).toFixed(2) });
const requestLine = (id: string, codigo: string, quantity: number) => ({ id, part_id: '66666666-6666-4666-8666-666666666666', supplier_item_id: `77777777-7777-4777-8777-77777777777${id.at(-1)}`,
  supplier_invent_id: `ID-${codigo}`, sku: `SKU-${codigo}`, name: 'TAMBOR', codigo, brand: 'KSM', description: `TAMBOR ${codigo}`, quantity,
  stock: { reported_quantity: 10, reserved_quantity: quantity, available_quantity: 10 - quantity, shortfall: 0, held_for_deal: quantity, updated_at: stamp } });

// A confirmed deal for 3 of A-001 and 2 of B-002 at 12.35, and a server that applies the post-handshake decisions like mall.deal_lifecycle:
// the supplier marks Procesando, then En camino (or Entregado at once, a pickup), then Entregado; only then the client marks Recibido.
async function fixture(page: Page) {
  const quotation = { id: quoteId, revision: 1, currency: 'USD' as const, terms: '', total: '61.75', created_at: stamp, supplier_confirmed_at: stamp, client_confirmed_at: stamp,
    lines: [quoteLine(lineA, 'A-001', 3), quoteLine(lineB, 'B-002', 2)] };
  const missing: Record<string, number> = {}, pending: Record<string, number> = {};
  let order: Deal = { id: orderId, reference: 'ORD-ACUERDO-001', client: { id: clientId, name: 'TALLER CENTRAL' }, supplier: { id: supplierId, name: 'REPUESTOS CENTRAL' },
    submission_id: '55555555-5555-4555-8555-555555555555', status: 'handshaked', version: 4, created_at: stamp, reviewed_at: stamp, handshaked_at: stamp,
    line_count: 2, unit_count: 5, notes: '', quotation, quotations: [quotation], events: [], lines: [requestLine(lineA, 'A-001', 3), requestLine(lineB, 'B-002', 2)],
    shortfalls: [], delivered_at: null, cancellation: null } as Deal;
  const agreement = () => {
    const lines = quotation.lines.map(line => ({ order_line_id: line.order_line_id, codigo: line.codigo, description: line.description, agreed: line.quantity,
      missing: missing[line.order_line_id] || 0, pending_missing: pending[line.order_line_id] || 0, final: line.quantity - (missing[line.order_line_id] || 0),
      unit_price: line.unit_price, total: ((line.quantity - (missing[line.order_line_id] || 0)) * 12.35).toFixed(2) }));
    return { quotation_id: quoteId, units: lines.reduce((sum, line) => sum + line.final, 0), total: lines.reduce((sum, line) => sum + Number(line.total), 0).toFixed(2), lines };
  };
  order = { ...order, agreement: agreement() } as Deal;
  const actions: Record<string, unknown>[] = [], listed: string[] = [], unexpected: string[] = [];
  const save = (changes: Partial<Deal>) => { order = { ...order, ...changes, version: order.version + 1, agreement: agreement() } as Deal; return order; };
  await page.route('**/api/**', route => { if (signedOutInbox(route)) return; unexpected.push(`${route.request().method()} ${new URL(route.request().url()).pathname}`); return route.fulfill({ status: 404, json: { detail: 'Ruta no prevista' } }); });
  await fakeServerBasket(page);  // the client's cesta (mall/basket.py), empty
  await page.route('**/api/session', route => route.fulfill({ json: { authenticated: true, user: { id: 801, username: 'EMPLEADO', is_superuser: false }, accounts: { ...empty, count: 2, results: accounts } } }));
  await page.route('**/api/market/analytics/events', route => route.fulfill({ status: 202, json: { recorded: true } }));
  await page.route('**/api/market/wishlist/state', route => route.fulfill({ json: { part_ids: [], count: 0 } }));
  await page.route(/\/api\/market\/catalog(?:\?.*)?$/, route => route.fulfill({ json: empty }));
  await page.route(/\/api\/market\/accounts\/[^/]+\/inventory(?:\?.*)?$/, route => route.fulfill({ json: empty }));
  await page.route(/\/api\/market\/accounts\/[^/]+\/sent-requests(?:\?.*)?$/, route => {
    listed.push(new URL(route.request().url()).searchParams.get('status') || '');
    return route.fulfill({ json: { ...empty, count: 1, results: [{ ...order, quoted_unit_count: 5, shortfall_pending: !!order.shortfalls?.some(item => item.status === 'pending') }] } });
  });
  await page.route(/\/api\/market\/accounts\/[^/]+\/(sent-requests|requests)\/[^/]+$/, route => route.fulfill({ json: order }));
  await page.route(`/api/market/accounts/${supplierId}/requests/${orderId}/review`, route => route.fulfill({ json: order }));
  await page.route(/\/api\/market\/accounts\/[^/]+\/deals\/[^/]+\/messages(?:\?.*)?$/, route => route.fulfill({ json: { results: [], cursor: 0, has_more: false, has_earlier: false } }));
  const steps: Record<string, [Deal['status'][], Partial<Deal>]> = {
    process: [['handshaked'], { status: 'processing', processing_at: stamp }], ship: [['processing'], { status: 'in_transit', in_transit_at: stamp }],
    deliver: [['processing', 'in_transit'], { status: 'delivered', delivered_at: stamp }], receive: [['delivered'], { status: 'received', received_at: stamp }],
  };
  await page.route(/\/api\/market\/accounts\/[^/]+\/deals\/[^/]+\/actions$/, route => {
    const payload = route.request().postDataJSON(); actions.push(payload);
    if (payload.action in steps) {
      const [from, changes] = steps[payload.action];
      if (!from.includes(order.status)) return route.fulfill({ status: 409, json: { detail: 'Esta orden no admite ese paso.' } });
      return route.fulfill({ json: save(changes) });
    }
    if (payload.action === 'report_shortfall') {
      for (const line of payload.shortfall) pending[line.order_line_id] = line.missing;
      const shortfall: DealShortfall = { id: '99999999-9999-4999-8999-999999999999', status: 'pending', reason: payload.reason_code, reason_label: 'Sin existencias', note: payload.reason,
        reported_at: stamp, responded_at: null, lines: payload.shortfall.map((line: { order_line_id: string; missing: number }) => ({ order_line_id: line.order_line_id,
          codigo: line.order_line_id === lineA ? 'A-001' : 'B-002', description: 'TAMBOR', agreed: line.order_line_id === lineA ? 3 : 2, missing: line.missing })) };
      return route.fulfill({ json: save({ shortfalls: [shortfall], shortfall_pending: true }) });
    }
    if (payload.action === 'accept_shortfall') {
      Object.assign(missing, pending); Object.keys(pending).forEach(key => delete pending[key]);
      return route.fulfill({ json: save({ shortfalls: order.shortfalls!.map(item => ({ ...item, status: 'accepted' as const })), shortfall_pending: false }) });
    }
    if (payload.action === 'cancel') {
      Object.keys(pending).forEach(key => delete pending[key]);
      return route.fulfill({ json: save({ status: 'cancelled', cancellation: { side: 'client', reason: payload.reason_code, reason_label: 'No acepta el faltante', note: payload.reason, cancelled_at: stamp },
        shortfalls: order.shortfalls!.map(item => item.status === 'pending' ? { ...item, status: 'declined' as const } : item), shortfall_pending: false }) });
    }
    return route.fulfill({ status: 400, json: { detail: 'Acción no prevista' } });
  });
  return { actions, listed, unexpected, set: (changes: Partial<Deal>, missingUnits?: Record<string, number>) => { Object.assign(pending, missingUnits || {}); save(changes); } };
}

const supplierPage = (page: Page) => page.locator('.deal-order-content');
async function openSupplierDeal(page: Page) {
  await page.goto(`/proveedor/${supplierId}/ordenes/${orderId}`);
  await supplierPage(page).getByRole('button', { name: 'Cotización', exact: true }).click();
}
async function openClientDeal(page: Page) {
  await page.goto('/?vista=solicitudes');
  await page.getByRole('button', { name: 'Ver enviada ORD-ACUERDO-001', exact: true }).click();
  return page.locator('dialog.deal-modal');
}
const processing: Partial<Deal> = { status: 'processing', processing_at: stamp };
const steps = (deal: Page | ReturnType<Page['locator']>) => deal.getByRole('list', { name: 'Seguimiento del acuerdo' });
const pendingShortfall = (): Partial<Deal> => ({ shortfall_pending: true, shortfalls: [{ id: '99999999-9999-4999-8999-999999999999', status: 'pending', reason: 'no_stock',
  reason_label: 'Sin existencias', note: 'SE DAÑÓ EN BODEGA', reported_at: stamp, responded_at: null,
  lines: [{ order_line_id: lineA, codigo: 'A-001', description: 'TAMBOR A-001', agreed: 3, missing: 2 }] }] });

test('the supplier marks a confirmed deal delivered and the deal shows it', async ({ page }) => {
  const mock = await fixture(page);
  mock.set(processing);
  await openSupplierDeal(page);
  const deal = supplierPage(page);
  await deal.getByRole('button', { name: 'Marcar como entregado', exact: true }).click();
  await expect(deal.getByText('Confirmas que entregaste 5 unidades al cliente (también si la retiró en tu local). El cliente deberá confirmar que la recibió.', { exact: false })).toBeVisible();
  await deal.getByRole('button', { name: 'Sí, marcar entregado', exact: true }).click();
  await expect(deal.getByRole('status').filter({ hasText: 'Orden marcada como entregada. Falta que el cliente confirme que la recibió.' })).toBeVisible();
  expect(mock.actions).toEqual([{ action: 'deliver', expected_version: 5, quotation_id: quoteId, operation_id: expect.any(String) }]);
  await expect(deal.locator('.deal-outcome.delivered')).toContainText('Entregado · esperando al cliente');
  await expect(deal.locator('.deal-outcome.delivered')).toContainText('Sus unidades siguen reservadas hasta que tu próxima carga de existencias las descuente.');
  await expect(deal.locator('.supplier-request-status.delivered').first()).toHaveText('Entregado · por recibir');
  await expect(steps(deal).locator('[aria-current="step"]')).toContainText('Entregado');
  await expect(steps(deal).locator('[aria-current="step"]')).toContainText('por recibir');
  await expect(deal.getByRole('button', { name: 'Cancelar acuerdo', exact: true })).toHaveCount(0);  // the order left: only MotionPartes cancels now
  await expect(deal.getByRole('button', { name: 'Marcar como entregado', exact: true })).toHaveCount(0);
  await expect(deal.getByRole('button', { name: 'Confirmar recibido', exact: true })).toHaveCount(0);  // the client's to confirm
  expect(mock.unexpected).toEqual([]);
});

test('the supplier moves the order through Procesando and En camino and the client confirms Recibido', async ({ page }) => {
  const mock = await fixture(page);
  await openSupplierDeal(page);
  const deal = supplierPage(page);
  const button = (name: string) => deal.getByRole('button', { name, exact: true });
  // Only the next step: Procesando first, never Entregado straight from the agreement.
  await expect(button('Marcar en camino')).toHaveCount(0);
  await expect(button('Marcar como entregado')).toHaveCount(0);
  await expect(deal.locator('.deal-state-note')).toContainText('Márcala como Procesando cuando empieces a prepararla.');
  await button('Marcar como procesando').click();
  await expect(deal.getByRole('button', { name: 'Sí, marcar procesando', exact: true })).toBeVisible();
  await expect(deal.locator('.deal-confirmation')).toContainText('¿Marcar la orden como Procesando?Avisaremos al cliente que la estás preparando. Tus existencias no cambian.');
  await button('Volver').click();
  await expect(deal.locator('.deal-confirmation')).toHaveCount(0);
  expect(mock.actions).toEqual([]);
  await button('Marcar como procesando').click();
  await button('Sí, marcar procesando').click();
  await expect(deal.getByRole('status').filter({ hasText: 'Orden marcada como Procesando. Avisamos al cliente.' })).toBeVisible();
  await expect(deal.locator('.supplier-request-status').first()).toHaveText('Procesando');
  await expect(steps(deal).locator('[aria-current="step"]')).toContainText('Procesando');
  await expect(deal.getByText('HANDSHAKED · Acuerdo confirmado', { exact: true })).toBeVisible();
  await expect(button('Marcar como procesando')).toHaveCount(0);
  for (const name of ['Marcar en camino', 'Marcar como entregado', 'Reportar faltante', 'Cancelar acuerdo']) await expect(button(name)).toHaveCount(1);

  await button('Marcar en camino').click();
  await expect(deal.locator('.deal-confirmation')).toContainText('Confirmas que despachaste 5 unidades. Después ya no podrás reportar faltantes ni cancelarla; avisaremos al cliente.');
  await button('Sí, va en camino').click();
  await expect(deal.getByRole('status').filter({ hasText: 'Orden marcada En camino. Avisamos al cliente.' })).toBeVisible();
  await expect(deal.locator('.supplier-request-status').first()).toHaveText('En camino');
  await expect(deal.locator('.deal-outcome.in_transit')).toContainText('La marcaste en camino el');
  await expect(deal.getByRole('note').filter({ hasText: 'Ya va en camino' })).toContainText('MotionPartes puede cancelar el acuerdo si hay una disputa.');
  for (const name of ['Marcar en camino', 'Reportar faltante', 'Cancelar acuerdo']) await expect(button(name)).toHaveCount(0);
  await page.screenshot({ path: '/tmp/motionpartes-deal-in-transit-supplier.png', fullPage: true });
  await button('Marcar como entregado').click();
  await button('Sí, marcar entregado').click();
  await expect(deal.locator('.supplier-request-status').first()).toHaveText('Entregado · por recibir');
  await expect(deal.locator('.deal-state-note')).toContainText('Marcaste la orden como entregada. Falta que el cliente confirme que la recibió.');
  await expect(steps(deal).locator('li.skipped')).toHaveCount(0);  // went through every step
  expect(mock.actions.map(action => [action.action, action.expected_version])).toEqual([['process', 4], ['ship', 5], ['deliver', 6]]);

  await page.goto(`/?vista=solicitudes&cuenta=${clientId}`);
  await expect(page.getByRole('article', { name: /ORD-ACUERDO-001/ })).toContainText('Confirma que la recibiste');
  await page.getByRole('button', { name: 'Ver enviada ORD-ACUERDO-001', exact: true }).click();
  const client = page.locator('dialog.deal-modal');
  await client.getByRole('button', { name: 'Cotización', exact: true }).click();
  await expect(client.locator('.deal-outcome.delivered')).toContainText('Entregado · falta tu confirmación');
  await expect(client.locator('.deal-outcome.delivered')).toContainText('Si no la recibiste, escríbele al proveedor en la conversación.');
  await expect(client.getByRole('button', { name: 'Cancelar acuerdo', exact: true })).toHaveCount(0);
  await client.getByRole('button', { name: 'Confirmar recibido', exact: true }).click();
  const confirmation = client.locator('.deal-confirmation');
  await expect(confirmation).toContainText('¿Confirmas que recibiste la orden ORD-ACUERDO-001?');
  await expect(confirmation).toContainText('Recibiste 5 unidades de REPUESTOS CENTRAL. La orden queda cerrada y el proveedor verá tu confirmación.');
  await client.getByRole('button', { name: 'Sí, la recibí', exact: true }).click();
  await expect(client.getByRole('status').filter({ hasText: 'Confirmaste que recibiste la orden. ¡Gracias!' })).toBeVisible();
  await expect(client.locator('.supplier-request-status').first()).toHaveText('Recibido');
  await expect(client.locator('.deal-outcome.received')).toContainText(/Entregada el .+ · recibida el /);
  await expect(steps(client).locator('[aria-current="step"]')).toContainText('recibido');
  await expect(client.getByRole('button', { name: 'Confirmar recibido', exact: true })).toHaveCount(0);
  await page.screenshot({ path: '/tmp/motionpartes-deal-received-client.png' });
  expect(mock.actions.at(-1)).toEqual({ action: 'receive', expected_version: 7, quotation_id: quoteId, operation_id: expect.any(String) });
  expect(mock.unexpected).toEqual([]);
});

test('the client sees no Confirmar recibido before Entregado and no cancel once En camino', async ({ page }) => {
  const mock = await fixture(page);
  mock.set(processing);
  let deal = await openClientDeal(page);
  await deal.getByRole('button', { name: 'Cotización', exact: true }).click();
  await expect(deal.locator('.deal-state-note')).toContainText('El proveedor está preparando tu orden.');
  await expect(deal.getByRole('button', { name: 'Cancelar acuerdo', exact: true })).toBeVisible();
  for (const name of ['Confirmar recibido', 'Marcar como procesando', 'Marcar en camino', 'Marcar como entregado', 'Reportar faltante'])
    await expect(deal.getByRole('button', { name, exact: true })).toHaveCount(0);
  mock.set({ status: 'in_transit', in_transit_at: stamp });
  await page.reload();
  deal = await openClientDeal(page);
  await deal.getByRole('button', { name: 'Cotización', exact: true }).click();
  await expect(deal.locator('.deal-state-note')).toContainText('Tu orden va en camino.');
  await expect(deal.locator('.deal-outcome.in_transit')).toContainText('El proveedor la envió el');
  await expect(deal.getByRole('note').filter({ hasText: 'Tu orden va en camino.' })).toContainText('MotionPartes puede intervenir si no se ponen de acuerdo.');
  for (const name of ['Cancelar acuerdo', 'Confirmar recibido']) await expect(deal.getByRole('button', { name, exact: true })).toHaveCount(0);
  expect(mock.actions).toEqual([]);
  expect(mock.unexpected).toEqual([]);
});

test('a pickup skips En camino and the progress shows it as omitted', async ({ page }) => {
  const mock = await fixture(page);
  mock.set(processing);
  await openSupplierDeal(page);
  const deal = supplierPage(page);
  await expect(deal.getByRole('button', { name: 'Marcar en camino', exact: true })).toHaveClass(/primary/);
  await expect(deal.getByRole('button', { name: 'Marcar como entregado', exact: true })).toHaveClass(/soft/);  // a pickup at the counter
  await deal.getByRole('button', { name: 'Marcar como entregado', exact: true }).click();
  await deal.getByRole('button', { name: 'Sí, marcar entregado', exact: true }).click();
  await expect(deal.locator('.supplier-request-status').first()).toHaveText('Entregado · por recibir');
  expect(mock.actions.map(action => action.action)).toEqual(['deliver']);
  const progress = steps(deal).getByRole('listitem');
  await expect(progress).toHaveCount(7);
  await expect(progress.nth(4)).toHaveClass(/reached/);
  await expect(progress.nth(4)).not.toHaveClass(/skipped/);
  await expect(progress.nth(5)).toHaveClass(/skipped/);
  await expect(progress.nth(5)).toContainText('En camino, omitido');
  await expect(progress.nth(6)).toHaveAttribute('aria-current', 'step');
  expect(mock.unexpected).toEqual([]);
});

test('on a phone the seven progress steps fit without horizontal scroll', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const mock = await fixture(page);
  mock.set({ ...processing, status: 'delivered', delivered_at: stamp });
  await openSupplierDeal(page);
  for (const side of ['supplier', 'client']) {
    const progress = steps(side === 'supplier' ? supplierPage(page) : await openClientDeal(page));
    await expect(progress.getByRole('listitem')).toHaveCount(7);
    expect(await progress.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
    for (const box of await progress.getByRole('listitem').evaluateAll(items => items.map(item => item.getBoundingClientRect()))) {
      expect(box.left).toBeGreaterThanOrEqual(0);
      expect(box.right).toBeLessThanOrEqual(390);
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await progress.scrollIntoViewIfNeeded();
    await page.screenshot({ path: `/tmp/motionpartes-deal-progress-mobile-${side}.png` });
  }
});

test('the supplier reports a shortfall and delivery waits for the client', async ({ page }) => {
  const mock = await fixture(page);
  mock.set(processing);
  await openSupplierDeal(page);
  const deal = supplierPage(page);
  await deal.getByRole('button', { name: 'Reportar faltante', exact: true }).click();
  const form = deal.getByRole('form', { name: 'Reportar faltante' });
  const submit = form.getByRole('button', { name: 'Reportar faltante', exact: true });
  await expect(submit).toBeDisabled();
  await form.getByLabel('Faltan de A-001', { exact: true }).fill('4');  // more than agreed
  await form.getByLabel('Motivo del faltante', { exact: true }).selectOption('no_stock');
  await expect(submit).toBeDisabled();
  await form.getByLabel('Faltan de A-001', { exact: true }).fill('2');
  await form.getByLabel('Nota para el cliente (opcional)').fill('se dañó en bodega');
  await submit.click();
  expect(mock.actions).toEqual([{ action: 'report_shortfall', expected_version: 5, quotation_id: quoteId, shortfall: [{ order_line_id: lineA, missing: 2 }],
    reason_code: 'no_stock', reason: 'SE DAÑÓ EN BODEGA', operation_id: expect.any(String) }]);
  const card = deal.getByRole('region', { name: 'Faltante reportado' });
  await expect(card).toContainText('Faltante reportado · esperando la respuesta del cliente');
  await expect(card).toContainText('A-001: faltan 2 de 3');
  await expect(card.getByRole('link', { name: 'Inventario', exact: true })).toHaveAttribute('href', /seccion=inventario/);
  // A prepared order stays where it is until the client answers: neither En camino nor Entregado.
  for (const name of ['Marcar en camino', 'Marcar como entregado', 'Reportar faltante']) await expect(deal.getByRole('button', { name, exact: true })).toHaveCount(0);
  await expect(deal.getByRole('button', { name: 'Cancelar acuerdo', exact: true })).toBeVisible();
  await expect(deal.locator('.supplier-request-status').first()).toHaveText('Procesando');
  await expect(deal.getByRole('region', { name: 'Acuerdo vigente' })).toContainText('2 por responder');
  await page.screenshot({ path: '/tmp/motionpartes-deal-shortfall-supplier.png' });
  expect(mock.unexpected).toEqual([]);
});

test('the client accepts the reduced agreement', async ({ page }) => {
  const mock = await fixture(page);
  mock.set(pendingShortfall(), { [lineA]: 2 });
  const deal = await openClientDeal(page);
  await expect(deal.getByText('El proveedor reportó un faltante. Revisa el acuerdo reducido y decide.')).toBeVisible();
  await deal.getByRole('button', { name: 'Ver faltante', exact: true }).click();
  const card = deal.getByRole('region', { name: 'Faltante reportado' });
  await expect(card).toContainText('El proveedor no tiene todas las unidades');
  await expect(card).toContainText('A-001: faltan 2 de 3');
  await expect(card).toContainText('SE DAÑÓ EN BODEGA');
  const total = await page.evaluate(() => new Intl.NumberFormat('es-PA', { style: 'currency', currency: 'USD' }).format(37.05));
  await deal.getByRole('button', { name: 'Aceptar el acuerdo reducido', exact: true }).click();
  await expect(deal.getByText(`El acuerdo quedará en 3 unidades por ${total}.`, { exact: false })).toBeVisible();
  await deal.getByRole('button', { name: 'Sí, aceptar', exact: true }).click();
  await expect(deal.getByRole('status').filter({ hasText: 'Aceptaste el acuerdo reducido.' })).toBeVisible();
  expect(mock.actions).toEqual([{ action: 'accept_shortfall', expected_version: 5, quotation_id: quoteId, operation_id: expect.any(String) }]);
  const agreement = deal.getByRole('region', { name: 'Acuerdo vigente' });
  await expect(agreement.getByRole('row').filter({ hasText: 'A-001' })).toContainText('2 aceptadas');
  await expect(agreement).toContainText(total);
  await expect(deal.getByRole('region', { name: 'Faltante reportado' })).toHaveCount(0);
  expect(mock.unexpected).toEqual([]);
});

test('the client cancels instead, with a reason, and the deal shows who cancelled', async ({ page }) => {
  const mock = await fixture(page);
  mock.set(pendingShortfall(), { [lineA]: 2 });
  const deal = await openClientDeal(page);
  await deal.getByRole('button', { name: 'Cotización', exact: true }).click();
  await deal.getByRole('button', { name: 'Cancelar acuerdo', exact: true }).click();
  const form = deal.getByRole('form', { name: 'Cancelar acuerdo' });
  await expect(form.getByRole('button', { name: 'Cancelar el acuerdo', exact: true })).toBeDisabled();
  await form.getByLabel('Motivo de la cancelación', { exact: true }).selectOption({ label: 'No acepto el faltante' });
  await form.getByLabel('Nota (opcional)').fill('lo busco en otro lado');
  await form.getByRole('button', { name: 'Cancelar el acuerdo', exact: true }).click();
  // The side travels with every cancellation (an account on both sides of a deal must name it).
  expect(mock.actions).toEqual([{ action: 'cancel', expected_version: 5, quotation_id: quoteId, reason_code: 'shortfall', reason: 'LO BUSCO EN OTRO LADO', side: 'client',
    operation_id: expect.any(String) }]);
  await expect(deal.locator('.deal-outcome.cancelled')).toContainText('Acuerdo cancelado por el cliente');
  await expect(deal.locator('.deal-outcome.cancelled')).toContainText('LO BUSCO EN OTRO LADO');
  await expect(deal.getByRole('button', { name: 'Cancelar acuerdo', exact: true })).toHaveCount(0);
  expect(mock.unexpected).toEqual([]);
});

test('a supplier member who may not decide sees why instead of the post-handshake actions', async ({ page }) => {
  const mock = await fixture(page);
  mock.set({ can_decide: false });
  await openSupplierDeal(page);
  const deal = supplierPage(page);
  await expect(deal.getByText('Solo un miembro con permiso para decidir sobre acuerdos puede marcar el avance de la orden (Procesando, En camino, Entregado), reportar un faltante o cancelar.', { exact: true })).toBeVisible();
  for (const name of ['Marcar como procesando', 'Marcar en camino', 'Marcar como entregado', 'Reportar faltante', 'Cancelar acuerdo']) await expect(deal.getByRole('button', { name, exact: true })).toHaveCount(0);
  expect(mock.unexpected).toEqual([]);
});

test('purchase history lists every confirmed deal and flags a shortfall waiting for the client', async ({ page }) => {
  const mock = await fixture(page);
  mock.set(pendingShortfall(), { [lineA]: 2 });
  await page.goto('/?vista=compras');
  await expect(page.getByRole('article', { name: /ORD-ACUERDO-001/ })).toContainText('Faltante por responder');
  expect(mock.listed.at(-1)).toBe('agreements');
  expect(mock.unexpected).toEqual([]);
});

test('on a phone the shortfall form stays inside the screen', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await fixture(page);
  await openSupplierDeal(page);
  const deal = supplierPage(page);
  await deal.getByRole('button', { name: 'Reportar faltante', exact: true }).click();
  const form = deal.getByRole('form', { name: 'Reportar faltante' });
  await expect(form).toBeVisible();
  const box = (await form.boundingBox())!;
  expect(box.x).toBeGreaterThanOrEqual(0);
  expect(box.x + box.width).toBeLessThanOrEqual(390);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await form.scrollIntoViewIfNeeded();
  await page.screenshot({ path: '/tmp/motionpartes-deal-shortfall-mobile.png' });
});

// Acuerdos, for superusers: server-rendered administration needs a session token.
test('superusers find a deal in Acuerdos and cancel a dispute as MotionPartes', async ({ page }) => {
  test.skip(!process.env.E2E_ADMIN_TOKEN, 'A disposable superuser token is required for server-rendered administration.');
  await page.context().addCookies([{ name: 'partsmall_session', value: process.env.E2E_ADMIN_TOKEN!, url: 'http://localhost:8080', httpOnly: true, sameSite: 'Strict' }]);
  const row = { id: orderId, reference: 'ORD-ACUERDO-001', status: 'handshaked', version: 5, client: { id: clientId, name: 'TALLER CENTRAL' }, supplier: { id: supplierId, name: 'REPUESTOS CENTRAL' },
    created_at: stamp, handshaked_at: stamp, processing_at: null as string | null, in_transit_at: null as string | null, delivered_at: null as string | null, received_at: null, cancelled_at: null, cancelled_side: null, cancel_reason_label: null, line_count: 2, unit_count: 5,
    shortfall_count: 1, shortfall_pending: true };
  let detail = { ...row, cancel_note: '', lines: [{ id: lineA, codigo: 'A-001', sku: 'SKU-A', description: 'TAMBOR A-001', requested: 3, agreed: 3, missing: 0, pending_missing: 2, final: 3 }],
    shortfalls: [{ id: 'S1', status: 'pending', reason_label: 'Sin existencias', note: '', reported_at: stamp, responded_at: null, lines: [{ codigo: 'A-001', agreed: 3, missing: 2 }] }],
    events: [{ id: 1, kind: 'report_shortfall', created_at: stamp, account_name: 'REPUESTOS CENTRAL', description: 'EL PROVEEDOR REPORTÓ UN FALTANTE (SIN EXISTENCIAS): A-001: FALTAN 2 DE 3.' }] };
  const cancels: unknown[] = [], unexpected: string[] = [];
  const lists: string[] = [];
  await page.route('**/api/**', route => { if (signedOutInbox(route)) return; unexpected.push(`${route.request().method()} ${new URL(route.request().url()).pathname}`); return route.fulfill({ status: 404, json: { detail: 'Ruta no prevista' } }); });
  await fakeServerBasket(page);  // the client's cesta (mall/basket.py), empty
  await page.route('**/api/market/analytics/events', route => route.fulfill({ status: 202, json: { recorded: true } }));
  await page.route(/\/api\/management\/roles(?:\?.*)?$/, route => route.fulfill({ json: [] }));
  await page.route(/\/api\/management\/deals(?:\?.*)?$/, route => { lists.push(new URL(route.request().url()).search); return route.fulfill({ json: { ...empty, count: 1, results: [detail] } }); });
  await page.route(`**/api/management/deals/${orderId}`, route => route.fulfill({ json: detail }));
  await page.route(`**/api/management/deals/${orderId}/cancel`, route => {
    cancels.push(route.request().postDataJSON());
    detail = { ...detail, status: 'cancelled', version: 6, cancelled_side: 'admin' as never, cancel_reason_label: 'Disputa entre las partes' as never, cancelled_at: stamp as never, shortfall_pending: false };
    return route.fulfill({ json: detail });
  });
  await page.goto('/administracion?seccion=acuerdos');
  await expect(page.getByRole('heading', { name: 'Acuerdos' }).first()).toBeVisible();
  expect(lists[0]).toContain('status=agreements');
  // Abiertos: every confirmed order not received or cancelled, those already on their way included.
  await page.getByRole('group', { name: 'Estado de los acuerdos' }).getByRole('button', { name: 'Abiertos', exact: true }).click();
  await expect.poll(() => lists.at(-1)).toContain('status=open_agreements');
  const listed = page.getByRole('row').filter({ hasText: 'ORD-ACUERDO-001' });
  await expect(listed).toContainText('Faltante por responder');
  await listed.getByRole('button', { name: 'Ver acuerdo ORD-ACUERDO-001', exact: true }).click();
  const panel = page.getByRole('dialog', { name: 'Acuerdo ORD-ACUERDO-001' });
  await expect(panel).toContainText('A-001: faltan 2 de 3');
  await expect(panel).not.toContainText('12.35');
  await panel.getByRole('button', { name: 'Cancelar como MotionPartes', exact: true }).click();
  await panel.getByLabel('Motivo de la cancelación', { exact: true }).selectOption('dispute');
  await panel.getByLabel('Nota para las partes (opcional)').fill('las partes no se ponen de acuerdo');
  await panel.getByRole('button', { name: 'Cancelar el acuerdo', exact: true }).click();
  await expect(panel.locator('.supplier-request-status.cancelled')).toHaveText('Cancelado');
  expect(cancels).toEqual([{ expected_version: 5, reason_code: 'dispute', note: 'LAS PARTES NO SE PONEN DE ACUERDO' }]);
  await panel.getByRole('button', { name: 'Cerrar ventana', exact: true }).click();
  // After dispatch the parties no longer cancel; MotionPartes still can, and says what happens to the stock.
  for (const status of ['in_transit', 'delivered'] as const) {
    detail = { ...detail, status, version: 6, processing_at: stamp, in_transit_at: stamp, delivered_at: status === 'delivered' ? stamp : null, cancelled_side: null as never,
      cancel_reason_label: null as never, cancelled_at: null as never, shortfall_pending: false };
    await listed.getByRole('button', { name: 'Ver acuerdo ORD-ACUERDO-001', exact: true }).click();
    await panel.getByRole('button', { name: 'Cancelar como MotionPartes', exact: true }).click();
    await expect(panel.getByRole('form', { name: 'Cancelar como MotionPartes' })).toContainText('La orden ya salió del proveedor.');
    await panel.getByRole('button', { name: 'Cerrar ventana', exact: true }).click();
  }
  expect(unexpected).toEqual([]);
});
