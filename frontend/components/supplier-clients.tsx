'use client';
import { createContext, useContext, useEffect, useMemo, useState } from 'react';
import type { ColDef } from 'ag-grid-community';
import type { CustomCellRendererProps } from 'ag-grid-react';
import ServerGrid from './server-grid';
import { ArrowUpRight, Search, Users, X } from 'lucide-react';
import { percent } from './price-explanation';
import { Account } from '@/lib/types';
import type { PricingClient, PricingClientPage } from '@/lib/pricing-types';

const day = (value: string) => new Date(value).toLocaleDateString('es-PA', { timeZone: 'America/Panama', dateStyle: 'medium' });

// "Clientes": only accounts that sent at least one order to this supplier, each with the summary of its private profile.
type Cell = CustomCellRendererProps<PricingClient, unknown, { onOpen: (clientId: string) => void }>;
const DefaultList = createContext<PricingClientPage['default_list']>(null);
function ClientCell({ data }: Cell) {
  return data ? <div className="admin-grid-stack"><strong>{data.name}</strong>{!data.profile.exists && <small>Sin perfil comercial</small>}</div> : null;
}
function ListCell({ data }: Cell) {
  const fallback = useContext(DefaultList);
  if (!data) return null;
  return data.profile.price_list ? <div className="admin-grid-stack"><strong>{data.profile.price_list.code}</strong>{!data.profile.price_list.active && <small>Archivada · se usa {fallback?.code || 'la predeterminada'}</small>}</div>
    : <span className="pricing-muted">{fallback ? `${fallback.code} (predeterminada)` : 'Predeterminada'}</span>;
}
function OpenCell({ data, context }: Cell) {
  return data ? <div className="admin-grid-actions"><button type="button" className="button soft small" aria-label={`Abrir perfil comercial de ${data.name}`} onClick={() => context.onOpen(data.id)}>{data.profile.exists ? 'Abrir perfil' : 'Configurar'}<ArrowUpRight size={14}/></button></div> : null;
}
const right = ['ag-right-aligned-cell'];
const columns: ColDef<PricingClient>[] = [
  { colId: 'name', headerName: 'Cliente', flex: 1.2, minWidth: 200, cellRenderer: ClientCell },
  { colId: 'code', headerName: 'Código en tu sistema', valueGetter: ({ data }) => data?.profile.customer_code || '—', width: 170, cellClass: 'admin-grid-text' },
  { colId: 'list', headerName: 'Lista', width: 190, cellRenderer: ListCell },
  { colId: 'discount', headerName: 'Descuento general', valueGetter: ({ data }) => data && Number(data.profile.discount_percent) > 0 ? percent(data.profile.discount_percent) : '—', width: 150, type: 'rightAligned', cellClass: right },
  { colId: 'rules', headerName: 'Reglas propias', valueGetter: ({ data }) => data?.profile.rule_count ?? 0, width: 130, type: 'rightAligned', cellClass: right },
  { colId: 'orders', headerName: 'Órdenes', field: 'order_count', width: 110, type: 'rightAligned', cellClass: right },
  { colId: 'last', headerName: 'Última orden', valueGetter: ({ data }) => data ? day(data.last_order_at) : '', width: 150, cellClass: 'admin-grid-text' },
  { colId: 'open', headerName: '', width: 160, cellRenderer: OpenCell, suppressHeaderMenuButton: true, resizable: false },
];

export default function SupplierClients({ account, onOpen }: { account: Account; onOpen: (clientId: string) => void }) {
  const [search, setSearch] = useState('');
  const [applied, setApplied] = useState('');
  const [defaultList, setDefaultList] = useState<PricingClientPage['default_list']>(null);
  useEffect(() => { const timer = setTimeout(() => setApplied(search.trim()), 350); return () => clearTimeout(timer); }, [search]);
  const params = useMemo(() => ({ search: applied }), [applied]);
  const context = useMemo(() => ({ onOpen }), [onOpen]);
  return <DefaultList.Provider value={defaultList}>
    <div className="supplier-pricing-heading"><div><h2>Clientes</h2><p>Asigna a cada cliente su lista, su descuento general, su moneda y sus condiciones. Solo aparecen los clientes que te han enviado órdenes.</p></div></div>
    <div className="supplier-requests-search price-grid-search"><Search size={18}/><input aria-label="Buscar clientes" value={search} maxLength={200} placeholder="Nombre del cliente o código en tu sistema…" autoComplete="off" onChange={event => setSearch(event.target.value)}/>{search && <button type="button" className="icon-button" aria-label="Limpiar búsqueda de clientes" onClick={() => setSearch('')}><X size={16}/></button>}</div>
    <ServerGrid<PricingClient> storageKey="supplier-pricing-clients" label="Clientes" path={`/api/market/accounts/${account.id}/clients`} params={params} columns={columns} rowId={client => client.id}
      ordering={{}} revision={0} rowHeight={60} pageSize={100} context={context} onPage={page => setDefaultList((page as PricingClientPage).default_list ?? null)}
      empty={<div className="empty-state compact"><Users size={30}/><h3>{applied ? 'No hay clientes para esta búsqueda.' : 'Todavía no tienes clientes.'}</h3><p>Cuando un cliente te envíe su primera orden podrás configurar aquí su perfil comercial.</p></div>}/>
  </DefaultList.Provider>;
}
