'use client';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { ColDef, GridApi } from 'ag-grid-community';
import type { CustomCellRendererProps } from 'ag-grid-react';
import { BrainCircuit, Check, LoaderCircle, PackageCheck, Search, Sparkles, X } from 'lucide-react';
import Modal from './modal';
import ServerGrid from './server-grid';
import PartTypePicker from './part-type-picker';
import { PartPicker } from './admin-catalog-editors';
import ReferencesEditor from './admin-references-editor';
import type { OEMLine } from '@/lib/line-types';
import { UppercaseInput } from './uppercase-field';
import NewProductsReview from './admin-new-products-review';
import { ApiError, request } from '@/lib/types';
import { AiReview, BulkResult, FieldSource, Proposal, ProposalStatus, ProposalSummary, proposalStatusLabels, rejectReasons, sourceLabels, verdictLabels } from '@/lib/new-product-types';

const base = '/api/management/catalog/proposals';
const number = (value: number) => value.toLocaleString('es-PA');
const errorText = (error: unknown) => error instanceof Error ? error.message : 'No se pudo guardar. Inténtalo de nuevo.';
const statusClass: Partial<Record<ProposalStatus, string>> = { approved: 'matched', linked: 'matched', rejected: 'review' };
// The grid keeps the context it was created with: busy is read through a function, and the actions column is redrawn when it changes.
type Actions = { open: (row: Proposal) => void; approve: (row: Proposal) => void; busy: () => string };
type Cell = CustomCellRendererProps<Proposal, unknown, Actions>;

function SkuCell({ data }: Cell) {
  if (!data) return null;
  const genuine = data.oem && data.oem.code.toUpperCase() === data.sku.toUpperCase();
  return <div className="admin-grid-stack"><strong>{data.part?.sku && data.part.sku !== data.sku ? `${data.sku} → ${data.part.sku}` : data.sku}</strong>
    <small>{data.earlier ? `Ya existe como ${data.earlier.sku}` : genuine ? `OEM ${data.oem!.brand}` : data.brand ? `Código del proveedor · ${data.brand}` : 'Código del proveedor'}</small></div>;
}
function OemCell({ data }: Cell) {
  if (!data) return null;
  return data.oem ? <div className="admin-grid-stack"><strong>{data.oem.code}</strong><small>{data.oem.brand}</small></div> : <span className="admin-grid-muted">SIN OEM</span>;
}
function SuppliersCell({ data, context }: Cell) {
  if (!data) return null;
  const [first] = data.items;
  return <button type="button" className="admin-grid-link" aria-label={`Ver artículos de ${data.sku}`} onClick={() => context.open(data)}>
    <span className="admin-grid-stack"><strong>{first ? `${first.supplier_name} · ${first.codigo}` : '—'}</strong>
      <small>{data.item_count} {data.item_count === 1 ? 'artículo' : 'artículos'} · {data.supplier_count} {data.supplier_count === 1 ? 'proveedor' : 'proveedores'}</small></span></button>;
}
// The latest Revisión IA verdict: BUENA / DUDOSA / MALA with its confidence, or CAMBIÓ once the proposal changed since.
function aiPill(review: AiReview | null) {
  if (!review) return null;
  const kind = review.stale ? 'stale' : review.verdict;
  return <span className={`review-ai-pill ${kind}`} title={review.reasons.join(' ')}>{verdictLabels[kind]}{review.stale ? '' : ` · ${Math.round(review.confidence * 100)} %`}</span>;
}
function AiCell({ data }: Cell) {
  if (!data) return null;
  return data.ai_review ? <div className="admin-grid-stack">{aiPill(data.ai_review)}<small title={data.ai_review.reasons.join(' ')}>{data.ai_review.reasons[0] ?? ''}</small></div>
    : <span className="admin-grid-muted">{data.status === 'pending' ? 'SIN REVISAR' : '—'}</span>;
}
function ActionsCell({ data, context }: Cell) {
  if (!data) return null;
  const busy = context.busy();
  return <div className="admin-grid-actions">
    {data.status === 'pending' && <button type="button" className="button primary small" disabled={!!busy} aria-label={`Aprobar ${data.sku}`} onClick={() => context.approve(data)}>
      {busy === data.id ? <LoaderCircle className="spin" size={14}/> : <Check size={14}/>}Aprobar</button>}
    <button type="button" className="button ghost small" aria-label={`Ver detalle de ${data.sku}`} onClick={() => context.open(data)}>Detalle</button></div>;
}

const columns: ColDef<Proposal>[] = [
  { colId: 'sku', headerName: 'SKU propuesto', minWidth: 190, flex: 1, cellRenderer: SkuCell },
  { colId: 'oem', headerName: 'OEM', width: 150, cellRenderer: OemCell },
  { colId: 'description', headerName: 'Descripción', field: 'description', flex: 1.2, minWidth: 200, cellClass: 'admin-grid-text', tooltipField: 'description' },
  { colId: 'ai', headerName: 'Revisión IA', width: 200, cellRenderer: AiCell, sortable: false },
  { colId: 'subgroup', headerName: 'Grupo / subgrupo', width: 160, cellClass: 'admin-grid-text',
    valueGetter: ({ data }) => data ? data.part_type ? `${data.part_type.category} / ${data.part_type.name}` : 'POR INVESTIGAR' : '' },
  { colId: 'codes', headerName: 'Alternos', width: 130, cellClass: 'admin-grid-text',
    valueGetter: ({ data }) => data ? data.references.length ? `${data.references.slice(0, 2).map(ref => ref.brand ? `${ref.brand}:${ref.code}` : ref.code).join(' · ')}${data.references.length > 2 ? ` · +${data.references.length - 2}` : ''}` : '—' : '' },
  { colId: 'suppliers', headerName: 'Proveedor / artículo', width: 220, cellRenderer: SuppliersCell },
  { colId: 'stock', headerName: 'Existencias', field: 'stock', width: 110, type: 'rightAligned', valueFormatter: ({ value }) => number(value ?? 0), cellClass: ['ag-right-aligned-cell', 'admin-grid-number'] },
  { colId: 'status', headerName: 'Estado', width: 130, cellRenderer: ({ data }: Cell) => data ? <span className={`status ${statusClass[data.status] || ''}`}>{data.status_label}</span> : null },
  { colId: 'actions', headerName: 'Acciones', width: 180, pinned: 'right', cellRenderer: ActionsCell, suppressHeaderMenuButton: true, resizable: false },
];
const ordering = { sku: 'sku', description: 'description', stock: 'stock_total', status: 'status', suppliers: 'item_total' };

// Productos nuevos: the SKU each unmatched supplier product would become. Approving creates it (the rules of «Crear SKU») and links its
// supplier items; nothing reaches the catalog without that. Grupo, subgrupo and the rest can be filled here, or later by the AI.
export default function AdminNewProducts({ onCount, onChanged }: { onCount: (count: number | null) => void; onChanged: () => void }) {
  const [state, setState] = useState<ProposalStatus | 'all'>('pending');
  const [search, setSearch] = useState(''); const [query, setQuery] = useState('');
  const [summary, setSummary] = useState<ProposalSummary | null>(null);
  const [revision, setRevision] = useState(0); const [count, setCount] = useState<number | null>(null);
  const [selected, setSelected] = useState<Proposal[]>([]);
  const [detail, setDetail] = useState<string | null>(null);
  const [ai, setAi] = useState('');   // Revisión IA filter: good, unsure, bad, stale (changed since) or none (not reviewed)
  const [reviewing, setReviewing] = useState(false);
  const [reviewRefresh, setReviewRefresh] = useState(0); const [reviewNotice, setReviewNotice] = useState('');   // a drawer opened from the report
  const [busy, setBusy] = useState(''); const [notice, setNotice] = useState(''); const [error, setError] = useState('');
  const grid = useRef<GridApi<Proposal> | null>(null);
  const busyRef = useRef(''); const inFlight = useRef(false);
  useEffect(() => { const timer = setTimeout(() => setQuery(search.trim()), 300); return () => clearTimeout(timer); }, [search]);
  useEffect(() => { busyRef.current = busy; grid.current?.refreshCells({ columns: ['actions'], force: true }); }, [busy]);
  useEffect(() => { grid.current?.deselectAll(); setSelected([]); }, [state, query, ai]);   // another list: nothing selected in it
  useEffect(() => {
    let cancelled = false;
    request<ProposalSummary>(`${base}/summary`).then(data => { if (!cancelled) { setSummary(data); onCount(data.pending); } }).catch(() => { if (!cancelled) onCount(null); });
    return () => { cancelled = true; };
  }, [revision, onCount]);
  const changed = useCallback((text: string) => { setNotice(text); setError(''); setSelected([]); grid.current?.deselectAll(); setRevision(value => value + 1); onChanged(); }, [onChanged]);
  const approve = useCallback(async (row: Proposal) => {
    if (inFlight.current) return;
    inFlight.current = true; setBusy(row.id); setError(''); setNotice('');
    try { const result = await request<Proposal>(`${base}/${row.id}`, { method: 'POST', body: JSON.stringify({ action: 'approve', expected_version: row.version }) }); changed(`SKU ${result.part?.sku ?? row.sku} publicado y vinculado a ${row.item_count} ${row.item_count === 1 ? 'artículo' : 'artículos'}.`); }
    catch (error) { setError(errorText(error)); setRevision(value => value + 1); }
    finally { setBusy(''); inFlight.current = false; }
  }, [changed]);
  async function approveSelected() {
    if (inFlight.current) return;
    const rows = grid.current?.getSelectedRows() ?? selected;   // the rows as the grid has them now: a refresh brings their current version
    if (!rows.length) return;
    inFlight.current = true; setBusy('bulk'); setError(''); setNotice('');
    try {
      const result = await request<BulkResult>(`${base}/bulk`, { method: 'POST', body: JSON.stringify({ action: 'approve', proposals: rows.map(row => ({ id: row.id, version: row.version })) }) });
      const failed = result.results.filter(entry => !entry.ok);
      const done = result.done ?? result.approved;
      changed(`${done} ${done === 1 ? 'SKU publicado' : 'SKU publicados'}.${failed.length ? ` ${failed.length} no se aprobaron: ${failed.map(entry => `${rows.find(row => row.id === entry.id)?.sku ?? entry.id} (${entry.message})`).join('; ')}` : ''}`);
    } catch (error) { setError(errorText(error)); }
    finally { setBusy(''); inFlight.current = false; }
  }
  const context = useMemo<Actions>(() => ({ open: row => setDetail(row.id), approve, busy: () => busyRef.current }), [approve]);
  const params = useMemo(() => ({ status: state, search: query, ...(ai && state === 'pending' ? { ai } : {}) }), [state, query, ai]);
  const gridProps = useMemo(() => ({
    rowSelection: { mode: 'multiRow' as const, checkboxes: true, headerCheckbox: false, enableClickSelection: false, isRowSelectable: (node: { data?: Proposal }) => node.data?.status === 'pending' },
    onSelectionChanged: (event: { api: GridApi<Proposal> }) => setSelected(event.api.getSelectedRows()),
  }), []);
  return <div className="new-products">
    <div className="admin-inventory-tabs" role="group" aria-label="Estado de los productos nuevos">
      {(['pending', 'approved', 'linked', 'rejected', 'resolved', 'all'] as const).map(value => <button type="button" key={value} className={state === value ? 'selected' : ''} aria-pressed={state === value}
        onClick={() => setState(value)}>{value === 'all' ? 'Todos' : proposalStatusLabels[value]}{value !== 'all' && summary ? ` · ${number(summary[value])}` : ''}</button>)}
    </div>
    <div className="inventory-filters">
      <div className="catalog-search admin-search"><Search size={19}/><label className="sr-only" htmlFor="new-products-search">Buscar productos nuevos</label>
        <UppercaseInput id="new-products-search" value={search} onChange={event => setSearch(event.target.value)} placeholder="SKU, OEM, DESCRIPCIÓN, CÓDIGO O PROVEEDOR…"/></div>
      {state === 'pending' && <label className="new-products-ai-filter">Revisión IA<select value={ai} onChange={event => setAi(event.target.value)}>
        <option value="">Todas</option><option value="good">Buenas</option><option value="unsure">Dudosas</option><option value="bad">Malas</option>
        <option value="stale">Cambiaron desde la revisión</option><option value="none">Sin revisar</option></select></label>}
      <span className="inventory-count" aria-live="polite">{count === null ? 'Cargando…' : `${number(count)} ${count === 1 ? 'producto' : 'productos'}`}</span>
      <button type="button" className="button primary small" onClick={() => setReviewing(true)}><BrainCircuit size={15}/>Revisión IA</button>
    </div>
    {notice && <div className="notice success" role="status"><Check size={16}/>{notice}<button type="button" aria-label="Cerrar aviso" onClick={() => setNotice('')}><X size={14}/></button></div>}
    {error && <div className="notice error" role="alert">{error}</div>}
    {selected.length > 0 && <div className="oem-bulk-bar" role="group" aria-label="Seleccionadas"><strong>{selected.length} {selected.length === 1 ? 'propuesta seleccionada' : 'propuestas seleccionadas'}</strong>
      <button type="button" className="button primary small" disabled={!!busy} onClick={approveSelected}>{busy === 'bulk' ? <LoaderCircle className="spin" size={14}/> : <PackageCheck size={14}/>}Aprobar seleccionadas</button>
      <button type="button" className="button text small" onClick={() => { grid.current?.deselectAll(); setSelected([]); }}>Quitar selección</button></div>}
    <ServerGrid<Proposal> storageKey="admin-new-products" label="Productos nuevos" path={base} params={params} columns={columns} rowId={row => row.id} ordering={ordering}
      revision={revision} rowHeight={62} context={context} onCount={setCount} gridProps={gridProps} onReady={api => { grid.current = api; }}
      empty={<div className="empty-state"><Sparkles size={30}/><h3>{state === 'pending' ? 'No hay productos nuevos por revisar.' : 'No hay productos en este estado.'}</h3>
        <p>Los artículos de proveedores que no coinciden con ningún SKU aparecen aquí después de cada análisis de coincidencias.</p></div>}/>
    {reviewing && <NewProductsReview search={query} onClose={() => { setReviewing(false); setRevision(value => value + 1); }} onChanged={() => { setRevision(value => value + 1); onChanged(); }}
      onOpen={setDetail} refresh={reviewRefresh} message={reviewNotice}/>}
    {detail && <ProposalDetail id={detail} onClose={() => setDetail(null)} onSaved={() => { setRevision(value => value + 1); if (reviewing) { setReviewNotice(''); setReviewRefresh(value => value + 1); } }}
      onDone={text => { setDetail(null); changed(text); if (reviewing) { setReviewNotice(text); setReviewRefresh(value => value + 1); } }}/>}
  </div>;
}

function Source({ source }: { source?: FieldSource }) {
  if (!source) return null;
  const confidence = source.source === 'ai' && typeof source.confidence === 'number' ? ` · ${Math.round(source.confidence * 100)} %` : '';
  return <small className={`field-source${source.source === 'ai' ? ' ai' : ''}`} title={source.note || undefined}>{sourceLabels[source.source]}{source.by ? ` por ${source.by}` : ''}{confidence}</small>;
}

export function ProposalDetail({ id, onClose, onSaved, onDone }: { id: string; onClose: () => void; onSaved: () => void; onDone: (text: string) => void }) {
  const [proposal, setProposal] = useState<Proposal | null>(null);
  const [form, setForm] = useState({ sku: '', description: '', oem_code: '', oem_brand: '', internal_code: false });
  // The product is a line in preparación (S6): its References are edited one by one in the References editor (plan S8.9).
  const [line, setLine] = useState<OEMLine | null>(null); const [lineError, setLineError] = useState('');
  const [partType, setPartType] = useState<{ id: string; category: string; name: string } | null>(null);
  const [mode, setMode] = useState<'' | 'link' | 'reject'>(''); const [part, setPart] = useState('');
  const [reason, setReason] = useState('duplicate'); const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false); const [error, setError] = useState('');
  const [owners, setOwners] = useState<{ id: string; sku: string; active: boolean }[]>([]);   // the catalog SKUs an approval was refused for
  const load = useCallback((data: Proposal) => {
    setProposal(data); setPartType(data.part_type); setOwners([]);   // fresh data: the server's catalog_match says it from now on
    setForm({ sku: data.sku, description: data.description, oem_code: data.oem?.code ?? '', oem_brand: data.oem?.brand ?? '', internal_code: data.internal_code });
  }, []);
  useEffect(() => { let cancelled = false; request<Proposal>(`${base}/${id}`).then(data => { if (!cancelled) load(data); }).catch(error => { if (!cancelled) setError(errorText(error)); }); return () => { cancelled = true; }; }, [id, load]);
  const lineVersion = proposal?.version;
  useEffect(() => { let cancelled = false; setLineError(''); request<OEMLine>(`/api/management/lines/${id}`).then(data => { if (!cancelled) setLine(data); }).catch(error => { if (!cancelled) setLineError(errorText(error)); }); return () => { cancelled = true; }; }, [id, lineVersion]);
  const pending = proposal?.status === 'pending';
  const dirty = !!proposal && (form.sku !== proposal.sku || form.description !== proposal.description || form.oem_code !== (proposal.oem?.code ?? '')
    || form.oem_brand !== (proposal.oem?.brand ?? '') || (partType?.id ?? null) !== (proposal.part_type?.id ?? null)
    || form.internal_code !== proposal.internal_code);
  async function call<T>(run: () => Promise<T>) {
    setBusy(true); setError('');
    try { return await run(); }
    catch (error) {
      if (error instanceof ApiError && error.status === 409 && (error.body as { proposal?: Proposal })?.proposal) { load((error.body as { proposal: Proposal }).proposal); onSaved(); }
      if (error instanceof ApiError && Array.isArray((error.body as { owners?: unknown })?.owners)) setOwners((error.body as { owners: { id: string; sku: string; active: boolean }[] }).owners);
      setError(errorText(error)); return null;
    } finally { setBusy(false); }
  }
  async function save() {
    if (!proposal) return null;
    // Only what the administrator changed: a field sent becomes theirs, and the suppliers' data (or the AI) no longer refills it.
    const changes: Record<string, unknown> = {};
    if (form.sku !== proposal.sku) changes.sku = form.sku;
    if (form.description !== proposal.description) changes.description = form.description;
    if (form.oem_code !== (proposal.oem?.code ?? '') || form.oem_brand !== (proposal.oem?.brand ?? '')) Object.assign(changes, { oem_code: form.oem_code, oem_brand: form.oem_brand });
    if ((partType?.id ?? null) !== (proposal.part_type?.id ?? null)) changes.part_type = partType?.id ?? null;
    if (form.internal_code !== proposal.internal_code) changes.internal_code = form.internal_code;
    const data = await call(() => request<Proposal>(`${base}/${proposal.id}`, { method: 'PATCH', body: JSON.stringify({ expected_version: proposal.version, ...changes }) }));
    if (data) { load(data); onSaved(); }
    return data;
  }
  async function decide(action: 'approve' | 'link' | 'reject', target = part) {
    let current = proposal;
    if (!current) return;
    if (dirty) { current = await save(); if (!current) return; }
    const body = { action, expected_version: current.version, ...(action === 'link' ? { part: target } : {}), ...(action === 'reject' ? { reason, note } : {}) };
    const data = await call(() => request<Proposal>(`${base}/${current!.id}`, { method: 'POST', body: JSON.stringify(body) }));
    if (data) onDone(action === 'approve' ? `SKU ${data.part?.sku} publicado y vinculado.` : action === 'link' ? `Artículos vinculados a ${data.part?.sku}.` : `Propuesta ${data.sku} rechazada.`);
  }
  return <Modal title={`Producto nuevo · ${proposal?.sku ?? ''}`} onClose={onClose} wide drawer className="side-panel">
    {!proposal && !error && <div className="loading"><LoaderCircle className="spin"/>Cargando…</div>}
    {error && <div className="notice error" role="alert">{error}</div>}
    {proposal && <div className="stack-form new-product-detail">
      {pending && !proposal.earlier && (proposal.catalog_match?.length || owners.length) ? <div className="notice new-product-earlier" role="note">
        Este código ya está en el catálogo: los artículos son variantes de ese SKU, vincúlalos ahí.
        {(proposal.catalog_match?.length ? proposal.catalog_match : owners).map(match => match.active
          ? <button key={match.id} type="button" className="button primary small" disabled={busy} onClick={() => decide('link', match.id)}>Vincular a {match.sku}</button>
          : <span key={match.id}>{match.sku} está retirado: reactívalo para vincular ahí sus artículos.</span>)}</div> : null}
      {pending && proposal.earlier && <div className="notice new-product-earlier" role="note">Este producto ya se vinculó antes al SKU <strong>{proposal.earlier.sku}</strong>: lo más probable es que estos artículos vayan ahí.
        <button type="button" className="button primary small" disabled={busy} onClick={() => decide('link', proposal.earlier!.id)}>Vincular a {proposal.earlier.sku}</button></div>}
      {pending && proposal.ai_review && <AiVerdict review={proposal.ai_review} current={form.description} onUseDescription={value => setForm({ ...form, description: value })}/>}
      <p className="modal-description">{pending ? 'Revisa y completa lo que falte. Al aprobar, esta línea OEM en preparación se publica como SKU y sus artículos se vinculan.' : `${proposal.status_label}${proposal.decided_by ? ` por ${proposal.decided_by}` : ''}${proposal.part ? ` · SKU ${proposal.part.sku}` : ''}${proposal.reject_reason_label ? ` · ${proposal.reject_reason_label}` : ''}${proposal.reject_note ? `: ${proposal.reject_note}` : ''}.`}</p>
      <fieldset disabled={!pending || busy}>
        <label>SKU propuesto <Source source={proposal.field_sources.sku}/><UppercaseInput value={form.sku} onChange={event => setForm({ ...form, sku: event.target.value })}/></label>
        <label>Descripción <Source source={proposal.field_sources.description}/><UppercaseInput value={form.description} onChange={event => setForm({ ...form, description: event.target.value })}/></label>
        <div className="form-row"><label>Número OEM <Source source={proposal.field_sources.oem}/><UppercaseInput value={form.oem_code} onChange={event => setForm({ ...form, oem_code: event.target.value })} placeholder="SIN OEM"/></label>
          <label>Fabricante del OEM<UppercaseInput value={form.oem_brand} onChange={event => setForm({ ...form, oem_brand: event.target.value })} placeholder="TOYOTA, KAIYI…"/></label></div>
        <div className="new-product-subgroup"><span>Grupo / subgrupo <Source source={proposal.field_sources.part_type}/></span><strong>{partType ? `${partType.category} / ${partType.name}` : 'POR INVESTIGAR'}</strong>
          {pending && <PartTypePicker onSelect={type => setPartType({ id: type.id, category: type.category, name: type.name })}/>}</div>
        <label className="admin-checkbox new-product-internal"><input type="checkbox" checked={form.internal_code} onChange={event => setForm({ ...form, internal_code: event.target.checked,
          ...(event.target.checked ? { oem_code: '', oem_brand: '' } : { oem_code: proposal.oem?.code ?? '', oem_brand: proposal.oem?.brand ?? '' }) })}/>
          Código interno del proveedor, no es un número OEM (mercancía, herramientas…) <Source source={proposal.field_sources.internal_code}/></label>
      </fieldset>
      <section className="new-product-references" aria-label="Referencias de la línea OEM"><h3>Referencias <Source source={proposal.field_sources.references}/></h3>
        {line ? <ReferencesEditor line={line} onLine={next => { setLine(next); onSaved(); }}/> : lineError ? <div className="notice error" role="alert">{lineError}</div>
          : <div className="loading"><LoaderCircle className="spin" size={16}/>Cargando referencias…</div>}</section>
      <h3>Artículos del proveedor</h3>
      <div className="table-scroll"><table className="new-product-items"><thead><tr><th>Proveedor</th><th>ID</th><th>Código</th><th>Marca</th><th>Descripción</th><th>Disponibles</th><th>Origen</th></tr></thead>
        <tbody>{proposal.items.map(item => <tr key={item.id}><td>{item.supplier_name}</td><td>{item.supplier_invent_id}</td><td><strong>{item.codigo}</strong></td><td>{item.brand}</td>
          <td>{item.description}</td><td>{number(item.available)}</td><td>{item.source === 'apiag' ? 'apiag-cloud' : 'Carga manual'}</td></tr>)}</tbody></table></div>
      {pending && <>
        {mode === 'link' && <div className="new-product-choice"><PartPicker label="SKU del catálogo" value={part} onChange={setPart}/>
          <div className="admin-row-actions"><button type="button" className="button primary small" disabled={!part || busy} onClick={() => decide('link')}>Vincular artículos</button><button type="button" className="button text small" onClick={() => setMode('')}>Cancelar</button></div></div>}
        {mode === 'reject' && <div className="new-product-choice"><label>Motivo<select value={reason} onChange={event => setReason(event.target.value)}>{Object.entries(rejectReasons).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
          <label>Nota (opcional)<textarea rows={2} value={note} onChange={event => setNote(event.target.value)}/></label>
          <div className="admin-row-actions"><button type="button" className="button soft small danger-text" disabled={busy} onClick={() => decide('reject')}>Rechazar propuesta</button><button type="button" className="button text small" onClick={() => setMode('')}>Cancelar</button></div></div>}
        {!mode && <div className="admin-row-actions new-product-actions">
          <button type="button" className="button primary" disabled={busy || !form.sku.trim()} onClick={() => decide('approve')}>{busy ? <LoaderCircle className="spin" size={16}/> : <Check size={16}/>}{dirty ? 'Guardar y aprobar' : 'Aprobar y publicar SKU'}</button>
          {dirty && <button type="button" className="button soft" disabled={busy} onClick={save}>Guardar cambios</button>}
          <button type="button" className="button soft" disabled={busy} onClick={() => setMode('link')}>Vincular a SKU existente</button>
          <button type="button" className="button text danger-text" disabled={busy} onClick={() => setMode('reject')}>Rechazar</button></div>}
      </>}
    </div>}
  </Modal>;
}

// The latest Revisión IA verdict in the drawer: what it concluded and why, and a description it drafted but did not fill.
function AiVerdict({ review, current, onUseDescription }: { review: AiReview; current: string; onUseDescription: (value: string) => void }) {
  const suggested = review.suggestions.description;   // drafted from a sibling, or a fix below the fill threshold: offered, not filled
  const drafted = suggested && suggested.value !== current ? suggested.value : '';
  return <div className={`notice review-ai-verdict ${review.stale ? 'stale' : review.verdict}`} role="note">
    <div><strong>Revisión IA: {aiPill(review)}</strong>{review.stale && <small> La propuesta cambió desde la revisión.</small>}
      <ul>{review.reasons.map(reason => <li key={reason}>{reason}</li>)}</ul>
      {review.suggestions.duplicate_of && <small>Posiblemente igual a {review.suggestions.duplicate_of.kind === 'proposal' ? 'el producto nuevo' : 'la línea OEM'} {review.suggestions.duplicate_of.sku}: decídelo tú.</small>}
      {review.link_part && <small>El catálogo ya tiene este código como {review.link_part.sku}.</small>}
      {review.suggestions.internal_code && <small>La IA cree que su código es interno, no un número OEM ({Math.round(review.suggestions.internal_code.confidence * 100)} %): márcalo si es así.</small>}
      {review.suggestions.part_type && <small>Grupo/subgrupo sugerido: {review.suggestions.part_type.category} / {review.suggestions.part_type.name} ({Math.round(review.suggestions.part_type.confidence * 100)} %).</small>}</div>
    {drafted && <button type="button" className="button soft small" onClick={() => onUseDescription(drafted)}>Usar «{drafted}»</button>}
  </div>;
}
