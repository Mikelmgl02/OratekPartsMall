import { expect, Page, test } from '@playwright/test';
import type { Deal, Quotation } from '../lib/deal-types';
import type { SupplierRequestLine } from '../lib/request-types';
import { mockQuoteDrafts } from './quote-draft-mock';
import { fakeServerBasket } from './server-basket';

// The conversations panel's right column: its Cotizaciones table opens the deal itself, embedded next to the chat, with its decisions above its sections.
const clientId = '11111111-1111-4111-8111-111111111111';
const supplierId = '22222222-2222-4222-8222-222222222222';
const orderId = '33333333-3333-4333-8333-333333333333';
const lineId = '44444444-4444-4444-8444-444444444444';
const stamp = '2026-10-03T12:00:00Z';
const empty = { count: 0, results: [], next: null, previous: null };
const client = { id: clientId, name: 'TALLER CENTRAL' }, supplier = { id: supplierId, name: 'REPUESTOS CENTRAL' };
const quote: Quotation = { id: '88888888-8888-4888-8888-000000000001', revision: 1, currency: 'USD', terms: 'RETIRO EN TIENDA', total: '61.75', created_at: stamp,
  supplier_confirmed_at: stamp, client_confirmed_at: null,
  lines: [{ order_line_id: lineId, codigo: '58411-1R000-G', sku: '58411-1R000', description: 'TAMBOR', quantity: 5, unit_price: '12.35', total: '61.75' }] };
const line: SupplierRequestLine = { id: lineId, part_id: '66666666-6666-4666-8666-666666666666', supplier_item_id: '77777777-7777-4777-8777-777777777777',
  supplier_invent_id: 'ID-1', sku: '58411-1R000', name: 'TAMBOR', codigo: '58411-1R000-G', brand: 'KSM', description: 'TAMBOR', quantity: 5,
  stock: { reported_quantity: 9, reserved_quantity: 0, available_quantity: 9, shortfall: 0, updated_at: stamp } };

async function panel(page: Page, side: 'client' | 'supplier', status: Deal['status'], quoted: boolean, lines: SupplierRequestLine[] = [line], extra: Partial<Deal> = {}) {
  const account = side === 'client' ? { ...client, roles: ['client_business'], capabilities: ['client'] } : { ...supplier, roles: ['supplier_retail'], capabilities: ['supplier'] };
  let deal = { id: orderId, reference: 'ORD-0123', client, supplier, status, version: 3, created_at: stamp, reviewed_at: stamp, handshaked_at: null,
    line_count: lines.length, unit_count: 5, notes: '', quotation: quoted ? quote : null, quotations: quoted ? [quote] : [], events: [], lines, can_decide: true, ...extra } as unknown as Deal;
  const actions: Record<string, unknown>[] = [], reviews: string[] = [];
  const counterpart = side === 'client' ? supplier : client;
  await page.route('**/api/**', route => route.fulfill({ json: empty }));
  await page.route('**/api/session', route => route.fulfill({ json: { authenticated: true, user: { id: 1, username: 'EMPLEADO', first_name: '', last_name: '', is_superuser: false },
    accounts: { ...empty, count: 1, results: [{ ...account, permission: 'owner' }] } } }));
  await page.route(/\/api\/market\/inbox$/, route => route.fulfill({ json: { unread_messages: 0, unread_conversations: 0, unseen_notifications: 0, latest_message: null, latest_notification: null } }));
  // One conversation (its own id) with this order.
  await page.route(/\/api\/market\/inbox\/conversations(\?.*)?$/, route => route.fulfill({ json: { ...empty, count: 1, results: [{ id: 'cccccccc-cccc-4ccc-8ccc-cccccccccccc',
    order_id: orderId, reference: 'ORD-0123', status: deal.status, orders: [{ id: orderId, reference: 'ORD-0123', status: deal.status }], order_count: 1, open_orders: 1, side,
    account: { id: account.id, name: account.name }, counterpart, unread: 0, activity: stamp, last_message: null }] } }));
  await page.route(/\/deals\/[^/]+\/messages(\?.*)?$/, route => route.fulfill({ json: { results: [], cursor: 0, has_more: false, has_earlier: false } }));
  await page.route(new RegExp(`/(requests|sent-requests)/${orderId}$`), route => route.fulfill({ json: deal }));
  await page.route(/\/threads\/[^/]+\/orders$/, route => route.fulfill({ json: { results: [{ id: orderId, reference: 'ORD-0123', status: deal.status, created_at: stamp, reviewed_at: stamp,
    line_count: lines.length, unit_count: 5, quotation_revision: quoted ? 1 : null, quotation_total: quoted ? quote.total : null, quotation_currency: 'USD' }] } }));
  await page.route(new RegExp(`/requests/${orderId}/review$`), route => { reviews.push(route.request().method()); deal = { ...deal, status: 'reviewed', version: deal.version + 1 }; return route.fulfill({ json: deal }); });
  await page.route(new RegExp(`/deals/${orderId}/actions$`), route => {
    const payload = route.request().postDataJSON(); actions.push(payload);
    const status = ({ accept: 'handshaked', request_adjustment: 'adjustment', reject: 'cancelled' } as Record<string, Deal['status']>)[payload.action];
    deal = { ...deal, status, version: deal.version + 1 };
    return route.fulfill({ json: deal });
  });
  const drafts = side === 'supplier' ? await mockQuoteDrafts(page, () => deal) : null;
  await page.goto('/');
  await page.getByRole('button', { name: 'Mensajes', exact: true }).click();
  const conversations = page.getByRole('dialog', { name: 'Conversaciones', exact: true });
  await conversations.getByRole('table', { name: `Cotizaciones con ${counterpart.name}`, exact: true }).getByRole('button', { name: 'Abrir ORD-0123', exact: true }).click();
  const document = conversations.getByRole('region', { name: 'Documento de la orden', exact: true });
  await expect(document).toBeVisible();
  return { document, conversations, actions, reviews, drafts };
}

test('a client confirms or asks to adjust a quotation from the panel, next to the chat', async ({ page }) => {
  const { document, conversations, actions } = await panel(page, 'client', 'quoted', true);
  await expect(document.getByRole('region', { name: 'Cotización vigente v1', exact: true })).toContainText('USD 61.75');  // opens on the quotation
  await expect(conversations.getByRole('log', { name: 'Mensajes del acuerdo', exact: true })).toHaveCount(1);  // the panel's chat only
  await expect(document.getByRole('button', { name: 'Conversación', exact: true })).toHaveCount(0);
  const decisions = document.getByRole('group', { name: 'Decisiones', exact: true });
  await decisions.getByRole('button', { name: 'Solicitar ajuste', exact: true }).click();
  await document.getByLabel('¿Qué necesitas ajustar?').fill('solo 3 unidades');
  await document.getByRole('button', { name: 'Enviar ajuste', exact: true }).click();
  await expect.poll(() => actions.at(-1)).toMatchObject({ action: 'request_adjustment', quotation_id: quote.id, reason: 'SOLO 3 UNIDADES' });
  await expect(decisions.getByRole('button', { name: 'Confirmar', exact: true })).toHaveCount(0);  // now with the supplier
});

test('a sent quotation says until when it holds its units, and when the hold lapsed', async ({ page }) => {
  const held = { stock_hold: { state: 'held' as const, units: 5, until: '2026-10-15T15:00:00Z' } };
  const { document } = await panel(page, 'client', 'quoted', true, [line], held);
  await expect(document.getByRole('note', { name: 'Reserva de existencias', exact: true })).toHaveText(
    'REPUESTOS CENTRAL reserva estas 5 unidades para ti. Confírmala antes del 10/15/2026, 10:00 a. m. para asegurarlas.');
  await page.unrouteAll({ behavior: 'ignoreErrors' });
  const lapsed = await panel(page, 'supplier', 'quoted', true, [line], { stock_hold: { state: 'lapsed', units: 5, until: '2026-10-15T15:00:00Z' } });
  await expect(lapsed.document.getByRole('note', { name: 'Reserva de existencias', exact: true })).toHaveText(
    'La reserva venció el 10/15/2026, 10:00 a. m.: sus 5 unidades volvieron a tu inventario. Si el cliente confirma, se revisan tus existencias.');
});

test('a short quotation lets the client ask another supplier for what is missing, into the cesta', async ({ page }) => {
  const short = { ...quote, total: '37.05', lines: [{ ...quote.lines[0], quantity: 3, total: '37.05' }] };  // 3 of the 5 asked for
  const { document } = await panel(page, 'client', 'quoted', true, [line], { quotation: short, quotations: [short] });
  const basket = await fakeServerBasket(page, { server: { [clientId]: [] } });
  const other = { id: '99999999-9999-4999-8999-000000000001', name: 'AUTOPARTES DEL ISTMO' }, itemId = '99999999-9999-4999-8999-000000000002';
  const item = { id: itemId, codigo: '58411-1R000-G', brand: 'KSM', description: 'TAMBOR' };
  const posted: unknown[] = [];
  await page.route(new RegExp(`/sent-requests/${orderId}/missing$`), route => {
    if (route.request().method() !== 'POST') return route.fulfill({ json: { order: { id: orderId, reference: 'ORD-0123', status: 'quoted', supplier_name: supplier.name }, added_at: null,
      lines: [{ order_line_id: lineId, part_id: line.part_id, sku: '58411-1R000', codigo: '58411-1R000-G', brand: 'KSM', description: 'TAMBOR', requested: 5, missing: 2,
        in_basket: 0, suggested: itemId, offers: [{ supplier_id: other.id, supplier_name: other.name, known: true, items: [item] }] }] } });
    posted.push(route.request().postDataJSON());
    basket.lines[clientId] = [{ key: `${line.part_id}:${other.id}:${itemId}`, quantity: 2, source: 'faltante', available: true, changed: false,
      part: { id: line.part_id, sku: '58411-1R000', name: '', description: 'TAMBOR', codes: [] },
      offer: { supplier_id: other.id, supplier_name: other.name, in_stock: true, selected_item: item } }];
    return route.fulfill({ json: { lines: basket.lines[clientId], count: 1 } });
  });
  await expect(document.getByRole('group', { name: 'Lo que falta', exact: true })).toContainText('La cotización no ofrece 2 unidades de lo que pediste.');
  await document.getByRole('button', { name: 'Pedir lo que falta a otro proveedor', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Pedir lo que falta · ORD-0123', exact: true });
  await expect(dialog).toContainText('REPUESTOS CENTRAL no traerá 2 unidades de esta orden.');
  await expect(dialog.getByRole('list', { name: 'Lo que falta', exact: true })).toContainText('Faltan 2 de 5');
  await expect(dialog.getByLabel('Proveedor para 58411-1R000-G', { exact: true })).toHaveValue(itemId);  // suggested, among those in stock
  await expect(dialog.getByLabel('Unidades de 58411-1R000-G', { exact: true })).toHaveValue('2');
  await dialog.getByRole('button', { name: 'Agregar a mi cesta (1)', exact: true }).click();
  await expect(dialog.getByRole('status')).toContainText('Se agregó 1 artículo a tu cesta.');
  expect(posted).toEqual([{ lines: [{ order_line_id: lineId, supplier_item_id: itemId, quantity: 2 }] }]);
  await expect(page.getByRole('button', { name: 'Abrir cesta, 1 artículo', exact: true })).toBeVisible();  // the header counts it at once
  await dialog.getByRole('button', { name: 'Ver cesta', exact: true }).click();
  await expect(page.getByRole('dialog', { name: 'Conversaciones', exact: true })).toHaveCount(0);  // the cesta shows instead
  await expect(page.getByText('Lo que faltó en otra orden', { exact: true })).toBeVisible();
});

test('a client confirms the quotation in two steps from the panel', async ({ page }) => {
  const { document, actions } = await panel(page, 'client', 'quoted', true);
  await document.getByRole('group', { name: 'Decisiones', exact: true }).getByRole('button', { name: 'Confirmar', exact: true }).click();
  await expect(document).toContainText('¿Confirmar este acuerdo por USD 61.75?');
  expect(actions).toEqual([]);
  await document.getByRole('button', { name: 'Sí, confirmar acuerdo', exact: true }).click();
  await expect.poll(() => actions.at(-1)).toMatchObject({ action: 'accept', quotation_id: quote.id });
  await expect(document.getByRole('status')).toContainText('Acuerdo confirmado por ambas partes');
});

test('Rechazar ends the order with a reason, before or after the quotation', async ({ page }) => {
  const { document, actions } = await panel(page, 'client', 'quoted', true);
  await document.getByRole('group', { name: 'Decisiones', exact: true }).getByRole('button', { name: 'Rechazar', exact: true }).click();
  const form = document.getByRole('form', { name: 'Rechazar la orden', exact: true });
  await expect(form).toContainText('¿Rechazar la orden ORD-0123? Se cerrará sin acuerdo y REPUESTOS CENTRAL recibirá el motivo.');
  await expect(form.getByRole('button', { name: 'Rechazar orden', exact: true })).toBeDisabled();  // a reason first
  await expect(form.getByLabel('Motivo del rechazo', { exact: true }).locator('option').first()).toHaveText('Elige un motivo…');
  await expect(form.getByLabel('Motivo del rechazo', { exact: true }).locator('option').nth(1)).toHaveText('El precio es muy alto');
  await form.getByLabel('Motivo del rechazo', { exact: true }).selectOption('price_too_high');
  await form.getByLabel('Comentario para el proveedor', { exact: true }).fill('Otro proveedor lo tenía hoy');
  await form.getByRole('button', { name: 'Rechazar orden', exact: true }).click();
  await expect.poll(() => actions.at(-1)).toMatchObject({ action: 'reject', quotation_id: quote.id, reason_code: 'price_too_high', reason: 'Otro proveedor lo tenía hoy' });
  await expect(document.getByRole('status')).toContainText('Rechazaste la orden.');
  await expect(document.getByRole('group', { name: 'Decisiones', exact: true }).getByRole('button')).toHaveCount(0);
});

test('a pending order is rejected without a quotation', async ({ page }) => {
  const { document, actions } = await panel(page, 'client', 'pending', false);
  await document.getByRole('group', { name: 'Decisiones', exact: true }).getByRole('button', { name: 'Rechazar', exact: true }).click();
  await document.getByLabel('Motivo del rechazo', { exact: true }).selectOption('not_needed');
  await document.getByRole('button', { name: 'Rechazar orden', exact: true }).click();
  await expect.poll(() => actions.at(-1)).toMatchObject({ action: 'reject', reason_code: 'not_needed' });
  expect(actions.at(-1)).not.toHaveProperty('quotation_id');
});

test('a supplier sets prices in the panel: viewing never opens the order, Fijar precios does and shows the editor', async ({ page }) => {
  const { document, reviews } = await panel(page, 'supplier', 'pending', false);
  await expect(document.getByRole('group', { name: 'Decisiones', exact: true }).getByRole('button', { name: 'Fijar precios', exact: true })).toBeVisible();
  expect(reviews).toEqual([]);  // looking at it in the panel does not lock the client's items
  await document.getByRole('button', { name: 'Fijar precios', exact: true }).click();
  await expect.poll(() => reviews).toEqual(['POST']);
  await expect(document.getByRole('navigation', { name: 'Secciones del acuerdo', exact: true }).getByRole('button', { name: 'Cotización', exact: true })).toHaveAttribute('aria-pressed', 'true');
  await expect(document.locator('.quotation-grid[aria-label="Artículos de la cotización"]')).toBeVisible();
  await expect(document.getByRole('button', { name: /Confirmar y enviar cotización/ })).toBeVisible();
});

test('a supplier edits a sent quotation on the go, until the client confirms it', async ({ page }) => {
  const { document } = await panel(page, 'supplier', 'quoted', true);
  await expect(document).toContainText('Enviaste la cotización v1. Mientras el cliente no la confirme, puedes editarla y enviarle una nueva versión.');
  await expect(document.locator('.quotation-grid[aria-label="Artículos de la cotización"]')).toHaveCount(0);  // the editor opens only when asked
  await document.getByRole('group', { name: 'Decisiones', exact: true }).getByRole('button', { name: 'Editar cotización', exact: true }).click();
  await expect(document.locator('.quotation-grid[aria-label="Artículos de la cotización"]')).toBeVisible();
  await expect(document.getByRole('button', { name: /Confirmar y enviar cotización/ })).toBeVisible();
  await expect(document.getByRole('region', { name: 'Cotización vigente v1', exact: true })).toContainText('USD 61.75');  // still what the client sees
});

test('a long order lists its items in a grid: searched, and narrowed to the ones whose stock needs confirming', async ({ page }) => {
  const many = Array.from({ length: 12 }, (_, index) => ({ ...line, id: `44444444-4444-4444-8444-${String(index).padStart(12, '0')}`, codigo: `COD-${index}`, sku: `SKU-${index}`,
    description: index === 5 ? 'BOMBA DE AGUA' : 'TAMBOR', stock: index === 7 ? null : { ...line.stock!, shortfall: index === 3 ? 2 : 0 } }));
  const { document } = await panel(page, 'supplier', 'pending', false, many);
  const grid = document.getByRole('region', { name: 'Artículos de la orden', exact: true });
  await expect(grid.getByRole('row')).toHaveCount(13);  // the heading and every item
  await expect(grid.getByRole('columnheader')).toHaveText(['Código / descripción', 'Solicitadas', 'Disponibles', 'SKU interno', 'Tu ID de inventario']);
  await document.getByLabel('Buscar en los artículos', { exact: true }).fill('bomba');
  await expect(grid.getByRole('row')).toHaveCount(2);
  await expect(grid).toContainText('COD-5');
  await document.getByLabel('Buscar en los artículos', { exact: true }).fill('');
  await document.getByRole('group', { name: 'Mostrar artículos', exact: true }).getByRole('button', { name: 'Por revisar (2)', exact: true }).click();
  await expect(grid.getByRole('row')).toHaveCount(3);
  await expect(grid).toContainText('Faltan 2 unidades');
  await expect(grid).toContainText('Revisar artículo');
  const row = grid.getByRole('row').filter({ hasText: 'COD-7' });
  await row.getByRole('gridcell').first().click();  // the focused row stays highlighted
  await expect(row).toHaveClass(/ag-row-focus/);
  await expect(row).toHaveCSS('background-color', 'rgb(238, 246, 226)');
  await expect(grid.getByRole('row').filter({ hasText: 'COD-3' })).not.toHaveCSS('background-color', 'rgb(238, 246, 226)');
});

test('a short grid’s column menu opens over the panel instead of being clipped by the grid', async ({ page }) => {
  const { document } = await panel(page, 'client', 'quoted', true);
  const grid = document.getByRole('region', { name: 'Líneas de la cotización v1', exact: true });
  await grid.getByRole('columnheader', { name: 'Código / descripción' }).hover();
  await grid.locator('.ag-header-cell[col-id="item"] .ag-header-cell-menu-button').click();
  const menu = page.locator('.ag-menu');
  await expect(menu.getByText('Ordenar Ascendente')).toBeVisible();
  expect(await menu.evaluate(element => !element.closest('.quote-lines') && !!element.closest('dialog'))).toBe(true);  // in the dialog, outside the grid
  const [menuBox, gridBox] = [(await menu.boundingBox())!, (await grid.boundingBox())!];
  expect(menuBox.y + menuBox.height).toBeGreaterThan(gridBox.y + gridBox.height);  // taller than the one-row grid, and whole
  await expect(menu.getByText('Elegir Columnas', { exact: true })).toHaveCount(0);  // columns cannot be hidden
  await expect(menu.getByText('Restablecer Columnas', { exact: true })).toBeVisible();
  await page.keyboard.press('Escape');  // closes the menu, not the panel
  await expect(menu).toHaveCount(0);
  await expect(grid).toBeVisible();
  const headers = await grid.getByRole('columnheader').count();
  await grid.getByRole('columnheader', { name: 'Código / descripción' }).dragTo(page.locator('body'), { targetPosition: { x: 20, y: 20 } });  // dragged out of the grid
  await expect(grid.getByRole('columnheader')).toHaveCount(headers);
});

test.describe('on a touch screen', () => {
  test.use({ hasTouch: true, viewport: { width: 1100, height: 800 } });  // a column narrower than the grid
  test('a sideways swipe on a grid’s header or rows moves its columns', async ({ page }) => {
    const many = Array.from({ length: 6 }, (_, index) => ({ ...line, id: `44444444-4444-4444-8444-${String(index).padStart(12, '0')}`, codigo: `COD-${index}`, sku: `SKU-${index}` }));
    const { document } = await panel(page, 'supplier', 'pending', false, many);
    const grid = document.getByRole('region', { name: 'Artículos de la orden', exact: true });
    const columns = grid.locator('.ag-center-cols-viewport');
    expect(await columns.evaluate(element => element.scrollWidth > element.clientWidth)).toBe(true);  // wider than the side panel
    const cdp = await page.context().newCDPSession(page);
    const swipe = async (box: { x: number; y: number; width: number; height: number }) => cdp.send('Input.synthesizeScrollGesture',
      { x: box.x + box.width / 2, y: box.y + box.height / 2, xDistance: -150, yDistance: 0, gestureSourceType: 'touch', speed: 800 });
    const left = () => columns.evaluate(element => element.scrollLeft);
    await swipe((await grid.locator('.ag-header-viewport').boundingBox())!);
    await expect.poll(left).toBeGreaterThan(100);  // the header, which never scrolls by itself
    const afterHeader = await left();
    await swipe((await grid.getByRole('row').nth(1).boundingBox())!);
    await expect.poll(left).toBeGreaterThan(afterHeader);  // and the rows
  });
});
