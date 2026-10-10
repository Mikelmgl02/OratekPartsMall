'use client';
import { type ReactNode, useEffect, useMemo, useRef, useState } from 'react';
import type { ColDef, GridApi } from 'ag-grid-community';
import type { CustomCellRendererProps } from 'ag-grid-react';
import { Search } from 'lucide-react';
import ClientGrid from './client-grid';
import type { Deal } from '@/lib/deal-types';
import type { SupplierRequestLine } from '@/lib/request-types';

// The order's items (Artículos) in a grid, since an order can list many: sorted by any column, searched by code, SKU or description,
// and, for the supplier, narrowed to the items whose stock needs confirming. It grows with its rows up to a dozen, then scrolls. The
// requested and available units come right after the item, so a narrow panel shows them first; the SKU and inventory ID follow.
type Line = Deal['lines'][number];
type Context = { price?: (line: Line) => ReactNode };
type Cell = CustomCellRendererProps<Line, unknown, Context>;
const stockOf = (line: Line) => 'stock' in line ? (line as SupplierRequestLine).stock : undefined;
const needsReview = (line: Line) => { const stock = stockOf(line); return !stock || stock.shortfall > 0; };

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

export default function DealLinesGrid({ lines, supplier, price }: { lines: Deal['lines']; supplier: boolean; price?: (line: Line) => ReactNode }) {
  const [search, setSearch] = useState('');
  const [onlyReview, setOnlyReview] = useState(false);
  const reviewing = supplier ? lines.filter(needsReview).length : 0;
  const hasPrice = !!price;
  // The suggested prices change without the lines changing (a draft saved or repriced): only that column redraws.
  const api = useRef<GridApi<Line> | null>(null);
  useEffect(() => { api.current?.refreshCells({ columns: ['price'], force: true }); }, [price]);
  const columns = useMemo<ColDef<Line>[]>(() => [
    { colId: 'item', headerName: 'Código / descripción', flex: 2, minWidth: 210, valueGetter: ({ data }) => data?.codigo, cellRenderer: ItemCell,
      getQuickFilterText: ({ data }) => `${data.codigo} ${data.brand} ${data.description}`, tooltipValueGetter: ({ data }) => data && `${data.codigo}\n${data.brand || 'SIN MARCA INDICADA'}\n${data.description}` },
    { colId: 'quantity', headerName: 'Solicitadas', field: 'quantity', width: 128, type: 'rightAligned', cellClass: ['ag-right-aligned-cell', 'admin-grid-number'] },
    ...(supplier ? [{ colId: 'stock', headerName: 'Disponibles', flex: 1.4, minWidth: 210, cellRenderer: StockCell, getQuickFilterText: () => '',
      // Sorted ascending, the items to review come first.
      valueGetter: ({ data }) => { const stock = data && stockOf(data); return stock ? stock.available_quantity - stock.shortfall * 1e6 : -1e9; } } as ColDef<Line>] : []),
    ...(hasPrice ? [{ colId: 'price', headerName: 'Precio sugerido', flex: 1, minWidth: 170, cellRenderer: PriceCell, sortable: false, getQuickFilterText: () => '' } as ColDef<Line>] : []),
    { colId: 'sku', headerName: 'SKU interno', field: 'sku', flex: 1, minWidth: 130, cellRenderer: TextCell },
    ...(supplier ? [{ colId: 'invent', headerName: 'Tu ID de inventario', flex: 1, minWidth: 130, cellRenderer: TextCell,
      valueGetter: ({ data }) => data && 'supplier_invent_id' in data ? (data as SupplierRequestLine).supplier_invent_id : '' } as ColDef<Line>] : []),
  ], [supplier, hasPrice]);
  const rows = onlyReview ? lines.filter(needsReview) : lines;
  return <div className="deal-lines-grid">
    {(lines.length > 8 || reviewing > 0) && <div className="deal-lines-tools">
      {lines.length > 8 && <label className="inbox-search"><Search size={15}/><span className="sr-only">Buscar en los artículos</span>
        <input type="search" value={search} onChange={event => setSearch(event.target.value)} placeholder="Buscar código, SKU o descripción…" autoComplete="off"/></label>}
      {reviewing > 0 && <div className="inbox-filter" role="group" aria-label="Mostrar artículos">
        <button type="button" aria-pressed={!onlyReview} onClick={() => setOnlyReview(false)}>Todos ({lines.length})</button>
        <button type="button" aria-pressed={onlyReview} onClick={() => setOnlyReview(true)}>Por revisar ({reviewing})</button>
      </div>}
    </div>}
    <ClientGrid<Line> label="Artículos de la orden" className="supplier-request-lines deal-lines" rows={rows} columns={columns} rowId={line => line.id} rowHeight={58} maxRows={12}
      context={{ price } satisfies Context} gridProps={{ quickFilterText: search, tooltipShowDelay: 400, onGridReady: ({ api: ready }) => { api.current = ready; } }}/>
  </div>;
}
