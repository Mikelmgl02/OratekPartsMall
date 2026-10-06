import { expect, Page, test } from '@playwright/test';
import type { Deal, Quotation, QuotePayload } from '../lib/deal-types';
import type { QuoteDraft } from '../lib/pricing-types';
import type { SupplierRequestLine } from '../lib/request-types';
import { applySave, mockQuoteDrafts, publishBlockers, virtualDraft } from './quote-draft-mock';

const clientId = '11111111-1111-4111-8111-111111111111';
const supplierId = '22222222-2222-4222-8222-222222222222';
const orderId = '33333333-3333-4333-8333-333333333333';
const lineId = '44444444-4444-4444-8444-444444444444';
const secondId = '44444444-4444-4444-8444-444444444445';
const stamp = '2026-10-03T12:00:00Z';
const empty = { count: 0, results: [], next: null, previous: null };
const orderUrl = `/proveedor/${supplierId}/ordenes/${orderId}`;
const any = expect.any(String);
const content = (page: Page) => page.locator('.deal-order-content');
// The wrapping label also names the textarea with its current value, so match it as a prefix.
const termsField = (page: Page) => content(page).getByLabel('Condiciones de entrega y cotización');
const draftStatus = (page: Page) => page.getByRole('status', { name: 'Estado del borrador', exact: true });
const quoteCell = (page: Page, column: string, id = lineId) => content(page).locator(`.quotation-grid .ag-row[row-id="${id}"] .ag-cell[col-id="${column}"]`);

function line(id: string, codigo: string, quantity: number, stock: SupplierRequestLine['stock']): SupplierRequestLine {
  return { id, part_id: '66666666-6666-4666-8666-666666666666', supplier_item_id: '77777777-7777-4777-8777-777777777777', supplier_invent_id: `ID-${codigo}`,
    sku: '58411-1R000', name: 'TAMBOR', codigo, brand: 'KSM', description: `REPUESTO ${codigo}`, quantity, stock };
}
function quotation(revision: number, lines: { order_line_id: string; quantity: number; unit_price: string }[], currency: 'USD' | 'PAB', terms: string): Quotation {
  const values = lines.map(value => ({ ...value, codigo: value.order_line_id === lineId ? '58411-1R000-G' : '00700-NP', sku: '58411-1R000', description: 'REPUESTO',
    total: (value.quantity * Number(value.unit_price)).toFixed(2) }));
  return { id: `88888888-8888-4888-8888-${String(revision).padStart(12, '0')}`, revision, currency, terms, lines: values, created_at: stamp,
    supplier_confirmed_at: stamp, client_confirmed_at: null, total: values.reduce((sum, value) => sum + Number(value.total), 0).toFixed(2) };
}

// A blank price counts as 0.00 only on a line offered with 0 units, exactly like the server binding.
function matchesDraft(payload: QuotePayload, draft: QuoteDraft) {
  const cents = (value: string) => Math.round(Number(value) * 100);
  return payload.currency === draft.currency && payload.terms.trim().toUpperCase() === draft.terms && payload.lines.length === draft.lines.length
    && draft.lines.every(line => payload.lines.some(sent => sent.order_line_id === line.order_line_id && sent.quantity === line.quantity
      && (line.unit_price === null ? line.quantity === 0 && cents(sent.unit_price) === 0 : cents(sent.unit_price) === cents(line.unit_price))));
}

async function fixture(page: Page, status: 'reviewed' | 'adjustment' = 'reviewed', { exceptions = false, permissions = undefined as QuoteDraft['permissions'] | undefined } = {}) {
  const previous = status === 'adjustment' ? quotation(1, [{ order_line_id: lineId, quantity: 5, unit_price: '9.00' }, { order_line_id: secondId, quantity: 1, unit_price: '2.00' }], 'USD', 'RETIRO') : null;
  let order: Deal = { id: orderId, reference: 'ORD-BORRADOR-001', client: { id: clientId, name: 'TALLER CENTRAL' }, supplier: { id: supplierId, name: 'REPUESTOS CENTRAL' },
    status, version: 3, created_at: stamp, reviewed_at: stamp, handshaked_at: null, line_count: 2, unit_count: 6, notes: '',
    quotation: previous, quotations: previous ? [previous] : [], events: [], lines: [
      line(lineId, '58411-1R000-G', 5, { reported_quantity: 4, reserved_quantity: 1, available_quantity: 3, shortfall: 2, updated_at: stamp }),
      line(secondId, '00700-NP', 1, null)] };
  const actions: Record<string, unknown>[] = [];
  const unexpected: string[] = [];
  await page.route('**/api/**', route => { unexpected.push(`${route.request().method()} ${new URL(route.request().url()).pathname}`); return route.fulfill({ status: 404, json: { detail: 'Ruta no prevista' } }); });
  await page.route('**/api/session', route => route.fulfill({ json: { authenticated: true, user: { id: 801, username: 'EMPLEADO', is_superuser: false },
    accounts: { ...empty, count: 1, results: [{ id: supplierId, name: 'REPUESTOS CENTRAL', roles: ['supplier_retail'], capabilities: ['supplier'], permission: 'staff' }] } } }));
  await page.route('**/api/market/analytics/events', route => route.fulfill({ status: 202, json: { recorded: true } }));
  await page.route(`/api/market/accounts/${supplierId}/requests/${orderId}`, route => route.fulfill({ json: order }));
  await page.route(`/api/market/accounts/${supplierId}/requests/${orderId}/review`, route => route.fulfill({ json: order }));
  await page.route(/\/api\/market\/accounts\/[^/]+\/deals\/[^/]+\/messages(?:\?.*)?$/, route => route.fulfill({ json: { results: [], cursor: 0, has_more: false, has_earlier: false } }));
  const drafts = await mockQuoteDrafts(page, () => order, { exceptions, permissions });
  await page.route(`/api/market/accounts/${supplierId}/deals/${orderId}/actions`, route => {
    const payload = route.request().postDataJSON(); actions.push(payload);
    if (payload.action === 'quote') {
      const draft = drafts.current();
      // Same binding as the server (QUOTES_REQUIRE_DRAFT): every quote names the saved draft_version and matches its values.
      if (payload.draft_version === undefined) return route.fulfill({ status: 409, json: { detail: 'Prepara la cotización en el borrador y envía la versión guardada.' } });
      if (payload.draft_version !== draft.draft_version) return route.fulfill({ status: 409, json: { detail: 'El borrador cambió. Revísalo antes de publicar.' } });
      if (!matchesDraft(payload, draft)) return route.fulfill({ status: 409, json: { detail: 'La cotización no coincide con el borrador guardado.' } });
      const blockers = exceptions ? publishBlockers(draft) : [];
      if (blockers.length) return route.fulfill({ status: 409, json: { detail: 'Revisa las alertas antes de enviar.', exceptions: blockers } });
      const quote = quotation(order.quotations.length + 1, payload.lines, payload.currency, payload.terms);
      order = { ...order, status: 'quoted', version: order.version + 1, quotation: quote, quotations: [...order.quotations, quote] };
      drafts.clear();
    }
    return route.fulfill({ json: order });
  });
  return { actions, drafts, unexpected, getOrder: () => order };
}
async function openQuote(page: Page) {
  await page.goto(orderUrl);
  await content(page).getByRole('button', { name: 'Cotización', exact: true }).click();
}
async function editQuote(page: Page, column: 'unit_price' | 'quantity', value: string, codigo = '58411-1R000-G', id = lineId) {
  await quoteCell(page, column, id).click();
  const input = content(page).getByLabel(`${column === 'unit_price' ? 'Precio' : 'Unidades ofrecidas'} de ${codigo}`, { exact: true });
  await input.fill(value); await input.press('Enter');
}

test('supplier edits survive a reload, autosave never calls deal actions and publishing sends the saved draft version', async ({ page }) => {
  const state = await fixture(page);
  await openQuote(page);
  await expect(draftStatus(page)).toHaveText('Borrador nuevo · se guarda automáticamente al editar');
  await expect(quoteCell(page, 'available')).toHaveText('3');
  await expect(quoteCell(page, 'available', secondId)).toHaveText('Revisar artículo');
  await editQuote(page, 'unit_price', '12,5');
  await expect(draftStatus(page)).toContainText('Borrador v1 · guardado por EMPLEADO');
  expect(state.drafts.saves).toEqual([{ save_id: any, expected_draft_version: 0, lines: [{ order_line_id: lineId, unit_price: '12.50' }] }]);
  await editQuote(page, 'quantity', '3');
  await editQuote(page, 'unit_price', '4', '00700-NP', secondId);
  await termsField(page).fill('retiro en sucursal');
  await expect.poll(() => state.drafts.current().terms).toBe('RETIRO EN SUCURSAL');
  await expect(draftStatus(page)).toContainText('guardado por EMPLEADO');
  // Partial saves: later payloads never resend the price that was already saved.
  expect(state.drafts.saves.slice(1).every(save => !JSON.stringify(save).includes('12.50'))).toBe(true);
  expect(state.drafts.current().lines.map(value => [value.quantity, value.unit_price])).toEqual([[3, '12.50'], [1, '4.00']]);
  expect(state.actions).toEqual([]);
  await page.reload();
  await expect(content(page).locator('.deal-tabs .deal-tab-dot')).toBeVisible();
  await content(page).getByRole('button', { name: 'Cotización', exact: true }).click();
  await expect(quoteCell(page, 'unit_price')).toContainText('12.50');
  await expect(quoteCell(page, 'quantity')).toHaveText('3');
  await expect(quoteCell(page, 'unit_price', secondId)).toContainText('4.00');
  await expect(termsField(page)).toHaveValue('RETIRO EN SUCURSAL');
  await expect(draftStatus(page)).toContainText(`Borrador v${state.drafts.current().draft_version} · guardado por EMPLEADO`);
  const version = state.drafts.current().draft_version;
  await content(page).getByRole('button', { name: 'Confirmar y enviar cotización', exact: true }).click();
  await expect(content(page).getByRole('region', { name: 'Cotización vigente v1', exact: true })).toContainText('41.50');
  expect(state.actions).toEqual([{ action: 'quote', expected_version: 3, operation_id: any, draft_version: version, currency: 'USD', terms: 'RETIRO EN SUCURSAL',
    lines: [{ order_line_id: lineId, quantity: 3, unit_price: '12.50' }, { order_line_id: secondId, quantity: 1, unit_price: '4.00' }] }]);
  expect(state.unexpected).toEqual([]);
});

test('another member\'s save is rebased under untouched cells and a real conflict shows the banner', async ({ page }) => {
  const state = await fixture(page);
  state.drafts.set(applySave(virtualDraft(state.getOrder()), { lines: [{ order_line_id: lineId, unit_price: '10.00' }, { order_line_id: secondId, unit_price: '2.00' }] }, 'OTRO MIEMBRO'));
  await openQuote(page);
  await expect(draftStatus(page)).toContainText('Borrador v1 · guardado por OTRO MIEMBRO');
  await expect(content(page).locator('.deal-tabs .deal-tab-dot')).toBeVisible();
  await expect(quoteCell(page, 'unit_price')).toContainText('10.00');
  state.drafts.set(applySave(state.drafts.current(), { lines: [{ order_line_id: lineId, quantity: 2 }] }, 'OTRO MIEMBRO'));
  await editQuote(page, 'unit_price', '11.00');
  await expect(draftStatus(page)).toContainText('Borrador v3 · guardado por EMPLEADO');
  await expect(quoteCell(page, 'quantity')).toHaveText('2');
  await expect(quoteCell(page, 'unit_price')).toContainText('11.00');
  expect(state.drafts.saves).toEqual([{ save_id: any, expected_draft_version: 2, lines: [{ order_line_id: lineId, unit_price: '11.00' }] }]);
  state.drafts.set(applySave(state.drafts.current(), { lines: [{ order_line_id: lineId, unit_price: '12.00' }] }, 'OTRO MIEMBRO'));
  await editQuote(page, 'unit_price', '13.00');
  const banner = content(page).getByRole('alert');
  await expect(banner).toContainText('Otro miembro de tu equipo modificó este borrador');
  await expect(draftStatus(page)).toHaveText('Guardado automático en pausa');
  await expect(quoteCell(page, 'unit_price')).toContainText('13.00');
  await banner.getByRole('button', { name: 'Cargar la versión más reciente', exact: true }).click();
  await expect(quoteCell(page, 'unit_price')).toContainText('12.00');
  await expect(content(page).getByRole('alert')).toHaveCount(0);
  await expect(draftStatus(page)).toContainText('Borrador v4 · guardado por OTRO MIEMBRO');
  expect(state.drafts.saves).toHaveLength(1);
  expect(state.actions).toEqual([]);
  expect(state.unexpected).toEqual([]);
});

test('an adjustment draft starts from the previous revision, discarding restores it and a virtual draft is saved before publishing', async ({ page }) => {
  const state = await fixture(page, 'adjustment');
  await openQuote(page);
  await expect(quoteCell(page, 'unit_price')).toContainText('9.00');
  await expect(quoteCell(page, 'quantity')).toHaveText('5');
  await expect(termsField(page)).toHaveValue('RETIRO');
  await editQuote(page, 'unit_price', '8.00');
  await expect(draftStatus(page)).toContainText('Borrador v1 · guardado por EMPLEADO');
  const warning = content(page).getByText('Si devuelves la misma cotización, se descartará el borrador en curso.', { exact: true });
  await expect(warning).toBeVisible();
  await content(page).getByRole('button', { name: 'Descartar borrador', exact: true }).click();
  await content(page).getByRole('button', { name: 'Sí, descartar', exact: true }).click();
  await expect(draftStatus(page)).toHaveText('Borrador nuevo · se guarda automáticamente al editar');
  await expect(quoteCell(page, 'unit_price')).toContainText('9.00');
  await expect(warning).toHaveCount(0);
  expect(state.drafts.discards).toEqual([{ save_id: any, expected_draft_version: 1 }]);
  await content(page).getByRole('button', { name: 'Confirmar y enviar cotización', exact: true }).click();
  await expect(content(page).getByRole('region', { name: 'Cotización vigente v2', exact: true })).toContainText('47.00');
  expect(state.drafts.saves.at(-1)).toEqual({ save_id: any, expected_draft_version: 0 });
  expect(state.actions).toEqual([expect.objectContaining({ action: 'quote', draft_version: 1, terms: 'RETIRO',
    lines: [{ order_line_id: lineId, quantity: 5, unit_price: '9.00' }, { order_line_id: secondId, quantity: 1, unit_price: '2.00' }] })]);
  expect(state.unexpected).toEqual([]);
});

test('edits made while publishing flushes the draft are saved first and published with it', async ({ page }) => {
  const state = await fixture(page);
  await openQuote(page);
  await editQuote(page, 'unit_price', '12.50');
  await editQuote(page, 'unit_price', '4', '00700-NP', secondId);
  await expect.poll(() => state.drafts.current().lines.map(value => value.unit_price)).toEqual(['12.50', '4.00']);
  await expect(draftStatus(page)).toContainText('guardado por EMPLEADO');
  const version = state.drafts.current().draft_version, release = state.drafts.hold();
  await editQuote(page, 'quantity', '3');
  await content(page).getByRole('button', { name: 'Confirmar y enviar cotización', exact: true }).click();
  await expect.poll(() => state.drafts.held()).toBe(1);
  await termsField(page).fill('retiro hoy');
  release();
  await expect(content(page).getByRole('region', { name: 'Cotización vigente v1', exact: true })).toContainText('41.50');
  expect(state.drafts.saves.slice(-2).map(({ save_id: _, ...save }) => save)).toEqual([
    { expected_draft_version: version, lines: [{ order_line_id: lineId, quantity: 3 }] }, { expected_draft_version: version + 1, terms: 'RETIRO HOY' }]);
  expect(state.actions).toEqual([expect.objectContaining({ action: 'quote', draft_version: version + 2, terms: 'RETIRO HOY',
    lines: [{ order_line_id: lineId, quantity: 3, unit_price: '12.50' }, { order_line_id: secondId, quantity: 1, unit_price: '4.00' }] })]);
  expect(state.unexpected).toEqual([]);
});

test('leaving the order while a save is in flight still saves the edits made after it', async ({ page }) => {
  const state = await fixture(page);
  await openQuote(page);
  const release = state.drafts.hold();
  await editQuote(page, 'unit_price', '12.50');
  await expect.poll(() => state.drafts.held()).toBe(1);
  await editQuote(page, 'unit_price', '4', '00700-NP', secondId);
  await page.getByRole('link', { name: 'Volver a solicitudes', exact: true }).click();
  await expect(page).not.toHaveURL(new RegExp(orderId));
  release();
  await expect.poll(() => state.drafts.current().lines.map(value => value.unit_price)).toEqual(['12.50', '4.00']);
  expect(state.drafts.saves.map(({ save_id: _, ...save }) => save)).toEqual([
    { expected_draft_version: 0, lines: [{ order_line_id: lineId, unit_price: '12.50' }] },
    { expected_draft_version: 1, lines: [{ order_line_id: secondId, unit_price: '4.00' }] }]);
  expect(state.actions).toEqual([]);
});

test('when the draft cannot be loaded the editor is read-only and Reintentar loads the draft before anything can be sent', async ({ page }) => {
  const state = await fixture(page);
  let failing = true;
  await page.route(/\/api\/market\/accounts\/[^/]+\/requests\/[^/]+\/draft$/, route => failing ? route.fulfill({ status: 500, json: { detail: 'Error interno.' } }) : route.fallback());
  await openQuote(page);
  const notice = content(page).getByRole('alert').filter({ hasText: 'No se pudo cargar el borrador de la cotización.' });
  await expect(notice).toContainText('Las cotizaciones se preparan y se envían desde el borrador guardado.');
  await expect(draftStatus(page)).toHaveCount(0);
  await expect(quoteCell(page, 'available')).toHaveText('3');
  // Quotations are published only from the saved draft (QUOTES_REQUIRE_DRAFT): nothing here can be edited or sent.
  await expect(content(page).getByRole('button', { name: 'Confirmar y enviar cotización', exact: true })).toHaveCount(0);
  await expect(termsField(page)).toBeDisabled();
  await quoteCell(page, 'unit_price').click();
  await expect(content(page).getByLabel('Precio de 58411-1R000-G', { exact: true })).toHaveCount(0);
  await notice.getByRole('button', { name: 'Reintentar', exact: true }).click();
  await expect(notice).toContainText('Todavía no se pudo cargar. Inténtalo de nuevo en unos segundos.');
  failing = false;
  await notice.getByRole('button', { name: 'Reintentar', exact: true }).click();
  await expect(draftStatus(page)).toHaveText('Borrador nuevo · se guarda automáticamente al editar');
  await expect(notice).toHaveCount(0);
  await editQuote(page, 'unit_price', '12.50');
  await expect(draftStatus(page)).toContainText('Borrador v1 · guardado por EMPLEADO');
  expect(state.drafts.saves).toEqual([{ save_id: any, expected_draft_version: 0, lines: [{ order_line_id: lineId, unit_price: '12.50' }] }]);
  expect(state.actions).toEqual([]);
  expect(state.unexpected).toEqual([]);
});

test('alerts gate publishing: adjusting to available stock and confirming a changed item are saved to the draft first', async ({ page }) => {
  const state = await fixture(page, 'reviewed', { exceptions: true });
  await openQuote(page);
  const panel = content(page).locator('details.quotation-exceptions');
  await expect(panel.locator('summary')).toHaveText('Revisa antes de enviar (4)');
  // On load the review panel stays folded, so currency, terms and send stay close; a compact count beside the grid opens it.
  await expect(panel).not.toHaveAttribute('open', '');
  const chip = content(page).getByRole('button', { name: '4 por revisar', exact: true });
  await expect(chip).toHaveAttribute('aria-expanded', 'false');
  await chip.click();
  await expect(panel).toHaveAttribute('open', '');
  await expect(chip).toHaveAttribute('aria-expanded', 'true');
  await expect(quoteCell(page, 'alerts')).toHaveText('1 por corregir');
  await expect(panel.getByRole('listitem').filter({ hasText: 'Falta el precio.' })).toHaveCount(2);
  await panel.locator('summary').click();
  await expect(panel).not.toHaveAttribute('open', '');
  await editQuote(page, 'unit_price', '12.50');
  await editQuote(page, 'unit_price', '4', '00700-NP', secondId);
  await expect(quoteCell(page, 'alerts')).toHaveText('1 por confirmar');
  await expect(quoteCell(page, 'alerts', secondId)).toHaveText('1 por confirmar');
  await expect(panel.locator('summary')).toHaveText('Revisa antes de enviar (2)');
  await expect(content(page).getByRole('button', { name: '2 por revisar', exact: true })).toBeVisible();
  // The server refuses unreviewed alerts: the panel opens by itself with the error and nothing is published.
  await content(page).getByRole('button', { name: 'Confirmar y enviar cotización', exact: true }).click();
  await expect(content(page).getByRole('alert')).toHaveText('Revisa las alertas antes de enviar.');
  await expect(panel).toHaveAttribute('open', '');
  expect(state.actions).toEqual([expect.objectContaining({ action: 'quote', draft_version: state.drafts.current().draft_version })]);
  expect(state.getOrder().status).toBe('reviewed');
  const savesBefore = state.drafts.saves.length;
  await content(page).getByRole('button', { name: 'Ajustar a disponibles', exact: true }).click();
  await expect(quoteCell(page, 'quantity')).toHaveText('3');
  await expect(quoteCell(page, 'alerts')).toHaveText('Sin alertas');
  await expect(content(page).getByRole('button', { name: 'Ajustar a disponibles', exact: true })).toHaveCount(0);
  expect(state.drafts.saves.slice(savesBefore).map(({ save_id: _, ...save }) => save)).toEqual([
    { expected_draft_version: state.drafts.current().draft_version - 1, lines: [{ order_line_id: lineId, quantity: 3 }] }]);
  const confirm = panel.getByRole('checkbox', { name: 'Confirmo: 00700-NP · El artículo de tu inventario cambió desde la solicitud; confirma que ofreces el mismo repuesto.', exact: true });
  await confirm.check();
  await expect(quoteCell(page, 'alerts', secondId)).toHaveText('Confirmada');
  await expect(panel.locator('summary')).toHaveText('Revisa antes de enviar (0)');
  expect(state.drafts.saves.at(-1)).toEqual({ save_id: any, expected_draft_version: state.drafts.current().draft_version - 1,
    lines: [{ order_line_id: secondId, acknowledge: [{ code: 'identity_changed', context: 'a1b2c3d4e5f6' }] }] });
  // Withdrawing the confirmation is saved too, and the alert is pending again until confirmed.
  await confirm.uncheck();
  await expect(quoteCell(page, 'alerts', secondId)).toHaveText('1 por confirmar');
  expect(state.drafts.saves.at(-1)).toMatchObject({ lines: [{ order_line_id: secondId, revoke: ['identity_changed'] }] });
  await confirm.check();
  await expect(quoteCell(page, 'alerts', secondId)).toHaveText('Confirmada');
  await content(page).getByRole('button', { name: 'Confirmar y enviar cotización', exact: true }).click();
  await expect(content(page).getByRole('region', { name: 'Cotización vigente v1', exact: true })).toContainText('41.50');
  expect(state.actions).toHaveLength(2);
  expect(state.actions[1]).toMatchObject({ action: 'quote', lines: [{ order_line_id: lineId, quantity: 3, unit_price: '12.50' }, { order_line_id: secondId, quantity: 1, unit_price: '4.00' }] });
  expect(state.unexpected).toEqual([]);
});

test('the private trace of the current quotation loads only when the supplier opens it, and again on reopening', async ({ page }) => {
  const state = await fixture(page, 'adjustment');
  const quote = state.getOrder().quotation!, traces: string[] = [];
  await page.route(/\/api\/market\/accounts\/[^/]+\/requests\/[^/]+\/quotations\/[^/]+\/trace$/, route => {
    traces.push(new URL(route.request().url()).pathname);
    return route.fulfill({ json: { available: true, quotation_id: quote.id, revision: 1, draft_version: 3, publisher: { name: 'EMPLEADO' }, publisher_permission: 'staff',
      published_at: stamp, settings_snapshot: {}, order_exceptions: [], accept_check: { result: 'blocked', at: stamp, lines: [] }, lines: [
        { order_line_id: lineId, codigo: '58411-1R000-G', brand: 'KSM', description: 'REPUESTO 58411-1R000-G', quantity: 5, unit_price: '9.00', available_at_quote: 3,
          identity_ok_at_quote: true, available_at_accept: 2, suggested_price: null, price_source: 'manual', quantity_source: 'requested', engine_fingerprint: '', explanation: {},
          exceptions: [{ code: 'offered_gt_available', severity: 'confirm', context: '5>3', acknowledged_by: { name: 'EMPLEADO' }, acknowledged_at: stamp }] },
        { order_line_id: secondId, codigo: '00700-NP', brand: 'KSM', description: 'REPUESTO 00700-NP', quantity: 1, unit_price: '2.00', available_at_quote: null,
          identity_ok_at_quote: false, available_at_accept: null, suggested_price: null, price_source: 'previous', quantity_source: 'requested', engine_fingerprint: '', explanation: {},
          exceptions: [{ code: 'identity_changed', severity: 'confirm', context: 'a1b2c3d4e5f6', acknowledged_by: null, acknowledged_at: null }] }] } });
  });
  await openQuote(page);
  const trace = content(page).locator('details.deal-quote-trace');
  await expect(trace.locator('summary')).toHaveText('Ver origen de precios');
  expect(traces).toEqual([]);
  await trace.locator('summary').click();
  await expect(trace).toContainText('Publicada por EMPLEADO · borrador v3');
  await expect(trace).toContainText('Confirmación bloqueada por existencias');
  const rows = trace.getByRole('row');
  await expect(rows.nth(1)).toContainText('Más que las disponibles (5>3) · confirmada por EMPLEADO');
  await expect(rows.nth(1).getByRole('cell').nth(2)).toHaveText('3');
  await expect(rows.nth(1).getByRole('cell').nth(3)).toHaveText('2');
  await expect(rows.nth(2).getByRole('cell').nth(2)).toHaveText('Artículo cambiado');
  await expect(rows.nth(2)).toContainText('Versión anterior');
  await expect(rows.nth(2)).toContainText('Artículo cambiado (a1b2c3d4e5f6) · sin confirmar');
  // Reopening reads it again, so an accept check recorded meanwhile shows up.
  await trace.locator('summary').click();
  await trace.locator('summary').click();
  await expect.poll(() => traces.length).toBe(2);
  expect(new Set(traces)).toEqual(new Set([`/api/market/accounts/${supplierId}/requests/${orderId}/quotations/${quote.id}/trace`]));
  expect(state.unexpected).toEqual([]);
});

test('a member below the publish permission requests approval instead of sending, and editing or withdrawing clears the request', async ({ page }) => {
  const state = await fixture(page, 'adjustment', { permissions: { can_publish: false, publish_requires: 'manager' } });
  await openQuote(page);
  const send = content(page).getByRole('button', { name: 'Confirmar y enviar cotización', exact: true });
  const ask = content(page).getByRole('button', { name: 'Solicitar aprobación', exact: true });
  await expect(send).toHaveCount(0);
  await expect(ask).toBeEnabled();
  await expect(content(page).getByText('Un administrador de tu cuenta debe publicar esta cotización.', { exact: true })).toBeVisible();
  // Returning the same quotation republishes it, so it is not offered either.
  await expect(content(page).getByRole('button', { name: 'Devolver la misma cotización para confirmar', exact: true })).toBeDisabled();
  await expect(content(page).getByText('Solo un miembro con permiso para enviar cotizaciones puede devolverla.', { exact: true })).toBeVisible();
  await editQuote(page, 'unit_price', '8.00');
  await ask.click();
  const notice = content(page).getByRole('status', { name: 'Solicitud de aprobación', exact: true });
  await expect(notice).toContainText('Aprobación solicitada por EMPLEADO hace un momento. Un administrador de tu cuenta debe publicar esta cotización.');
  await expect(content(page).getByRole('button', { name: 'Aprobación solicitada', exact: true })).toBeDisabled();
  // The edit is flushed first, then the request is saved on the version that flush confirmed.
  expect(state.drafts.saves.map(({ save_id: _, ...save }) => save)).toEqual([
    { expected_draft_version: 0, lines: [{ order_line_id: lineId, unit_price: '8.00' }] }, { expected_draft_version: 1, request_review: true }]);
  expect(state.drafts.current()).toMatchObject({ status: 'review_requested', review_requested_by: { name: 'EMPLEADO' } });
  // Changing a price withdraws the request on the server; the member asks again.
  await editQuote(page, 'unit_price', '7.50');
  await expect(notice).toHaveCount(0);
  await expect(ask).toBeEnabled();
  expect(state.drafts.current().status).toBe('editing');
  await ask.click();
  await expect(notice).toBeVisible();
  await notice.getByRole('button', { name: 'Retirar solicitud', exact: true }).click();
  await expect(notice).toHaveCount(0);
  expect(state.drafts.saves.at(-1)).toEqual({ save_id: any, expected_draft_version: state.drafts.current().draft_version - 1, request_review: false });
  expect(state.drafts.current().status).toBe('editing');
  expect(state.actions).toEqual([]);
  expect(state.unexpected).toEqual([]);
});

test('a member allowed to publish sees who requested approval and sends exactly the reviewed draft', async ({ page }) => {
  const state = await fixture(page);
  state.drafts.set({ ...applySave(virtualDraft(state.getOrder()), { lines: [{ order_line_id: lineId, quantity: 3, unit_price: '12.50' }, { order_line_id: secondId, unit_price: '4.00' }],
    request_review: true }, 'VENDEDOR JUNIOR'), review_requested_at: new Date(Date.now() - 5 * 60000).toISOString() });
  await openQuote(page);
  const notice = content(page).getByRole('status', { name: 'Solicitud de aprobación', exact: true });
  await expect(notice).toHaveText('Por aprobar · VENDEDOR JUNIOR solicitó aprobación hace 5 min. Revisa la cotización y envíala al cliente.');
  await expect(notice.getByRole('button')).toHaveCount(0);
  await expect(content(page).getByRole('button', { name: 'Solicitar aprobación', exact: true })).toHaveCount(0);
  await content(page).getByRole('button', { name: 'Confirmar y enviar cotización', exact: true }).click();
  await expect(content(page).getByRole('region', { name: 'Cotización vigente v1', exact: true })).toContainText('41.50');
  expect(state.drafts.saves).toEqual([]);
  expect(state.actions).toEqual([expect.objectContaining({ action: 'quote', draft_version: 1,
    lines: [{ order_line_id: lineId, quantity: 3, unit_price: '12.50' }, { order_line_id: secondId, quantity: 1, unit_price: '4.00' }] })]);
  expect(state.unexpected).toEqual([]);
});

test('the alert chip reads "Alertas revisadas" only once confirmations exist and are reviewed; informative notices are counted', async ({ page }) => {
  const state = await fixture(page, 'reviewed', { exceptions: true });
  await openQuote(page);
  await editQuote(page, 'quantity', '3');
  await editQuote(page, 'unit_price', '12.50');
  await editQuote(page, 'quantity', '0', '00700-NP', secondId);
  await expect(quoteCell(page, 'alerts', secondId)).toHaveText('No ofreces este artículo.');
  const chip = content(page).locator('.quotation-alert-chip');
  await expect(chip).toHaveText('1 aviso');
  await expect(chip).toHaveClass(/\binfo\b/);
  await editQuote(page, 'quantity', '1', '00700-NP', secondId);
  await editQuote(page, 'unit_price', '4', '00700-NP', secondId);
  await expect(chip).toHaveText('1 por revisar');
  await chip.click();
  await content(page).locator('details.quotation-exceptions').getByRole('checkbox', { name: /^Confirmo: 00700-NP/ }).check();
  await expect(chip).toHaveText('Alertas revisadas');
  await expect(chip).not.toHaveClass(/\b(?:info|confirm|block)\b/);
  expect(state.actions).toEqual([]);
  expect(state.unexpected).toEqual([]);
});
