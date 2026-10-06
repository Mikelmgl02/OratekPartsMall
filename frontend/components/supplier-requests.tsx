'use client';

import { useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { TriangleAlert, ArrowUpRight, Boxes, CalendarDays, CheckCircle2, ClipboardList, LockKeyhole, LoaderCircle, RefreshCw, Search, UserRound, X } from 'lucide-react';
import DealStatusBadge from './deal-status';
import { UppercaseInput } from './uppercase-field';
import { Account, Page, request } from '@/lib/types';
import { SupplierRequestListItem, SupplierRequestSummary } from '@/lib/request-types';
import { supplierOrderHref, supplierRequestsHref } from '@/lib/supplier-navigation';

type StatusFilter = '' | SupplierRequestSummary['status'];
type RequestSnapshot = { accountId: string; search: string; status: StatusFilter; page: number; data: Page<SupplierRequestListItem> };
const availabilityAlerts = { blocked: 'Confirmación bloqueada por existencias', accepted_with_shortfall: 'Confirmado con faltante' };
function dateLabel(value: string) {
  return new Date(value).toLocaleString('es-PA', { timeZone: 'America/Panama', dateStyle: 'medium', timeStyle: 'short' });
}
function failureMessage(error: unknown, fallback: string) {
  return error instanceof TypeError ? 'Se perdió la conexión. Inténtalo de nuevo.' : error instanceof Error ? error.message : fallback;
}
function RequestSkeletons() {
  return <><span className="sr-only" role="status">Cargando solicitudes…</span><div className="supplier-request-skeletons" aria-hidden="true">{Array.from({ length: 4 }, (_, index) => <div className="supplier-request-skeleton" key={index}><div><span className="skeleton-block"/><span className="skeleton-block"/></div><span className="skeleton-block"/><div><span className="skeleton-block"/><span className="skeleton-block"/><span className="skeleton-block"/></div></div>)}</div></>;
}

export default function SupplierRequests({ account }: { account: Account }) {
  const [search, setSearch] = useState('');
  const [appliedSearch, setAppliedSearch] = useState('');
  const [status, setStatus] = useState<StatusFilter>('');
  const [page, setPage] = useState(1);
  const [revision, setRevision] = useState(0);
  const [snapshot, setSnapshot] = useState<RequestSnapshot | null>(null);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState('');
  const backgroundRefresh = useRef(false);
  const activeAccount = useRef(account.id);
  activeAccount.current = account.id;
  useEffect(() => {
    if (search.trim() === appliedSearch) return;
    const timer = setTimeout(() => { setAppliedSearch(search.trim()); setPage(1); }, 350);
    return () => clearTimeout(timer);
  }, [search, appliedSearch]);
  useEffect(() => {
    let cancelled = false;
    const accountId = account.id;
    const quiet = backgroundRefresh.current; backgroundRefresh.current = false;
    if (!quiet) { setBusy(true); setError(''); }
    const query = new URLSearchParams({ page: String(page) });
    if (appliedSearch) query.set('search', appliedSearch);
    if (status) query.set('status', status);
    request<Page<SupplierRequestListItem>>(`/api/market/accounts/${accountId}/requests?${query}`).then(data => {
      if (!cancelled && activeAccount.current === accountId) setSnapshot({ accountId, search: appliedSearch, status, page, data });
    }).catch(caught => {
      if (!quiet && !cancelled && activeAccount.current === accountId) setError(failureMessage(caught, 'No se pudieron cargar las solicitudes.'));
    }).finally(() => { if (!cancelled && activeAccount.current === accountId) setBusy(false); });
    return () => { cancelled = true; };
  }, [account.id, appliedSearch, status, page, revision]);
  const current = snapshot?.accountId === account.id && snapshot.search === appliedSearch && snapshot.status === status && snapshot.page === page;
  const data = current ? snapshot.data : null;
  const loading = busy || (!current && !error);
  const refresh = () => { backgroundRefresh.current = false; setRevision(value => value + 1); };
  useEffect(() => { const timer = setInterval(() => { if (document.visibilityState === 'visible') { backgroundRefresh.current = true; setRevision(value => value + 1); } }, 15000); return () => clearInterval(timer); }, []);

  return <section className="supplier-requests" aria-label="Solicitudes de clientes">
    <div className="supplier-requests-heading"><div><h2>Órdenes de tus clientes</h2><p>Gestiona órdenes, cotizaciones y acuerdos de {account.name}.</p></div><button type="button" className="button soft small" disabled={busy} onClick={refresh}><RefreshCw size={15}/>Actualizar solicitudes</button></div>
    <div className="supplier-requests-controls"><div className="supplier-requests-search"><Search size={19}/><UppercaseInput aria-label="Buscar solicitudes" value={search} maxLength={200} placeholder="CLIENTE, REFERENCIA O CÓDIGO…" onChange={event => setSearch(event.target.value)}/>{search && <button type="button" className="icon-button" aria-label="Limpiar búsqueda de solicitudes" onClick={() => setSearch('')}><X size={16}/></button>}</div><div className="supplier-request-filters" role="group" aria-label="Estado de las solicitudes">{([{ value: '', label: 'Todas' }, { value: 'pending', label: 'Pendientes' }, { value: 'reviewed', label: 'En revisión' }, { value: 'quoted', label: 'Por confirmar' }, { value: 'adjustment', label: 'Ajustes' }, { value: 'handshaked', label: 'Acuerdos' }] as const).map(filter => <button type="button" key={filter.value} aria-pressed={status === filter.value} className={status === filter.value ? 'selected' : ''} onClick={() => { setStatus(filter.value); setPage(1); }}>{filter.label}</button>)}</div></div>
    {error && <div className="notice error supplier-request-error" role="alert"><span>{error}</span><button type="button" onClick={refresh}>Intentar de nuevo</button></div>}
    <div className="supplier-request-list" role="region" aria-label="Listado de solicitudes" aria-busy={loading}>
      {loading ? <RequestSkeletons/> : error ? null : !data?.results.length ? <div className="supplier-request-empty"><ClipboardList size={34}/><h3>{appliedSearch || status ? 'No hay solicitudes para estos filtros.' : 'Todavía no has recibido solicitudes.'}</h3><p>{appliedSearch || status ? 'Prueba con otro cliente, referencia o código, o cambia el estado.' : 'Cuando un cliente solicite tus repuestos, podrás revisar aquí sus cantidades y datos.'}</p>{(appliedSearch || status) && <button type="button" className="button soft small" onClick={() => { setSearch(''); setAppliedSearch(''); setStatus(''); setPage(1); }}>Limpiar filtros</button>}</div> : <div className="supplier-request-cards">{data.results.map(item => <article className="supplier-request-card" key={item.id} aria-label={`Solicitud ${item.reference} de ${item.client.name}`}><div className="supplier-request-card-main"><div className="supplier-request-reference"><h3>{item.reference}</h3><DealStatusBadge status={item.status}/>{item.availability_alert && <span className={`supplier-request-alert ${item.availability_alert}`}><TriangleAlert size={13}/>{availabilityAlerts[item.availability_alert]}</span>}</div><p className="supplier-request-client"><UserRound size={15}/>{item.client.name}</p><p className="supplier-request-date"><CalendarDays size={14}/>{dateLabel(item.created_at)}</p></div><div className="supplier-request-counts"><span><strong>{item.line_count}</strong>{item.line_count === 1 ? 'artículo' : 'artículos'}</span><span><strong>{item.unit_count.toLocaleString('es-PA')}</strong>{item.unit_count === 1 ? 'unidad solicitada' : 'unidades solicitadas'}</span></div><Link className="button soft small" aria-label={`Ver solicitud ${item.reference}`} href={supplierOrderHref(account.id, item.id)} prefetch={false} onNavigate={() => window.history.replaceState(window.history.state, "", supplierRequestsHref(account.id))}>Abrir orden<ArrowUpRight size={16}/></Link></article>)}</div>}
    </div>
    <div className="pagination supplier-request-pagination"><span>{data ? `${data.count} ${data.count === 1 ? 'solicitud' : 'solicitudes'}` : 'Solicitudes de clientes'}</span><div><button type="button" className="button soft small" disabled={!data?.previous || loading} onClick={() => setPage(value => value - 1)}>Anterior</button><span>Página {page}</span><button type="button" className="button soft small" disabled={!data?.next || loading} onClick={() => setPage(value => value + 1)}>Siguiente</button></div></div>
    <div className="supplier-request-private-note"><LockKeyhole size={16}/><p>Al abrir una orden empieza la revisión y sus artículos se bloquean. Las nuevas cestas se agregarán a otra orden pendiente.</p></div>
  </section>;
}
