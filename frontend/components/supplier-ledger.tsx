'use client';
import { useEffect, useMemo, useState } from 'react';
import type { ColDef } from 'ag-grid-community';
import type { CustomCellRendererProps } from 'ag-grid-react';
import { History, LoaderCircle, RefreshCw, Search } from 'lucide-react';
import ServerGrid from './server-grid';
import Modal from './modal';
import { UppercaseInput } from './uppercase-field';
import { request } from '@/lib/types';
import { LedgerChange, LedgerRow, LedgerRun, actionLabels, kindLabels, originLabels } from '@/lib/ledger-types';

// Libro de movimientos del proveedor (mall.inventory_history), for the supplier's own panel and for Administración. "movements" lists every
// stock movement: what confirmed quotations reserved, discounted, found missing or released, and what each load (Excel, apiag-cloud, manual)
// moved, with the balance after it. "runs" lists the loads that changed something, and each one opens what it did to every item.
export type LedgerView = 'movements' | 'runs';
// Horizontal tabs: the arrows move to the next or previous tab, Home and End to the first and last -> the tab to select, or null.
export function arrowTab<T extends string>(keys: readonly T[], current: T, event: React.KeyboardEvent): T | null {
  const index = keys.indexOf(current), steps: Record<string, number> = { ArrowRight: 1, ArrowLeft: -1 };
  const next = event.key === 'Home' ? 0 : event.key === 'End' ? keys.length - 1 : event.key in steps ? (index + steps[event.key] + keys.length) % keys.length : -1;
  if (next < 0) return null;
  event.preventDefault();
  return keys[next];
}
type Actions = { openRun: (id: string) => void; orderHref?: (order: NonNullable<LedgerRow['order']>) => string };
const when = (value: string) => new Date(value).toLocaleString('es-PA', { timeZone: 'America/Panama', dateStyle: 'short', timeStyle: 'short' });
const number = (value: number) => value.toLocaleString('es-PA');
const signed = (value: number) => value > 0 ? `+${number(value)}` : value < 0 ? `−${number(-value)}` : '0';
const tone = (value: number) => value > 0 ? 'ledger-credit' : value < 0 ? 'ledger-debit' : 'ledger-zero';
const text = (value: unknown) => Array.isArray(value) ? value.map(ref => typeof ref === 'object' && ref ? `${(ref as { brand?: string }).brand ? `${(ref as { brand: string }).brand}:` : ''}${(ref as { code?: string }).code ?? ''}` : String(ref)).join(' · ') || '—'
  : value === null || value === undefined || value === '' ? '—' : String(value);

function ItemCell({ data }: CustomCellRendererProps<LedgerRow | LedgerChange>) {
  if (!data) return null;
  return <div className="admin-grid-stack" title={data.item.description}><strong>{data.item.codigo}</strong><small>ID {data.item.supplier_invent_id}{data.item.brand ? ` · ${data.item.brand}` : ''}</small></div>;
}
function ReferenceCell({ data, context }: CustomCellRendererProps<LedgerRow, unknown, Actions>) {
  if (!data) return null;
  if (data.order) {
    const label = `${data.order.reference} · ${data.order.client_name}`;
    const href = context.orderHref?.(data.order);
    return <div className="admin-grid-stack">{href ? <a className="admin-grid-link" href={href}><strong>{label}</strong></a> : <strong>{label}</strong>}
      <small>Cotización v{data.order.revision} · {data.order.status_label}</small></div>;
  }
  if (data.run) return <button type="button" className="admin-grid-link" aria-label={`Ver qué cambió en ${data.run.name}`} onClick={() => context.openRun(data.run!.id)}>
    <span className="admin-grid-stack"><strong>{data.run.name}</strong><small>Ver qué cambió</small></span></button>;
  return <span className="admin-grid-text" title={data.reference}>{data.reference}</span>;
}
const delta = (field: 'stock_delta' | 'reserved_delta' | 'available_delta'): Partial<ColDef<LedgerRow>> => ({
  type: 'rightAligned', valueGetter: ({ data }) => data ? signed(data[field]) : '', cellClass: ({ data }) => ['ag-right-aligned-cell', 'admin-grid-number', data ? tone(data[field]) : ''] });

const movementColumns: ColDef<LedgerRow>[] = [
  { colId: 'when', headerName: 'Fecha', width: 132, valueGetter: ({ data }) => data ? when(data.created_at) : '' },
  { colId: 'item', headerName: 'Artículo', minWidth: 170, flex: 1, cellRenderer: ItemCell },
  { colId: 'origin', headerName: 'Origen', width: 160, cellRenderer: ({ data }: CustomCellRendererProps<LedgerRow>) => data ? <span className={`ledger-origin ${data.origin}`}>{data.origin_label}</span> : null },
  { colId: 'kind', headerName: 'Movimiento', width: 160, valueGetter: ({ data }) => data?.kind_label ?? '', cellClass: 'admin-grid-text' },
  { colId: 'stock', headerName: 'Existencias', width: 108, ...delta('stock_delta') },
  { colId: 'reserved', headerName: 'Reservadas', width: 108, ...delta('reserved_delta') },
  { colId: 'available', headerName: 'Disponibles', width: 110, ...delta('available_delta') },
  { colId: 'balance', headerName: 'Saldo después', width: 150, valueGetter: ({ data }) => data ? `${number(data.reported_after)} · ${number(data.reserved_after)} res. · ${number(data.available_after)} disp.` : '', cellClass: 'admin-grid-text' },
  { colId: 'reference', headerName: 'Referencia', minWidth: 210, flex: 1.2, cellRenderer: ReferenceCell },
  { colId: 'actor', headerName: 'Usuario', width: 150, valueGetter: ({ data }) => data?.actor ?? '', cellClass: 'admin-grid-text' },
];

function RunLabelCell({ data, context }: CustomCellRendererProps<LedgerRun, unknown, Actions>) {
  if (!data) return null;
  return <button type="button" className="admin-grid-link" aria-label={`Ver qué cambió en ${data.name}`} onClick={() => context.openRun(data.id)}>
    <span className="admin-grid-stack"><strong>{data.name}</strong><small>{data.rebuilt ? 'Reconstruida: sin detalle de campos' : data.mode ? `Sincronización ${data.mode_label}` : data.label}</small></span></button>;
}
const count = (key: keyof LedgerRun['counts'], header: string): ColDef<LedgerRun> => ({ colId: key, headerName: header, width: 104, type: 'rightAligned',
  valueGetter: ({ data }) => data ? number(data.counts[key] ?? 0) : '', cellClass: ['ag-right-aligned-cell', 'admin-grid-number'] });
const runColumns: ColDef<LedgerRun>[] = [
  { colId: 'when', headerName: 'Fecha', width: 132, valueGetter: ({ data }) => data ? when(data.started_at) : '' },
  { colId: 'origin', headerName: 'Origen', width: 150, cellRenderer: ({ data }: CustomCellRendererProps<LedgerRun>) => data ? <span className={`ledger-origin ${data.origin}`}>{data.origin_label}</span> : null },
  { colId: 'label', headerName: 'Carga', minWidth: 220, flex: 1, cellRenderer: RunLabelCell },
  count('created', 'Creados'), count('updated', 'Modificados'), count('stock', 'Existencias'), count('withdrawn', 'Retirados'),
  { colId: 'units', headerName: 'Unidades', width: 120, type: 'rightAligned', cellClass: ['ag-right-aligned-cell', 'admin-grid-number'],
    valueGetter: ({ data }) => data ? `+${number(data.counts.credit_units ?? 0)} / −${number(data.counts.debit_units ?? 0)}` : '' },
  { colId: 'actor', headerName: 'Usuario', width: 160, valueGetter: ({ data }) => data?.actor ?? '', cellClass: 'admin-grid-text' },
];

export default function SupplierLedger({ basePath, view, orderHref, revision = 0 }: { basePath: string; view: LedgerView; orderHref?: Actions['orderHref']; revision?: number }) {
  const [refresh, setRefresh] = useState(0);
  const gridRevision = revision + refresh;  // a save, a finished sync or Actualizar reads the rows again
  const [origin, setOrigin] = useState(''); const [kind, setKind] = useState('');
  const [search, setSearch] = useState(''); const [query, setQuery] = useState('');
  const [after, setAfter] = useState(''); const [before, setBefore] = useState('');
  const [total, setTotal] = useState<number | null>(null);
  const [run, setRun] = useState<string | null>(null);
  useEffect(() => { const timer = setTimeout(() => setQuery(search.trim()), 300); return () => clearTimeout(timer); }, [search]);
  useEffect(() => { setOrigin(''); setTotal(null); }, [view, basePath]);
  const context = useMemo<Actions>(() => ({ openRun: setRun, orderHref }), [orderHref]);
  const movementParams = useMemo(() => ({ origin, kind, search: query, after, before }), [origin, kind, query, after, before]);
  const runParams = useMemo(() => ({ origin }), [origin]);
  const origins = view === 'movements' ? Object.entries(originLabels) : Object.entries(originLabels).filter(([key]) => key !== 'quotation');
  return <div className="supplier-ledger-panel">
    <div className="inventory-filters ledger-filters">
      {view === 'movements' && <div className="catalog-search admin-search"><Search size={19}/><label className="sr-only" htmlFor={`${basePath}-ledger-search`}>Buscar movimientos</label>
        <UppercaseInput id={`${basePath}-ledger-search`} value={search} onChange={event => setSearch(event.target.value)} placeholder="CÓDIGO, ID, ORDEN O CLIENTE…"/></div>}
      <label className="inventory-filter-select"><span>Origen</span><select aria-label="Origen" value={origin} onChange={event => setOrigin(event.target.value)}>
        <option value="">TODOS</option>{origins.map(([key, label]) => <option key={key} value={key}>{label.toUpperCase()}</option>)}</select></label>
      {view === 'movements' && <>
        <label className="inventory-filter-select"><span>Movimiento</span><select aria-label="Movimiento" value={kind} onChange={event => setKind(event.target.value)}>
          <option value="">TODOS</option>{Object.entries(kindLabels).filter(([key]) => key !== 'delivery').map(([key, label]) => <option key={key} value={key}>{label.toUpperCase()}</option>)}</select></label>
        <label className="inventory-filter-select"><span>Desde</span><input type="date" aria-label="Desde" value={after} onChange={event => setAfter(event.target.value)}/></label>
        <label className="inventory-filter-select"><span>Hasta</span><input type="date" aria-label="Hasta" value={before} onChange={event => setBefore(event.target.value)}/></label>
      </>}
      <span className="inventory-count" aria-live="polite">{total === null ? 'Cargando…' : `${number(total)} ${view === 'movements' ? (total === 1 ? 'movimiento' : 'movimientos') : (total === 1 ? 'carga' : 'cargas')}`}</span>
      <button type="button" className="button text" onClick={() => setRefresh(value => value + 1)}><RefreshCw size={15}/>Actualizar</button>
    </div>
    {view === 'movements'
      ? <ServerGrid<LedgerRow> key={`${basePath}-movements`} storageKey="supplier-ledger" label="Libro de movimientos" path={basePath} params={movementParams} columns={movementColumns}
          rowId={row => String(row.id)} ordering={{}} revision={gridRevision} rowHeight={58} context={context} onCount={setTotal}
          empty={<div className="empty-state"><History size={30}/><h3>No hay movimientos con estos filtros.</h3><p>Aquí aparece cada reserva, descuento, faltante o liberación de una cotización confirmada y cada carga que movió existencias.</p></div>}/>
      : <ServerGrid<LedgerRun> key={`${basePath}-runs`} storageKey="supplier-ledger-runs" label="Cargas y sincronizaciones" path={`${basePath}/runs`} params={runParams} columns={runColumns}
          rowId={row => row.id} ordering={{}} revision={gridRevision} rowHeight={58} context={context} onCount={setTotal}
          empty={<div className="empty-state"><History size={30}/><h3>Todavía no hay cargas que hayan cambiado algo.</h3><p>Cada carga Excel, sincronización con apiag-cloud o actualización manual que cree, modifique o mueva un artículo aparece aquí.</p></div>}/>}
    {run && <RunDetail basePath={basePath} id={run} onClose={() => setRun(null)}/>}
  </div>;
}

function ChangeFields({ data }: CustomCellRendererProps<LedgerChange>) {
  if (!data) return null;
  if (data.reason) return <span className="admin-grid-text">{data.reason_label}</span>;
  if (data.action === 'created') return <span className="admin-grid-muted">Artículo nuevo</span>;
  if (!data.fields.length) return <span className="admin-grid-muted">—</span>;
  return <div className="ledger-fields">{data.fields.map(field => <span key={field.field}><strong>{field.label}:</strong> {text(field.before)} → {text(field.after)}</span>)}</div>;
}
const changeColumns: ColDef<LedgerChange>[] = [
  { colId: 'item', headerName: 'Artículo', minWidth: 170, flex: 1, cellRenderer: ItemCell },
  { colId: 'action', headerName: 'Qué cambió', width: 130, valueGetter: ({ data }) => data?.action_label ?? '', cellClass: 'admin-grid-text' },
  { colId: 'fields', headerName: 'Campos (antes → después)', minWidth: 260, flex: 1.5, cellRenderer: ChangeFields, autoHeight: true, wrapText: true },
  { colId: 'quantity', headerName: 'Existencias', width: 150, type: 'rightAligned', cellClass: ['ag-right-aligned-cell', 'admin-grid-number'],
    valueGetter: ({ data }) => !data || data.quantity_after === null ? '—' : `${number(data.quantity_before ?? 0)} → ${number(data.quantity_after)}${data.delta ? ` (${signed(data.delta)})` : ''}` },
  { colId: 'branches', headerName: 'Por sucursal', minWidth: 200, flex: 1, autoHeight: true, wrapText: true, cellClass: 'admin-grid-text',
    valueGetter: ({ data }) => data?.branches.map(row => `${row.nombre}: ${number(row.before)} → ${number(row.after)}`).join(' · ') || '—' },
];

function RunDetail({ basePath, id, onClose }: { basePath: string; id: string; onClose: () => void }) {
  const [run, setRun] = useState<LedgerRun | null>(null); const [error, setError] = useState('');
  const [action, setAction] = useState(''); const [search, setSearch] = useState(''); const [query, setQuery] = useState('');
  useEffect(() => { let live = true; request<LedgerRun>(`${basePath}/runs/${id}`).then(data => { if (live) setRun(data); }).catch(reason => { if (live) setError((reason as Error).message); }); return () => { live = false; }; }, [basePath, id]);
  useEffect(() => { const timer = setTimeout(() => setQuery(search.trim()), 300); return () => clearTimeout(timer); }, [search]);
  const params = useMemo(() => ({ action, search: query }), [action, query]);
  // An apiag-cloud pull's name already says apiag-cloud (its label); the others need their origin in front.
  return <Modal title={run ? run.origin === 'apiag' ? run.name : `${run.origin_label} · ${run.name}` : 'Carga'} wide drawer className="side-panel" onClose={onClose}>
    {error && <div className="notice error" role="alert">{error}</div>}
    {!run && !error && <div className="loading"><LoaderCircle className="spin"/>Cargando…</div>}
    {run && <div className="stack-form ledger-run">
      <p className="modal-description">{when(run.started_at)}{run.finished_at ? ` – ${when(run.finished_at)}` : ' · sin terminar'}{run.actor ? ` · ${run.actor}` : ''}{run.mode ? ` · sincronización ${run.mode_label}` : ''}.
        {run.rebuilt ? ' Reconstruida de los registros anteriores al libro: muestra qué artículos creó y cuántas unidades movió, sin el detalle de los campos que cambió.' : ''}</p>
      <dl className="ledger-run-counts">{(['created', 'updated', 'stock', 'withdrawn'] as const).map(key => <div key={key}><dt>{actionLabels[key]}</dt><dd>{number(run.counts[key] ?? 0)}</dd></div>)}
        <div><dt>Unidades</dt><dd>+{number(run.counts.credit_units ?? 0)} / −{number(run.counts.debit_units ?? 0)}</dd></div></dl>
      <div className="inventory-filters">
        <div className="catalog-search admin-search"><Search size={19}/><label className="sr-only" htmlFor={`run-${id}-search`}>Buscar artículos de la carga</label>
          <UppercaseInput id={`run-${id}-search`} value={search} onChange={event => setSearch(event.target.value)} placeholder="CÓDIGO, ID O DESCRIPCIÓN…"/></div>
        <label className="inventory-filter-select"><span>Qué cambió</span><select aria-label="Qué cambió" value={action} onChange={event => setAction(event.target.value)}>
          <option value="">TODO</option>{Object.entries(actionLabels).map(([key, label]) => <option key={key} value={key}>{label.toUpperCase()}</option>)}</select></label>
      </div>
      <ServerGrid<LedgerChange> storageKey="supplier-ledger-changes" label="Cambios de la carga" path={`${basePath}/runs/${id}/changes`} params={params} columns={changeColumns}
        rowId={row => String(row.id)} ordering={{}} revision={0} rowHeight={58}
        empty={<div className="empty-state"><History size={30}/><h3>No hay cambios con estos filtros.</h3></div>}/>
    </div>}
  </Modal>;
}
