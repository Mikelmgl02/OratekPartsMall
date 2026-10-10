'use client';
import { useEffect, useMemo, useRef, useState } from 'react';
import type { ColDef } from 'ag-grid-community';
import type { CustomCellRendererProps } from 'ag-grid-react';
import ClientGrid from './client-grid';
import type { Quotation } from '@/lib/deal-types';

// A quotation's lines in a grid, like the order's items (deal-lines-grid.tsx): sortable, sized to its rows up to a dozen, then scrolling.
// Narrower than four columns fit (a phone, the conversations panel's side at its usual width) it keeps two: the item, and the amount with
// "2 × USD 86.50" under it.
type Line = Quotation['lines'][number];
type Cell = CustomCellRendererProps<Line, unknown, { currency: string }>;
const NARROW = 600;
const money = (value: string | number, currency = 'USD') => new Intl.NumberFormat('es-PA', { style: 'currency', currency }).format(Number(value));
const offered = (line: Line, currency: string) => line.quantity === 0 ? 'No disponible' : `${line.quantity} × ${money(line.unit_price, currency)}`;

function ItemCell({ data }: Cell) {
  return data ? <div className="admin-grid-stack"><strong>{data.codigo}</strong><small>{data.description}</small></div> : null;
}
function AmountCell({ data, context }: Cell) {
  return data ? <div className="admin-grid-stack quote-amount"><strong>{money(data.total, context.currency)}</strong><small>{offered(data, context.currency)}</small></div> : null;
}

export default function QuoteLinesGrid({ quote, label }: { quote: Quotation; label: string }) {
  const box = useRef<HTMLDivElement>(null);
  const [narrow, setNarrow] = useState(false);
  useEffect(() => {
    const element = box.current;
    if (!element) return;
    const observer = new ResizeObserver(([entry]) => { if (entry.contentRect.width) setNarrow(entry.contentRect.width < NARROW); });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  const currency = quote.currency;
  const columns = useMemo<ColDef<Line>[]>(() => {
    const item: ColDef<Line> = { colId: 'item', headerName: 'Artículo', flex: 2, minWidth: narrow ? 150 : 220, valueGetter: ({ data }) => data?.codigo, cellRenderer: ItemCell,
      tooltipValueGetter: ({ data }) => data && `${data.codigo}\n${data.description}` };
    if (narrow) return [item, { colId: 'total', headerName: 'Importe', width: 150, type: 'rightAligned', cellClass: ['ag-right-aligned-cell'], cellRenderer: AmountCell,
      valueGetter: ({ data }) => data && Number(data.total) }];
    const amount = (colId: string, headerName: string, value: (line: Line) => string): ColDef<Line> => ({ colId, headerName, width: 140, type: 'rightAligned',
      cellClass: ['ag-right-aligned-cell'], valueGetter: ({ data }) => data && Number(value(data)), valueFormatter: ({ value: number }) => money(number, currency) });
    return [item,
      { colId: 'quantity', headerName: 'Ofrecidas', field: 'quantity', width: 116, type: 'rightAligned', cellClass: ['ag-right-aligned-cell', 'admin-grid-number'],
        valueFormatter: ({ value }) => value === 0 ? 'No disponible' : String(value) },
      amount('unit_price', 'Precio unitario', line => line.unit_price),
      { ...amount('total', 'Importe', line => line.total), cellClass: ['ag-right-aligned-cell', 'admin-grid-number'] }];
  }, [narrow, currency]);
  return <div ref={box} className="quote-lines-grid">
    <ClientGrid<Line> label={label} className="quote-lines" rows={quote.lines} columns={columns} rowId={line => line.order_line_id} rowHeight={56} maxRows={12}
      context={{ currency }}/>
  </div>;
}
