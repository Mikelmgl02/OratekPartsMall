'use client';
import Link from 'next/link';
import type { ColDef } from 'ag-grid-community';
import type { CustomCellRendererProps } from 'ag-grid-react';
import { ArrowUpRight, ClipboardCheck, FilePen, History, TriangleAlert } from 'lucide-react';
import DealStatusBadge from './deal-status';
import type { StockItem } from '@/lib/types';
import type { SupplierRequestListItem } from '@/lib/request-types';
import { supplierOrderHref, supplierRequestsHref } from '@/lib/supplier-navigation';

const number = (value: number | null | undefined) => (value ?? 0).toLocaleString('es-PA');
const right = (extra?: string) => ['ag-right-aligned-cell', ...(extra ? [extra] : [])];

// ---------------------------------------------------------------- Inventario del proveedor
// Column order matters to the import spec, which reads cells by position (Disponibles is the fourth). No pinned columns: a pinned
// column splits each grid row into separate row elements.
type InventoryCell = CustomCellRendererProps<StockItem, unknown, { openLedger: (item: StockItem) => void }>;
function ItemCell({ data }: InventoryCell) {
  return data ? <div className="admin-grid-stack"><strong>{data.codigo}</strong><small>{data.brand}{data.description ? ` · ${data.description}` : ''}</small></div> : null;
}
function MatchCell({ data }: InventoryCell) {
  return data ? <span className={`status ${data.matching_status}`}>{data.matching_status === 'matched' ? 'Vinculado' : data.matching_status === 'review' ? 'Por revisar' : 'Pendiente'}</span> : null;
}
function LedgerCell({ data, context }: InventoryCell) {
  return data ? <button type="button" className="button ghost small" aria-label={`Ver movimientos de ${data.codigo}`} onClick={() => context.openLedger(data)}><History size={14} aria-hidden="true"/>Movimientos</button> : null;
}
export const inventoryOrdering: Record<string, string> = { codigo: 'codigo', invent: 'supplier_invent_id', source: 'source', available: 'available', reserved: 'reserved_quantity', match: 'matching_status' };
export const inventoryColumns: ColDef<StockItem>[] = [
  { colId: 'codigo', headerName: 'Repuesto / código', flex: 1.4, minWidth: 230, cellRenderer: ItemCell, tooltipValueGetter: ({ data }) => data?.description },
  { colId: 'invent', headerName: 'Tu ID de inventario', field: 'supplier_invent_id', width: 170, cellClass: 'admin-grid-text' },
  { colId: 'source', headerName: 'Origen', valueGetter: ({ data }) => data ? data.source === 'apiag' ? 'apiag-cloud' : 'Carga manual' : '', width: 140, cellClass: 'admin-grid-text muted' },
  { colId: 'available', headerName: 'Disponibles', field: 'available_quantity', valueFormatter: ({ value }) => number(value), width: 124, type: 'rightAligned', cellClass: right('admin-grid-number') },
  { colId: 'reserved', headerName: 'Reservadas', field: 'reserved_quantity', valueFormatter: ({ value }) => number(value), width: 120, type: 'rightAligned', cellClass: right() },
  { colId: 'match', headerName: 'Coincidencia', width: 150, cellRenderer: MatchCell },
  { colId: 'history', headerName: 'Historial', width: 170, cellRenderer: LedgerCell, suppressHeaderMenuButton: true, resizable: false },
];

// ---------------------------------------------------------------- Solicitudes
const availabilityAlerts = { blocked: 'Confirmación bloqueada por existencias', accepted_with_shortfall: 'Confirmado con faltante' };
// The private draft of the next quotation: saved by the team ("Borrador") or waiting for a member allowed to send it ("Por aprobar").
const draftStates = { editing: ['Borrador', FilePen], review_requested: ['Por aprobar', ClipboardCheck] } as const;
const dateLabel = (value: string) => new Date(value).toLocaleString('es-PA', { timeZone: 'America/Panama', dateStyle: 'medium', timeStyle: 'short' });
type RequestCell = CustomCellRendererProps<SupplierRequestListItem, unknown, { accountId: string }>;
function ReferenceCell({ data }: RequestCell) {
  return data ? <div className="admin-grid-stack"><strong>{data.reference}</strong><small>{dateLabel(data.created_at)}</small></div> : null;
}
function AlertsCell({ data }: RequestCell) {
  if (!data) return null;
  const draft = data.draft_state ? draftStates[data.draft_state] : null;
  if (!draft && !data.availability_alert) return <span className="admin-grid-muted">—</span>;
  return <div className="admin-grid-chips supplier-request-flags">{draft && (() => { const [label, Icon] = draft; return <span className={`supplier-request-draft ${data.draft_state}`}><Icon size={13}/>{label}</span>; })()}
    {data.availability_alert && <span className={`supplier-request-alert ${data.availability_alert}`}><TriangleAlert size={13}/>{availabilityAlerts[data.availability_alert]}</span>}</div>;
}
function OpenCell({ data, context }: RequestCell) {
  return data ? <div className="admin-grid-actions"><Link className="button soft small" aria-label={`Ver solicitud ${data.reference}`} href={supplierOrderHref(context.accountId, data.id)} prefetch={false}
    onNavigate={() => window.history.replaceState(window.history.state, '', supplierRequestsHref(context.accountId))}>Abrir orden<ArrowUpRight size={15}/></Link></div> : null;
}
export const requestOrdering: Record<string, string> = { reference: 'created_at', status: 'status', client: 'client_name', lines: 'line_count', units: 'unit_count' };
// No pinned columns here either: the requests spec reads each order as one grid row.
export function requestColumns(): ColDef<SupplierRequestListItem>[] {
  return [
    { colId: 'reference', headerName: 'Orden / recibida', width: 185, cellRenderer: ReferenceCell },
    { colId: 'status', headerName: 'Estado', width: 160, cellRenderer: ({ data }: RequestCell) => data ? <DealStatusBadge status={data.status}/> : null },
    { colId: 'client', headerName: 'Cliente', valueGetter: ({ data }) => data?.client.name ?? '', flex: 1, minWidth: 170, cellClass: 'admin-grid-text strong' },
    { colId: 'lines', headerName: 'Artículos', field: 'line_count', valueFormatter: ({ value }) => number(value), width: 118, type: 'rightAligned', cellClass: right('admin-grid-number') },
    { colId: 'units', headerName: 'Unidades', field: 'unit_count', valueFormatter: ({ value }) => number(value), width: 100, type: 'rightAligned', cellClass: right() },
    { colId: 'alerts', headerName: 'Avisos', flex: 1, minWidth: 150, cellRenderer: AlertsCell },
    { colId: 'open', headerName: 'Acciones', width: 140, cellRenderer: OpenCell, suppressHeaderMenuButton: true, resizable: false },
  ];
}
