'use client';
import { useEffect, useRef, useState } from 'react';
import { ArrowLeft, ArrowUpRight, CalendarDays, CheckCircle2, ClipboardList, LockKeyhole, RefreshCw, Search, Store, X } from 'lucide-react';
import { Account, Page, request } from '@/lib/types';
import type { ClientRequestDetail, ClientRequestSummary, RequestSubmissionResult } from '@/lib/request-types';
import ClientRequestNavigation from './client-request-navigation';
import DealDetail from './deal-detail';
import DealStatusBadge from './deal-status';
import { UppercaseInput } from './uppercase-field';

const dateLabel = (value: string) => new Date(value).toLocaleString('es-PA', { timeZone: 'America/Panama', dateStyle: 'medium', timeStyle: 'short' });
const failureMessage = (error: unknown) => error instanceof TypeError ? 'Se perdió la conexión. Inténtalo de nuevo.' : error instanceof Error ? error.message : 'No se pudieron cargar tus solicitudes.';
export default function ClientRequests({ account, draftCount, onDraft, onCatalog, receipt }: {
  account?: Account; draftCount: number; onDraft: () => void; onCatalog: () => void; receipt?: RequestSubmissionResult;
}) {
  const canView = !!account?.capabilities.includes('client');
  return <main className="basket-page client-requests-page" aria-label="Solicitudes enviadas"><div className="section-container basket-page-container">
    <button type="button" className="basket-page-back" onClick={onCatalog}><ArrowLeft size={16}/>Volver al catálogo</button>
    <ClientRequestNavigation selected="sent" count={draftCount} canViewSent={canView} onDraft={onDraft} onSent={() => {}}/>
    <div className="basket-page-heading"><div><span className="eyebrow">TUS SOLICITUDES</span><h1>Órdenes enviadas</h1><p>Sigue cada orden desde su envío hasta la confirmación de la cotización.</p></div></div>
    {receipt && <div className="notice success client-request-receipt" role="status"><CheckCircle2 size={18}/><div><strong>Solicitud enviada</strong><p>Los artículos enviados salieron del borrador y quedaron guardados aquí.</p>{receipt.requests.map(row => <p key={row.id}>{row.supplier.name} · {row.reference}</p>)}</div></div>}
    {canView && account ? <SentRequestList key={account.id} account={account}/> : <div className="notice">Inicia sesión y selecciona una cuenta con rol de cliente para consultar sus solicitudes enviadas.</div>}
  </div></main>;
}

export function SentRequestList({ account, agreementsOnly = false }: { account: Account; agreementsOnly?: boolean }) {
  const [search, setSearch] = useState('');
  const [appliedSearch, setAppliedSearch] = useState('');
  const [status, setStatus] = useState<'' | ClientRequestSummary['status']>(agreementsOnly ? 'handshaked' : '');
  const [page, setPage] = useState(1);
  const [revision, setRevision] = useState(0);
  const backgroundRefresh = useRef(false);
  const [snapshot, setSnapshot] = useState<{ search: string; status: string; page: number; data: Page<ClientRequestSummary> } | null>(null);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState('');
  const [selected, setSelected] = useState<ClientRequestSummary | null>(null);
  useEffect(() => { if (search.trim() === appliedSearch) return; const timer = setTimeout(() => { setAppliedSearch(search.trim()); setPage(1); }, 350); return () => clearTimeout(timer); }, [search, appliedSearch]);
  useEffect(() => {
    const controller = new AbortController(); let cancelled = false;
    const quiet = backgroundRefresh.current; backgroundRefresh.current = false;
    if (!quiet) { setBusy(true); setError(''); }
    const query = new URLSearchParams({ page: String(page) });
    if (appliedSearch) query.set('search', appliedSearch);
    if (status) query.set('status', status);
    request<Page<ClientRequestSummary>>(`/api/market/accounts/${account.id}/sent-requests?${query}`, { signal: controller.signal })
      .then(data => { if (!cancelled) setSnapshot({ search: appliedSearch, status, page, data }); })
      .catch(error => { if (!quiet && !cancelled) setError(failureMessage(error)); })
      .finally(() => { if (!cancelled) setBusy(false); });
    return () => { cancelled = true; controller.abort(); };
  }, [account.id, appliedSearch, status, page, revision]);
  const data = snapshot?.search === appliedSearch && snapshot.status === status && snapshot.page === page ? snapshot.data : null;
  const loading = busy || (!data && !error);
  const refresh = () => { backgroundRefresh.current = false; setRevision(value => value + 1); };
  useEffect(() => { const timer = setInterval(() => { if (document.visibilityState === 'visible') { backgroundRefresh.current = true; setRevision(value => value + 1); } }, 15000); return () => clearInterval(timer); }, []);
  return <section className="supplier-requests" aria-label="Historial de solicitudes enviadas">
    <div className="supplier-requests-heading"><div><h2>{agreementsOnly ? 'Acuerdos confirmados' : 'Órdenes de'} {account.name}</h2><p>{agreementsOnly ? 'Cotizaciones confirmadas por cliente y proveedor. La entrega se coordina por separado.' : 'Cada proveedor recibe únicamente sus propios artículos.'}</p></div><button type="button" className="button soft small" disabled={busy} onClick={refresh}><RefreshCw size={15}/>Actualizar enviadas</button></div>
    <div className="supplier-requests-controls"><div className="supplier-requests-search"><Search size={19}/><UppercaseInput aria-label="Buscar solicitudes enviadas" maxLength={200} value={search} placeholder="PROVEEDOR, REFERENCIA O CÓDIGO…" onChange={event => setSearch(event.target.value)}/>{search && <button type="button" className="icon-button" aria-label="Limpiar búsqueda de enviadas" onClick={() => setSearch('')}><X size={16}/></button>}</div>{!agreementsOnly && <div className="supplier-request-filters" role="group" aria-label="Estado de solicitudes enviadas">{([{ value: '', label: 'Todas' }, { value: 'pending', label: 'Enviadas' }, { value: 'reviewed', label: 'En revisión' }, { value: 'quoted', label: 'Por confirmar' }, { value: 'adjustment', label: 'Ajustes' }, { value: 'handshaked', label: 'Acuerdos' }] as const).map(filter => <button type="button" key={filter.value} aria-pressed={status === filter.value} className={status === filter.value ? 'selected' : ''} onClick={() => { setStatus(filter.value); setPage(1); }}>{filter.label}</button>)}</div>}</div>
    {error && <div className="notice error" role="alert"><span>{error}</span><button type="button" onClick={refresh}>Intentar de nuevo</button></div>}
    <div className="supplier-request-list" aria-busy={loading}>
      {loading ? <><span className="sr-only" role="status">Cargando solicitudes enviadas…</span><div className="supplier-request-skeletons" aria-hidden="true">{Array.from({ length: 3 }, (_, i) => <div className="supplier-request-skeleton" key={i}><span className="skeleton-block"/><span className="skeleton-block"/></div>)}</div></> : error ? null : !data?.results.length ? <div className="supplier-request-empty"><ClipboardList size={34}/><h3>{agreementsOnly ? 'Aún no tienes acuerdos confirmados.' : appliedSearch || status ? 'No hay solicitudes para estos filtros.' : 'Todavía no has enviado solicitudes.'}</h3><p>{agreementsOnly ? 'Cuando ambas partes confirmen una cotización, aparecerá en este historial.' : appliedSearch || status ? 'Prueba otra búsqueda o cambia el estado.' : 'Prepara una solicitud en Borrador. Después de enviarla, podrás consultarla aquí.'}</p></div> : <div className="supplier-request-cards">{data.results.map(row => <article className="supplier-request-card" key={row.id} aria-label={`Solicitud ${row.reference} para ${row.supplier.name}`}><div className="supplier-request-card-main"><div className="supplier-request-reference"><h3>{row.reference}</h3><DealStatusBadge status={row.status}/></div><p className="supplier-request-client"><Store size={15}/>{row.supplier.name}</p><p className="supplier-request-date"><CalendarDays size={14}/>{dateLabel(row.created_at)}</p></div><div className="supplier-request-counts"><span><strong>{row.line_count}</strong>{row.line_count === 1 ? 'artículo' : 'artículos'}</span><span><strong>{(agreementsOnly ? row.quoted_unit_count ?? row.unit_count : row.unit_count).toLocaleString('es-PA')}</strong>{agreementsOnly ? 'unidades acordadas' : 'unidades solicitadas'}</span></div><button type="button" className="button soft small" aria-label={`Ver enviada ${row.reference}`} onClick={() => setSelected(row)}>Ver orden<ArrowUpRight size={16}/></button></article>)}</div>}
    </div>
    <div className="pagination supplier-request-pagination"><span>{data ? `${data.count} ${data.count === 1 ? 'solicitud' : 'solicitudes'}` : 'Solicitudes enviadas'}</span><div><button type="button" className="button soft small" disabled={!data?.previous || loading} onClick={() => setPage(value => value - 1)}>Anterior</button><span>Página {page}</span><button type="button" className="button soft small" disabled={!data?.next || loading} onClick={() => setPage(value => value + 1)}>Siguiente</button></div></div>
    <div className="supplier-request-private-note"><LockKeyhole size={16}/><p>Las órdenes y cotizaciones se guardan en tu cuenta. Cliente y proveedor confirman las condiciones para cerrar el acuerdo. La entrega se coordina con el proveedor.</p></div>
    {selected && <DealDetail key={selected.id} account={account} orderId={selected.id} reference={selected.reference} side="client" onClose={() => setSelected(null)} onChanged={refresh}/>}
  </section>;
}
