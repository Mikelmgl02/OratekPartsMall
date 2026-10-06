import type { Page } from '@playwright/test';
import type { Deal } from '../lib/deal-types';
import type { QuoteDraft, QuoteDraftLine, QuoteDraftSave } from '../lib/pricing-types';
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

// A server-like draft store: version checks return the current draft with 409, and a new revision makes old rows stale.
export async function mockQuoteDrafts(page: Page, getOrder: () => DraftSource) {
  let saved: QuoteDraft | null = null;
  const saves: QuoteDraftSave[] = [], discards: QuoteDraftSave[] = [], requests: string[] = [];
  // hold() keeps draft saves waiting until the returned release() is called, to act while a save is in flight.
  let gate: Promise<void> | null = null, held = 0;
  const editable = () => ['reviewed', 'adjustment'].includes(getOrder().status);
  const current = () => saved && saved.base_quotation_id === (getOrder().quotation?.id ?? null) ? saved : virtualDraft(getOrder());
  await page.route(/\/api\/market\/accounts\/[^/]+\/requests\/[^/]+\/draft(?:\/discard)?$/, async route => {
    requests.push(`${route.request().method()} ${new URL(route.request().url()).pathname}`);
    if (route.request().method() === 'GET') return route.fulfill({ json: editable() ? { editable: true, draft: current() } : { editable: false, draft: null } });
    if (gate) { held++; await gate; }
    const payload = route.request().postDataJSON() as QuoteDraftSave;
    if (!editable()) return route.fulfill({ status: 409, json: { detail: 'Esta orden ya no admite cambios en el borrador de cotización. Actualiza el acuerdo.' } });
    const draft = current();
    if (payload.expected_draft_version !== draft.draft_version) return route.fulfill({ status: 409, json: { detail: 'Otro miembro de tu equipo modificó este borrador.', draft } });
    if (route.request().url().endsWith('/discard')) { discards.push(payload); saved = null; return route.fulfill({ json: current() }); }
    saves.push(payload);
    saved = applySave(draft, payload);
    return route.fulfill({ json: saved });
  });
  const hold = () => { let release = () => {}; gate = new Promise<void>(resolve => { release = () => { gate = null; resolve(); }; }); return release; };
  return { saves, discards, requests, current, hold, held: () => held, clear: () => { saved = null; }, set: (draft: QuoteDraft) => { saved = draft; } };
}
