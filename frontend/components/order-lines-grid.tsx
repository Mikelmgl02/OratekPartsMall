'use client';
import { useMemo, type ReactNode } from 'react';
import type { ColDef, RowClassParams } from 'ag-grid-community';
import type { CustomCellRendererProps } from 'ag-grid-react';
import ClientGrid from './client-grid';
import { sortedLines } from './deal-items-table';
import type { Deal, DealAgreement, Quotation } from '@/lib/deal-types';
import type { SupplierRequestLine } from '@/lib/request-types';

// The order's lines on the supplier's order page (supplier-order-workspace.tsx), read-only, as one grid that fills the page: Código ·
// Descripción (brand, and grupo › subgrupo under it) · Disp (the stock to confirm, before the quotation) · Pedido · Precio sugerido (before a
// quotation, with price lists) · Cant. · Precio · Subtotal (once quoted; after the handshake, what remains of the agreement with what went
// short in its note). In grupo, subgrupo and description order until a header sorts it. The editor (quotation-editor.tsx) takes its place,
// with the same columns, while the supplier prepares the quotation.
type Line = Deal['lines'][number];
type Row = { line: Line; offered: Quotation['lines'][number] | undefined; kept: DealAgreement['lines'][number] | undefined };
type Cell = CustomCellRendererProps<Row>;
const money = (value: string | number, currency = 'USD') => new Intl.NumberFormat('es-PA', { style: 'currency', currency }).format(Number(value));
const units = (value: number) => value.toLocaleString('es-PA');
const stockOf = (line: Line) => 'stock' in line ? (line as SupplierRequestLine).stock : undefined;
// The cells that show more than their value (the stock's notes, a shortfall under Cant., the suggestion's origin) draw again on every read of
// the order: a row's new data alone redraws only the cells whose value changed.
const always = () => false;
const given = (row: Row) => row.kept ? row.kept.final : row.offered?.quantity ?? 0;  // what the quotation (or what remains of the agreement) brings

// Disp: available units (reported less reserved), red when short of what was asked, with what is missing; "?" when the code or its link to
// the SKU changed and the supplier must check it in Inventario.
export function DispCell({ line }: { line: Line }) {
  const stock = stockOf(line);
  const state = !stock ? 'unknown' : stock.available_quantity >= line.quantity ? 'enough' : stock.available_quantity > 0 ? 'short' : 'none';
  return <span className={`order-disp ${state}`} title={stock ? `Reportadas: ${units(stock.reported_quantity)} · Reservadas: ${units(stock.reserved_quantity)}${stock.held_for_deal ? ` (${units(stock.held_for_deal)} para esta orden)` : ''}`
    : 'El código o su vínculo con el SKU cambió: confirma la equivalencia en Inventario.'}>
    <strong>{stock ? units(stock.available_quantity) : '?'}</strong>{stock ? stock.shortfall > 0 && <small>faltan {units(stock.shortfall)}</small> : <small>revisar</small>}
  </span>;
}
export function DescriptionCell({ description, brand, grupo, subgrupo }: { description: string; brand?: string; grupo?: string; subgrupo?: string }) {
  const group = [grupo, subgrupo].filter(Boolean).join(' › ');
  return <div className="order-line-description"><span title={description}>{description}{brand && <em> · {brand}</em>}</span>{group && <small>{group}</small>}</div>;
}

export default function OrderLinesGrid({ deal, quote, agreement, showStock, price }: {
  deal: Deal; quote: Quotation | null; agreement: DealAgreement | null; showStock: boolean; price?: (line: Line) => ReactNode;
}) {
  const offered = new Map((quote?.lines || []).map(line => [line.order_line_id, line]));
  const reduced = agreement?.lines.some(line => line.missing || line.pending_missing) ? new Map(agreement.lines.map(line => [line.order_line_id, line])) : null;
  const currency = quote?.currency || 'USD', suggested = !!price && !quote;
  const rows: Row[] = sortedLines(deal.lines).map(line => ({ line, offered: offered.get(line.id), kept: reduced?.get(line.id) }));
  const quoted = !!quote;
  // Built again only when the columns themselves change: the order is read every 15 s, and new definitions would undo a resized or moved column.
  const columns = useMemo<ColDef<Row>[]>(() => [
    { colId: 'codigo', headerName: 'Código', width: 160, minWidth: 120, valueGetter: ({ data }) => data?.line.codigo ?? '', cellClass: 'order-line-code' },
    { colId: 'description', headerName: 'Descripción', flex: 2, minWidth: 160, valueGetter: ({ data }) => data?.line.description ?? '',
      cellRenderer: ({ data }: Cell) => data ? <DescriptionCell description={data.line.description} brand={data.line.brand} grupo={data.line.grupo} subgrupo={data.line.subgrupo}/> : null },
    ...(showStock ? [{ colId: 'available', headerName: 'Disp', width: 90, type: 'rightAligned', equals: always, valueGetter: ({ data }) => data ? stockOf(data.line)?.available_quantity ?? -1 : 0,
      cellRenderer: ({ data }: Cell) => data ? <DispCell line={data.line}/> : null } satisfies ColDef<Row>] : []),
    { colId: 'requested', headerName: 'Pedido', width: 86, type: 'rightAligned', valueGetter: ({ data }) => data?.line.quantity ?? 0, valueFormatter: ({ value }) => units(value ?? 0) },
    ...(suggested ? [{ colId: 'price', headerName: 'Precio sugerido', width: 190, sortable: false, type: 'rightAligned', autoHeight: true, equals: always,
      cellRenderer: ({ data }: Cell) => data ? <div className="order-line-suggested">{price!(data.line)}</div> : null } satisfies ColDef<Row>] : []),
    ...(quoted ? [
      // Grows with its notes (an accepted shortfall and a new one reported on the same line); other rows keep their height.
      { colId: 'quantity', headerName: 'Cant.', width: 124, type: 'rightAligned', autoHeight: true, equals: always, valueGetter: ({ data }) => data ? given(data) : 0,
        cellRenderer: ({ data }: Cell) => {
          if (!data) return null;
          const short = !!data.offered && given(data) < data.line.quantity;
          return <div className={`order-line-quantity ${short ? 'short' : ''}`}>
            <strong>{!data.offered ? '—' : given(data) === 0 ? data.kept ? '0' : 'No disponible' : units(given(data))}</strong>
            {data.kept && data.kept.final !== data.kept.agreed && <small>acordadas {units(data.kept.agreed)}</small>}
            {data.kept && data.kept.missing > 0 && <small>{units(data.kept.missing)} {data.kept.missing === 1 ? 'faltante aceptado' : 'faltantes aceptados'} · quedan {units(data.kept.final)}</small>}
            {data.kept && data.kept.pending_missing > 0 && <small className="pending">Faltante reportado: {units(data.kept.pending_missing)} por responder</small>}
          </div>;
        } },
      { colId: 'unit_price', headerName: 'Precio', width: 116, type: 'rightAligned', valueGetter: ({ data }) => data && given(data) > 0 && data.offered ? Number(data.offered.unit_price) : null,
        valueFormatter: ({ value }) => value === null || value === undefined ? '—' : money(value, currency) },
      { colId: 'line_total', headerName: 'Subtotal', width: 124, type: 'rightAligned', cellClass: ['ag-right-aligned-cell', 'order-line-total'],
        valueGetter: ({ data }) => data && given(data) > 0 && data.offered ? Number(data.kept ? data.kept.total : data.offered.total) : null,
        valueFormatter: ({ value }) => value === null || value === undefined ? '—' : money(value, currency) },
    ] satisfies ColDef<Row>[] : []),
  ], [showStock, suggested, quoted, currency, price]);
  // One tab stop: the arrow keys move inside it, Tab goes on to the sidebar's actions instead of walking every cell.
  const gridProps = useMemo(() => ({ tabToNextCell: () => false as const, tabToNextHeader: () => false as const,
    rowClassRules: { 'order-line-off': ({ data }: RowClassParams<Row>) => quoted && !!data && !(data.offered && given(data) > 0) } }), [quoted]);
  return <ClientGrid<Row> label="Artículos de la orden" rows={rows} columns={columns} rowId={row => row.line.id} rowHeight={58} fill="parent" className="order-lines-grid"
    gridProps={gridProps}/>;
}
