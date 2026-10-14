'use client';
import { useEffect, useId, useMemo, useRef, useState } from 'react';
import type { ColDef } from 'ag-grid-community';
import type { CustomCellRendererProps } from 'ag-grid-react';
import { ArrowUpRight, BadgeCheck, CheckCircle2, CircleX, ClipboardList, Clock3, FileText, LockKeyhole, PackageCheck, RefreshCw, RotateCcw, Search, TriangleAlert, Truck, X,
  type LucideIcon } from 'lucide-react';
import { Account, request } from '@/lib/types';
import type { ClientRequestSummary, MoneyTotal, RequestStatusFilter, RequestSubmissionResult, SentRequestStages } from '@/lib/request-types';
import { receiptHint } from '@/lib/deal-types';
import ClientRequestNavigation from './client-request-navigation';
import DealDetail from './deal-detail';
import DealStatusBadge from './deal-status';
import { UppercaseInput } from './uppercase-field';
import ServerGrid from './server-grid';

const dateLabel = (value: string) => new Date(value).toLocaleString('es-PA', { timeZone: 'America/Panama', dateStyle: 'medium', timeStyle: 'short' });
// initialStatus: the filter it opens on (the Recibido reminder's Ver todas opens Por recibir).
export default function ClientRequests({ account, draftCount, onDraft, onActivity, receipt, initialStatus }: {
  account?: Account; draftCount: number; onDraft: () => void; onActivity: () => void; receipt?: RequestSubmissionResult; initialStatus?: RequestStatusFilter;
}) {
  const canView = !!account?.capabilities.includes('client');
  return <main className="basket-page client-requests-page" aria-label="Solicitudes enviadas"><div className="section-container basket-page-container">
    <ClientRequestNavigation selected="sent" count={draftCount} canViewSent={canView} onDraft={onDraft} onSent={() => {}} onActivity={onActivity}/>
    {receipt && <div className="notice success client-request-receipt" role="status"><CheckCircle2 size={18}/><div><strong>Solicitud enviada</strong><p>Los artículos enviados salieron de la cesta y quedaron guardados aquí.</p>{receipt.requests.map(row => <p key={row.id}>{row.supplier.name} · {row.reference}</p>)}</div></div>}
    {canView && account ? <SentRequestList key={account.id} account={account} initialStatus={initialStatus}/> : <div className="notice">Inicia sesión y selecciona una cuenta con rol de cliente para consultar sus solicitudes enviadas.</div>}
  </div></main>;
}

type SentContext = { open: (row: ClientRequestSummary) => void; agreementsOnly: boolean };
type SentCell = CustomCellRendererProps<ClientRequestSummary, unknown, SentContext>;
const number = (value: number | null | undefined) => (value ?? 0).toLocaleString('es-PA');
const money = (value: string, currency = 'USD') => new Intl.NumberFormat('es-PA', { style: 'currency', currency }).format(Number(value));
// The client's orders by stage, as cards over its list that are also its filters: how many are in each and what the client wants to know of
// each, highlighted when the next step is the client's. En curso is every confirmed order before the supplier's Entregado; Por recibir waits
// for the client's Recibido.
type Stage = { value: RequestStatusFilter; label: string; hint: string; icon: LucideIcon; tone: string; count: (data: SentRequestStages) => number;
  note: (data: SentRequestStages) => string; attention?: (data: SentRequestStages) => boolean };
const amounts = (values: MoneyTotal[]) => values.length ? values.map(value => money(value.total, value.currency)).join(' · ') : '—';
// How long the oldest order of a stage that waits on the supplier has waited.
const waited = (at?: string) => {
  if (!at) return '—';
  const elapsed = Date.now() - new Date(at).getTime(), days = Math.floor(elapsed / 86400000), hours = Math.floor(elapsed / 3600000);
  return `más antigua: ${days >= 1 ? `${days} d` : hours >= 1 ? `${hours} h` : '< 1 h'}`;
};
const plural = (count: number, one: string, many: string) => `${count} ${count === 1 ? one : many}`;
const stages: Stage[] = [
  { value: '', label: 'Todas', hint: 'Todas las etapas', icon: ClipboardList, tone: 'all', count: data => data.total, note: data => `${data.sent_this_month} este mes` },
  { value: 'pending', label: 'Enviadas', hint: 'Por revisar', icon: Clock3, tone: 'pending', count: data => data.statuses.pending, note: data => waited(data.oldest.pending) },
  { value: 'reviewed', label: 'En revisión', hint: 'Cotizando', icon: CheckCircle2, tone: 'reviewed', count: data => data.statuses.reviewed,
    note: data => waited(data.oldest.reviewed) },
  { value: 'quoted', label: 'Por confirmar', hint: 'Cotización lista', icon: FileText, tone: 'quoted', count: data => data.statuses.quoted,
    note: data => amounts(data.to_confirm), attention: data => data.statuses.quoted > 0 },
  { value: 'adjustment', label: 'Ajustes', hint: 'Pediste cambios', icon: RotateCcw, tone: 'adjustment', count: data => data.statuses.adjustment,
    note: data => waited(data.oldest.adjustment) },
  { value: 'active_agreements', label: 'En curso', hint: 'Acordadas', icon: Truck, tone: 'in_transit',
    count: data => data.statuses.handshaked + data.statuses.processing + data.statuses.in_transit,
    note: data => data.shortfall_pending ? `responde ${plural(data.shortfall_pending, 'faltante', 'faltantes')}` : `${data.statuses.in_transit} en camino`,
    attention: data => data.shortfall_pending > 0 },
  { value: 'delivered', label: 'Por recibir', hint: 'Confirma que llegó', icon: PackageCheck, tone: 'delivered', count: data => data.statuses.delivered,
    note: data => amounts(data.to_receive), attention: data => data.statuses.delivered > 0 },
  { value: 'received', label: 'Recibidos', hint: 'Cerradas', icon: BadgeCheck, tone: 'received', count: data => data.statuses.received,
    note: data => `${data.received_this_month} este mes` },
  { value: 'cancelled', label: 'Cancelados', hint: 'Sin acuerdo', icon: CircleX, tone: 'cancelled', count: data => data.statuses.cancelled, note: () => '—' },
];
function OrderStages({ data, status, onStatus }: { data: SentRequestStages | null; status: RequestStatusFilter; onStatus: (value: RequestStatusFilter) => void }) {
  const id = useId();
  // Where the row scrolls (a phone, a narrow window) the selected stage (one the page opened on, Por recibir from the reminder) and a card
  // reached with Tab are brought into it, sideways only and to a card's start (a snap point, so the row stays there).
  const row = useRef<HTMLDivElement>(null);
  const reveal = (card: HTMLElement | null | undefined) => {
    const strip = row.current;
    if (!strip || !card) return;
    const left = card.offsetLeft - strip.offsetLeft, right = left + card.offsetWidth;
    if (left < strip.scrollLeft || right > strip.scrollLeft + strip.clientWidth) strip.scrollLeft = Math.max(0, left - 6);
  };
  useEffect(() => reveal(row.current?.querySelector<HTMLElement>('[aria-pressed="true"]')), [status]);
  return <div ref={row} className="order-stages" role="group" aria-label="Estado de solicitudes enviadas">{stages.map(stage => {
    const count = data ? stage.count(data) : null, Icon = stage.icon, selected = status === stage.value, figures = `${id}-${stage.value || 'all'}`;
    return <button type="button" key={stage.value} className={`order-stage tone-${stage.tone}${selected ? ' selected' : ''}${data && stage.attention?.(data) ? ' attention' : ''}`}
      aria-pressed={selected} aria-label={stage.label} aria-describedby={figures} onFocus={event => reveal(event.currentTarget)} onClick={() => onStatus(stage.value)}>
      <span className="order-stage-title"><Icon size={15} aria-hidden="true"/><strong>{stage.label}</strong></span>
      <small className="order-stage-hint">{stage.hint}</small>
      <span className="order-stage-figures" id={figures}><b>{count ?? '–'}</b> <small className="sr-only">{count === 1 ? 'orden' : 'órdenes'}</small>{data && <> <em title={stage.note(data)}>{stage.note(data)}</em></>}</span>
    </button>;
  })}</div>;
}
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
  const [stagesData, setStagesData] = useState<SentRequestStages | null>(null);
  // The stage cards follow the list: read again with it (every 15 s, on Actualizar and after a change in an order).
  useEffect(() => {
    if (agreementsOnly) return;
    let current = true;
    request<SentRequestStages>(`/api/market/accounts/${account.id}/sent-requests/summary`)
      .then(data => { if (current && data?.statuses) setStagesData(data); }).catch(() => undefined);
    return () => { current = false; };
  }, [account.id, agreementsOnly, revision]);
  useEffect(() => { if (search.trim() === appliedSearch) return; const timer = setTimeout(() => setAppliedSearch(search.trim()), 350); return () => clearTimeout(timer); }, [search, appliedSearch]);
  const params = useMemo(() => ({ search: appliedSearch, status }), [appliedSearch, status]);
  const context = useMemo<SentContext>(() => ({ open: setSelected, agreementsOnly }), [agreementsOnly]);
  const columns = useMemo(() => sentColumns(agreementsOnly), [agreementsOnly]);
  const refresh = () => setRevision(value => value + 1);
  // The supplier's answers arrive while the page is open: the loaded rows are read again every 15 s, keeping the scroll position.
  useEffect(() => { const timer = setInterval(() => { if (document.visibilityState === 'visible') setRevision(value => value + 1); }, 15000); return () => clearInterval(timer); }, []);
  const filtered = Boolean(appliedSearch || (status && !agreementsOnly));
  return <section className="supplier-requests" aria-label="Historial de solicitudes enviadas">
    <div className="supplier-requests-heading"><div><h2>{agreementsOnly ? 'Acuerdos confirmados' : 'Órdenes de'} {account.name}</h2><p>{agreementsOnly ? 'Cotizaciones confirmadas por cliente y proveedor. El proveedor marca Procesando, En camino y Entregado; tú confirmas que la recibiste.' : 'Cada proveedor recibe únicamente sus propios artículos.'}</p></div><div className="supplier-requests-heading-actions">{stagesData && <span className="order-stages-updated">Actualizado {new Date(stagesData.updated_at).toLocaleTimeString('es-PA', { timeZone: 'America/Panama', hour: '2-digit', minute: '2-digit' })}</span>}<button type="button" className="button soft small" onClick={refresh}><RefreshCw size={15}/>Actualizar enviadas</button></div></div>
    {!agreementsOnly && <OrderStages data={stagesData} status={status} onStatus={setStatus}/>}
    <div className="supplier-requests-controls"><div className="supplier-requests-search"><Search size={19}/><UppercaseInput aria-label="Buscar solicitudes enviadas" maxLength={200} value={search} placeholder="PROVEEDOR, REFERENCIA O CÓDIGO…" onChange={event => setSearch(event.target.value)}/>{search && <button type="button" className="icon-button" aria-label="Limpiar búsqueda de enviadas" onClick={() => setSearch('')}><X size={16}/></button>}</div>
      <span className="inventory-count supplier-request-count" aria-live="polite">{count === null ? 'Cargando solicitudes…' : `${count.toLocaleString('es-PA')} ${count === 1 ? 'solicitud' : 'solicitudes'}`}</span></div>
    <ServerGrid<ClientRequestSummary> storageKey={agreementsOnly ? 'client-agreements' : 'client-sent-requests'} label={agreementsOnly ? 'Acuerdos confirmados' : 'Órdenes enviadas'}
      path={`/api/market/accounts/${account.id}/sent-requests`} params={params} columns={columns} rowId={row => row.id} ordering={sentOrdering} revision={revision} rowHeight={60}
      context={context} onCount={setCount} className="supplier-request-grid"
      empty={<div className="supplier-request-empty"><ClipboardList size={34}/><h3>{agreementsOnly ? 'Aún no tienes acuerdos confirmados.' : filtered ? 'No hay solicitudes para estos filtros.' : 'Todavía no has enviado solicitudes.'}</h3><p>{agreementsOnly ? 'Cuando ambas partes confirmen una cotización, aparecerá en este historial.' : filtered ? 'Prueba otra búsqueda o cambia el estado.' : 'Prepara una solicitud en la Cesta. Después de enviarla, podrás consultarla aquí.'}</p>{filtered && <button type="button" className="button soft small" onClick={() => { setSearch(''); setAppliedSearch(''); setStatus(''); }}>Limpiar filtros</button>}</div>}/>
    <div className="supplier-request-private-note"><LockKeyhole size={16}/><p>Las órdenes y cotizaciones se guardan en tu cuenta. Cliente y proveedor confirman las condiciones para cerrar el acuerdo. El proveedor marca Procesando, En camino y Entregado; tú confirmas que la recibiste.</p></div>
    {selected && <DealDetail key={selected.id} account={account} orderId={selected.id} reference={selected.reference} side="client" onClose={() => setSelected(null)} onChanged={refresh}/>}
  </section>;
}
