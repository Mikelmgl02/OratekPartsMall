'use client';
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import type { ColDef, GridApi } from 'ag-grid-community';
import type { CustomCellRendererProps } from 'ag-grid-react';
import { MessageCircle, PackageSearch, Trash2, TriangleAlert } from 'lucide-react';
import ClientGrid from './client-grid';
import { CatalogPhoto } from './part-image';
import type { BasketLine } from '@/lib/types';
import { visibleReferences } from '@/lib/reference-label';
import { stockHelp, stockLabel, stockOf, stockRank, stockShort } from '@/lib/basket-stock';
import QuantityInput from './quantity-input';

// One supplier's cesta items as a grid (basket.tsx), by grupo, subgrupo and description: photo, grupo, the article (its code, brand and where it came from), its description with any
// warning (changed since it was added, or no longer available), the internal SKU, Disponible (the supplier's stock band, as the catalog's: no
// units), the quantity to ask for (a plain number cell, typed in place) and Eliminar.
type Context = { sending: boolean; narrow?: boolean; setQuantity: (key: string, quantity: number) => void; remove: (key: string) => void };
type Cell = CustomCellRendererProps<BasketLine, unknown, Context>;
export const lineName = (line: BasketLine) => line.offer.selected_item?.codigo || line.part.name || line.part.sku;
const descriptionOf = (line: BasketLine) => line.offer.selected_item?.description || line.part.description || '';
const groupOf = (line: BasketLine) => [line.part.category, line.part.subcategory].filter(Boolean).join(' › ');
const collate = (a = '', b = '') => (a ? b ? a.localeCompare(b, 'es') : -1 : b ? 1 : 0);  // the ungrouped last
// Like the order's document: by grupo, subgrupo and description (the headers can still sort it another way).
export const sortedLines = (lines: BasketLine[]) => [...lines].sort((a, b) => collate(a.part.category, b.part.category) || collate(a.part.subcategory, b.part.subcategory)
  || descriptionOf(a).localeCompare(descriptionOf(b), 'es') || lineName(a).localeCompare(lineName(b)));

function PhotoCell({ data }: Cell) {
  return data ? <div className="basket-grid-photo"><CatalogPhoto image={data.part.images?.[0]} alt={data.part.name || data.part.description || data.part.sku} thumbnail/></div> : null;
}
function ArticleCell({ data }: Cell) {
  if (!data) return null;
  const selected = data.offer.selected_item, first = visibleReferences({ codes: data.part.codes ?? [], equivalents: data.part.equivalents })[0];
  return <div className="admin-grid-stack basket-grid-article">
    {data.source === 'whatsapp' && <span className="basket-page-source"><MessageCircle size={11}/>Agregado por WhatsApp</span>}
    {data.source === 'faltante' && <span className="basket-page-source missing"><PackageSearch size={11}/>Lo que faltó en otra orden</span>}
    <h3>{lineName(data)}</h3>
    <small>{selected ? `${selected.codigo} · ${selected.brand || 'SIN MARCA INDICADA'}` : first ? `${first.code} · ${first.brand || 'SIN MARCA INDICADA'}` : data.part.sku}</small>
  </div>;
}
function DescriptionCell({ data }: Cell) {
  if (!data) return null;
  const selected = data.offer.selected_item, description = selected?.description || data.part.description;
  return <div className="admin-grid-stack basket-grid-description">
    {selected && data.part.name && data.part.name !== lineName(data) && <strong>{data.part.name}</strong>}
    {description && <small>{description}</small>}
    {data.available === false ? <span className="basket-page-stale" role="note"><TriangleAlert size={13}/>Este artículo ya no se puede solicitar. Quítalo de la cesta.</span>
      : data.changed ? <span className="basket-page-stale" role="note"><TriangleAlert size={13}/>Cambió desde que lo agregaste. Quítalo y vuelve a elegirlo en el catálogo.</span> : null}
  </div>;
}
function StockCell({ data }: Cell) {
  if (!data) return null;
  const stock = stockOf(data);
  return <span className={`basket-grid-stock ${stock}`} data-short={stockShort[stock]} title={stockHelp}>{stockLabel(data)}</span>;
}
function QuantityCell({ data, context }: Cell) {
  return data ? <QuantityInput name={lineName(data)} quantity={data.quantity} disabled={context.sending} onChange={quantity => context.setQuantity(data.key, quantity)}/> : null;
}
function RemoveCell({ data, context }: Cell) {
  return data ? <button type="button" className="basket-page-remove" disabled={context.sending} aria-label={`Eliminar ${lineName(data)}`} onClick={() => context.remove(data.key)}><Trash2 size={14}/>Eliminar</button> : null;
}

const columns: ColDef<BasketLine>[] = [
  { colId: 'photo', headerName: '', width: 76, cellRenderer: PhotoCell, sortable: false, resizable: false, suppressHeaderMenuButton: true },
  { colId: 'group', headerName: 'Grupo', width: 170, valueGetter: ({ data }) => data ? groupOf(data) || 'Sin grupo' : '', cellClass: 'admin-grid-text muted', tooltipValueGetter: ({ value }) => value },
  { colId: 'article', headerName: 'Artículo', flex: 1.1, minWidth: 116, cellRenderer: ArticleCell, valueGetter: ({ data }) => data ? lineName(data) : '' },
  { colId: 'description', headerName: 'Descripción', flex: 1.6, minWidth: 180, cellRenderer: DescriptionCell,
    valueGetter: ({ data }) => data ? data.offer.selected_item?.description || data.part.description || '' : '', tooltipValueGetter: ({ value }) => value },
  { colId: 'sku', headerName: 'SKU interno', width: 160, valueGetter: ({ data }) => data?.part.sku || data?.part.name || '', cellClass: 'admin-grid-text' },
  { colId: 'stock', headerName: 'Disponible', headerValueGetter: ({ context }) => context?.narrow ? 'Disp.' : 'Disponible', width: 150, cellRenderer: StockCell, valueGetter: ({ data }) => data ? stockLabel(data) : '',
    comparator: (_a, _b, a, b) => stockRank(a.data) - stockRank(b.data) },
  { colId: 'quantity', headerName: 'Cantidad', headerValueGetter: ({ context }) => context?.narrow ? 'Cant.' : 'Cantidad', width: 112, field: 'quantity', cellRenderer: QuantityCell, type: 'rightAligned', cellClass: 'basket-grid-quantity-cell',
    // Keys typed in the field are the field's (caret, Home/End, arrows); Tab still moves between cells.
    suppressKeyboardEvent: ({ event }) => event.target instanceof HTMLInputElement && event.key !== 'Tab' },
  { colId: 'remove', headerName: '', width: 116, cellRenderer: RemoveCell, sortable: false, resizable: false, suppressHeaderMenuButton: true },
];

// The columns that fit the grid's width (each step's least width, plus room for the border and a scrollbar). A phone keeps the article,
// Disponible, the quantity and Eliminar (312px); from 640 the photo and the description come back (568px); from 770 Disponible and Eliminar
// spell out their labels (750px); from 1100 Grupo and SKU interno come back (1080px).
const fit = (grid: GridApi<BasketLine>, width: number, setNarrow: (narrow: boolean) => void) => {
  const wide = width >= 640, roomy = width >= 770, full = width >= 1100;
  grid.setColumnsVisible(['photo', 'description'], wide);
  grid.setColumnsVisible(['group', 'sku'], full);
  grid.setColumnWidths([{ key: 'stock', newWidth: roomy ? 150 : 84 }, { key: 'quantity', newWidth: roomy ? 112 : 72 }, { key: 'remove', newWidth: roomy ? 116 : 40 }]);
  setNarrow(!roomy);
};
export default function BasketGrid({ supplier, lines, sending, setQuantity, remove }: { supplier: string; lines: BasketLine[] } & Context) {
  // While the cesta is being sent its controls lock: the grid draws them again (its cells do not follow the context by themselves).
  const api = useRef<GridApi<BasketLine> | null>(null);
  const [narrow, setNarrow] = useState(false);
  // A cell keeps the context it was drawn with, and rows whose data did not change are not drawn again: the handlers it calls are the
  // latest ones, so editing one row never acts on an older copy of the others.
  const handlers = useRef({ setQuantity, remove });
  useLayoutEffect(() => { handlers.current = { setQuantity, remove }; });
  const context = useMemo<Context>(() => ({ sending, narrow, setQuantity: (key, quantity) => handlers.current.setQuantity(key, quantity), remove: key => handlers.current.remove(key) }), [sending, narrow]);
  useEffect(() => { api.current?.refreshCells({ force: true, columns: ['quantity', 'remove'] }); }, [sending]);
  useEffect(() => { api.current?.refreshHeader(); }, [narrow]);
  return <ClientGrid<BasketLine> label={`Artículos para ${supplier}`} rows={sortedLines(lines)} columns={columns} rowId={line => line.key} rowHeight={72} fill="stacked"
    className={`basket-grid ${narrow ? 'narrow' : ''}`} context={context}
    // No sliding columns on the switch: a quantity typed in meanwhile would scroll the grid sideways, away from its header.
    gridProps={{ suppressColumnMoveAnimation: true, onGridReady: event => { api.current = event.api; }, onGridSizeChanged: event => fit(event.api, event.clientWidth, setNarrow), rowClassRules: { 'basket-grid-stale': ({ data }) => !!data && (data.available === false || !!data.changed) } }}/>;
}
