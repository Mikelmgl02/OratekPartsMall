'use client';
import { useEffect, useRef, useState } from 'react';
import type { ColDef, GridApi } from 'ag-grid-community';
import type { CustomCellRendererProps } from 'ag-grid-react';
import { MessageCircle, Minus, PackageSearch, Plus, Trash2, TriangleAlert } from 'lucide-react';
import ClientGrid from './client-grid';
import { CatalogPhoto } from './part-image';
import type { BasketLine } from '@/lib/types';
import { visibleReferences } from '@/lib/reference-label';

// One supplier's cesta items as a grid (basket.tsx), by grupo, subgrupo and description: photo, grupo, the article (its code, brand and where it came from), its description with any
// warning (changed since it was added, or no longer available), the internal SKU, the quantity to ask for and Eliminar.
type Context = { sending: boolean; setQuantity: (key: string, quantity: number) => void; remove: (key: string) => void };
type Cell = CustomCellRendererProps<BasketLine, unknown, Context>;
export const lineName = (line: BasketLine) => line.offer.selected_item?.codigo || line.part.name || line.part.sku;
const descriptionOf = (line: BasketLine) => line.offer.selected_item?.description || line.part.description || '';
const groupOf = (line: BasketLine) => [line.part.category, line.part.subcategory].filter(Boolean).join(' › ');
const collate = (a = '', b = '') => (a ? b ? a.localeCompare(b, 'es') : -1 : b ? 1 : 0);  // the ungrouped last
// Like the order's document: by grupo, subgrupo and description (the headers can still sort it another way).
export const sortedLines = (lines: BasketLine[]) => [...lines].sort((a, b) => collate(a.part.category, b.part.category) || collate(a.part.subcategory, b.part.subcategory)
  || descriptionOf(a).localeCompare(descriptionOf(b), 'es') || lineName(a).localeCompare(lineName(b)));

function QuantityControl({ name, quantity, onChange, disabled }: { name: string; quantity: number; onChange: (quantity: number) => void; disabled: boolean }) {
  const [value, setValue] = useState(String(quantity));
  // The grid hands the saved quantity back a moment later: while someone is typing, what they typed stays.
  const input = useRef<HTMLInputElement>(null);
  useEffect(() => { if (document.activeElement !== input.current) setValue(String(quantity)); }, [quantity]);
  function finish() {
    const parsed = Number(value);
    const next = Number.isFinite(parsed) ? Math.max(1, Math.min(9999, Math.trunc(parsed))) : quantity;
    setValue(String(next));
    if (next !== quantity) onChange(next);
  }
  return <div className="basket-page-quantity basket-grid-quantity"><div>
    <button type="button" aria-label={`Reducir cantidad de ${name}`} disabled={disabled || quantity <= 1} onClick={() => onChange(quantity - 1)}><Minus size={14}/></button>
    <input ref={input} type="number" min={1} max={9999} step={1} inputMode="numeric" autoComplete="off" disabled={disabled} aria-label={`Cantidad de ${name}`} value={value} onChange={event => {
      const draft = event.target.value; setValue(draft);
      const next = Number(draft);
      if (draft && Number.isInteger(next) && next >= 1 && next <= 9999) onChange(next);
    }} onBlur={finish} onKeyDown={event => { event.stopPropagation(); if (event.key === 'Enter') event.currentTarget.blur(); }}/>
    <button type="button" aria-label={`Aumentar cantidad de ${name}`} disabled={disabled || quantity >= 9999} onClick={() => onChange(quantity + 1)}><Plus size={14}/></button>
  </div></div>;
}

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
function QuantityCell({ data, context }: Cell) {
  return data ? <QuantityControl name={lineName(data)} quantity={data.quantity} disabled={context.sending} onChange={quantity => context.setQuantity(data.key, quantity)}/> : null;
}
function RemoveCell({ data, context }: Cell) {
  return data ? <button type="button" className="basket-page-remove" disabled={context.sending} aria-label={`Eliminar ${lineName(data)}`} onClick={() => context.remove(data.key)}><Trash2 size={14}/>Eliminar</button> : null;
}

const columns: ColDef<BasketLine>[] = [
  { colId: 'photo', headerName: '', width: 76, cellRenderer: PhotoCell, sortable: false, resizable: false, suppressHeaderMenuButton: true },
  { colId: 'group', headerName: 'Grupo', width: 170, valueGetter: ({ data }) => data ? groupOf(data) || 'Sin grupo' : '', cellClass: 'admin-grid-text muted', tooltipValueGetter: ({ value }) => value },
  { colId: 'article', headerName: 'Artículo', flex: 1.1, minWidth: 116, cellRenderer: ArticleCell, valueGetter: ({ data }) => data ? lineName(data) : '' },
  { colId: 'description', headerName: 'Descripción', flex: 1.6, minWidth: 240, cellRenderer: DescriptionCell,
    valueGetter: ({ data }) => data ? data.offer.selected_item?.description || data.part.description || '' : '', tooltipValueGetter: ({ value }) => value },
  { colId: 'sku', headerName: 'SKU interno', width: 160, valueGetter: ({ data }) => data?.part.sku || data?.part.name || '', cellClass: 'admin-grid-text' },
  { colId: 'quantity', headerName: 'Cantidad', width: 160, field: 'quantity', cellRenderer: QuantityCell, type: 'rightAligned' },
  { colId: 'remove', headerName: '', width: 124, cellRenderer: RemoveCell, sortable: false, resizable: false, suppressHeaderMenuButton: true },
];

// On a phone (a grid under 640px) the item keeps its name, quantity and Eliminar; the photo, description and SKU make room.
const fit = (grid: GridApi<BasketLine>, width: number) => {
  const wide = width >= 640;
  grid.setColumnsVisible(['photo', 'group', 'description', 'sku'], wide);
  grid.setColumnWidths([{ key: 'quantity', newWidth: wide ? 160 : 118 }, { key: 'remove', newWidth: wide ? 124 : 96 }]);
};
export default function BasketGrid({ supplier, lines, sending, setQuantity, remove }: { supplier: string; lines: BasketLine[] } & Context) {
  // While the cesta is being sent its controls lock: the grid draws them again (its cells do not follow the context by themselves).
  const api = useRef<GridApi<BasketLine> | null>(null);
  useEffect(() => { api.current?.refreshCells({ force: true, columns: ['quantity', 'remove'] }); }, [sending]);
  return <ClientGrid<BasketLine> label={`Artículos para ${supplier}`} rows={sortedLines(lines)} columns={columns} rowId={line => line.key} rowHeight={72} autoHeight
    className="basket-grid" context={{ sending, setQuantity, remove }}
    gridProps={{ onGridReady: event => { api.current = event.api; }, onGridSizeChanged: event => fit(event.api, event.clientWidth), rowClassRules: { 'basket-grid-stale': ({ data }) => !!data && (data.available === false || !!data.changed) } }}/>;
}
