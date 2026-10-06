'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { CloudOff, LoaderCircle, PackageMinus, RefreshCcw, RotateCcw, Send, Sparkles, TriangleAlert } from 'lucide-react';
import { AgGridReact, type CustomCellEditorProps, type CustomCellRendererProps } from 'ag-grid-react';
import type { CellClickedEvent, ColDef, GetRowIdParams, GridReadyEvent, ValueSetterParams } from 'ag-grid-community';
import { gridLocale, motionGridTheme } from '@/lib/ag-grid';
import type { Deal, QuotePayload } from '@/lib/deal-types';
import type { DraftException, DraftPricing, DraftStock, DraftSuggestion, PriceExplanation, PriceSource, PublishException, QuoteDraft, QuoteDraftEnvelope,
  QuoteDraftLineChange, QuoteDraftSave, RepricedLine, RepriceScope } from '@/lib/pricing-types';
import type { SupplierRequestLine } from '@/lib/request-types';
import { ApiError, request } from '@/lib/types';
import { decimal, moneyFormatter, priceCents, quantityValue } from '@/lib/money';
import Modal from './modal';
import { PriceCalculation, money } from './price-explanation';
import { UppercaseTextarea } from './uppercase-field';

type QuoteRow = { order_line_id: string; codigo: string; description: string; requested: number; quantity: string; unit_price: string };
type Currency = 'USD' | 'PAB';
// The last values the server confirmed; local cells are compared against it to send only what changed.
type Base = { version: number; persisted: boolean; currency: Currency; terms: string; updatedAt: string | null; updatedBy: string;
  lines: Record<string, { quantity: number | null; unit_price: string | null }> };
type SaveState = 'idle' | 'pending' | 'saving' | 'error' | 'conflict';
type Outcome = 'saved' | 'rebased' | 'failed';
// Server-computed alerts of the last confirmed draft (live) or of a refused draftless publish (not confirmable here).
type Alerts = { lines: Record<string, DraftException[]>; order: DraftException[]; live: boolean };
// A "Confirmo" click waiting to be saved: it confirms the context on screen or withdraws the confirmation.
type PendingAck = { lineId: string; code: string; context: string; revoke: boolean };
// The confirmed draft's price origin and live suggestion per line (supplier-only), read by the Origen del precio column and the drawer.
type Pricing = { summary: DraftPricing | null; lines: Record<string, { source: PriceSource; suggestion: DraftSuggestion | null }> };
function pricingFrom(draft: QuoteDraft | null): Pricing {
  return { summary: draft?.pricing ?? null, lines: Object.fromEntries((draft?.lines || []).map(line => [line.order_line_id, { source: line.price_source, suggestion: line.suggestion }])) };
}
function engineLabel(explanation: PriceExplanation | undefined) {
  const rule = explanation?.steps.find(step => step.kind === 'rule');
  return rule && rule.kind === 'rule' ? rule.action === 'net_price' ? 'Neto especial' : 'Regla' : 'Lista';
}
const sourceLabels: Record<PriceSource, string> = { engine: 'Lista', previous: 'Versión anterior', manual: 'Manual', none: 'Sin precio' };
const repriceNotices: Record<RepricedLine['reason'], [string, string]> = {
  blank: ['precio sugerido aplicado', 'precios sugeridos aplicados'], engine: ['precio recalculado con tu lista', 'precios recalculados con tu lista'],
  lines: ['precio sugerido aplicado', 'precios sugeridos aplicados'], quantity: ['precio recalculado por cantidad', 'precios recalculados por cantidad'] };
const rowId = ({ data }: GetRowIdParams<QuoteRow>) => data.order_line_id;
const severityRank = { block: 0, confirm: 1, info: 2 };
function alertsFrom(draft: QuoteDraft | null): Alerts {
  return { lines: Object.fromEntries((draft?.lines || []).map(line => [line.order_line_id, line.exceptions])), order: draft?.order_exceptions || [], live: !!draft };
}
function alertsFromPublish(items: PublishException[]): Alerts {
  const lines: Record<string, DraftException[]> = {};
  for (const { order_line_id: id, ...item } of items) if (id) (lines[id] ||= []).push(item);
  return { lines, order: items.filter(item => !item.order_line_id).map(({ order_line_id: _, ...item }) => item), live: false };
}
function alertLabel(items: DraftException[]) {
  const blocking = items.filter(item => item.severity === 'block').length, confirm = items.filter(item => item.severity === 'confirm');
  const pending = confirm.filter(item => !item.acknowledged).length, info = items.find(item => item.severity === 'info');
  if (blocking) return `${blocking} por corregir`;
  if (pending) return `${pending} por confirmar`;
  if (confirm.length) return confirm.length === 1 ? 'Confirmada' : `${confirm.length} confirmadas`;
  return info ? info.message : 'Sin alertas';
}

function rowCents(row: QuoteRow) { return BigInt(quantityValue(row.quantity) ?? 0) * (priceCents(row.unit_price) ?? BigInt(0)); }
// undefined marks a cell that cannot be saved yet; a blank cell is a deliberate null.
const cellQuantity = (value: string) => !value.trim() ? null : quantityValue(value) ?? undefined;
function cellPrice(value: string) { if (!value.trim()) return null; const cents = priceCents(value); return cents === null ? undefined : decimal(cents); }
const normalTerms = (value: string) => value.trim().toUpperCase();
function baseFrom(draft: QuoteDraft): Base {
  return { version: draft.draft_version, persisted: draft.persisted, currency: draft.currency, terms: draft.terms, updatedAt: draft.updated_at,
    updatedBy: draft.updated_by?.name || '', lines: Object.fromEntries(draft.lines.map(line => [line.order_line_id,
      { quantity: line.quantity, unit_price: line.unit_price === null ? null : cellPrice(line.unit_price) ?? line.unit_price }])) };
}
function draftRows(draft: QuoteDraft): QuoteRow[] {
  return draft.lines.map(line => ({ order_line_id: line.order_line_id, codigo: line.codigo, description: line.description, requested: line.requested,
    quantity: line.quantity === null ? '' : String(line.quantity), unit_price: line.unit_price ?? '' }));
}
function dealRows(deal: Deal): QuoteRow[] {
  return deal.lines.map(line => {
    const prior = deal.quotation?.lines.find(value => value.order_line_id === line.id);
    return { order_line_id: line.id, codigo: line.codigo, description: line.description || line.name,
      requested: line.quantity, quantity: String(prior?.quantity ?? line.quantity), unit_price: prior?.unit_price ?? '' };
  });
}
const draftStock = (draft: QuoteDraft) => Object.fromEntries(draft.lines.map(line => [line.order_line_id, line.stock]));
function ago(value: string | null, now: number) {
  if (!value) return '';
  const minutes = Math.floor((now - new Date(value).getTime()) / 60000);
  if (minutes < 1) return 'hace un momento';
  if (minutes < 60) return `hace ${minutes} min`;
  if (minutes < 1440) return `hace ${Math.floor(minutes / 60)} h`;
  return `el ${new Date(value).toLocaleDateString('es-PA', { timeZone: 'America/Panama', dateStyle: 'medium' })}`;
}

function NumericEditor({ value, onValueChange, data, colDef, stopEditing, api, node, column }: CustomCellEditorProps<QuoteRow, string>) {
  const input = useRef<HTMLInputElement>(null);
  const price = colDef.field === 'unit_price';
  useEffect(() => { input.current?.focus(); input.current?.select(); }, []);
  return <input ref={input} className="quotation-cell-input" type="text" inputMode={price ? 'decimal' : 'numeric'}
    aria-label={`${price ? 'Precio' : 'Unidades ofrecidas'} de ${data.codigo}`} autoComplete="off" autoCorrect="off" spellCheck={false}
    value={value ?? ''} onChange={event => onValueChange(event.target.value)}
    onKeyDownCapture={event => { if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); api.stopEditing(true); if (node.rowIndex !== null) api.setFocusedCell(node.rowIndex, column, node.rowPinned); } }}
    onKeyDown={event => { if (event.key === 'Enter') { event.preventDefault(); event.stopPropagation(); stopEditing(); } }}/>;
}
function PartCell({ data }: CustomCellRendererProps<QuoteRow>) {
  return data ? <div className="quotation-part-cell"><strong>{data.codigo}</strong>{data.description && <small title={data.description}>{data.description}</small>}</div> : null;
}

export default function QuotationEditor({ deal, draft, draftPath, disabled, onSend, onDraftChange, pricingRevision = 0 }: {
  deal: Deal; draft: QuoteDraft | null; draftPath: string; disabled: boolean;
  // Resolves true once published, or with the refused request's error so its alerts can be shown.
  onSend: (values: QuotePayload) => Promise<true | ApiError | false>; onDraftChange?: (persisted: boolean, draft: QuoteDraft) => void;
  // Bumped when the client's profile or rules changed elsewhere: the editor reloads its suggestions (stored prices never move by themselves).
  pricingRevision?: number;
}) {
  // Without a loaded draft the editor falls back to the previous revision and nothing is saved automatically.
  const autosave = draft !== null;
  const [rows, setRows] = useState<QuoteRow[]>(() => draft ? draftRows(draft) : dealRows(deal));
  const rowsRef = useRef(rows);
  // Live stock is read by the Disponibles column from a ref, so refreshing it never rebuilds row state.
  const stock = useRef<Record<string, DraftStock | null>>(draft ? draftStock(draft) : Object.fromEntries((deal.lines as SupplierRequestLine[]).map(line => [line.id, line.stock ?? null])));
  const grid = useRef<AgGridReact<QuoteRow>>(null);
  const host = useRef<HTMLDivElement>(null);
  const [currency, setCurrency] = useState<Currency>(draft?.currency || deal.quotation?.currency || 'USD');
  const [terms, setTerms] = useState(draft ? draft.terms : deal.quotation?.terms || '');
  const [termsOrigin, setTermsOrigin] = useState(draft?.terms_origin);
  const header = useRef({ currency, terms });
  const base = useRef<Base | null>(draft ? baseFrom(draft) : null);
  const [saved, setSaved] = useState(base.current);
  const [saveState, setSaveState] = useState<SaveState>('idle');
  const [saveError, setSaveError] = useState('');
  const [conflict, setConflict] = useState<QuoteDraft | null>(null);
  const conflictRef = useRef<QuoteDraft | null>(null);
  const [invalidCells, setInvalidCells] = useState(0);
  const [publishing, setPublishing] = useState(false);
  const [confirmDiscard, setConfirmDiscard] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  const [error, setError] = useState('');
  // Alerts are read by the Alertas column from a ref, like stock, so a refresh never rebuilds row state.
  const alertsRef = useRef<Alerts>(alertsFrom(draft));
  const [alerts, setAlerts] = useState(alertsRef.current);
  const pendingAcks = useRef(new Map<string, PendingAck>());
  const [, setAckRevision] = useState(0);
  // The review panel stays folded under a compact count; it opens on demand or when a publish is refused.
  const [panelOpen, setPanelOpen] = useState(false);
  const panel = useRef<HTMLDetailsElement>(null);
  const pricingRef = useRef<Pricing>(pricingFrom(draft));
  const [pricing, setPricing] = useState(pricingRef.current.summary);
  const [drawerLine, setDrawerLine] = useState<string | null>(null);
  const [repricing, setRepricing] = useState(false);
  const [priceNotice, setPriceNotice] = useState('');
  // Lines whose price the server just recalculated flash briefly in the grid.
  const flashed = useRef(new Set<string>());
  const openDrawer = useRef((lineId: string) => setDrawerLine(lineId));
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const inFlight = useRef<Promise<Outcome> | null>(null);
  const attempt = useRef<{ key: string; id: string } | null>(null);
  const mounted = useRef(true);
  const props = useRef({ draftPath, onDraftChange });
  props.current = { draftPath, onDraftChange };
  const formatMoney = useMemo(() => moneyFormatter(currency), [currency]);
  const total = rows.reduce((sum, row) => sum + rowCents(row), BigInt(0));
  const completed = rows.filter(row => {
    const quantity = quantityValue(row.quantity);
    return quantity !== null && ((quantity === 0 && !row.unit_price.trim()) || priceCents(row.unit_price) !== null);
  }).length;

  // These helpers only read refs, so the memoized grid callbacks can keep calling them safely.
  function pendingChanges() {
    const current = base.current!, lines: QuoteDraftLineChange[] = [], acks = [...pendingAcks.current.values()];
    let invalid = 0;
    for (const row of rowsRef.current) {
      const prior = current.lines[row.order_line_id];
      if (!prior) continue;
      const quantity = cellQuantity(row.quantity), price = cellPrice(row.unit_price), change: QuoteDraftLineChange = { order_line_id: row.order_line_id };
      if (quantity === undefined || price === undefined) invalid++;
      if (quantity !== undefined && quantity !== prior.quantity) change.quantity = quantity;
      if (price !== undefined && price !== prior.unit_price) change.unit_price = price;
      const own = acks.filter(ack => ack.lineId === row.order_line_id);
      if (own.some(ack => !ack.revoke)) change.acknowledge = own.filter(ack => !ack.revoke).map(({ code, context }) => ({ code, context }));
      if (own.some(ack => ack.revoke)) change.revoke = own.filter(ack => ack.revoke).map(ack => ack.code);
      if (Object.keys(change).length > 1) lines.push(change);
    }
    const values = header.current;
    const body: Omit<QuoteDraftSave, 'save_id' | 'expected_draft_version'> = {
      ...(values.currency !== current.currency ? { currency: values.currency } : {}),
      ...(normalTerms(values.terms) !== current.terms ? { terms: normalTerms(values.terms) } : {}), ...(lines.length ? { lines } : {}) };
    return { body, invalid, acks, dirty: Object.keys(body).length > 0 };
  }
  function payloadFor(body: ReturnType<typeof pendingChanges>['body']) {
    const payload = { expected_draft_version: base.current!.version, ...body };
    const key = JSON.stringify(payload);
    // A retry of the very same change reuses its save_id, so a lost response can never apply it twice.
    if (attempt.current?.key !== key) attempt.current = { key, id: crypto.randomUUID() };
    return JSON.stringify({ save_id: attempt.current.id, ...payload });
  }
  function showAlerts(next: Alerts) {
    alertsRef.current = next; setAlerts(next);
    grid.current?.api?.refreshCells({ columns: ['alerts'], force: true });
  }
  function refreshStock(next: QuoteDraft) {
    stock.current = draftStock(next);
    pricingRef.current = pricingFrom(next); setPricing(pricingRef.current.summary);
    grid.current?.api?.refreshCells({ columns: ['available', 'price_source'], force: true });
    showAlerts(alertsFrom(next));
  }
  function adopt(next: QuoteDraft) {
    base.current = baseFrom(next); setSaved(base.current); refreshStock(next); setTermsOrigin(next.terms_origin);
    props.current.onDraftChange?.(next.persisted, next);
  }
  function replaceWith(next: QuoteDraft) {
    const values = draftRows(next);
    rowsRef.current = values; setRows(values);
    header.current = { currency: next.currency, terms: next.terms }; setCurrency(next.currency); setTerms(next.terms);
    conflictRef.current = null; setConflict(null); attempt.current = null;
    pendingAcks.current.clear(); setAckRevision(value => value + 1);
    adopt(next); setSaveState('idle'); setSaveError(''); setInvalidCells(0); setError('');
  }
  // Server-side changes (a reprice, an engine price following a quantity) replace the cells that still show the value they replaced.
  function absorb(server: QuoteDraft) {
    const old = base.current!, next = baseFrom(server);
    let touched = false;
    const values = rowsRef.current.map(row => {
      const was = old.lines[row.order_line_id], latest = next.lines[row.order_line_id];
      if (!was || !latest) return row;
      const quantity = cellQuantity(row.quantity) === was.quantity && latest.quantity !== was.quantity ? latest.quantity === null ? '' : String(latest.quantity) : row.quantity;
      const unitPrice = cellPrice(row.unit_price) === was.unit_price && latest.unit_price !== was.unit_price ? latest.unit_price ?? '' : row.unit_price;
      if (quantity === row.quantity && unitPrice === row.unit_price) return row;
      touched = true;
      return { ...row, quantity, unit_price: unitPrice };
    });
    if (touched) { rowsRef.current = values; setRows(values); }
    adopt(server);
    announce(server.repriced || []);
  }
  function announce(repriced: RepricedLine[], scope?: RepriceScope) {
    if (!repriced.length) { if (scope) setPriceNotice(scope === 'engine' ? 'Tus precios sugeridos ya estaban al día.' : 'No había precios por aplicar.'); return; }
    const [one, many] = repriceNotices[repriced[0].reason];
    // A quantity reprice names the rule that now wins (a volume break): "KSM-123 (FILTROS VOLUMEN 10+)".
    const codes = repriced.map(item => {
      const code = rowsRef.current.find(row => row.order_line_id === item.order_line_id)?.codigo;
      const rule = pricingRef.current.lines[item.order_line_id]?.suggestion?.explanation.steps.find(step => step.kind === 'rule');
      return code && rule?.kind === 'rule' ? `${code} (${rule.name})` : code;
    }).filter(Boolean);
    setPriceNotice(`${repriced.length} ${repriced.length === 1 ? one : many}${repriced[0].reason === 'quantity' ? `: ${codes.join(', ')}` : ''}.`);
    for (const item of repriced) flashed.current.add(item.order_line_id);
    grid.current?.api?.refreshCells({ columns: ['unit_price'], force: true });
    setTimeout(() => { for (const item of repriced) flashed.current.delete(item.order_line_id); grid.current?.api?.refreshCells({ columns: ['unit_price'], force: true }); }, 4000);
  }
  // Another member saved first: keep local edits on cells they did not touch; when both changed one cell, ask.
  function rebase(server: QuoteDraft) {
    const old = base.current!, next = baseFrom(server);
    let conflicted = false;
    const merge = (local: string, parsed: unknown, before: unknown, after: unknown, theirs: string) => {
      if (parsed === before) return theirs;
      if (after !== before && parsed !== after) conflicted = true;
      return local;
    };
    const values = rowsRef.current.map(row => {
      const was = old.lines[row.order_line_id], latest = next.lines[row.order_line_id];
      if (!was || !latest) return row;
      return { ...row, quantity: merge(row.quantity, cellQuantity(row.quantity), was.quantity, latest.quantity, latest.quantity === null ? '' : String(latest.quantity)),
        unit_price: merge(row.unit_price, cellPrice(row.unit_price), was.unit_price, latest.unit_price, latest.unit_price ?? '') };
    });
    const local = header.current;
    const nextCurrency = merge(local.currency, local.currency, old.currency, next.currency, next.currency) as Currency;
    const nextTerms = merge(local.terms, normalTerms(local.terms), old.terms, next.terms, next.terms);
    if (conflicted) { conflictRef.current = server; setConflict(server); setSaveState('conflict'); return false; }
    rowsRef.current = values; setRows(values);
    header.current = { currency: nextCurrency, terms: nextTerms }; setCurrency(nextCurrency); setTerms(nextTerms);
    adopt(server);
    return true;
  }
  async function send(body: string): Promise<Outcome> {
    try {
      const next = await request<QuoteDraft>(props.current.draftPath, { method: 'POST', body });
      attempt.current = null;
      // After unmount the confirmed version still matters: the background flush of later edits is based on it.
      if (!mounted.current) { base.current = baseFrom(next); return 'failed'; }
      absorb(next);
      return 'saved';
    } catch (caught) {
      if (!mounted.current) return 'failed';
      const server = caught instanceof ApiError && caught.status === 409 ? (caught.body as { draft?: QuoteDraft } | null)?.draft : undefined;
      if (server) { attempt.current = null; return rebase(server) ? 'rebased' : 'failed'; }
      setSaveState('error'); setSaveError(caught instanceof ApiError ? caught.detail : '');
      return 'failed';
    }
  }
  // One save in flight at a time; force also materializes a virtual draft so it can be published.
  async function save(force = false): Promise<boolean> {
    while (inFlight.current) await inFlight.current;
    if (!base.current || conflictRef.current || !mounted.current) return false;
    if (timer.current) { clearTimeout(timer.current); timer.current = null; }
    const { body, invalid, acks, dirty } = pendingChanges();
    setInvalidCells(invalid);
    if (!dirty && (base.current.persisted || !force)) { setSaveState('idle'); return true; }
    setSaveState('saving'); setSaveError('');
    const task = send(payloadFor(body));
    inFlight.current = task;
    const outcome = await task;
    inFlight.current = null;
    if (outcome === 'saved') {
      // Confirmations clicked while this save was in flight stay pending for the next one.
      for (const [key, ack] of [...pendingAcks.current]) if (acks.includes(ack)) pendingAcks.current.delete(key);
      setAckRevision(value => value + 1);
    }
    if (outcome === 'rebased') return save(force);
    if (outcome === 'failed') return false;
    if (pendingChanges().dirty) { if (force) return save(force); schedule(); } else setSaveState('idle');
    return true;
  }
  function schedule() {
    if (!base.current || conflictRef.current) return;
    if (timer.current) clearTimeout(timer.current);
    setSaveState(state => state === 'saving' ? state : 'pending');
    timer.current = setTimeout(() => { timer.current = null; void save(); }, 1000);
  }
  function flushInBackground() {
    if (!base.current || conflictRef.current) return;
    // Edits made while a save is in flight follow it, on the version that save confirms.
    if (inFlight.current) { void inFlight.current.then(flushInBackground); return; }
    const { body, dirty } = pendingChanges();
    if (dirty) void fetch(props.current.draftPath, { method: 'POST', keepalive: true, headers: { 'Content-Type': 'application/json' }, body: payloadFor(body) }).catch(() => undefined);
  }
  async function discard() {
    while (inFlight.current) await inFlight.current;
    if (!base.current || !mounted.current) return;
    if (timer.current) { clearTimeout(timer.current); timer.current = null; }
    setSaveState('saving');
    const task = (async (): Promise<Outcome> => {
      try {
        const fresh = await request<QuoteDraft>(`${props.current.draftPath}/discard`, { method: 'POST',
          body: JSON.stringify({ save_id: crypto.randomUUID(), expected_draft_version: base.current!.version }) });
        if (mounted.current) replaceWith(fresh);
        return 'saved';
      } catch (caught) {
        if (!mounted.current) return 'failed';
        const server = caught instanceof ApiError && caught.status === 409 ? (caught.body as { draft?: QuoteDraft } | null)?.draft : undefined;
        if (server) { conflictRef.current = server; setConflict(server); setSaveState('conflict'); }
        else { setSaveState('error'); setSaveError(caught instanceof ApiError ? caught.detail : ''); }
        return 'failed';
      }
    })();
    inFlight.current = task;
    await task;
    inFlight.current = null;
    if (mounted.current) setConfirmDiscard(false);
  }
  // The client's profile or rules changed: reload the live suggestions and alerts. Cells still showing what the server had are updated
  // (a virtual draft follows the new prefill), local edits are kept, and saved engine prices only raise stale_price.
  async function refreshPricing() {
    if (!autosave || conflictRef.current) return;
    while (inFlight.current) await inFlight.current;
    try {
      const envelope = await request<QuoteDraftEnvelope>(props.current.draftPath);
      if (!mounted.current || !envelope.draft || !base.current || conflictRef.current) return;
      if (rebase(envelope.draft) && pendingChanges().dirty) schedule();
    } catch { /* The next save brings the new suggestions. */ }
  }
  // After a refused publish, compare with the server: a newer draft means another member saved in between; the same version
  // still brings alerts recomputed with live stock.
  async function verify() {
    try {
      const envelope = await request<QuoteDraftEnvelope>(props.current.draftPath);
      if (!mounted.current || !envelope.draft || !base.current) return;
      if (envelope.draft.draft_version === base.current.version) { adopt(envelope.draft); return; }
      if (rebase(envelope.draft) && pendingChanges().dirty) schedule();
    } catch { /* The publish error is already on screen. */ }
  }
  function focusRow(lineId: string | null | undefined, column = 'quantity') {
    const api = grid.current?.api, node = lineId ? api?.getRowNode(lineId) : null;
    if (!api || node?.rowIndex == null) return;
    api.ensureIndexVisible(node.rowIndex); api.ensureColumnVisible(column); api.setFocusedCell(node.rowIndex, column);
  }
  function ackState(lineId: string, item: DraftException) {
    const pending = pendingAcks.current.get(`${lineId}|${item.code}`);
    return pending ? !pending.revoke && pending.context === item.context : item.acknowledged;
  }
  // "Confirmo" saves an acknowledgement tied to the context on screen; it counts while that value stays the same.
  function toggleAck(lineId: string, item: DraftException, checked: boolean) {
    if (!base.current || conflictRef.current) return;
    pendingAcks.current.set(`${lineId}|${item.code}`, { lineId, code: item.code, context: item.context, revoke: !checked });
    setAckRevision(value => value + 1);
    void save();
  }
  // Suggested prices: flush pending edits, then let the server apply the supplier's own list (blank lines, engine lines or chosen lines).
  async function reprice(scope: RepriceScope, ids?: string[]) {
    if (!autosave || disabled || publishing || repricing || conflictRef.current) return false;
    grid.current?.api?.stopEditing();
    setRepricing(true); setError(''); setPriceNotice('');
    try {
      if (!await save()) { if (mounted.current && !conflictRef.current) setError('No se pudo guardar el borrador. Reintenta antes de aplicar los precios sugeridos.'); return false; }
      while (inFlight.current) await inFlight.current;
      if (!base.current || conflictRef.current || !mounted.current) return false;
      const body = JSON.stringify({ save_id: crypto.randomUUID(), expected_draft_version: base.current.version, scope, ...(ids ? { order_line_ids: ids } : {}) });
      const task = (async (): Promise<Outcome> => {
        try {
          const next = await request<QuoteDraft>(`${props.current.draftPath}/reprice`, { method: 'POST', body });
          if (!mounted.current) return 'failed';
          absorb({ ...next, repriced: [] }); announce(next.repriced, scope);
          return 'saved';
        } catch (caught) {
          if (!mounted.current) return 'failed';
          const server = caught instanceof ApiError && caught.status === 409 ? (caught.body as { draft?: QuoteDraft } | null)?.draft : undefined;
          if (server && rebase(server)) setError('Otro miembro de tu equipo modificó el borrador. Revisa los precios y vuelve a intentarlo.');
          else if (!server) setError(caught instanceof ApiError ? caught.detail : 'No se pudieron aplicar los precios sugeridos.');
          return 'failed';
        }
      })();
      inFlight.current = task;
      const outcome = await task;
      inFlight.current = null;
      if (mounted.current && pendingChanges().dirty) schedule();
      return outcome === 'saved';
    } finally { if (mounted.current) setRepricing(false); }
  }
  function originLabel(row: QuoteRow) {
    const info = pricingRef.current.lines[row.order_line_id], saved = base.current?.lines[row.order_line_id];
    if (!info) return '—';
    if (!row.unit_price.trim()) return 'Sin precio';
    if (saved && cellPrice(row.unit_price) !== saved.unit_price) return 'Manual';
    return info.source === 'engine' ? engineLabel(info.suggestion?.explanation) : sourceLabels[info.source];
  }
  // Offers min(requested, available) on every line above its available stock; nothing else changes.
  function adjustToStock() {
    if (disabled || publishing) return;
    grid.current?.api?.stopEditing();
    const next = rowsRef.current.map(row => {
      const quantity = quantityValue(row.quantity), available = stock.current[row.order_line_id]?.available_quantity;
      return quantity !== null && available !== undefined && quantity > available ? { ...row, quantity: String(Math.min(row.requested, available)) } : row;
    });
    rowsRef.current = next; setRows(next); setError('');
    schedule();
  }

  const edited = useCallback((event: ValueSetterParams<QuoteRow>) => {
    const field = event.colDef.field;
    if (disabled || !event.data || (field !== 'quantity' && field !== 'unit_price')) return false;
    const value = String(event.newValue ?? '').trim();
    if (event.data[field] === value) return false;
    event.data[field] = value;
    const next = rowsRef.current.map(row => row.order_line_id === event.data.order_line_id ? { ...row, [field]: value } : row);
    // Value setters run synchronously, including when Send finishes an active edit.
    rowsRef.current = next; setRows(next); setError(''); setPriceNotice('');
    schedule();
    return true;
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [disabled]);
  const invalidTooltip = useCallback((value: string, parse: (value: string) => unknown) => parse(value) === undefined ? 'Corrige para guardar' : undefined, []);
  const columns = useMemo<ColDef<QuoteRow>[]>(() => [
    { field: 'codigo', headerName: 'Código / descripción', minWidth: 230, flex: 1.6, filter: 'agTextColumnFilter', cellRenderer: PartCell, tooltipField: 'description' },
    { field: 'requested', headerName: 'Solicitadas', width: 130, minWidth: 125, type: 'numericColumn', filter: 'agNumberColumnFilter' },
    { colId: 'available', headerName: 'Disponibles', width: 130, minWidth: 125, type: 'numericColumn',
      valueGetter: params => params.data ? stock.current[params.data.order_line_id]?.available_quantity ?? null : null,
      valueFormatter: params => params.value === null || params.value === undefined ? 'Revisar artículo' : String(params.value),
      tooltipValueGetter: params => {
        const value = params.data && stock.current[params.data.order_line_id];
        return value ? `Reportadas: ${value.reported_quantity} · Reservadas: ${value.reserved_quantity}` : 'El código o el vínculo con el SKU cambió. Confirma la equivalencia en Inventario.';
      },
      cellClass: params => {
        const value = params.data && stock.current[params.data.order_line_id];
        return ['ag-right-aligned-cell', !value ? 'quotation-stock-review' : value.shortfall > 0 ? 'quotation-stock-short' : ''];
      },
      comparator: (a: number | null, b: number | null) => (a ?? -1) - (b ?? -1) },
    { field: 'quantity', headerName: 'Ofrecidas', width: 125, minWidth: 120, type: 'numericColumn', editable: !disabled, cellEditor: NumericEditor, valueSetter: edited,
      tooltipValueGetter: params => invalidTooltip(params.value || '', cellQuantity),
      cellClass: params => ['ag-right-aligned-cell', quantityValue(params.value || '') === null ? 'quotation-invalid-cell' : 'quotation-editable-cell'],
      comparator: (a, b) => (quantityValue(a || '') ?? -1) - (quantityValue(b || '') ?? -1) },
    { field: 'unit_price', headerName: 'Precio unitario', width: 165, minWidth: 155, flex: 1, type: 'numericColumn', editable: !disabled, cellEditor: NumericEditor, valueSetter: edited,
      tooltipValueGetter: params => invalidTooltip(params.value || '', cellPrice),
      valueFormatter: params => priceCents(params.value || '') === null ? (params.value ? params.value : 'Ingresar precio') : formatMoney(priceCents(params.value)!),
      cellClass: params => ['ag-right-aligned-cell', params.value && priceCents(params.value) === null ? 'quotation-invalid-cell' : 'quotation-editable-cell',
        params.data && flashed.current.has(params.data.order_line_id) ? 'quotation-repriced-cell' : ''],
      comparator: (a, b) => Number((priceCents(a || '') ?? BigInt(-1)) - (priceCents(b || '') ?? BigInt(-1))) },
    { colId: 'price_source', headerName: 'Origen del precio', width: 150, minWidth: 140, sortable: false, hide: !autosave,
      valueGetter: params => params.data ? originLabel(params.data) : '',
      cellRenderer: ({ data, value }: CustomCellRendererProps<QuoteRow, string>) => data ? <button type="button" className={`quotation-origin-chip ${(value || '').replace(/\s+/g, '-').toLowerCase()}`}
        aria-label={`¿De dónde sale el precio de ${data.codigo}? ${value}`} onClick={() => openDrawer.current(data.order_line_id)}>{value}</button> : null },
    { colId: 'line_total', headerName: 'Importe', width: 155, minWidth: 145, type: 'numericColumn', valueGetter: params => params.data ? rowCents(params.data) : BigInt(0),
      valueFormatter: params => formatMoney(params.value ?? BigInt(0)), comparator: (a: bigint, b: bigint) => a === b ? 0 : a < b ? -1 : 1 },
    { colId: 'alerts', headerName: 'Alertas', width: 175, minWidth: 160, sortable: false,
      valueGetter: params => params.data ? alertLabel(alertsRef.current.lines[params.data.order_line_id] || []) : '',
      tooltipValueGetter: params => (params.data && alertsRef.current.lines[params.data.order_line_id] || []).map(item => item.message).join(' · ') || undefined,
      cellClass: params => {
        const items = params.data ? alertsRef.current.lines[params.data.order_line_id] || [] : [];
        return ['quotation-alert-cell', items.some(item => item.severity === 'block') ? 'block' : items.some(item => item.severity === 'confirm' && !item.acknowledged) ? 'confirm' : ''];
      },
      onCellClicked: (event: CellClickedEvent<QuoteRow>) => { if ((alertsRef.current.lines[event.data?.order_line_id || ''] || []).length) setPanelOpen(true); } },
  // eslint-disable-next-line react-hooks/exhaustive-deps
  ], [disabled, formatMoney, edited, invalidTooltip, autosave]);
  const defaultColumn = useMemo<ColDef<QuoteRow>>(() => ({ sortable: true, resizable: true, cellDataType: false, wrapHeaderText: true, autoHeaderHeight: true }), []);
  useEffect(() => { if (disabled) grid.current?.api?.stopEditing(true); }, [disabled]);
  const pricingSeen = useRef(pricingRevision);
  useEffect(() => {
    if (pricingSeen.current === pricingRevision) return;
    pricingSeen.current = pricingRevision; void refreshPricing();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pricingRevision]);
  useEffect(() => {
    mounted.current = true;
    const tick = setInterval(() => setNow(Date.now()), 30000);
    if (!autosave) return () => { mounted.current = false; clearInterval(tick); };
    const warn = (event: BeforeUnloadEvent) => { if (base.current && (inFlight.current || pendingChanges().dirty)) { event.preventDefault(); event.returnValue = ''; } };
    window.addEventListener('beforeunload', warn);
    window.addEventListener('pagehide', flushInBackground);
    return () => {
      // Leaving the order inside the app keeps the last edits too.
      flushInBackground();
      mounted.current = false; clearInterval(tick);
      if (timer.current) { clearTimeout(timer.current); timer.current = null; }
      window.removeEventListener('beforeunload', warn); window.removeEventListener('pagehide', flushInBackground);
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autosave]);
  function ready(event: GridReadyEvent<QuoteRow>) {
    const dialog = host.current?.closest('dialog');
    if (dialog) event.api.setGridOption('popupParent', dialog);
  }
  function validLines() {
    const lines = [];
    for (const row of rowsRef.current) {
      const quantity = quantityValue(row.quantity);
      const cents = quantity === 0 && !row.unit_price.trim() ? BigInt(0) : priceCents(row.unit_price);
      if (quantity === null || cents === null) {
        setError(quantity === null ? `Revisa las unidades de ${row.codigo}. Deben ser un número entero entre 0 y 9.999.` : `Revisa el precio de ${row.codigo}. Introduce un importe positivo o cero, con un máximo de dos decimales.`);
        const node = grid.current?.api?.getRowNode(row.order_line_id);
        if (node?.rowIndex != null) { grid.current?.api?.ensureIndexVisible(node.rowIndex); grid.current?.api?.ensureColumnVisible(quantity === null ? 'quantity' : 'unit_price'); grid.current?.api?.setFocusedCell(node.rowIndex, quantity === null ? 'quantity' : 'unit_price'); }
        return null;
      }
      lines.push({ order_line_id: row.order_line_id, quantity, unit_price: decimal(cents) });
    }
    if (!lines.some(line => line.quantity > 0)) { setError('Ofrece al menos una unidad para enviar la cotización.'); return null; }
    return lines;
  }
  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (disabled || publishing) return;
    grid.current?.api?.stopEditing();
    const lines = validLines();
    if (!lines) return;
    if (!autosave) {
      const outcome = await onSend({ currency: header.current.currency, terms: header.current.terms, lines });
      const refused = publishAlerts(outcome);
      if (refused && mounted.current) { showAlerts(alertsFromPublish(refused)); focusRow(refused.find(item => item.order_line_id)?.order_line_id); }
      return;
    }
    if (conflictRef.current) { setError('Carga la versión más reciente del borrador antes de enviar la cotización.'); return; }
    setPublishing(true); setError('');
    try {
      // Publishing is bound to the saved draft: flush every edit first, then send its draft_version.
      if (!await save(true)) { if (mounted.current && !conflictRef.current) setError('No se pudo guardar el borrador. Reintenta antes de enviar la cotización.'); return; }
      // Read the values again: edits made during the flush were saved too, and the payload must equal the draft.
      const flushed = validLines();
      if (!flushed) return;
      const outcome = await onSend({ currency: header.current.currency, terms: header.current.terms, lines: flushed, draft_version: base.current!.version });
      if (outcome === true) return;
      // Alerts not yet reviewed: open the panel with the server's live view and point at the first affected line.
      const refused = publishAlerts(outcome);
      await verify();
      if (refused && mounted.current) focusRow(refused.find(item => item.order_line_id)?.order_line_id);
    } finally { if (mounted.current) setPublishing(false); }
  }
  function publishAlerts(outcome: true | ApiError | false) {
    const items = outcome instanceof ApiError && outcome.status === 409 ? (outcome.body as { exceptions?: PublishException[] } | null)?.exceptions : undefined;
    if (!items || !mounted.current) return undefined;
    setError(outcome instanceof ApiError ? outcome.detail : ''); setPanelOpen(true);
    requestAnimationFrame(() => panel.current?.scrollIntoView({ block: 'nearest' }));
    return items;
  }
  const busy = disabled || publishing || repricing;
  const overStock = rows.filter(row => {
    const quantity = quantityValue(row.quantity), available = stock.current[row.order_line_id]?.available_quantity;
    return quantity !== null && available !== undefined && quantity > available;
  }).length;
  const flat = [...alerts.order.map(item => ({ lineId: null as string | null, codigo: 'Cotización', item })),
    ...rows.flatMap(row => (alerts.lines[row.order_line_id] || []).map(item => ({ lineId: row.order_line_id as string | null, codigo: row.codigo, item })))]
    .sort((a, b) => severityRank[a.item.severity] - severityRank[b.item.severity]);
  const confirmed = (lineId: string | null, item: DraftException) => lineId ? ackState(lineId, item) : item.acknowledged;
  const toReview = flat.filter(({ lineId, item }) => item.severity === 'block' || (item.severity === 'confirm' && !confirmed(lineId, item))).length;
  const blocking = flat.some(({ item }) => item.severity === 'block');
  const drawerRow = drawerLine ? rows.find(row => row.order_line_id === drawerLine) : undefined;
  const status = !autosave ? '' : saveState === 'conflict' ? 'Guardado automático en pausa' : saveState === 'saving' || saveState === 'pending' ? 'Guardando borrador…'
    : saveState === 'error' ? `Cambios sin guardar${saveError ? ` · ${saveError}` : ''}` : invalidCells ? 'Corrige las celdas marcadas para guardarlas.'
    : saved?.persisted ? `Borrador v${saved.version} · guardado por ${saved.updatedBy} ${ago(saved.updatedAt, now)}` : 'Borrador nuevo · se guarda automáticamente al editar';
  return <><form className="deal-quote-editor" onSubmit={submit} autoComplete="off">
    <h3>{deal.quotation ? 'Preparar versión ajustada' : 'Convertir orden en cotización'}</h3>
    <p>Edita las unidades ofrecidas y el precio de cada artículo. Usa 0 para artículos no disponibles. Al enviar, confirmas la cotización y sus condiciones quedan bloqueadas.</p>
    {autosave ? <div className={`quotation-draft-bar ${saveState}`}>
      <span role="status" aria-label="Estado del borrador">{saveState === 'saving' || saveState === 'pending' ? <LoaderCircle size={14} className="spin"/> : null}{status}</span>
      {saveState === 'error' && <button type="button" className="button soft small" disabled={busy} onClick={() => void save(true)}>Reintentar</button>}
      {saved?.persisted && saveState !== 'conflict' && !confirmDiscard && <button type="button" className="button soft small" disabled={busy || saveState === 'saving'} onClick={() => setConfirmDiscard(true)}><RotateCcw size={14}/>Descartar borrador</button>}
      {confirmDiscard && <span className="quotation-draft-discard">¿Descartar el borrador guardado?<button type="button" disabled={busy} onClick={() => void discard()}>Sí, descartar</button><button type="button" onClick={() => setConfirmDiscard(false)}>Cancelar</button></span>}
    </div> : <div className="notice quotation-draft-fallback" role="status"><CloudOff size={16}/>No se pudo cargar el borrador guardado; tus cambios no se guardarán automáticamente.</div>}
    {conflict && <div className="notice error quotation-draft-conflict" role="alert"><span>Otro miembro de tu equipo modificó este borrador</span><button type="button" onClick={() => replaceWith(conflict)}>Cargar la versión más reciente</button></div>}
    <div className="quotation-grid-guide"><span>Haz clic en una celda para editar · Tab para avanzar</span>
      {autosave && !!pricing?.fillable && <button type="button" className="button soft small" disabled={busy || !!conflict} onClick={() => void reprice('blank')}><Sparkles size={14}/>Aplicar precios sugeridos ({pricing.fillable})</button>}
      {autosave && !!pricing?.stale && <button type="button" className="button soft small" disabled={busy || !!conflict} title="Tu lista cambió desde que se calcularon estos precios" onClick={() => void reprice('engine')}><RefreshCcw size={14}/>Recalcular precios sugeridos ({pricing.stale})</button>}
      {overStock > 0 && <button type="button" className="button soft small" disabled={busy}
      title={`Ofrece como máximo lo disponible en ${overStock} ${overStock === 1 ? 'artículo' : 'artículos'}`} onClick={adjustToStock}><PackageMinus size={14}/>Ajustar a disponibles</button>}
      {flat.length > 0 && <button type="button" className={`quotation-alert-chip ${blocking ? 'block' : toReview ? 'confirm' : ''}`} aria-expanded={panelOpen} aria-controls="quotation-review-panel"
        onClick={() => { setPanelOpen(value => !value); requestAnimationFrame(() => panel.current?.scrollIntoView({ block: 'nearest' })); }}><TriangleAlert size={13}/>{toReview ? `${toReview} por revisar` : 'Alertas revisadas'}</button>}
      <strong>{completed} de {rows.length} {rows.length === 1 ? 'artículo completo' : 'artículos completos'}</strong></div>
    {priceNotice && <div className="notice success quotation-price-notice" role="status">{priceNotice}</div>}
    <div ref={host} className="quotation-grid" aria-label="Artículos de la cotización" style={{ height: Math.min(450, Math.max(170, rows.length * 56 + 60)) }}>
      <AgGridReact<QuoteRow> ref={grid} theme={motionGridTheme} localeText={gridLocale} rowData={rows} columnDefs={columns} defaultColDef={defaultColumn}
        getRowId={rowId} singleClickEdit stopEditingWhenCellsLoseFocus
        onGridReady={ready} cellSelection={!disabled} suppressClipboardPaste={disabled}
        ensureDomOrder suppressColumnVirtualisation enableBrowserTooltips loadThemeGoogleFonts={false}/>
    </div>
    {flat.length > 0 && <details ref={panel} id="quotation-review-panel" className="quotation-exceptions" open={panelOpen} onToggle={event => setPanelOpen(event.currentTarget.open)}>
      <summary>Revisa antes de enviar ({toReview})</summary>
      <ul aria-label="Alertas de la cotización">{flat.map(({ lineId, codigo, item }) => {
        const checked = confirmed(lineId, item);
        return <li key={`${lineId}|${item.code}`} className={`quotation-exception ${item.severity}${item.severity === 'confirm' && checked ? ' confirmed' : ''}`}>
          <div>{lineId ? <button type="button" onClick={() => focusRow(lineId)}>{codigo}</button> : <strong>{codigo}</strong>}<span>{item.message}</span></div>
          {item.severity === 'block' && <small>Corrige para continuar</small>}
          {item.code === 'differs_from_list' && lineId && alerts.live && <button type="button" className="quotation-exception-action" disabled={busy || !!conflict}
            onClick={() => void reprice('lines', [lineId])}>Usar precio actual</button>}
          {item.severity === 'confirm' && <label><input type="checkbox" checked={checked} disabled={!alerts.live || !lineId || busy || !!conflict}
            aria-label={`Confirmo: ${codigo} · ${item.message}`} onChange={event => { if (lineId) toggleAck(lineId, item, event.target.checked); }}/>Confirmo</label>}
        </li>;
      })}</ul>
      {!alerts.live && <p>Sin el borrador guardado no puedes confirmar alertas aquí. Corrige las marcadas y vuelve a enviar.</p>}
    </details>}
    {error && <div className="notice error" role="alert">{error}</div>}
    <div className="deal-editor-total"><label>Moneda<select aria-label="Moneda de la cotización" disabled={disabled} value={currency} onChange={event => { header.current = { ...header.current, currency: event.target.value as Currency }; setCurrency(event.target.value as Currency); schedule(); }}><option value="USD">USD</option><option value="PAB">PAB</option></select></label><strong>Total: {formatMoney(total)}</strong></div>
    <label>Condiciones de entrega y cotización<UppercaseTextarea disabled={disabled} maxLength={5000} value={terms} onChange={event => { header.current = { ...header.current, terms: event.target.value }; setTerms(event.target.value); schedule(); }} placeholder="PLAZO DE ENTREGA, RETIRO Y OTRAS CONDICIONES…"/>
      {autosave && termsOrigin === 'profile' && normalTerms(terms) === saved?.terms && <small className="quotation-terms-origin">Condiciones predeterminadas del perfil del cliente. Revísalas antes de enviar.</small>}</label>
    <button type="submit" className="button primary" disabled={busy}>{busy ? <LoaderCircle size={16} className="spin"/> : <Send size={16}/>}Confirmar y enviar cotización</button>
  </form>
  {/* Outside the form: the drawer's buttons must never submit the quotation. */}
  {drawerRow && pricingRef.current.lines[drawerRow.order_line_id] && <PriceDrawer row={drawerRow} info={pricingRef.current.lines[drawerRow.order_line_id]} currency={currency}
    origin={originLabel(drawerRow)} busy={busy || !!conflict} onClose={() => setDrawerLine(null)}
    onUse={async () => { if (await reprice('lines', [drawerRow.order_line_id])) setDrawerLine(null); }}/>}
  </>;
}

// "¿De dónde sale este precio?": the supplier-only explanation of the live suggestion, step by step, as the engine computed it.
function PriceDrawer({ row, info, currency, origin, busy, onClose, onUse }: { row: QuoteRow; info: Pricing['lines'][string]; currency: string; origin: string; busy: boolean;
  onClose: () => void; onUse: () => void }) {
  const suggestion = info.suggestion, explanation = suggestion?.explanation;
  const current = cellPrice(row.unit_price) ?? null, usable = !!suggestion?.unit_price && (current !== suggestion.unit_price || info.source !== 'engine');
  return <Modal drawer title="¿De dónde sale este precio?" onClose={onClose} className="price-drawer-modal">
    <div className="price-drawer">
      <p className="price-drawer-item"><strong>{row.codigo}</strong>{row.description && <small>{row.description}</small>}</p>
      <dl>
        <div><dt>En el borrador</dt><dd>{row.unit_price.trim() ? money(current, currency) : 'Sin precio'} · {origin}</dd></div>
        <div><dt>Precio sugerido</dt><dd>{suggestion?.unit_price ? money(suggestion.unit_price, currency) : 'Sin precio sugerido'}</dd></div>
      </dl>
      {!suggestion ? <p className="price-drawer-note">Todavía no tienes listas de precios. Créalas en Precios › Listas de precios para recibir un precio sugerido en cada cotización.</p> : <>
        <h4>Cómo se calculó</h4>
        <PriceCalculation explanation={explanation!} currency={currency}/>
        {info.source === 'previous' && <p className="price-drawer-note">El precio del borrador viene de la versión anterior de la cotización.</p>}
      </>}
      {usable && <button type="button" className="button primary" disabled={busy} onClick={onUse}><Sparkles size={15}/>Usar precio sugerido</button>}
      <p className="form-footnote">Solo tu equipo ve este cálculo. El cliente recibe únicamente el precio que envíes.</p>
    </div>
  </Modal>;
}
