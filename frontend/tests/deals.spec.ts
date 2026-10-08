import { expect, Page, test } from '@playwright/test';
import type { Deal, DealMessage, DealStatus } from '../lib/deal-types';
import { openSupplierNavigation } from './supplier-navigation';
import { mockQuoteDrafts } from './quote-draft-mock';

const clientId = '11111111-1111-4111-8111-111111111111';
const supplierId = '22222222-2222-4222-8222-222222222222';
const orderId = '33333333-3333-4333-8333-333333333333';
const lineId = '44444444-4444-4444-8444-444444444444';
const accounts = [{ id: clientId, name: 'TALLER CENTRAL', roles: ['client_business'], capabilities: ['client'] },
  { id: supplierId, name: 'REPUESTOS CENTRAL', roles: ['supplier_retail'], capabilities: ['supplier'] }];
const empty = { count: 0, results: [], next: null, previous: null };
const stamp = '2026-10-03T12:00:00Z';
const dialog = (page: Page) => page.locator('.deal-order-content, dialog.deal-modal');
async function backToRequests(page: Page) {
  if (new URL(page.url()).pathname.startsWith('/proveedor/')) await page.getByRole('link', {name:'Volver a solicitudes', exact:true}).click();
  else if (await page.getByRole('dialog').count()) await dialog(page).getByRole('button', {name:'Cerrar ventana', exact:true}).click();
}

async function fixture(page: Page) {
  const gridWarnings: string[] = [];
  page.on('console', message => { if (/license|licence|not registered/i.test(message.text())) gridWarnings.push('Grid configuration warning'); });
  let order: Deal = { id: orderId, reference: 'ORD-PRUEBA-001', client: { id: clientId, name: accounts[0].name },
    supplier: { id: supplierId, name: accounts[1].name }, submission_id: '55555555-5555-4555-8555-555555555555', status: 'pending',
    version: 1, created_at: stamp, reviewed_at: null, handshaked_at: null, line_count: 1, unit_count: 5, notes: '',
    quotation: null, quotations: [], events: [], lines: [{ id: lineId, part_id: '66666666-6666-4666-8666-666666666666',
      supplier_item_id: '77777777-7777-4777-8777-777777777777', supplier_invent_id: 'ID-PRIVADO', sku: '58411-1R000',
      name: 'TAMBOR', codigo: '58411-1R000-G', brand: '', description: 'TAMBOR HYUNDAI ACCENT', quantity: 5,
      stock: { reported_quantity: 5, reserved_quantity: 0, available_quantity: 5, shortfall: 0, updated_at: stamp } }] };
  const messages: DealMessage[] = [];
  const actions: Record<string, unknown>[] = [];
  let reviews = 0;
  // When set, the server refuses accepts because stock the supplier relied on dropped after the quote.
  let blockAccept = false;
  const unexpected: string[] = [];
  await page.route('**/api/**', route => { unexpected.push(`${route.request().method()} ${new URL(route.request().url()).pathname}`); return route.fulfill({status:404, json:{detail:'Ruta no prevista'}}); });
  await page.route('**/api/session', route => route.fulfill({json:{authenticated:true, user:{id:801, username:'EMPLEADO', is_superuser:false}, accounts:{...empty, count:2, results:accounts}}}));
  await page.route('**/api/market/analytics/events', route => route.fulfill({status:202, json:{recorded:true}}));
  await page.route('**/api/market/wishlist/state', route => route.fulfill({json:{part_ids:[], count:0}}));
  await page.route(/\/api\/market\/catalog(?:\?.*)?$/, route => route.fulfill({json:empty}));
  await page.route(/\/api\/market\/accounts\/[^/]+\/inventory(?:\?.*)?$/, route => route.fulfill({json:empty}));
  await page.route(/\/api\/market\/accounts\/[^/]+\/(sent-requests|requests)(?:\?.*)?$/, route => {
    const status = new URL(route.request().url()).searchParams.get('status');
    const supplierList = new URL(route.request().url()).pathname.includes('/requests');
    const results = !status || status === order.status ? [{...order, quoted_unit_count: order.quotation?.lines.reduce((sum,line) => sum + line.quantity, 0),
      ...(supplierList ? {draft_state: null, availability_alert: blockAccept ? 'blocked' : null} : {})}] : [];
    return route.fulfill({json:{...empty, count:results.length, results}});
  });
  await page.route(/\/api\/market\/accounts\/[^/]+\/(sent-requests|requests)\/[^/]+$/, route => route.fulfill({json:order}));
  await page.route(`/api/market/accounts/${supplierId}/requests/${orderId}/review`, route => {
    reviews++;
    if (order.status === 'pending') order = {...order, status:'reviewed', version:order.version + 1, reviewed_at:stamp};
    return route.fulfill({json:order});
  });
  await page.route(/\/api\/market\/accounts\/[^/]+\/deals\/[^/]+\/messages(?:\?.*)?$/, route => {
    const accountId = new URL(route.request().url()).pathname.split('/')[4];
    if (route.request().method() === 'POST') {
      const payload = route.request().postDataJSON();
      const found = messages.find(value => value.message_id === payload.message_id);
      const message = found || {id:messages.length + 1, message_id:payload.message_id, body:payload.body, account_id:accountId,
        account_name:accounts.find(value => value.id === accountId)!.name, actor_name:'EMPLEADO', created_at:stamp};
      if (!found) messages.push(message);
      return route.fulfill({json:message});
    }
    const after = Number(new URL(route.request().url()).searchParams.get('after') || 0);
    return route.fulfill({json:{results:messages.filter(value => value.id > after), cursor:messages.length, has_more:false, has_earlier:false}});
  });
  const drafts = await mockQuoteDrafts(page, () => order);
  await page.route(/\/api\/market\/accounts\/[^/]+\/deals\/[^/]+\/actions$/, route => {
    const payload = route.request().postDataJSON(); actions.push(payload);
    if (payload.expected_version !== order.version) return route.fulfill({status:409, json:{detail:'El acuerdo cambió. Actualízalo.'}});
    if (payload.action === 'accept' && blockAccept) return route.fulfill({status:409, json:{detail:'El proveedor debe confirmar la disponibilidad de algunos artículos antes de cerrar el acuerdo. Solicita un ajuste para que el proveedor prepare una versión actualizada.'}});
    // Publishing is bound to the saved draft, exactly like the server.
    if (payload.action === 'quote' && (!drafts.current().persisted || payload.draft_version !== drafts.current().draft_version)) return route.fulfill({status:409, json:{detail:'El borrador cambió. Revísalo antes de publicar.'}});
    if (payload.action === 'quote') {
      const revision = order.quotations.length + 1;
      const lines = payload.lines.map((line: {order_line_id:string; quantity:number; unit_price:string}) => {
        const original = order.lines.find(value => value.id === line.order_line_id)!;
        return {...line, codigo:original.codigo, sku:original.sku, description:original.description, total:(line.quantity * Number(line.unit_price)).toFixed(2)};
      });
      const quote = {id:`88888888-8888-4888-8888-${String(revision).padStart(12, '0')}`, revision, lines, currency:payload.currency,
        terms:payload.terms, total:lines.reduce((sum:number,line:{total:string}) => sum + Number(line.total), 0).toFixed(2),
        created_at:stamp, supplier_confirmed_at:stamp, client_confirmed_at:null};
      order = {...order, status:'quoted', quotation:quote, quotations:[...order.quotations, quote]};
      drafts.clear();
    } else if (payload.action === 'request_adjustment') {
      order = {...order, status:'adjustment'};
      messages.push({id:messages.length + 1, message_id:payload.operation_id, body:payload.reason, account_id:clientId, account_name:accounts[0].name, actor_name:'EMPLEADO', created_at:stamp});
    } else if (payload.action === 'return_quote') { order = {...order, status:'quoted'}; drafts.clear(); }
    else if (payload.action === 'accept') {
      order = {...order, status:'handshaked', handshaked_at:stamp, quotation:{...order.quotation!, client_confirmed_at:stamp}};
      order.quotations = order.quotations.map(quote => quote.id === order.quotation?.id ? order.quotation : quote);
    }
    order = {...order, version:order.version + 1, events:[...order.events, {id:order.events.length + 1, kind:payload.action,
      description:payload.reason || payload.action.toUpperCase(), account_name:'CUENTA', actor_name:'EMPLEADO', created_at:stamp, quotation_id:order.quotation?.id || null}]};
    return route.fulfill({json:order});
  });
  return {getOrder:() => order, setStatus:(status:DealStatus) => {order = {...order, status, version:order.version + 1};},
    setLines:(lines:Deal['lines']) => {order = {...order, lines, line_count:lines.length, unit_count:lines.reduce((sum,line) => sum + line.quantity, 0)} as Deal;},
    actions, messages, drafts, reviews:() => reviews, unexpected, gridWarnings, blockAccept:(value:boolean) => { blockAccept = value; }};
}
async function asSupplier(page: Page) {
  await backToRequests(page);
  await page.getByLabel('Cuenta activa', {exact:true}).selectOption(supplierId);
  if (page.viewportSize()!.width < 760) { await page.getByRole('button', {name:'Cuenta y listas', exact:true}).click(); await page.getByRole('region', {name:'Cuenta y listas', exact:true}).getByRole('button', {name:'Panel de proveedor', exact:true}).click(); }
  else await page.getByRole('button', {name:'Para proveedores', exact:true}).click();
  await openSupplierNavigation(page);
  await page.getByRole('tab', {name:'Solicitudes', exact:true}).click();
  await page.getByRole('link', {name:'Ver solicitud ORD-PRUEBA-001', exact:true}).click();
  await expect(dialog(page).getByText('Los artículos de esta orden están bloqueados.', {exact:false})).toBeVisible();
}
async function asClient(page: Page) {
  await backToRequests(page);
  await page.getByLabel('Cuenta activa', {exact:true}).selectOption(clientId);
  await page.goto('/?vista=solicitudes');
  await page.getByRole('button', {name:'Ver enviada ORD-PRUEBA-001', exact:true}).click();
}
async function quoteTab(page: Page) { await dialog(page).getByRole('button', {name:'Cotización', exact:true}).click(); }
const quoteCell = (page: Page, column: string, id = lineId) => dialog(page).locator(`.quotation-grid .ag-row[row-id="${id}"] .ag-cell[col-id="${column}"]`);
async function editQuote(page: Page, column: 'unit_price' | 'quantity', value: string, codigo = '58411-1R000-G', id = lineId) {
  await quoteCell(page, column, id).click();
  const input = dialog(page).getByLabel(`${column === 'unit_price' ? 'Precio' : 'Unidades ofrecidas'} de ${codigo}`, {exact:true});
  await input.fill(value); await input.press('Enter');
}

for (const mobile of [false, true]) test(`${mobile ? 'mobile' : 'desktop'}: private chat, revised quotation and double confirmation close the deal and populate purchase history`, async ({page}) => {
  if (mobile) await page.setViewportSize({width:390, height:844});
  const state = await fixture(page);
  await page.goto('/?vista=solicitudes');
  await page.getByRole('button', {name:'Ver enviada ORD-PRUEBA-001', exact:true}).click();
  await quoteTab(page);
  await expect(dialog(page).getByRole('button', {name:'Confirmar cotización', exact:true})).toHaveCount(0);
  await expect(dialog(page).getByRole('button', {name:'Confirmar y enviar cotización', exact:true})).toHaveCount(0);
  await dialog(page).getByRole('button', {name:'Conversación', exact:true}).click();
  await dialog(page).getByLabel('Mensaje del acuerdo', {exact:true}).fill('retiro mañana');
  await dialog(page).getByRole('button', {name:'Enviar mensaje', exact:true}).click();
  await expect(dialog(page).getByRole('log')).toContainText('RETIRO MAÑANA');
  await asSupplier(page);
  await expect(dialog(page).getByText('En revisión', {exact:true}).first()).toBeVisible();
  await dialog(page).getByRole('button', {name:'Preparar cotización', exact:true}).click();
  await editQuote(page, 'unit_price', '12.50');
  expect(state.actions).toHaveLength(0);
  await dialog(page).getByRole('button', {name:'Conversación', exact:true}).click();
  await expect(dialog(page).getByRole('log')).toContainText('RETIRO MAÑANA');
  await quoteTab(page);
  await expect(quoteCell(page, 'unit_price')).toContainText('12.50');
  expect(await dialog(page).evaluate(element => element.scrollWidth > element.clientWidth)).toBe(false);
  await expect(dialog(page).locator('.ag-watermark')).not.toBeVisible();
  expect(state.gridWarnings).toEqual([]);
  await page.getByTestId('page-viewport').evaluate(element => { element.scrollTop = 0; });
  await page.screenshot({path:`../output/ui/order-page-${mobile ? 'mobile' : 'desktop'}.png`});
  await dialog(page).getByRole('button', {name:'Confirmar y enviar cotización', exact:true}).click();
  await expect(dialog(page).getByRole('region', {name:'Cotización vigente v1', exact:true})).toContainText('62.50');
  await expect(dialog(page).getByLabel('Precio de 58411-1R000-G', {exact:true})).toHaveCount(0);
  await asClient(page); await quoteTab(page);
  await dialog(page).getByRole('button', {name:'Solicitar ajuste', exact:true}).click();
  await dialog(page).getByLabel('¿Qué necesitas ajustar?', {exact:true}).fill('solo necesito tres');
  await dialog(page).getByRole('button', {name:'Enviar ajuste', exact:true}).click();
  await expect(dialog(page).getByText('Ajuste solicitado', {exact:true})).toBeVisible();
  await expect(dialog(page).getByRole('button', {name:'Confirmar cotización', exact:true})).toHaveCount(0);
  await dialog(page).getByRole('button', {name:'Conversación', exact:true}).click();
  await expect(dialog(page).getByRole('log')).toContainText('SOLO NECESITO TRES');
  await dialog(page).getByRole('button', {name:'Cerrar ventana', exact:true}).click();
  await page.getByLabel('Cuenta activa', {exact:true}).selectOption(supplierId);
  await page.goto('/');
  if (mobile) await page.getByRole('button', {name:'Abrir tu panel', exact:true}).click(); else await page.getByRole('button', {name:'Para proveedores', exact:true}).click();
  await openSupplierNavigation(page);
  await page.getByRole('tab', {name:'Solicitudes', exact:true}).click();
  await page.getByRole('link', {name:'Ver solicitud ORD-PRUEBA-001', exact:true}).click();
  await quoteTab(page);
  await editQuote(page, 'quantity', '3');
  await dialog(page).getByRole('button', {name:'Confirmar y enviar cotización', exact:true}).click();
  await expect(dialog(page).getByRole('region', {name:'Cotización vigente v2', exact:true})).toContainText('37.50');
  // The adjustment draft started from v1's price; only the edited quantity was saved before publishing it.
  expect(state.actions[2]).toMatchObject({action:'quote', draft_version:1, lines:[{order_line_id:lineId, quantity:3, unit_price:'12.50'}]});
  expect(state.drafts.saves.at(-1)).toMatchObject({expected_draft_version:0, lines:[{order_line_id:lineId, quantity:3}]});
  await asClient(page); await quoteTab(page);
  await dialog(page).getByRole('button', {name:'Confirmar cotización', exact:true}).click();
  expect(state.getOrder().status).toBe('quoted');
  await dialog(page).getByRole('button', {name:'Sí, confirmar acuerdo', exact:true}).click();
  await expect(dialog(page)).toContainText('HANDSHAKED · Acuerdo confirmado');
  await expect(dialog(page).getByRole('button', {name:'Solicitar ajuste', exact:true})).toHaveCount(0);
  await dialog(page).getByText('Versiones anteriores (1)', {exact:true}).click();
  await expect(dialog(page).getByRole('region', {name:'Cotización anterior v1', exact:true})).toContainText('62.50');
  expect(await dialog(page).evaluate(element => element.scrollWidth > element.clientWidth)).toBe(false);
  expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)).toBe(false);
  await page.screenshot({path:`../output/ui/deal-handshake-${mobile ? 'mobile' : 'desktop'}.png`});
  await dialog(page).getByRole('button', {name:'Cerrar ventana', exact:true}).click();
  await page.goto('/?vista=compras');
  await expect(page.getByRole('main', {name:'Historial de compras', exact:true})).toContainText('ORD-PRUEBA-001');
  await expect(page.getByRole('article')).toContainText('3unidades acordadas');
  expect(state.messages).toHaveLength(2);
  expect(state.actions.map(value => value.action)).toEqual(['quote','request_adjustment','quote','accept']);
  expect(state.actions[0]).toMatchObject({draft_version:1, lines:[{order_line_id:lineId, quantity:5, unit_price:'12.50'}]});
  // The draft is supplier-only: the client's screens never ask for it.
  expect(state.drafts.requests.length).toBeGreaterThan(0);
  expect(state.drafts.requests.filter(value => !value.includes(`/accounts/${supplierId}/`))).toEqual([]);
  expect(state.unexpected).toEqual([]);
});

test('supplier can return an unchanged quotation after adjustment and the client must decide again', async ({page}) => {
  const state = await fixture(page);
  await page.goto('/'); await asSupplier(page); await quoteTab(page);
  await editQuote(page, 'unit_price', '10.00');
  await dialog(page).getByRole('button', {name:'Confirmar y enviar cotización', exact:true}).click();
  await asClient(page); await quoteTab(page);
  await dialog(page).getByRole('button', {name:'Solicitar ajuste', exact:true}).click();
  await dialog(page).getByLabel('¿Qué necesitas ajustar?', {exact:true}).fill('descuento');
  await dialog(page).getByRole('button', {name:'Enviar ajuste', exact:true}).click();
  await dialog(page).getByRole('button', {name:'Cerrar ventana', exact:true}).click();
  await page.getByLabel('Cuenta activa', {exact:true}).selectOption(supplierId);
  await page.goto('/');
  await page.getByRole('button', {name:'Para proveedores', exact:true}).click();
  await openSupplierNavigation(page);
  await page.getByRole('tab', {name:'Solicitudes', exact:true}).click();
  await page.getByRole('link', {name:'Ver solicitud ORD-PRUEBA-001', exact:true}).click();
  await quoteTab(page);
  await dialog(page).getByRole('button', {name:'Devolver la misma cotización para confirmar', exact:true}).click();
  await expect(dialog(page).getByRole('region', {name:'Cotización vigente v1', exact:true})).toBeVisible();
  await expect(dialog(page).getByLabel('Precio de 58411-1R000-G', {exact:true})).toHaveCount(0);
  expect(state.getOrder().quotations).toHaveLength(1);
  expect(state.getOrder().status).toBe('quoted');
  expect(state.actions.at(-1)?.action).toBe('return_quote');
  expect(state.unexpected).toEqual([]);
});

test('quotation grid blocks incomplete or invalid prices and commits the active cell before sending', async ({page}) => {
  const state = await fixture(page);
  await page.goto('/'); await asSupplier(page); await quoteTab(page);
  await dialog(page).getByRole('button', {name:'Confirmar y enviar cotización', exact:true}).click();
  await expect(dialog(page).getByRole('alert')).toContainText('Revisa el precio');
  await editQuote(page, 'unit_price', '-2.50');
  await dialog(page).getByRole('button', {name:'Confirmar y enviar cotización', exact:true}).click();
  await expect(dialog(page).getByRole('alert')).toContainText('Revisa el precio');
  await editQuote(page, 'unit_price', '2.555');
  await dialog(page).getByRole('button', {name:'Confirmar y enviar cotización', exact:true}).click();
  await expect(dialog(page).getByRole('alert')).toContainText('dos decimales');
  await editQuote(page, 'quantity', '-1');
  await dialog(page).getByRole('button', {name:'Confirmar y enviar cotización', exact:true}).click();
  await expect(dialog(page).getByRole('alert')).toContainText('Revisa las unidades');
  await editQuote(page, 'quantity', '5');
  expect(state.actions).toHaveLength(0);
  await quoteCell(page, 'unit_price').click();
  await dialog(page).getByLabel('Precio de 58411-1R000-G', {exact:true}).fill('2,50');
  // Submit without blurring the active editor: its current value must be used.
  await dialog(page).locator('.deal-quote-editor').evaluate(element => (element as HTMLFormElement).requestSubmit());
  await expect(dialog(page).getByRole('region', {name:'Cotización vigente v1', exact:true})).toContainText('12.50');
  expect(state.actions[0].lines).toEqual([{order_line_id:lineId, quantity:5, unit_price:'2.50'}]);
  expect(state.unexpected).toEqual([]);
});

test('supplier pastes an Excel range into the grid while identities and requested quantities remain read-only', async ({page, context}) => {
  await context.grantPermissions(['clipboard-read', 'clipboard-write']);
  const state = await fixture(page);
  const secondId = '44444444-4444-4444-8444-444444444445';
  const first = state.getOrder().lines[0];
  state.setLines([first, {...first, id:secondId, codigo:'00700-NP', quantity:1}]);
  await page.goto('/'); await asSupplier(page); await quoteTab(page);
  await page.evaluate(() => navigator.clipboard.writeText('2\t1,25\n0\t'));
  await quoteCell(page, 'quantity').click();
  const quantityEditor = dialog(page).getByLabel('Unidades ofrecidas de 58411-1R000-G', {exact:true});
  await quantityEditor.fill('9'); await quantityEditor.press('Escape');
  await expect(dialog(page)).toBeVisible();
  await expect(quoteCell(page, 'quantity')).toHaveText('5');
  await page.keyboard.press('ControlOrMeta+V');
  await expect(quoteCell(page, 'quantity')).toHaveText('2');
  await expect(quoteCell(page, 'unit_price')).toContainText('1.25');
  await expect(quoteCell(page, 'quantity', secondId)).toHaveText('0');
  await expect(quoteCell(page, 'requested')).toHaveText('5');
  await expect(quoteCell(page, 'codigo', secondId)).toContainText('00700-NP');
  await dialog(page).getByRole('button', {name:'Confirmar y enviar cotización', exact:true}).click();
  await expect(dialog(page).getByRole('region', {name:'Cotización vigente v1', exact:true})).toContainText('2.50');
  expect(state.actions[0].lines).toEqual([{order_line_id:lineId, quantity:2, unit_price:'1.25'}, {order_line_id:secondId, quantity:0, unit_price:'0.00'}]);
  expect(state.unexpected).toEqual([]);
});

test('a client accept refused for availability keeps the deal open, explains it without quantities and points to Solicitar ajuste', async ({page}) => {
  const state = await fixture(page);
  await page.goto('/'); await asSupplier(page); await quoteTab(page);
  await editQuote(page, 'unit_price', '10.00');
  await dialog(page).getByRole('button', {name:'Confirmar y enviar cotización', exact:true}).click();
  await expect(dialog(page).getByRole('region', {name:'Cotización vigente v1', exact:true})).toBeVisible();
  state.blockAccept(true);
  await asClient(page); await quoteTab(page);
  await dialog(page).getByRole('button', {name:'Confirmar cotización', exact:true}).click();
  await dialog(page).getByRole('button', {name:'Sí, confirmar acuerdo', exact:true}).click();
  const notice = dialog(page).getByRole('alert');
  await expect(notice).toContainText('El proveedor debe confirmar la disponibilidad de algunos artículos antes de cerrar el acuerdo. Solicita un ajuste para que el proveedor prepare una versión actualizada.');
  expect(await notice.textContent()).not.toMatch(/\d/);
  const adjust = dialog(page).getByRole('button', {name:'Solicitar ajuste', exact:true});
  await expect(adjust).toHaveClass(/primary/);
  await expect(adjust).toHaveAccessibleDescription('Pide al proveedor una nueva versión con las unidades que puede confirmar.');
  await expect(dialog(page).getByRole('button', {name:'Confirmar cotización', exact:true})).toHaveClass(/soft/);
  await expect(dialog(page).getByText('HANDSHAKED · Acuerdo confirmado', {exact:true})).toHaveCount(0);
  expect(state.getOrder().status).toBe('quoted');
  await adjust.click();
  await dialog(page).getByLabel('¿Qué necesitas ajustar?', {exact:true}).fill('confirmar disponibles');
  await dialog(page).getByRole('button', {name:'Enviar ajuste', exact:true}).click();
  await expect(dialog(page).getByText('Ajuste solicitado', {exact:true})).toBeVisible();
  expect(state.actions.map(value => value.action)).toEqual(['quote', 'accept', 'request_adjustment']);
  // The supplier's list flags the order privately; the client's own list never carries the badge.
  await dialog(page).getByRole('button', {name:'Cerrar ventana', exact:true}).click();
  await expect(page.getByText('Confirmación bloqueada por existencias', {exact:true})).toHaveCount(0);
  await page.getByLabel('Cuenta activa', {exact:true}).selectOption(supplierId);
  await page.goto('/');
  await page.getByRole('button', {name:'Para proveedores', exact:true}).click();
  await openSupplierNavigation(page);
  await page.getByRole('tab', {name:'Solicitudes', exact:true}).click();
  // The supplier's request list is a grid: the order's row carries the availability warning.
  await expect(page.getByRole('region', {name:'Listado de solicitudes', exact:true}).getByRole('row').filter({hasText:'ORD-PRUEBA-001'}).filter({hasText:'TALLER CENTRAL'})).toContainText('Confirmación bloqueada por existencias');
  expect(state.unexpected).toEqual([]);
});

test('an accept refused because the deal moved on does not keep pointing the client to Solicitar ajuste', async ({page}) => {
  const state = await fixture(page);
  await page.goto('/'); await asSupplier(page); await quoteTab(page);
  await editQuote(page, 'unit_price', '10.00');
  await dialog(page).getByRole('button', {name:'Confirmar y enviar cotización', exact:true}).click();
  await expect(dialog(page).getByRole('region', {name:'Cotización vigente v1', exact:true})).toBeVisible();
  state.blockAccept(true);
  await asClient(page); await quoteTab(page);
  await dialog(page).getByRole('button', {name:'Confirmar cotización', exact:true}).click();
  await dialog(page).getByRole('button', {name:'Sí, confirmar acuerdo', exact:true}).click();
  const hint = dialog(page).getByText('Pide al proveedor una nueva versión con las unidades que puede confirmar.', {exact:true});
  await expect(hint).toBeVisible();
  // Another member asked for an adjustment and the supplier returned the same quotation: still quoted, on a newer version.
  state.setStatus('adjustment'); state.setStatus('quoted'); state.blockAccept(false);
  await dialog(page).getByRole('button', {name:'Confirmar cotización', exact:true}).click();
  await dialog(page).getByRole('button', {name:'Sí, confirmar acuerdo', exact:true}).click();
  await expect(dialog(page).getByRole('alert')).toContainText('El acuerdo cambió. Actualízalo.');
  await expect(hint).toHaveCount(0);
  await expect(dialog(page).getByRole('button', {name:'Solicitar ajuste', exact:true})).toHaveClass(/soft/);
  await expect(dialog(page).getByRole('button', {name:'Confirmar cotización', exact:true})).toHaveClass(/primary/);
  expect(state.actions.map(value => value.action)).toEqual(['quote', 'accept', 'accept']);
  expect(state.unexpected).toEqual([]);
});
