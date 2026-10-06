import { expect, Page, test } from '@playwright/test';
import type { Deal, DealMessage } from '../lib/deal-types';
import type { AssistantRun, AssistantState, QuoteDraft, QuoteDraftSave } from '../lib/pricing-types';
import type { SupplierRequestLine } from '../lib/request-types';
import { mockQuoteDrafts } from './quote-draft-mock';

const clientId = '11111111-1111-4111-8111-111111111111';
const supplierId = '22222222-2222-4222-8222-222222222222';
const orderId = '33333333-3333-4333-8333-333333333333';
const lineId = '44444444-4444-4444-8444-444444444444';
const secondId = '44444444-4444-4444-8444-444444444445';
const runId = '99999999-9999-4999-8999-999999999999';
const stamp = '2026-10-06T12:00:00Z';
const empty = { count: 0, results: [], next: null, previous: null };
const content = (page: Page) => page.locator('.deal-order-content');
const panel = (page: Page) => content(page).getByRole('region', { name: 'Asistente de cotización (IA)', exact: true });
const quoteCell = (page: Page, column: string, id = lineId) => content(page).locator(`.quotation-grid .ag-row[row-id="${id}"] .ag-cell[col-id="${column}"]`);
const any = expect.any(String);
const question = '¿CONFIRMAS QUE ACEPTAS MARCA ALTERNA EN LA ZAPATA?';

function line(id: string, codigo: string, quantity: number): SupplierRequestLine {
  return { id, part_id: '66666666-6666-4666-8666-666666666666', supplier_item_id: '77777777-7777-4777-8777-777777777777', supplier_invent_id: `ID-${codigo}`,
    sku: '58411-1R000', name: 'TAMBOR', codigo, brand: 'KSM', description: `REPUESTO ${codigo}`, quantity,
    stock: { reported_quantity: 9, reserved_quantity: 0, available_quantity: 9, shortfall: 0, updated_at: stamp } };
}
// What the server returns after an interpretation: proposals quote the client's words and none carries a price.
function interpreted(): AssistantRun {
  return { id: runId, status: 'completed', cached: false, stale: false, draft_version: 0, created_at: stamp, finished_at: stamp, created_by: { name: 'EMPLEADO' },
    summary: 'EL CLIENTE NECESITA MENOS TAMBORES Y PIDE UN DESCUENTO.', rejected_items: 0, proposals: [
      { id: 'p1', kind: 'quantity_change', detail: '', order_line_id: lineId, codigo: '58411-1R000-G', quantity: 2, text: '', evidence: 'SOLO NECESITO 2 DEL TAMBOR', decision: null, can_apply: true },
      { id: 'p2', kind: 'price_request', detail: 'descuento', order_line_id: lineId, codigo: '58411-1R000-G', quantity: null, text: '', evidence: 'HAZME UN DESCUENTO', decision: null, can_apply: false },
      { id: 'p3', kind: 'question', detail: '', order_line_id: null, codigo: '', quantity: null, text: question, evidence: '', decision: null, can_apply: false }] };
}

async function fixture(page: Page, initial: AssistantState | undefined, { notice = true } = {}) {
  const order: Deal = { id: orderId, reference: 'ORD-ASISTENTE-001', client: { id: clientId, name: 'TALLER CENTRAL' }, supplier: { id: supplierId, name: 'REPUESTOS CENTRAL' },
    status: 'reviewed', version: 3, created_at: stamp, reviewed_at: stamp, handshaked_at: null, line_count: 2, unit_count: 5,
    notes: 'SOLO NECESITO 2 DEL TAMBOR. HAZME UN DESCUENTO.', quotation: null, quotations: [], events: [],
    lines: [line(lineId, '58411-1R000-G', 3), line(secondId, '00700-NP', 2)] };
  let assistant = initial;
  const unexpected: string[] = [], actions: unknown[] = [], runs: unknown[] = [], decisions: Record<string, unknown>[] = [], posted: unknown[] = [];
  await page.route('**/api/**', route => { unexpected.push(`${route.request().method()} ${new URL(route.request().url()).pathname}`); return route.fulfill({ status: 404, json: { detail: 'Ruta no prevista' } }); });
  await page.route('**/api/session', route => route.fulfill({ json: { authenticated: true, user: { id: 801, username: 'EMPLEADO', is_superuser: false },
    accounts: { ...empty, count: 1, results: [{ id: supplierId, name: 'REPUESTOS CENTRAL', roles: ['supplier_retail'], capabilities: ['supplier'], permission: 'staff' }] } } }));
  await page.route('**/api/market/analytics/events', route => route.fulfill({ status: 202, json: { recorded: true } }));
  await page.route(`/api/market/accounts/${supplierId}/requests/${orderId}`, route => route.fulfill({ json: order }));
  await page.route(`/api/market/accounts/${supplierId}/requests/${orderId}/review`, route => route.fulfill({ json: order }));
  await page.route(/\/api\/market\/accounts\/[^/]+\/deals\/[^/]+\/messages(?:\?.*)?$/, route => {
    if (route.request().method() === 'POST') { posted.push(route.request().postDataJSON()); return route.fulfill({ status: 500, json: { detail: 'No debía enviarse' } }); }
    return route.fulfill({ json: { results: [] as DealMessage[], cursor: 0, has_more: false, has_earlier: false, assistant_notice: notice } });
  });
  const drafts = await mockQuoteDrafts(page, () => order, assistant ? { assistant: () => assistant! } : {});
  await page.route(`/api/market/accounts/${supplierId}/requests/${orderId}/draft/assistant`, route => {
    runs.push(route.request().postDataJSON());
    assistant = { ...assistant!, runs_left: assistant!.runs_left - 1, latest_run: interpreted() };
    return route.fulfill({ json: assistant });
  });
  // Like the server: applying is a versioned draft save (quantity with origin assistant); dismissing records the decision only.
  await page.route(`/api/market/accounts/${supplierId}/requests/${orderId}/draft/assistant/${runId}/decisions`, route => {
    const payload = route.request().postDataJSON() as QuoteDraftSave & { decisions: { proposal_id: string; action: 'apply' | 'dismiss' }[] };
    decisions.push(payload);
    const draft = drafts.current(), { proposal_id: id, action } = payload.decisions[0];
    if (payload.expected_draft_version !== draft.draft_version) return route.fulfill({ status: 409, json: { detail: 'Otro miembro de tu equipo modificó este borrador.', draft } });
    const run = assistant!.latest_run!, proposal = run.proposals.find(item => item.id === id)!;
    assistant = { ...assistant!, latest_run: { ...run, proposals: run.proposals.map(item => item.id === id ? { ...item, decision: action === 'apply' ? 'applied' : 'dismissed' } : item) } };
    if (action === 'apply') {
      const next: QuoteDraft = { ...draft, persisted: true, draft_version: draft.draft_version + 1, updated_at: new Date().toISOString(), updated_by: { name: 'EMPLEADO' }, terms_origin: 'saved',
        lines: draft.lines.map(value => value.order_line_id === proposal.order_line_id ? { ...value, quantity: proposal.quantity, quantity_source: 'assistant' as const } : value) };
      drafts.set(next);
    }
    return route.fulfill({ json: drafts.current() });
  });
  await page.route(`/api/market/accounts/${supplierId}/deals/${orderId}/actions`, route => { actions.push(route.request().postDataJSON()); return route.fulfill({ status: 500, json: {} }); });
  return { unexpected, actions, runs, decisions, posted, drafts };
}

test('the supplier interprets the client\'s request, applies a quantity as a draft save and copies a question to the chat without sending it', async ({ page }) => {
  const state = await fixture(page, { configured: true, enabled: true, runs_left: 5, unavailable: '', latest_run: null });
  await page.goto(`/proveedor/${supplierId}/ordenes/${orderId}`);
  await content(page).getByRole('button', { name: 'Cotización', exact: true }).click();
  await expect(panel(page)).toContainText('El asistente no propone precios: los precios salen solo de tus listas y reglas.');
  await panel(page).getByRole('button', { name: 'Interpretar solicitud del cliente', exact: true }).click();
  await expect(panel(page)).toContainText('EL CLIENTE NECESITA MENOS TAMBORES Y PIDE UN DESCUENTO.');
  expect(state.runs).toEqual([{ run_id: any, expected_draft_version: 0 }]);
  const proposals = panel(page).getByRole('list', { name: 'Propuestas del asistente', exact: true });
  await expect(proposals).toContainText('58411-1R000-G: ofrecer 2 unidades«SOLO NECESITO 2 DEL TAMBOR»');
  // A price request is evidence only: it can be dismissed or taken to the simulator, never applied.
  const prices = panel(page).getByRole('list', { name: 'Solicitudes de precio del cliente', exact: true });
  await expect(prices).toContainText('El cliente pide un descuento (58411-1R000-G)«HAZME UN DESCUENTO»');
  await expect(prices.getByRole('button', { name: /^Aplicar/ })).toHaveCount(0);
  await expect(prices.getByRole('link', { name: 'Abrir simulador', exact: true })).toHaveAttribute('href', /pestana=simulador/);
  await expect(quoteCell(page, 'quantity')).toHaveText('3');
  expect(state.drafts.saves).toEqual([]);
  await proposals.getByRole('button', { name: 'Aplicar: 58411-1R000-G: ofrecer 2 unidades', exact: true }).click();
  await expect(proposals).toContainText('Aplicada');
  await expect(quoteCell(page, 'quantity')).toHaveText('2');
  await expect(page.getByRole('status', { name: 'Estado del borrador', exact: true })).toContainText('Borrador v1 · guardado por EMPLEADO');
  expect(state.decisions).toEqual([{ save_id: any, expected_draft_version: 0, decisions: [{ proposal_id: 'p1', action: 'apply' }] }]);
  expect(state.drafts.current().lines.find(value => value.order_line_id === lineId)?.quantity_source).toBe('assistant');
  await prices.getByRole('button', { name: 'Descartar: El cliente pide un descuento (58411-1R000-G)', exact: true }).click();
  await expect(prices).toContainText('Descartada');
  expect(state.decisions[1]).toEqual({ save_id: any, expected_draft_version: 1, decisions: [{ proposal_id: 'p2', action: 'dismiss' }] });
  // A suggested question lands in the chat composer; nothing is sent until the supplier sends it.
  await panel(page).getByRole('button', { name: `Copiar a la conversación: ${question}`, exact: true }).click();
  const chat = content(page).getByRole('region', { name: 'Conversación del acuerdo', exact: true });
  await expect(chat).toBeVisible();
  await expect(chat.getByLabel('Mensaje del acuerdo', { exact: true })).toHaveValue(question);
  await expect(chat.getByRole('status')).toHaveText('Pregunta del asistente copiada. Revísala y envíala cuando quieras.');
  await expect(chat.getByRole('note', { name: 'Aviso de asistente de IA', exact: true })).toContainText('Tu cuenta tiene activado el asistente de IA');
  expect(state.posted).toEqual([]);
  expect(state.actions).toEqual([]);
  expect(state.unexpected).toEqual([]);
});

for (const [label, assistant] of [['without the assistant in the draft', undefined], ['when the platform has not configured it', { configured: false, enabled: true }],
  ['when the owner has not enabled it', { configured: true, enabled: false }]] as const) {
  test(`the assistant panel is hidden ${label}`, async ({ page }) => {
    const state = await fixture(page, assistant && { ...assistant, runs_left: 0, unavailable: '', latest_run: null }, { notice: false });
    await page.goto(`/proveedor/${supplierId}/ordenes/${orderId}`);
    await content(page).getByRole('button', { name: 'Cotización', exact: true }).click();
    await expect(quoteCell(page, 'quantity')).toHaveText('3');
    await expect(content(page).getByText('Asistente de cotización (IA)', { exact: true })).toHaveCount(0);
    await content(page).getByRole('button', { name: 'Conversación', exact: true }).click();
    await expect(content(page).getByRole('note', { name: 'Aviso de asistente de IA', exact: true })).toHaveCount(0);
    expect(state.runs).toEqual([]);
    expect(state.unexpected).toEqual([]);
  });
}

test('the panel explains why it cannot interpret yet and never calls the assistant', async ({ page }) => {
  const state = await fixture(page, { configured: true, enabled: true, runs_left: 5, unavailable: 'El cliente no escribió notas ni mensajes que el asistente pueda interpretar.', latest_run: null });
  await page.goto(`/proveedor/${supplierId}/ordenes/${orderId}`);
  await content(page).getByRole('button', { name: 'Cotización', exact: true }).click();
  await expect(panel(page).getByRole('status')).toHaveText('El cliente no escribió notas ni mensajes que el asistente pueda interpretar.');
  await expect(panel(page).getByRole('button', { name: 'Interpretar solicitud del cliente', exact: true })).toBeDisabled();
  expect(state.runs).toEqual([]);
  expect(state.unexpected).toEqual([]);
});

test('the client sees in the deal chat that the supplier uses an AI assistant, and nothing else about it', async ({ page }) => {
  const order = { id: orderId, reference: 'ORD-ASISTENTE-001', client: { id: clientId, name: 'TALLER CENTRAL' }, supplier: { id: supplierId, name: 'REPUESTOS CENTRAL' },
    submission_id: '55555555-5555-4555-8555-555555555555', status: 'reviewed', version: 2, created_at: stamp, reviewed_at: stamp, handshaked_at: null, line_count: 1,
    unit_count: 3, notes: 'SOLO NECESITO 2', quotation: null, quotations: [], events: [], lines: [{ id: lineId, part_id: '66666666-6666-4666-8666-666666666666', sku: '58411-1R000',
      name: 'TAMBOR', codigo: '58411-1R000-G', brand: 'KSM', description: 'TAMBOR', quantity: 3 }] };
  const unexpected: string[] = [];
  await page.route('**/api/**', route => { unexpected.push(`${route.request().method()} ${new URL(route.request().url()).pathname}`); return route.fulfill({ status: 404, json: { detail: 'Ruta no prevista' } }); });
  await page.route('**/api/session', route => route.fulfill({ json: { authenticated: true, user: { id: 802, username: 'CLIENTE', is_superuser: false },
    accounts: { ...empty, count: 1, results: [{ id: clientId, name: 'TALLER CENTRAL', roles: ['client_business'], capabilities: ['client'], permission: 'owner' }] } } }));
  await page.route('**/api/market/analytics/events', route => route.fulfill({ status: 202, json: { recorded: true } }));
  await page.route('**/api/market/wishlist/state', route => route.fulfill({ json: { part_ids: [], count: 0 } }));
  await page.route(/\/api\/market\/catalog(?:\?.*)?$/, route => route.fulfill({ json: empty }));
  await page.route(/\/api\/market\/accounts\/[^/]+\/sent-requests(?:\?.*)?$/, route => route.fulfill({ json: { ...empty, count: 1, results: [order] } }));
  await page.route(`/api/market/accounts/${clientId}/sent-requests/${orderId}`, route => route.fulfill({ json: order }));
  await page.route(/\/api\/market\/accounts\/[^/]+\/deals\/[^/]+\/messages(?:\?.*)?$/, route =>
    route.fulfill({ json: { results: [], cursor: 0, has_more: false, has_earlier: false, assistant_notice: true } }));
  await page.goto('/?vista=solicitudes');
  await page.getByRole('button', { name: 'Ver enviada ORD-ASISTENTE-001', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByRole('button', { name: 'Conversación', exact: true }).click();
  await expect(dialog.getByRole('note', { name: 'Aviso de asistente de IA', exact: true })).toHaveText(
    'El proveedor usa un asistente de IA (Google Gemini) que puede leer las notas de tu orden y esta conversación para preparar tu cotización. No recibe precios ni existencias.');
  await dialog.getByRole('button', { name: 'Cotización', exact: true }).click();
  await expect(dialog.getByText('Asistente de cotización (IA)', { exact: true })).toHaveCount(0);
  expect(unexpected).toEqual([]);
});
