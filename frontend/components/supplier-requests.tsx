'use client';

import { useEffect, useMemo, useState } from 'react';
import { ClipboardList, LockKeyhole, RefreshCw, Search, X } from 'lucide-react';
import { UppercaseInput } from './uppercase-field';
import ServerGrid from './server-grid';
import { requestColumns, requestOrdering } from './supplier-grids';
import { Account } from '@/lib/types';
import { SupplierRequestListItem, SupplierRequestSummary } from '@/lib/request-types';

type StatusFilter = '' | SupplierRequestSummary['status'];

export default function SupplierRequests({ account }: { account: Account }) {
  const [search, setSearch] = useState('');
  const [appliedSearch, setAppliedSearch] = useState('');
  const [status, setStatus] = useState<StatusFilter>('');
  const [revision, setRevision] = useState(0);
  const [count, setCount] = useState<number | null>(null);
  useEffect(() => {
    if (search.trim() === appliedSearch) return;
    const timer = setTimeout(() => setAppliedSearch(search.trim()), 350);
    return () => clearTimeout(timer);
  }, [search, appliedSearch]);
  const params = useMemo(() => ({ search: appliedSearch, status }), [appliedSearch, status]);
  const context = useMemo(() => ({ accountId: account.id }), [account.id]);
  const columns = useMemo(() => requestColumns(), []);
  const refresh = () => setRevision(value => value + 1);
  // New orders arrive while the page is open: the loaded rows are read again every 15 s, keeping the scroll position.
  useEffect(() => { const timer = setInterval(() => { if (document.visibilityState === 'visible') setRevision(value => value + 1); }, 15000); return () => clearInterval(timer); }, []);
  const filtered = Boolean(appliedSearch || status);

  return <section className="supplier-requests" aria-label="Solicitudes de clientes">
    <div className="supplier-requests-heading"><div><h2>Órdenes de tus clientes</h2><p>Gestiona órdenes, cotizaciones y acuerdos de {account.name}.</p></div><button type="button" className="button soft small" onClick={refresh}><RefreshCw size={15}/>Actualizar solicitudes</button></div>
    <div className="supplier-requests-controls"><div className="supplier-requests-search"><Search size={19}/><UppercaseInput aria-label="Buscar solicitudes" value={search} maxLength={200} placeholder="CLIENTE, REFERENCIA O CÓDIGO…" onChange={event => setSearch(event.target.value)}/>{search && <button type="button" className="icon-button" aria-label="Limpiar búsqueda de solicitudes" onClick={() => setSearch('')}><X size={16}/></button>}</div><div className="supplier-request-filters" role="group" aria-label="Estado de las solicitudes">{([{ value: '', label: 'Todas' }, { value: 'pending', label: 'Pendientes' }, { value: 'reviewed', label: 'En revisión' }, { value: 'quoted', label: 'Por confirmar' }, { value: 'adjustment', label: 'Ajustes' }, { value: 'handshaked', label: 'Acuerdos' }] as const).map(filter => <button type="button" key={filter.value} aria-pressed={status === filter.value} className={status === filter.value ? 'selected' : ''} onClick={() => { setStatus(filter.value); }}>{filter.label}</button>)}</div></div>
    <span className="inventory-count supplier-request-count" aria-live="polite">{count === null ? 'Cargando solicitudes…' : `${count.toLocaleString('es-PA')} ${count === 1 ? 'solicitud' : 'solicitudes'}`}</span>
    <ServerGrid<SupplierRequestListItem> storageKey={`supplier-requests`} label="Listado de solicitudes" path={`/api/market/accounts/${account.id}/requests`} params={params} columns={columns}
      rowId={row => row.id} ordering={requestOrdering} revision={revision} rowHeight={60} context={context} onCount={setCount} className="supplier-request-grid"
      empty={<div className="supplier-request-empty"><ClipboardList size={34}/><h3>{filtered ? 'No hay solicitudes para estos filtros.' : 'Todavía no has recibido solicitudes.'}</h3><p>{filtered ? 'Prueba con otro cliente, referencia o código, o cambia el estado.' : 'Cuando un cliente solicite tus repuestos, podrás revisar aquí sus cantidades y datos.'}</p>{filtered && <button type="button" className="button soft small" onClick={() => { setSearch(''); setAppliedSearch(''); setStatus(''); }}>Limpiar filtros</button>}</div>}/>
    <div className="supplier-request-private-note"><LockKeyhole size={16}/><p>Al abrir una orden empieza la revisión y sus artículos se bloquean. Las nuevas cestas se agregarán a otra orden pendiente.</p></div>
  </section>;
}
