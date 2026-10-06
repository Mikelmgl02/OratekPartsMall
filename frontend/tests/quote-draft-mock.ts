import type { Page } from '@playwright/test';
import type { Deal } from '../lib/deal-types';
import type { DraftAcknowledgement, DraftException, PublishException, QuoteDraft, QuoteDraftLine, QuoteDraftSave } from '../lib/pricing-types';
import type { SupplierRequestLine } from '../lib/request-types';

type DraftSource = Pick<Deal, 'lines' | 'status'> & { quotation?: Deal['quotation'] };
const cents = (value: string | null) => value === null ? 0 : Math.round(Number(value) * 100);
function summary(lines: QuoteDraftLine[]) {
  const total = lines.reduce((sum, line) => sum + (line.quantity ?? 0) * cents(line.unit_price), 0);
  return { blocking: 0, to_confirm: 0, info: 0, total: (total / 100).toFixed(2), line_count: lines.length };
}

// Mirrors the server prefill: previous revision first, then the requested quantity without a price.
export function virtualDraft(order: DraftSource): QuoteDraft {
  const prior = order.quotation;
  const lines: QuoteDraftLine[] = (order.lines as SupplierRequestLine[]).map(line => {
    const previous = prior?.lines.find(value => value.order_line_id === line.id);
    return { order_line_id: line.id, codigo: line.codigo, brand: line.brand, description: line.description || line.name, requested: line.quantity,
      stock: line.stock ?? null, quantity: previous ? previous.quantity : line.quantity, quantity_source: previous ? 'previous' : 'requested',
      unit_price: previous ? previous.unit_price : null, price_source: previous ? 'previous' : 'none', suggestion: null, note: '', exceptions: [] };
  });
  return { persisted: false, draft_version: 0, status: 'editing', base_quotation_id: prior?.id ?? null, currency: prior?.currency ?? 'USD',
    terms: prior?.terms ?? '', terms_origin: prior ? 'previous' : 'none', updated_at: null, updated_by: null,
    permissions: { can_publish: true, publish_requires: 'staff' }, lines, order_exceptions: [], summary: summary(lines) };
}

export function applySave(draft: QuoteDraft, payload: Omit<QuoteDraftSave, 'save_id' | 'expected_draft_version'>, author = 'EMPLEADO'): QuoteDraft {
  const lines = draft.lines.map(line => {
    const change = payload.lines?.find(value => value.order_line_id === line.order_line_id);
    if (!change) return line;
    const next = { ...line };
    if (change.quantity !== undefined && change.quantity !== line.quantity) Object.assign(next, { quantity: change.quantity, quantity_source: 'manual' });
    if (change.unit_price !== undefined && change.unit_price !== line.unit_price) Object.assign(next, { unit_price: change.unit_price, price_source: change.unit_price === null ? 'none' : 'manual' });
    return next;
  });
  return { ...draft, persisted: true, draft_version: draft.draft_version + 1, currency: payload.currency ?? draft.currency, terms: payload.terms ?? draft.terms,
    terms_origin: 'saved', updated_at: new Date().toISOString(), updated_by: { name: author }, lines, summary: summary(lines) };
}

// Mirrors the server's non-price exception catalog: confirmations count only while their context is unchanged.
type Acks = Record<string, DraftAcknowledgement[]>;
export function withExceptions(draft: QuoteDraft, acks: Acks): QuoteDraft {
  const lines = draft.lines.map(line => {
    const found: DraftException[] = [], quantity = line.quantity, add = (code: string, severity: DraftException['severity'], message: string, context = '') =>
      found.push({ code, severity, message, context, acknowledged: severity === 'confirm' && !!acks[line.order_line_id]?.some(ack => ack.code === code && ack.context === context) });
    if (quantity === null) add('quantity_missing', 'block', 'Indica la cantidad ofrecida (0 si no la ofreces).');
    if (line.unit_price === null && quantity) add('price_missing', 'block', 'Falta el precio.');
    if (quantity) {
      if (!line.stock) add('identity_changed', 'confirm', 'El artículo de tu inventario cambió desde la solicitud; confirma que ofreces el mismo repuesto.', 'a1b2c3d4e5f6');
      else if (quantity > line.stock.available_quantity) add('offered_gt_available', 'confirm', `Ofreces ${quantity} y tienes ${line.stock.available_quantity} disponibles.`, `${quantity}>${line.stock.available_quantity}`);
      if (quantity > line.requested) add('offered_gt_requested', 'confirm', `Ofreces más unidades de las solicitadas (${quantity} de ${line.requested}).`, `${quantity}>${line.requested}`);
      if (line.unit_price !== null && cents(line.unit_price) === 0) add('zero_price', 'confirm', 'Precio 0,00 con unidades ofrecidas.', `${quantity}@0.00`);
    }
    if (quantity === 0) add('zero_offered', 'info', 'No ofreces este artículo.');
    return { ...line, exceptions: found };
  });
  const order: DraftException[] = lines.every(line => line.quantity === 0) ? [{ code: 'all_zero', severity: 'block', message: 'Ofrece al menos una unidad.', context: '', acknowledged: false }] : [];
  const every = [...order, ...lines.flatMap(line => line.exceptions)];
  return { ...draft, lines, order_exceptions: order, summary: { ...draft.summary, blocking: every.filter(item => item.severity === 'block').length,
    to_confirm: every.filter(item => item.severity === 'confirm' && !item.acknowledged).length, info: every.filter(item => item.severity === 'info').length } };
}
// What a bound publish refuses: any block and every confirm-level alert not yet confirmed.
export function publishBlockers(draft: QuoteDraft): PublishException[] {
  return [...draft.order_exceptions.map(item => ({ ...item, order_line_id: null })), ...draft.lines.flatMap(line => line.exceptions.map(item => ({ ...item, order_line_id: line.order_line_id })))]
    .filter(item => item.severity === 'block' || (item.severity === 'confirm' && !item.acknowledged));
}

// A server-like draft store: version checks return the current draft with 409, and a new revision makes old rows stale.
// With exceptions on, drafts carry the server's alerts and saves apply acknowledge/revoke.
export async function mockQuoteDrafts(page: Page, getOrder: () => DraftSource, { exceptions = false } = {}) {
  let saved: QuoteDraft | null = null, acks: Acks = {};
  const saves: QuoteDraftSave[] = [], discards: QuoteDraftSave[] = [], requests: string[] = [];
  // hold() keeps draft saves waiting until the returned release() is called, to act while a save is in flight.
  let gate: Promise<void> | null = null, held = 0;
  const editable = () => ['reviewed', 'adjustment'].includes(getOrder().status);
  const stored = () => saved && saved.base_quotation_id === (getOrder().quotation?.id ?? null) ? saved : virtualDraft(getOrder());
  const current = () => exceptions ? withExceptions(stored(), saved ? acks : {}) : stored();
  await page.route(/\/api\/market\/accounts\/[^/]+\/requests\/[^/]+\/draft(?:\/discard)?$/, async route => {
    requests.push(`${route.request().method()} ${new URL(route.request().url()).pathname}`);
    if (route.request().method() === 'GET') return route.fulfill({ json: editable() ? { editable: true, draft: current() } : { editable: false, draft: null } });
    if (gate) { held++; await gate; }
    const payload = route.request().postDataJSON() as QuoteDraftSave;
    if (!editable()) return route.fulfill({ status: 409, json: { detail: 'Esta orden ya no admite cambios en el borrador de cotización. Actualiza el acuerdo.' } });
    const draft = current();
    if (payload.expected_draft_version !== draft.draft_version) return route.fulfill({ status: 409, json: { detail: 'Otro miembro de tu equipo modificó este borrador.', draft } });
    if (route.request().url().endsWith('/discard')) { discards.push(payload); saved = null; acks = {}; return route.fulfill({ json: current() }); }
    saves.push(payload);
    if (!saved) acks = {};
    for (const change of payload.lines || []) {
      const kept = (acks[change.order_line_id] || []).filter(ack => !change.revoke?.includes(ack.code) && !change.acknowledge?.some(value => value.code === ack.code));
      acks[change.order_line_id] = [...kept, ...(change.acknowledge || [])];
    }
    saved = applySave(stored(), payload);
    return route.fulfill({ json: current() });
  });
  const hold = () => { let release = () => {}; gate = new Promise<void>(resolve => { release = () => { gate = null; resolve(); }; }); return release; };
  return { saves, discards, requests, current, hold, held: () => held, clear: () => { saved = null; acks = {}; }, set: (draft: QuoteDraft) => { saved = draft; } };
}
