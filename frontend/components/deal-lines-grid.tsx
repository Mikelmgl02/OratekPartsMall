'use client';
import { type ReactNode, useEffect, useMemo, useRef, useState } from 'react';
import type { ColDef, GridApi } from 'ag-grid-community';
import type { CustomCellRendererProps } from 'ag-grid-react';
import { Search } from 'lucide-react';
import ClientGrid from './client-grid';
import type { Deal, Quotation } from '@/lib/deal-types';
import type { SupplierRequestLine } from '@/lib/request-types';
import { useNarrow } from '@/lib/use-narrow';

// The order's document in a grid, since an order can list many items: each item the client asked for and, once the supplier sent its
// quotation (quote), what it offers of it, at what price and for how much, in the same row. Sorted by any column, searched by code, SKU or
// description, and, for the supplier, narrowed to the items whose stock needs confirming. It grows with its rows up to a dozen, then
// scrolls (maxRows). The requested and available units come right after the item; the SKU and inventory ID follow. A quoted one keeps
// the prices in view as it narrows: below MEDIUM the SKU and inventory ID go (in the item's tooltip), and below NARROW (a phone) it keeps
// the item, the units and the amount with "2 × USD 86.50" under it.
type Line = Deal['lines'][number];
type Quoted = Quotation['lines'][number];
type Context = { price?: (line: Line) => ReactNode; quoted: Map<string, Quoted>; currency: string };
type Cell = CustomCellRendererProps<Line, unknown, Context>;
const NARROW = 640, MEDIUM = 880;
const money = (value: string | number, currency = 'USD') => new Intl.NumberFormat('es-PA', { style: 'currency', currency }).format(Number(value));
const stockOf = (line: Line) => 'stock' in line ? (line as SupplierRequestLine).stock : undefined;
const needsReview = (line: Line) => { const stock = stockOf(line); return !stock || stock.shortfall > 0; };
const offeredOf = (line: Line | undefined, context: Context) => line ? context.quoted.get(line.id) : undefined;

function ItemCell({ data }: Cell) {
  return data ? <div className="admin-grid-stack"><strong>{data.codigo}</strong><small>{data.brand ? `${data.brand} · ` : ''}{data.description}</small></div> : null;
}
function TextCell({ value }: Cell) {
  return <span className="admin-grid-text" title={String(value ?? '')}>{String(value ?? '')}</span>;
}
function StockCell({ data }: Cell) {
  if (!data) return null;
  const stock = stockOf(data);
  if (!stock) return <div className="admin-grid-stack"><strong className="stock-shortfall">Revisar artículo</strong><small>El código o el vínculo con el SKU cambió. Confirma la equivalencia en Inventario.</small></div>;
  return <div className="admin-grid-stack">
    <strong>{stock.available_quantity} <span className={stock.shortfall ? 'stock-shortfall' : 'stock-sufficient'}>· {stock.shortfall ? `Faltan ${stock.shortfall} unidades` : 'Existencias suficientes'}</span></strong>
    <small>Reportadas: {stock.reported_quantity} · Reservadas: {stock.reserved_quantity}{stock.held_for_deal ? ` (${stock.held_for_deal} para esta orden)` : ''}</small>
  </div>;
}
function PriceCell({ data, context }: Cell) {
  return data && context?.price ? <div className="admin-grid-stack">{context.price(data)}</div> : null;
}
// Ofrecidas: what the supplier offers of the item, marked when it is less than asked.
function OfferedCell({ data, context }: Cell) {
  const offer = offeredOf(data ?? undefined, context);
  if (!data || !offer) return <span className="admin-grid-muted">—</span>;
  if (offer.quantity === 0) return <span className="deal-line-short">No disponible</span>;
  return <span className={offer.quantity < data.quantity ? 'deal-line-short' : undefined} title={offer.quantity < data.quantity ? `Ofrece ${offer.quantity} de ${data.quantity}` : undefined}>{offer.quantity}</span>;
}
// Narrow: the units asked for, with what is offered under them when it differs.
function UnitsCell({ data, context }: Cell) {
  if (!data) return null;
  const offer = offeredOf(data, context);
  return <div className="admin-grid-stack"><strong>{data.quantity}</strong>{offer && offer.quantity !== data.quantity
    && <small className="deal-line-short">{offer.quantity === 0 ? 'No disponible' : `Ofrece ${offer.quantity}`}</small>}</div>;
}
// Narrow: the amount, with "2 × USD 86.50" under it.
function AmountCell({ data, context }: Cell) {
  const offer = offeredOf(data ?? undefined, context);
  if (!offer) return <span className="admin-grid-muted">—</span>;
  return <div className="admin-grid-stack quote-amount"><strong>{money(offer.total, context.currency)}</strong>
    <small>{offer.quantity === 0 ? 'No disponible' : `${offer.quantity} × ${money(offer.unit_price, context.currency)}`}</small></div>;
}

export default function DealLinesGrid({ lines, supplier, price, quote, maxRows = 25 }: { lines: Deal['lines']; supplier: boolean; price?: (line: Line) => ReactNode; quote?: Quotation | null; maxRows?: number }) {
  const [search, setSearch] = useState('');
  const [onlyReview, setOnlyReview] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  const reviewing = supplier ? lines.filter(needsReview).length : 0;
  const hasPrice = !!price, hasQuote = !!quote;
  const currency = quote?.currency || 'USD';
  const narrow = useNarrow(box, NARROW) && hasQuote, medium = useNarrow(box, MEDIUM) && hasQuote;
  const quoted = useMemo(() => new Map((quote?.lines || []).map(line => [line.order_line_id, line])), [quote]);
  // The suggested prices change without the lines changing (a draft saved or repriced): only that column redraws.
  const api = useRef<GridApi<Line> | null>(null);
  useEffect(() => { api.current?.refreshCells({ columns: ['price'], force: true }); }, [price]);
  const columns = useMemo<ColDef<Line>[]>(() => {
    const offer = (line: Line | undefined) => line ? quoted.get(line.id) : undefined;
    const item: ColDef<Line> = { colId: 'item', headerName: 'Código / descripción', flex: 2, minWidth: narrow ? 150 : medium ? 180 : 210, valueGetter: ({ data }) => data?.codigo, cellRenderer: ItemCell,
      getQuickFilterText: ({ data }) => `${data.codigo} ${data.brand} ${data.description} ${data.sku}`,
      tooltipValueGetter: ({ data }) => data && `${data.codigo}\n${data.brand || 'SIN MARCA INDICADA'}\n${data.description}\nSKU ${data.sku}` };
    const stock: ColDef<Line> = { colId: 'stock', headerName: 'Disponibles', flex: 1.4, minWidth: 210, cellRenderer: StockCell, getQuickFilterText: () => '',
      // Sorted ascending, the items to review come first.
      valueGetter: ({ data }) => { const value = data && stockOf(data); return value ? value.available_quantity - value.shortfall * 1e6 : -1e9; } };
    const amount = (colId: string, headerName: string, value: (line: Quoted) => string, width: number): ColDef<Line> => ({ colId, headerName, width, headerTooltip: headerName, type: 'rightAligned',
      cellClass: ['ag-right-aligned-cell'], getQuickFilterText: () => '', valueGetter: ({ data }) => { const line = offer(data); return line ? Number(value(line)) : null; },
      valueFormatter: ({ value: number }) => number === null || number === undefined ? '—' : money(number, currency) });
    if (narrow) return [item,
      { colId: 'quantity', headerName: 'Solicitadas', width: 124, type: 'rightAligned', cellClass: ['ag-right-aligned-cell', 'admin-grid-number'], field: 'quantity', cellRenderer: UnitsCell },
      ...(hasQuote ? [{ colId: 'total', headerName: 'Importe', width: 146, type: 'rightAligned', cellClass: ['ag-right-aligned-cell'], cellRenderer: AmountCell, getQuickFilterText: () => '',
        valueGetter: ({ data }) => { const line = offer(data); return line ? Number(line.total) : null; } } as ColDef<Line>] : [])];
    return [item,
      { colId: 'quantity', headerName: 'Solicitadas', field: 'quantity', width: 120, type: 'rightAligned', cellClass: ['ag-right-aligned-cell', 'admin-grid-number'] },
      ...(supplier ? [stock] : []),
      ...(hasQuote ? [
        { colId: 'offered', headerName: 'Ofrecidas', width: medium ? 112 : 116, type: 'rightAligned', cellClass: ['ag-right-aligned-cell', 'admin-grid-number'], cellRenderer: OfferedCell,
          getQuickFilterText: () => '', valueGetter: ({ data }) => offer(data)?.quantity ?? null } as ColDef<Line>,
        // Medium: "Precio" (unit price) next to Importe, so the five columns fit.
        { ...amount('unit_price', medium ? 'Precio' : 'Precio unitario', line => line.unit_price, medium ? 100 : 156), headerTooltip: 'Precio unitario' },
        { ...amount('total', 'Importe', line => line.total, medium ? 116 : 136), cellClass: ['ag-right-aligned-cell', 'admin-grid-number'] }] : []),
      ...(hasPrice ? [{ colId: 'price', headerName: 'Precio sugerido', flex: 1, minWidth: 170, cellRenderer: PriceCell, sortable: false, getQuickFilterText: () => '' } as ColDef<Line>] : []),
      ...(medium ? [] : [{ colId: 'sku', headerName: 'SKU interno', field: 'sku', flex: 1, minWidth: 130, cellRenderer: TextCell } as ColDef<Line>]),
      ...(supplier && !medium ? [{ colId: 'invent', headerName: 'Tu ID de inventario', flex: 1, minWidth: 130, cellRenderer: TextCell,
        valueGetter: ({ data }) => data && 'supplier_invent_id' in data ? (data as SupplierRequestLine).supplier_invent_id : '' } as ColDef<Line>] : []),
    ];
  }, [supplier, hasPrice, hasQuote, narrow, medium, quoted, currency]);
  const rows = onlyReview ? lines.filter(needsReview) : lines;
  return <div ref={box} className="deal-lines-grid">
    {(lines.length > 8 || reviewing > 0) && <div className="deal-lines-tools">
      {lines.length > 8 && <label className="inbox-search"><Search size={15}/><span className="sr-only">Buscar en los artículos</span>
        <input type="search" value={search} onChange={event => setSearch(event.target.value)} placeholder="Buscar código, SKU o descripción…" autoComplete="off"/></label>}
      {reviewing > 0 && <div className="inbox-filter" role="group" aria-label="Mostrar artículos">
        <button type="button" aria-pressed={!onlyReview} onClick={() => setOnlyReview(false)}>Todos ({lines.length})</button>
        <button type="button" aria-pressed={onlyReview} onClick={() => setOnlyReview(true)}>Por revisar ({reviewing})</button>
      </div>}
    </div>}
    <ClientGrid<Line> label={quote ? `Líneas de la cotización v${quote.revision}` : 'Artículos de la orden'} className={`supplier-request-lines deal-lines${quote ? ' quote-lines' : ''}`}
      rows={rows} columns={columns} rowId={line => line.id} rowHeight={58} maxRows={maxRows}
      context={{ price, quoted, currency } satisfies Context} gridProps={{ quickFilterText: search, tooltipShowDelay: 400, onGridReady: ({ api: ready }) => { api.current = ready; } }}/>
  </div>;
}
