'use client';
import { useMemo } from 'react';
import type { ColDef } from 'ag-grid-community';
import type { CustomCellRendererProps } from 'ag-grid-react';
import ClientGrid from './client-grid';
import type { QuotationTrace, TraceException } from '@/lib/pricing-types';

// Supplier-only "Ver origen de precios": each line of a published quotation with the stock it relied on, where its price came from and the
// alerts confirmed when it was sent, in a grid like the quotation's own lines.
type Line = Extract<QuotationTrace, { available: true }>['lines'][number];
type Cell = CustomCellRendererProps<Line, unknown, { currency: string }>;
const money = (value: string | number, currency = 'USD') => new Intl.NumberFormat('es-PA', { style: 'currency', currency }).format(Number(value));
const priceSources = { engine: 'Lista o regla', previous: 'Versión anterior', manual: 'Manual', none: 'Sin precio', unspecified: 'Sin especificar' };
// Keep in sync with EXCEPTION_CODES (mall/quotation_exceptions.py); the trace lists the block and confirm ones.
const alertNames: Record<string, string> = { offered_gt_available: 'Más que las disponibles', offered_gt_requested: 'Más que las solicitadas',
  identity_changed: 'Artículo cambiado', zero_price: 'Precio 0,00', below_floor: 'Bajo tu precio mínimo', stale_price: 'Precio sugerido cambió',
  quantity_missing: 'Falta la cantidad', price_missing: 'Falta el precio', all_zero: 'Sin unidades', draft_outdated: 'Borrador desactualizado',
  reduced_to_stock: 'Ajustado a existencias', zero_offered: 'No ofrecido', manual_price: 'Precio manual', differs_from_list: 'Distinto de tu lista',
  no_list_price: 'Sin precio en tu lista', currency_parity: 'Paridad USD/PAB', currency_mismatch: 'Moneda distinta', fallback_list: 'Lista de respaldo',
  rule_conflict: 'Reglas igual de específicas', no_profile: 'Cliente sin perfil comercial', client_price_request: 'El cliente pide precio',
  assistant_unmatched: 'Mencionado fuera de la orden' };
const SHOWN_ALERTS = 2;
const alerts = (line: Line) => line.exceptions.filter(item => item.severity !== 'info');
const alertText = (item: TraceException) => `${alertNames[item.code] || item.code} (${item.context}) · ${item.acknowledged_by ? `confirmada por ${item.acknowledged_by.name}` : 'sin confirmar'}`;

function ItemCell({ data }: Cell) {
  return data ? <div className="admin-grid-stack"><strong>{data.codigo}</strong><small>{data.description}</small></div> : null;
}
function SourceCell({ data, context }: Cell) {
  return data ? <div className="admin-grid-stack"><span>{priceSources[data.price_source]}</span>{data.suggested_price && <small>Sugerido {money(data.suggested_price, context.currency)}</small>}</div> : null;
}
// Two alerts at most on the line; the tooltip lists them all, in full.
function AlertsCell({ data }: Cell) {
  if (!data) return null;
  const list = alerts(data);
  if (!list.length) return <span className="admin-grid-muted">—</span>;
  return <div className="admin-grid-stack trace-alerts">{list.slice(0, SHOWN_ALERTS).map(item => <small key={item.code} className={item.acknowledged_by ? '' : 'unconfirmed'}>{alertText(item)}</small>)}
    {list.length > SHOWN_ALERTS && <small>y {list.length - SHOWN_ALERTS} más</small>}</div>;
}

export default function QuoteTraceGrid({ lines, currency }: { lines: Line[]; currency: string }) {
  const columns = useMemo<ColDef<Line>[]>(() => [
    { colId: 'item', headerName: 'Artículo', flex: 2, minWidth: 160, valueGetter: ({ data }) => data?.codigo, cellRenderer: ItemCell,
      tooltipValueGetter: ({ data }) => data && `${data.codigo}\n${data.description}` },
    { colId: 'quantity', headerName: 'Ofrecidas', field: 'quantity', width: 108, type: 'rightAligned', cellClass: ['ag-right-aligned-cell', 'admin-grid-number'] },
    { colId: 'at_quote', headerName: 'Al cotizar', headerTooltip: 'Disponibles al cotizar', width: 120, type: 'rightAligned', cellClass: ['ag-right-aligned-cell'],
      valueGetter: ({ data }) => data && (data.identity_ok_at_quote ? data.available_at_quote : 'Artículo cambiado') },
    { colId: 'at_accept', headerName: 'Al confirmar', headerTooltip: 'Disponibles cuando el cliente confirmó', width: 134, type: 'rightAligned', cellClass: ['ag-right-aligned-cell'], valueGetter: ({ data }) => data?.available_at_accept ?? '—' },
    { colId: 'source', headerName: 'Origen del precio', headerTooltip: 'Origen del precio', flex: 1, minWidth: 140, valueGetter: ({ data }) => data && priceSources[data.price_source], cellRenderer: SourceCell },
    { colId: 'alerts', headerName: 'Alertas', flex: 2, minWidth: 220, valueGetter: ({ data }) => data && alerts(data).length, cellRenderer: AlertsCell,
      tooltipValueGetter: ({ data }) => data && alerts(data).length ? alerts(data).map(alertText).join('\n') : undefined },
  ], []);
  return <ClientGrid<Line> label="Origen de los precios por artículo" className="quote-trace-lines" rows={lines} columns={columns} rowId={line => line.order_line_id}
    rowHeight={58} maxRows={12} context={{ currency }}/>;
}
