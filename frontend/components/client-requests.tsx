'use client';
import { useEffect, useMemo, useState } from 'react';
import type { ColDef } from 'ag-grid-community';
import type { CustomCellRendererProps } from 'ag-grid-react';
import { ArrowUpRight, BadgeCheck, CheckCircle2, ClipboardList, LockKeyhole, RefreshCw, Search, TriangleAlert, X } from 'lucide-react';
import { Account } from '@/lib/types';
import type { ClientRequestSummary, RequestStatusFilter, RequestSubmissionResult } from '@/lib/request-types';
import { receiptHint } from '@/lib/deal-types';
import ClientRequestNavigation from './client-request-navigation';
import DealDetail from './deal-detail';
import DealStatusBadge from './deal-status';
import { UppercaseInput } from './uppercase-field';
import ServerGrid from './server-grid';

const dateLabel = (value: string) => new Date(value).toLocaleString('es-PA', { timeZone: 'America/Panama', dateStyle: 'medium', timeStyle: 'short' });
// initialStatus: the filter it opens on (the Recibido reminder's Ver todas opens Por recibir).
export default function ClientRequests({ account, draftCount, onDraft, receipt, initialStatus }: {
  account?: Account; draftCount: number; onDraft: () => void; receipt?: RequestSubmissionResult; initialStatus?: RequestStatusFilter;
}) {
  const canView = !!account?.capabilities.includes('client');
  return <main className="basket-page client-requests-page" aria-label="Solicitudes enviadas"><div className="section-container basket-page-container">
    <ClientRequestNavigation selected="sent" count={draftCount} canViewSent={canView} onDraft={onDraft} onSent={() => {}}/>
    {receipt && <div className="notice success client-request-receipt" role="status"><CheckCircle2 size={18}/><div><strong>Solicitud enviada</strong><p>Los artículos enviados salieron de la cesta y quedaron guardados aquí.</p>{receipt.requests.map(row => <p key={row.id}>{row.supplier.name} · {row.reference}</p>)}</div></div>}
    {canView && account ? <SentRequestList key={account.id} account={account} initialStatus={initialStatus}/> : <div className="notice">Inicia sesión y selecciona una cuenta con rol de cliente para consultar sus solicitudes enviadas.</div>}
  </div></main>;
}

// The client's filters: En curso is every confirmed order before the supplier's Entregado; Por recibir waits for the client's Recibido.
const sentFilters: { value: RequestStatusFilter; label: string }[] = [{ value: '', label: 'Todas' }, { value: 'pending', label: 'Enviadas' }, { value: 'reviewed', label: 'En revisión' },
  { value: 'quoted', label: 'Por confirmar' }, { value: 'adjustment', label: 'Ajustes' }, { value: 'active_agreements', label: 'En curso' }, { value: 'delivered', label: 'Por recibir' },
  { value: 'received', label: 'Recibidos' }, { value: 'cancelled', label: 'Cancelados' }];

type SentContext = { open: (row: ClientRequestSummary) => void; agreementsOnly: boolean };
type SentCell = CustomCellRendererProps<ClientRequestSummary, unknown, SentContext>;
const number = (value: number | null | undefined) => (value ?? 0).toLocaleString('es-PA');
const money = (value: string, currency = 'USD') => new Intl.NumberFormat('es-PA', { style: 'currency', currency }).format(Number(value));
function ReferenceCell({ data }: SentCell) {
  return data ? <div className="admin-grid-stack"><strong>{data.reference}</strong><small>{dateLabel(data.created_at)}</small></div> : null;
}
function AlertsCell({ data }: SentCell) {
  if (!data || (data.status !== 'delivered' && !data.shortfall_pending)) return data ? <span className="admin-grid-muted">—</span> : null;
  return <div className="admin-grid-chips supplier-request-flags">
    {data.status === 'delivered' && <span className="supplier-request-alert receipt"><BadgeCheck size={13}/>{receiptHint(false)}</span>}
    {data.shortfall_pending && <span className="supplier-request-alert shortfall"><TriangleAlert size={13}/>Faltante por responder</span>}</div>;
}
function OpenCell({ data, context }: SentCell) {
  return data ? <div className="admin-grid-actions"><button type="button" className="button soft small" aria-label={`Ver enviada ${data.reference}`} onClick={() => context.open(data)}>Ver orden<ArrowUpRight size={15}/></button></div> : null;
}
// The grid's sortable columns and the API ordering names they sort by (status in the order's lifecycle).
const sentOrdering: Record<string, string> = { reference: 'created_at', status: 'status', supplier: 'supplier_name', lines: 'line_count', units: 'unit_count', total: 'quotation_total' };
function sentColumns(agreementsOnly: boolean): ColDef<ClientRequestSummary>[] {
  return [
    { colId: 'reference', headerName: 'Orden / enviada', width: 240, cellRenderer: ReferenceCell },
    { colId: 'status', headerName: 'Estado', width: 200, cellRenderer: ({ data }: SentCell) => data ? <DealStatusBadge status={data.status}/> : null },
    { colId: 'supplier', headerName: 'Proveedor', valueGetter: ({ data }) => data?.supplier.name ?? '', flex: 1, minWidth: 170, cellClass: 'admin-grid-text strong' },
    { colId: 'lines', headerName: 'Artículos', field: 'line_count', valueFormatter: ({ value }) => number(value), width: 112, type: 'rightAligned', cellClass: ['ag-right-aligned-cell', 'admin-grid-number'] },
    { colId: 'units', headerName: agreementsOnly ? 'Acordadas' : 'Unidades', width: 112, type: 'rightAligned', cellClass: ['ag-right-aligned-cell'],
      valueGetter: ({ data }) => data ? agreementsOnly ? data.agreed_unit_count ?? data.quoted_unit_count ?? data.unit_count : data.unit_count : null, valueFormatter: ({ value }) => number(value) },
    { colId: 'total', headerName: 'Total', width: 130, type: 'rightAligned', cellClass: ['ag-right-aligned-cell', 'admin-grid-text', 'strong'],
      valueGetter: ({ data }) => data?.quotation_total ? money(data.quotation_total, data.quotation_currency || 'USD') : '—',
      tooltipValueGetter: ({ data }) => data?.quotation_revision ? `Cotización v${data.quotation_revision}` : 'Sin cotizar' },
    { colId: 'alerts', headerName: 'Avisos', flex: 1, minWidth: 170, cellRenderer: AlertsCell },
    { colId: 'open', headerName: 'Acciones', width: 130, cellRenderer: OpenCell, suppressHeaderMenuButton: true, resizable: false },
  ];
}

// The client's orders as a grid (ServerGrid): rows load while scrolling, sorting runs on the server, search and the status chips filter it.
export function SentRequestList({ account, agreementsOnly = false, initialStatus = '' }: { account: Account; agreementsOnly?: boolean; initialStatus?: RequestStatusFilter }) {
  const [search, setSearch] = useState('');
  const [appliedSearch, setAppliedSearch] = useState('');
  // Historial de compras lists every confirmed deal, in progress, delivered, received or cancelled.
  const [status, setStatus] = useState<RequestStatusFilter>(agreementsOnly ? 'agreements' : initialStatus);
  const [revision, setRevision] = useState(0);
  const [count, setCount] = useState<number | null>(null);
  const [selected, setSelected] = useState<ClientRequestSummary | null>(null);
  useEffect(() => { if (search.trim() === appliedSearch) return; const timer = setTimeout(() => setAppliedSearch(search.trim()), 350); return () => clearTimeout(timer); }, [search, appliedSearch]);
  const params = useMemo(() => ({ search: appliedSearch, status }), [appliedSearch, status]);
  const context = useMemo<SentContext>(() => ({ open: setSelected, agreementsOnly }), [agreementsOnly]);
  const columns = useMemo(() => sentColumns(agreementsOnly), [agreementsOnly]);
  const refresh = () => setRevision(value => value + 1);
  // The supplier's answers arrive while the page is open: the loaded rows are read again every 15 s, keeping the scroll position.
  useEffect(() => { const timer = setInterval(() => { if (document.visibilityState === 'visible') setRevision(value => value + 1); }, 15000); return () => clearInterval(timer); }, []);
  const filtered = Boolean(appliedSearch || (status && !agreementsOnly));
  return <section className="supplier-requests" aria-label="Historial de solicitudes enviadas">
    <div className="supplier-requests-heading"><div><h2>{agreementsOnly ? 'Acuerdos confirmados' : 'Órdenes de'} {account.name}</h2><p>{agreementsOnly ? 'Cotizaciones confirmadas por cliente y proveedor. El proveedor marca Procesando, En camino y Entregado; tú confirmas que la recibiste.' : 'Cada proveedor recibe únicamente sus propios artículos.'}</p></div><button type="button" className="button soft small" onClick={refresh}><RefreshCw size={15}/>Actualizar enviadas</button></div>
    <div className="supplier-requests-controls"><div className="supplier-requests-search"><Search size={19}/><UppercaseInput aria-label="Buscar solicitudes enviadas" maxLength={200} value={search} placeholder="PROVEEDOR, REFERENCIA O CÓDIGO…" onChange={event => setSearch(event.target.value)}/>{search && <button type="button" className="icon-button" aria-label="Limpiar búsqueda de enviadas" onClick={() => setSearch('')}><X size={16}/></button>}</div>{!agreementsOnly && <div className="supplier-request-filters" role="group" aria-label="Estado de solicitudes enviadas">{sentFilters.map(filter => <button type="button" key={filter.value} aria-pressed={status === filter.value} className={status === filter.value ? 'selected' : ''} onClick={() => setStatus(filter.value)}>{filter.label}</button>)}</div>}</div>
    <span className="inventory-count supplier-request-count" aria-live="polite">{count === null ? 'Cargando solicitudes…' : `${count.toLocaleString('es-PA')} ${count === 1 ? 'solicitud' : 'solicitudes'}`}</span>
    <ServerGrid<ClientRequestSummary> storageKey={agreementsOnly ? 'client-agreements' : 'client-sent-requests'} label={agreementsOnly ? 'Acuerdos confirmados' : 'Órdenes enviadas'}
      path={`/api/market/accounts/${account.id}/sent-requests`} params={params} columns={columns} rowId={row => row.id} ordering={sentOrdering} revision={revision} rowHeight={60}
      context={context} onCount={setCount} className="supplier-request-grid"
      empty={<div className="supplier-request-empty"><ClipboardList size={34}/><h3>{agreementsOnly ? 'Aún no tienes acuerdos confirmados.' : filtered ? 'No hay solicitudes para estos filtros.' : 'Todavía no has enviado solicitudes.'}</h3><p>{agreementsOnly ? 'Cuando ambas partes confirmen una cotización, aparecerá en este historial.' : filtered ? 'Prueba otra búsqueda o cambia el estado.' : 'Prepara una solicitud en la Cesta. Después de enviarla, podrás consultarla aquí.'}</p>{filtered && <button type="button" className="button soft small" onClick={() => { setSearch(''); setAppliedSearch(''); setStatus(''); }}>Limpiar filtros</button>}</div>}/>
    <div className="supplier-request-private-note"><LockKeyhole size={16}/><p>Las órdenes y cotizaciones se guardan en tu cuenta. Cliente y proveedor confirman las condiciones para cerrar el acuerdo. El proveedor marca Procesando, En camino y Entregado; tú confirmas que la recibiste.</p></div>
    {selected && <DealDetail key={selected.id} account={account} orderId={selected.id} reference={selected.reference} side="client" onClose={() => setSelected(null)} onChanged={refresh}/>}
  </section>;
}
