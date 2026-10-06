import type { Page } from '@playwright/test';
import type { Deal } from '../lib/deal-types';
import type { DraftAcknowledgement, DraftException, DraftProfile, DraftSuggestion, PublishException, QuoteDraft, QuoteDraftLine, QuoteDraftReprice, QuoteDraftSave, RepricedLine } from '../lib/pricing-types';
import type { SupplierRequestLine } from '../lib/request-types';

type DraftSource = Pick<Deal, 'lines' | 'status'> & { quotation?: Deal['quotation'] };
const cents = (value: string | null) => value === null ? 0 : Math.round(Number(value) * 100);
function summary(lines: QuoteDraftLine[]) {
  const total = lines.reduce((sum, line) => sum + (line.quantity ?? 0) * cents(line.unit_price), 0);
  return { blocking: 0, to_confirm: 0, info: 0, total: (total / 100).toFixed(2), line_count: lines.length };
}

// A supplier-only engine suggestion from the GENERAL list (null price: the list has no price for the item).
export const generalList = { id: '99999999-0000-4000-8000-000000000001', code: 'GENERAL', name: 'LISTA GENERAL', currency: 'USD' as const };
export function suggestion(price: string | null, { floor = null, revision = 3 }: { floor?: string | null; revision?: number } = {}): DraftSuggestion {
  const fingerprint = `fp-${price ?? 'none'}-${revision}`;
  return { unit_price: price, fingerprint, explanation: { engine: 'pricing-v2', evaluated_on: '2026-10-05', quantity_basis: 1, currency: 'USD', unit_price: price,
    status: price ? 'priced' : 'missing', profile: null, price_list: generalList, steps: [], discarded: [], next_breaks: [], floor_price: floor, codes: price ? [] : ['no_list_price'],
    rounding: 'ROUND_HALF_UP_0.01', fingerprint, base: price ? { price_list_id: generalList.id, price_list_code: 'GENERAL', fallback: false, entry_revision: revision,
      unit_price: price, currency: 'USD', updated_at: '2026-10-01T17:00:00Z' } : null } };
}
type Suggestions = Record<string, DraftSuggestion>;
// Saved engine prices never follow a moved suggestion: like the server, they count as stale until the supplier recalculates.
function withPricing(draft: QuoteDraft, suggestions: Suggestions | null, profile?: DraftProfile): QuoteDraft {
  if (!suggestions) return draft;
  const lines = draft.lines.map(line => ({ ...line, suggestion: suggestions[line.order_line_id] ?? null }));
  return { ...draft, lines, pricing: { engine: 'pricing-v2', configured: true, price_list: generalList, ...(profile ? { profile } : {}),
    stale: draft.persisted ? lines.filter(line => line.price_source === 'engine' && line.suggestion && line.unit_price !== line.suggestion.unit_price).length : 0,
    fillable: lines.filter(line => line.unit_price === null && line.suggestion?.unit_price).length } };
}

// Mirrors the server prefill: previous revision first, then the engine suggestion (when the supplier has lists), then no price.
export function virtualDraft(order: DraftSource, suggestions: Suggestions | null = null): QuoteDraft {
  const prior = order.quotation;
  const lines: QuoteDraftLine[] = (order.lines as SupplierRequestLine[]).map(line => {
    const previous = prior?.lines.find(value => value.order_line_id === line.id), suggested = suggestions?.[line.id]?.unit_price ?? null;
    return { order_line_id: line.id, codigo: line.codigo, brand: line.brand, description: line.description || line.name, requested: line.quantity,
      stock: line.stock ?? null, quantity: previous ? previous.quantity : line.quantity, quantity_source: previous ? 'previous' : 'requested',
      unit_price: previous ? previous.unit_price : suggested, price_source: previous ? 'previous' : suggested ? 'engine' : 'none', suggestion: null, note: '', exceptions: [] };
  });
  return withPricing({ persisted: false, draft_version: 0, status: 'editing', base_quotation_id: prior?.id ?? null, currency: prior?.currency ?? 'USD',
    terms: prior?.terms ?? '', terms_origin: prior ? 'previous' : 'none', updated_at: null, updated_by: null,
    permissions: { can_publish: true, publish_requires: 'staff' }, pricing: { engine: 'pricing-v2', configured: false, price_list: null, stale: 0, fillable: 0 },
    lines, order_exceptions: [], summary: summary(lines), repriced: [] }, suggestions);
}

export function applySave(draft: QuoteDraft, payload: Omit<QuoteDraftSave, 'save_id' | 'expected_draft_version'>, author = 'EMPLEADO'): QuoteDraft {
  const lines = draft.lines.map(line => {
    const change = payload.lines?.find(value => value.order_line_id === line.order_line_id);
    if (!change) return line;
    const next = { ...line };
    if (change.quantity !== undefined && change.quantity !== line.quantity) Object.assign(next, { quantity: change.quantity, quantity_source: 'manual' });
    if (change.unit_price !== undefined && change.unit_price !== line.unit_price) Object.assign(next, { unit_price: change.unit_price,
      price_source: change.unit_price === null ? 'none' : change.unit_price === line.suggestion?.unit_price ? 'engine' : 'manual' });
    return next;
  });
  // Like the server: editing what the client receives withdraws an approval request unless the same save asks for it again.
  const edited = lines.some((line, index) => line.quantity !== draft.lines[index].quantity || line.unit_price !== draft.lines[index].unit_price)
    || (payload.currency !== undefined && payload.currency !== draft.currency) || (payload.terms !== undefined && payload.terms !== draft.terms);
  const review = payload.request_review && (draft.status !== 'review_requested' || edited) ? { status: 'review_requested' as const, review_requested_by: { name: author },
    review_requested_at: new Date().toISOString() } : payload.request_review === false || (payload.request_review === undefined && edited)
    ? { status: 'editing' as const, review_requested_by: null, review_requested_at: null } : {};
  return { ...draft, persisted: true, draft_version: draft.draft_version + 1, currency: payload.currency ?? draft.currency, terms: payload.terms ?? draft.terms,
    terms_origin: 'saved', updated_at: new Date().toISOString(), updated_by: { name: author }, lines, summary: summary(lines), ...review };
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

// What the server's reprice endpoint does with the live suggestions; it never touches a line without a suggested price.
export function applyReprice(draft: QuoteDraft, payload: QuoteDraftReprice, author = 'EMPLEADO'): QuoteDraft {
  const repriced: RepricedLine[] = [];
  const lines = draft.lines.map(line => {
    const price = line.suggestion?.unit_price ?? null;
    const chosen = payload.scope === 'blank' ? line.unit_price === null : payload.scope === 'engine' ? line.price_source === 'engine' : !!payload.order_line_ids?.includes(line.order_line_id);
    if (!chosen || price === null || (line.unit_price === price && line.price_source === 'engine')) return line;
    if (line.unit_price !== price) repriced.push({ order_line_id: line.order_line_id, previous_unit_price: line.unit_price, unit_price: price, reason: payload.scope });
    return { ...line, unit_price: price, price_source: 'engine' as const };
  });
  // Like the server: a reprice that moved a price withdraws a pending approval request.
  return { ...draft, persisted: true, draft_version: draft.draft_version + 1, updated_at: new Date().toISOString(), updated_by: { name: author }, lines, summary: summary(lines), repriced,
    ...(repriced.length ? { status: 'editing' as const, review_requested_by: null, review_requested_at: null } : {}) };
}

// A server-like draft store: version checks return the current draft with 409, and a new revision makes old rows stale.
// With exceptions on, drafts carry the server's alerts and saves apply acknowledge/revoke; with suggestions, lines carry the
// supplier's list prices and the reprice endpoint applies them.
export async function mockQuoteDrafts(page: Page, getOrder: () => DraftSource, { exceptions = false, suggestions = null as Suggestions | null, profile = undefined as DraftProfile | undefined,
  permissions = undefined as QuoteDraft['permissions'] | undefined } = {}) {
  let saved: QuoteDraft | null = null, acks: Acks = {};
  const saves: QuoteDraftSave[] = [], discards: QuoteDraftSave[] = [], reprices: QuoteDraftReprice[] = [], requests: string[] = [];
  // hold() keeps draft saves waiting until the returned release() is called, to act while a save is in flight.
  let gate: Promise<void> | null = null, held = 0;
  const editable = () => ['reviewed', 'adjustment'].includes(getOrder().status);
  const stored = () => ({ ...withPricing(saved && saved.base_quotation_id === (getOrder().quotation?.id ?? null) ? saved : virtualDraft(getOrder(), suggestions), suggestions, profile),
    ...(permissions ? { permissions } : {}) });
  const current = () => exceptions ? withExceptions(stored(), saved ? acks : {}) : stored();
  await page.route(/\/api\/market\/accounts\/[^/]+\/requests\/[^/]+\/draft(?:\/discard|\/reprice)?$/, async route => {
    requests.push(`${route.request().method()} ${new URL(route.request().url()).pathname}`);
    if (route.request().method() === 'GET') return route.fulfill({ json: editable() ? { editable: true, draft: current() } : { editable: false, draft: null } });
    if (gate) { held++; await gate; }
    const payload = route.request().postDataJSON() as QuoteDraftSave;
    if (!editable()) return route.fulfill({ status: 409, json: { detail: 'Esta orden ya no admite cambios en el borrador de cotización. Actualiza el acuerdo.' } });
    const draft = current();
    if (payload.expected_draft_version !== draft.draft_version) return route.fulfill({ status: 409, json: { detail: 'Otro miembro de tu equipo modificó este borrador.', draft } });
    if (route.request().url().endsWith('/discard')) { discards.push(payload); saved = null; acks = {}; return route.fulfill({ json: current() }); }
    if (route.request().url().endsWith('/reprice')) {
      const body = route.request().postDataJSON() as QuoteDraftReprice;
      reprices.push(body);
      const next = applyReprice(stored(), body);
      saved = { ...next, repriced: [] };
      return route.fulfill({ json: { ...current(), repriced: next.repriced } });
    }
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
  return { saves, discards, reprices, requests, current, hold, held: () => held, clear: () => { saved = null; acks = {}; }, set: (draft: QuoteDraft) => { saved = draft; },
    suggest: (lineId: string, value: DraftSuggestion) => { suggestions = { ...(suggestions || {}), [lineId]: value }; },
    setProfile: (value: DraftProfile) => { profile = value; } };
}
