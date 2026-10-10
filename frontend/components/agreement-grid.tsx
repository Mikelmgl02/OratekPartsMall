'use client';
import { useMemo, useRef } from 'react';
import type { ColDef } from 'ag-grid-community';
import type { CustomCellRendererProps } from 'ag-grid-react';
import ClientGrid from './client-grid';
import type { DealAgreement } from '@/lib/deal-types';
import { useNarrow } from '@/lib/use-narrow';

// "Acuerdo vigente": the agreed units less the shortfalls the client accepted (and those still waiting for an answer), in a grid like the
// quotation's lines. Narrower than five columns fit it keeps three: the item, the units left with what changed under them, and the amount.
type Line = DealAgreement['lines'][number];
type Cell = CustomCellRendererProps<Line, unknown, { currency: string }>;
const NARROW = 600;
const money = (value: string | number, currency = 'USD') => new Intl.NumberFormat('es-PA', { style: 'currency', currency }).format(Number(value));
const accepted = (count: number) => `${count} ${count === 1 ? 'aceptada' : 'aceptadas'}`;
const changes = (line: Line) => [line.missing > 0 && `${line.missing} ${line.missing === 1 ? 'faltante aceptado' : 'faltantes aceptados'}`,
  line.pending_missing > 0 && `${line.pending_missing} por responder`].filter(Boolean) as string[];

function ItemCell({ data }: Cell) {
  return data ? <div className="admin-grid-stack"><strong>{data.codigo}</strong><small>{data.description}</small></div> : null;
}
function MissingCell({ data }: Cell) {
  if (!data) return null;
  if (!data.missing && !data.pending_missing) return <span className="admin-grid-muted">—</span>;
  return <div className="admin-grid-stack">{data.missing > 0 && <span>{accepted(data.missing)}</span>}{data.pending_missing > 0 && <small className="pending">{data.pending_missing} por responder</small>}</div>;
}
// Narrow: the units left, with the agreed ones and the shortfalls under them.
function LeftCell({ data }: Cell) {
  return data ? <div className="admin-grid-stack quote-amount"><strong>{data.final}</strong><small>{[`de ${data.agreed}`, ...changes(data)].join(' · ')}</small></div> : null;
}

export default function AgreementGrid({ agreement, currency }: { agreement: DealAgreement; currency: string }) {
  const box = useRef<HTMLDivElement>(null);
  const narrow = useNarrow(box, NARROW);
  const columns = useMemo<ColDef<Line>[]>(() => {
    const item: ColDef<Line> = { colId: 'item', headerName: 'Artículo', flex: 2, minWidth: narrow ? 140 : 200, valueGetter: ({ data }) => data?.codigo, cellRenderer: ItemCell,
      tooltipValueGetter: ({ data }) => data && `${data.codigo}\n${data.description}` };
    const total: ColDef<Line> = { colId: 'total', headerName: 'Importe', width: 130, type: 'rightAligned', cellClass: ['ag-right-aligned-cell', 'admin-grid-number'],
      valueGetter: ({ data }) => data && Number(data.total), valueFormatter: ({ value }) => money(value, currency) };
    if (narrow) return [item, { colId: 'final', headerName: 'Quedan', width: 150, type: 'rightAligned', cellClass: ['ag-right-aligned-cell'], field: 'final', cellRenderer: LeftCell }, total];
    return [item,
      { colId: 'agreed', headerName: 'Acordadas', field: 'agreed', width: 116, type: 'rightAligned', cellClass: ['ag-right-aligned-cell'] },
      { colId: 'missing', headerName: 'Faltantes', width: 150, valueGetter: ({ data }) => data && data.missing + data.pending_missing, cellRenderer: MissingCell },
      { colId: 'final', headerName: 'Quedan', field: 'final', width: 104, type: 'rightAligned', cellClass: ['ag-right-aligned-cell', 'admin-grid-number'] },
      total];
  }, [narrow, currency]);
  return <div ref={box} className="quote-lines-grid">
    <ClientGrid<Line> label="Artículos del acuerdo" className="quote-lines agreement-lines" rows={agreement.lines} columns={columns} rowId={line => line.order_line_id}
      rowHeight={56} maxRows={12} context={{ currency }}/>
  </div>;
}
