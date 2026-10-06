'use client';
import { useEffect, useState } from 'react';
import { ArrowUpRight, LoaderCircle, Search, Users, X } from 'lucide-react';
import { percent } from './price-explanation';
import { Account, request } from '@/lib/types';
import type { PricingClientPage } from '@/lib/pricing-types';

const failure = (error: unknown, fallback: string) => error instanceof TypeError ? 'Se perdió la conexión. Inténtalo de nuevo.' : error instanceof Error ? error.message : fallback;
const day = (value: string) => new Date(value).toLocaleDateString('es-PA', { timeZone: 'America/Panama', dateStyle: 'medium' });

// "Clientes": only accounts that sent at least one order to this supplier, each with the summary of its private profile.
export default function SupplierClients({ account, onOpen }: { account: Account; onOpen: (clientId: string) => void }) {
  const [search, setSearch] = useState('');
  const [applied, setApplied] = useState('');
  const [page, setPage] = useState(1);
  const [data, setData] = useState<PricingClientPage | null>(null);
  const [error, setError] = useState('');
  const [revision, setRevision] = useState(0);
  useEffect(() => { const timer = setTimeout(() => { if (search.trim() !== applied) { setApplied(search.trim()); setPage(1); } }, 350); return () => clearTimeout(timer); }, [search, applied]);
  useEffect(() => {
    let cancelled = false; setError('');
    const query = new URLSearchParams({ page: String(page), ...(applied ? { search: applied } : {}) });
    request<PricingClientPage>(`/api/market/accounts/${account.id}/clients?${query}`).then(value => { if (!cancelled) setData(value); })
      .catch(caught => { if (!cancelled) setError(failure(caught, 'No se pudieron cargar tus clientes.')); });
    return () => { cancelled = true; };
  }, [account.id, applied, page, revision]);
  return <>
    <div className="supplier-pricing-heading"><div><h2>Clientes</h2><p>Asigna a cada cliente su lista, su descuento general, su moneda y sus condiciones. Solo aparecen los clientes que te han enviado órdenes.</p></div></div>
    <div className="supplier-requests-search price-grid-search"><Search size={18}/><input aria-label="Buscar clientes" value={search} maxLength={200} placeholder="Nombre del cliente o código en tu sistema…" autoComplete="off" onChange={event => setSearch(event.target.value)}/>{search && <button type="button" className="icon-button" aria-label="Limpiar búsqueda de clientes" onClick={() => setSearch('')}><X size={16}/></button>}</div>
    {error && <div className="notice error" role="alert"><span>{error}</span><button type="button" onClick={() => setRevision(value => value + 1)}>Intentar de nuevo</button></div>}
    {!data && !error ? <div className="loading"><LoaderCircle className="spin"/>Cargando tus clientes…</div> : data && (!data.results.length
      ? <div className="empty-state compact"><Users size={30}/><h3>{applied ? 'No hay clientes para esta búsqueda.' : 'Todavía no tienes clientes.'}</h3><p>Cuando un cliente te envíe su primera orden podrás configurar aquí su perfil comercial.</p></div>
      : <div className="table-scroll"><table className="pricing-client-table" aria-label="Clientes"><thead><tr><th>Cliente</th><th>Código en tu sistema</th><th>Lista</th><th>Descuento general</th><th>Reglas propias</th><th>Órdenes</th><th>Última orden</th><th><span className="sr-only">Abrir</span></th></tr></thead>
        <tbody>{data.results.map(client => <tr key={client.id}>
          <td><strong>{client.name}</strong>{!client.profile.exists && <small>Sin perfil comercial</small>}</td><td>{client.profile.customer_code || '—'}</td>
          <td>{client.profile.price_list ? <>{client.profile.price_list.code}{!client.profile.price_list.active && <small>Archivada · se usa {data.default_list?.code || 'la predeterminada'}</small>}</> : <span className="pricing-muted">{data.default_list ? `${data.default_list.code} (predeterminada)` : 'Predeterminada'}</span>}</td>
          <td>{Number(client.profile.discount_percent) > 0 ? percent(client.profile.discount_percent) : '—'}</td><td className="number-cell">{client.profile.rule_count}</td>
          <td className="number-cell">{client.order_count}</td><td>{day(client.last_order_at)}</td>
          <td><button type="button" className="button soft small" aria-label={`Abrir perfil comercial de ${client.name}`} onClick={() => onOpen(client.id)}>{client.profile.exists ? 'Abrir perfil' : 'Configurar'}<ArrowUpRight size={14}/></button></td></tr>)}</tbody></table></div>)}
    {data && data.count > data.results.length && <div className="pagination"><span>{data.count} clientes</span><div><button type="button" className="button soft small" disabled={!data.previous} onClick={() => setPage(value => value - 1)}>Anterior</button><span>Página {page}</span><button type="button" className="button soft small" disabled={!data.next} onClick={() => setPage(value => value + 1)}>Siguiente</button></div></div>}
  </>;
}
