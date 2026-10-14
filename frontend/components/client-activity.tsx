'use client';
import { useCallback, useEffect, useMemo, useState } from 'react';
import dynamic from 'next/dynamic';
import type { ColDef } from 'ag-grid-community';
import type { CustomCellRendererProps } from 'ag-grid-react';
import { ArrowUpRight, History, LoaderCircle, MessageCircle, PackageSearch, ReceiptText, ShoppingBag } from 'lucide-react';
import ClientRequestNavigation from './client-request-navigation';
import DealDetail from './deal-detail';
import { Account, BasketEvent, request } from '@/lib/types';

const ClientGrid = dynamic(() => import('./client-grid'), { ssr: false, loading: () => <div className="quotation-grid-loading" role="status"><LoaderCircle size={18} className="spin"/>Cargando actividad…</div> }) as typeof import('./client-grid').default;

// Actividad: the client's two ledgers, newest first. Actividad de cesta is what went in and out of the cesta (from the site or WhatsApp, and
// what was sent in an order); Actividad de compras is what happened in each order (quotations, adjustments, the agreement, its progress,
// shortfalls, cancellations), the same events each order shows. Both load 30 at a time; Ver más brings the earlier ones.
export type PurchaseEvent = { id: number; kind: string; description: string; created_at: string; order: { id: string; reference: string; status: string };
  supplier_name: string; account_name: string; actor_name: string };
type Ledger<T> = { results: T[]; has_more: boolean };
type View = 'cesta' | 'compras';

const when = (value: string) => new Date(value).toLocaleString('es-PA', { timeZone: 'America/Panama', dateStyle: 'medium', timeStyle: 'short' });
const moves: Record<BasketEvent['kind'], string> = { added: 'Agregado', changed: 'Cambiado', removed: 'Quitado', moved_in: 'Traído del navegador', sent: 'Enviado' };
const sources: Record<string, string> = { whatsapp: 'WhatsApp', faltante: 'Lo que faltó', web: 'Sitio' };

// One ledger, read from the newest; before= the last id already shown brings the earlier page.
function useLedger<T extends { id: number }>(path: string) {
  const [rows, setRows] = useState<T[] | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => {
    let current = true;
    if (!path) return;
    setRows(null); setError('');
    request<Ledger<T>>(path).then(data => { if (current) { setRows(data.results); setHasMore(data.has_more); } })
      .catch(() => { if (current) { setRows([]); setError('No se pudo cargar la actividad.'); } });
    return () => { current = false; };
  }, [path]);
  const more = useCallback(async () => {
    if (!rows?.length) return;
    setBusy(true);
    try {
      const data = await request<Ledger<T>>(`${path}?before=${rows.at(-1)!.id}`);
      setRows(current => [...(current || []), ...data.results]); setHasMore(data.has_more);
    } catch { setError('No se pudo cargar más actividad.'); } finally { setBusy(false); }
  }, [path, rows]);
  return { rows, hasMore, busy, error, more };
}

type CestaCell = CustomCellRendererProps<BasketEvent>;
const cestaColumns: ColDef<BasketEvent>[] = [
  { colId: 'when', headerName: 'Fecha', width: 190, valueGetter: ({ data }) => data ? when(data.created_at) : '', cellClass: 'admin-grid-text muted' },
  { colId: 'move', headerName: 'Movimiento', width: 170, cellRenderer: ({ data }: CestaCell) => data ? <span className={`client-activity-move ${data.kind}`}>{moves[data.kind]}</span> : null },
  { colId: 'article', headerName: 'Artículo', flex: 1, minWidth: 200, cellRenderer: ({ data }: CestaCell) => data ? <div className="admin-grid-stack"><strong>{data.codigo}</strong><small>{[data.brand, data.supplier_name].filter(Boolean).join(' · ')}</small></div> : null },
  { colId: 'quantity', headerName: 'Cantidad', width: 130, type: 'rightAligned', cellClass: ['ag-right-aligned-cell', 'admin-grid-number'],
    valueGetter: ({ data }) => !data ? '' : data.kind === 'added' || data.kind === 'moved_in' ? `+${data.quantity_after}` : data.kind === 'removed' ? `−${data.quantity_before}`
      : data.kind === 'sent' ? `${data.quantity_before - data.quantity_after} enviadas` : `${data.quantity_before} → ${data.quantity_after}` },
  { colId: 'source', headerName: 'Origen', width: 150, cellRenderer: ({ data }: CestaCell) => !data ? null : data.source === 'whatsapp' ? <span className="basket-page-source"><MessageCircle size={11}/>WhatsApp</span>
    : data.source === 'faltante' ? <span className="basket-page-source missing"><PackageSearch size={11}/>Lo que faltó</span> : <span className="admin-grid-muted">{sources[data.source] || 'Sitio'}</span> },
  { colId: 'reference', headerName: 'Orden', width: 200, valueGetter: ({ data }) => data?.reference || '—', cellClass: 'admin-grid-text' },
];

type CompraCell = CustomCellRendererProps<PurchaseEvent, unknown, { open: (event: PurchaseEvent) => void }>;
const compraColumns: ColDef<PurchaseEvent>[] = [
  { colId: 'when', headerName: 'Fecha', width: 190, valueGetter: ({ data }) => data ? when(data.created_at) : '', cellClass: 'admin-grid-text muted' },
  { colId: 'order', headerName: 'Orden', width: 230, cellRenderer: ({ data, context }: CompraCell) => data ? <button type="button" className="button text small client-activity-order"
    aria-label={`Abrir orden ${data.order.reference}`} onClick={() => context.open(data)}>{data.order.reference}<ArrowUpRight size={13}/></button> : null },
  { colId: 'supplier', headerName: 'Proveedor', width: 190, valueGetter: ({ data }) => data?.supplier_name ?? '', cellClass: 'admin-grid-text strong' },
  { colId: 'event', headerName: 'Evento', flex: 1, minWidth: 260, valueGetter: ({ data }) => data?.description ?? '', wrapText: true, autoHeight: true, cellClass: 'admin-grid-text client-activity-description' },
  { colId: 'actor', headerName: 'Por', width: 220, cellRenderer: ({ data }: CompraCell) => data ? <div className="admin-grid-stack"><strong>{data.account_name}</strong><small>{data.actor_name}</small></div> : null },
];

function LedgerGrid<T extends { id: number }>({ label, ledger, columns, context, empty }: { label: string; ledger: ReturnType<typeof useLedger<T>>; columns: ColDef<T>[]; context?: unknown; empty: string }) {
  if (!ledger.rows) return <div className="loading" role="status"><LoaderCircle className="spin"/>Cargando actividad…</div>;
  return <>
    {ledger.error && <div className="notice error" role="alert">{ledger.error}</div>}
    <ClientGrid<T> label={label} rows={ledger.rows} columns={columns} rowId={row => String(row.id)} rowHeight={56} fill context={context} className="client-activity-grid"
      empty={<div className="supplier-request-empty"><History size={32}/><h3>{empty}</h3></div>}/>
    {ledger.hasMore && <button type="button" className="button soft small client-activity-more" disabled={ledger.busy} onClick={() => void ledger.more()}>{ledger.busy && <LoaderCircle size={14} className="spin"/>}Ver más actividad</button>}
  </>;
}

export default function ClientActivity({ account, draftCount, onDraft, onSent }: { account?: Account; draftCount: number; onDraft: () => void; onSent: () => void }) {
  const [view, setView] = useState<View>('cesta');
  const [opened, setOpened] = useState<PurchaseEvent | null>(null);
  const canView = !!account?.capabilities.includes('client');
  const cesta = useLedger<BasketEvent>(canView && account ? `/api/market/accounts/${account.id}/basket/activity` : '');
  // The orders' ledger loads when its view is opened.
  const compras = useLedger<PurchaseEvent>(canView && account && view === 'compras' ? `/api/market/accounts/${account.id}/purchase-activity` : '');
  const context = useMemo(() => ({ open: setOpened }), []);
  return <main className="basket-page client-activity-page" aria-label="Actividad"><div className="section-container basket-page-container">
    <ClientRequestNavigation selected="activity" count={draftCount} canViewSent={canView} onDraft={onDraft} onSent={onSent} onActivity={() => {}}/>
    {!canView || !account ? <div className="notice">Inicia sesión y selecciona una cuenta con rol de cliente para consultar su actividad.</div> : <section className="supplier-requests client-activity" aria-label={view === 'cesta' ? 'Actividad de cesta' : 'Actividad de compras'}>
      <div className="supplier-requests-heading"><div><h2>{view === 'cesta' ? 'Actividad de cesta' : 'Actividad de compras'}</h2><p>{view === 'cesta'
        ? 'Lo que agregaste, cambiaste, quitaste o enviaste desde tu cesta, en el sitio o por WhatsApp.'
        : 'Lo que pasó en cada orden: cotizaciones, ajustes, acuerdos, avance, faltantes y cancelaciones.'}</p></div>
        <div className="supplier-request-filters client-activity-views" role="group" aria-label="Vista de actividad">
          <button type="button" aria-pressed={view === 'cesta'} className={view === 'cesta' ? 'selected' : ''} onClick={() => setView('cesta')}><ShoppingBag size={14}/>Actividad de cesta</button>
          <button type="button" aria-pressed={view === 'compras'} className={view === 'compras' ? 'selected' : ''} onClick={() => setView('compras')}><ReceiptText size={14}/>Actividad de compras</button>
        </div></div>
      {view === 'cesta' ? <LedgerGrid<BasketEvent> label="Actividad de cesta" ledger={cesta} columns={cestaColumns} empty="Tu cesta aún no tiene movimientos."/>
        : <LedgerGrid<PurchaseEvent> label="Actividad de compras" ledger={compras} columns={compraColumns} context={context} empty="Tus órdenes aún no tienen actividad."/>}
    </section>}
    {opened && account && <DealDetail key={opened.order.id} account={account} orderId={opened.order.id} reference={opened.order.reference} side="client" onClose={() => setOpened(null)}/>}
  </div></main>;
}
