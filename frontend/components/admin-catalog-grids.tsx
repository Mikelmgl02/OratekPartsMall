'use client';
import type { ColDef, SuppressKeyboardEventParams } from 'ag-grid-community';
import type { CustomCellRendererProps } from 'ag-grid-react';
import { Images, Pencil, SearchCheck, Trash2, Wrench } from 'lucide-react';
import { referenceLabel } from '@/lib/reference-label';
import type { ManagedAlternate, ManagedPart, ManagedStockItem, PartReference } from '@/lib/types';
import CodesEditor from './admin-codes-editor';

export type CatalogGridActions = {
  onEditPart: (part: ManagedPart) => void; onTechnical: (part: ManagedPart) => void; onImages: (part: ManagedPart) => void;
  onEditAlternate: (alternate: ManagedAlternate) => void; onRemoveAlternate: (alternate: ManagedAlternate) => void; onEditMatch: (item: ManagedStockItem) => void;
  onCodesChanged: (partId: string) => void;
};
// Codes pasted into the Alternos cell (comma, semicolon or line separated) wait here until the grid saves them one by one.
export const pastedCodes = new WeakMap<ManagedPart, string[]>();
// A row field and the column that shows it: what flashes when a fresh row comes in.
export const catalogFieldColumns: Record<string, string> = { description: 'description', name: 'description', category: 'subgroup', subcategory: 'subgroup',
  active: 'active', codes: 'codes', stock_record_count: 'stock', sku: 'sku', is_OEM: 'sku', identity: 'sku' };
type Cell<T> = CustomCellRendererProps<T, unknown, CatalogGridActions>;
const ALTERNATES_SHOWN = 4;
const number = (value: number | null | undefined) => (value ?? 0).toLocaleString('es-PA');

export function identityText(part: ManagedPart) {
  const main = part.is_OEM ? 'MAIN OEM' : part.identity?.ref_type === 'company' ? `MAIN INTERNO · ${part.identity.brand}` : 'MAIN INTERNO';
  return `${main}${!part.is_OEM ? part.identity?.status === 'choose_oem' ? ' · ELEGIR OEM' : ' · OEM PENDIENTE' : ''}`;
}
const describe = (part?: ManagedPart) => part ? part.description || (part.name !== part.sku ? part.name : '') : '';
export const subgroupLabel = (part?: Pick<ManagedPart, 'category' | 'subcategory'>) => [part?.category, part?.subcategory].filter(Boolean).join(' / ');
export const parseSubgroup = (label: unknown) => { const [category = '', subcategory = ''] = String(label || '').split(' / '); return { category, subcategory }; };
const codeText = (part?: ManagedPart) => part?.codes.map(code => `${code.code} · ${referenceLabel(code)}`).join('\n') || '';

// ---------------------------------------------------------------- Inventario interno
function SkuCell({ data }: Cell<ManagedPart>) {
  if (!data) return null;
  const tone = data.is_OEM ? 'oem' : data.identity?.status === 'choose_oem' ? 'choose' : 'pending';
  return <div className="admin-grid-stack"><strong>{data.sku}</strong><span className={`sku-identity ${tone}`}>{identityText(data)}</span></div>;
}
function CodesCell({ data }: Cell<ManagedPart>) {
  if (!data) return null;
  if (!data.codes.length) return <span className="admin-grid-muted">Solo el código del SKU</span>;
  const hidden = data.codes.length - ALTERNATES_SHOWN;
  // The chips may be clipped by the column width; the count of the rest sits outside the clipped area so it always shows.
  return <div className="admin-grid-codes"><div className="admin-grid-chips">{data.codes.slice(0, ALTERNATES_SHOWN).map(code => {
    const type = code.ref_type || (code.kind === 'oem' ? 'oem' : code.kind === 'manufacturer' ? 'company' : 'unknown');
    return <span key={`${code.brand}:${code.code}`} className={type}>{code.code}</span>;
  })}</div>{hidden > 0 && <span className="more">+{hidden}</span>}</div>;
}
function ActiveCell({ data }: Cell<ManagedPart>) {
  return data ? <span className={`status ${data.active ? 'matched' : ''}`}>{data.active ? 'Activo' : 'Inactivo'}</span> : null;
}
function PartActions({ data, context }: Cell<ManagedPart>) {
  if (!data) return null;
  return <div className="admin-grid-actions">
    <button type="button" className="button soft small" aria-label={`Editar SKU ${data.sku}`} onClick={() => context.onEditPart(data)}><Pencil size={13} aria-hidden="true"/>Editar</button>
    <button type="button" className="button ghost small" aria-label={`Ficha técnica de ${data.sku}`} onClick={() => context.onTechnical(data)}><Wrench size={13} aria-hidden="true"/>Ficha</button>
    <button type="button" className="button ghost small" aria-label={`Imágenes de ${data.sku}`} onClick={() => context.onImages(data)}><Images size={13} aria-hidden="true"/>{data.images?.length || 0}</button>
  </div>;
}
export const catalogOrdering: Record<string, string> = { sku: 'sku', description: 'description', subgroup: 'category,subcategory', active: 'active', stock: 'stock_record_count' };
// edit: Editar celdas is on (the catalog's group / subgroup labels, and whether a batch is being saved); null keeps the grid
// read-only. Description, group / subgroup and status edit in place (EssaMobileApp's batch: see admin-catalog-section), alternos in
// their popup; the SKU code keeps the dialog, since renaming a MAIN has side effects.
export type CatalogEdit = { subgroups: string[]; isSaving: () => boolean };
export function catalogColumns(wide: boolean, edit: CatalogEdit | null = null): ColDef<ManagedPart>[] {
  const editing = edit !== null;
  const editable = () => editing && !edit.isSaving();
  const cue = { 'editable-cell-active': editable };
  const title = (text: string) => editing ? `${text} ✎` : text;
  return [
    { colId: 'sku', headerName: 'SKU interno', width: 214, minWidth: 170, pinned: wide ? 'left' : undefined, cellRenderer: SkuCell, tooltipValueGetter: ({ data }) => data?.sku },
    { colId: 'description', headerName: title('Descripción'), field: 'description', valueFormatter: ({ data }) => describe(data), flex: 1.2, minWidth: 200, cellClass: 'admin-grid-text',
      cellClassRules: cue, tooltipValueGetter: ({ data }) => describe(data), editable, cellEditor: 'agTextCellEditor', cellDataType: false },
    // Always opens (read-only without Editar celdas) so every code can be read and copied. Enter and Tab belong to the popup's own
    // fields (the grid would otherwise close it and move to another cell).
    { colId: 'codes', headerName: title('Alternos'), field: 'codes', cellRenderer: CodesCell, flex: 1, minWidth: 230, tooltipValueGetter: ({ data }) => codeText(data),
      valueFormatter: ({ value }) => (value as PartReference[] | undefined)?.map(code => code.code).join(', ') ?? '', cellDataType: false, cellClassRules: cue,
      editable: true, cellEditor: CodesEditor, cellEditorPopup: true, cellEditorPopupPosition: 'under', cellEditorParams: { readOnly: !editing },
      suppressKeyboardEvent: ({ editing: open, event }: SuppressKeyboardEventParams<ManagedPart>) => open && (event.key === 'Enter' || event.key === 'Tab'),
      valueParser: ({ newValue }) => typeof newValue === 'string' ? newValue.split(/[,;\n]+/).map(text => text.trim().toUpperCase()).filter(Boolean) : newValue,
      valueSetter: ({ data, newValue }) => {
        if (!editable() || !Array.isArray(newValue) || !newValue.every(item => typeof item === 'string')) return false;
        const known = new Set(data.codes.map(code => code.code.toUpperCase()));
        const fresh = [...new Set(newValue as string[])].filter(text => !known.has(text));
        if (!fresh.length) return false;
        pastedCodes.set(data, fresh);
        return true;
      } },
    { colId: 'stock', headerName: 'Existencias', valueGetter: ({ data }) => data?.stock_record_count ?? null, valueFormatter: ({ value }) => value === null ? '' : number(value), width: 112, type: 'rightAligned', cellClass: ['ag-right-aligned-cell', 'admin-grid-number'] },
    // The select editor labels its options with the column's valueFormatter (the renderer still draws the cell).
    { colId: 'active', headerName: title('Estado'), field: 'active', width: 110, cellRenderer: ActiveCell, valueFormatter: ({ value }) => value ? 'ACTIVO' : 'INACTIVO',
      cellClassRules: cue, editable, cellEditor: 'agSelectCellEditor', cellEditorParams: { values: [true, false] }, cellDataType: false,
      // A pasted ACTIVO / INACTIVO (or SI / NO, TRUE / FALSE); anything else is ignored.
      valueParser: ({ newValue, oldValue }) => {
        const text = String(newValue ?? '').trim().toUpperCase();
        return ['ACTIVO', 'SI', 'SÍ', 'TRUE', '1'].includes(text) ? true : ['INACTIVO', 'NO', 'FALSE', '0'].includes(text) ? false : oldValue;
      } },
    { colId: 'subgroup', headerName: title('Grupo / subgrupo'), valueGetter: ({ data }) => subgroupLabel(data), valueFormatter: ({ value }) => value || '—', width: 220,
      cellClass: 'admin-grid-text muted', cellClassRules: cue, tooltipValueGetter: ({ data }) => subgroupLabel(data), cellDataType: false, editable,
      // Only the catalog's groups / subgroups (a paste of anything else is ignored), or blank to unclassify.
      valueSetter: ({ data, newValue }) => {
        const label = String(newValue ?? '').trim().toUpperCase();
        if (label && !edit?.subgroups.includes(label)) return false;
        const next = parseSubgroup(label);
        if (data.category === next.category && data.subcategory === next.subcategory) return false;
        Object.assign(data, next);
        return true;
      },
      cellEditor: 'agRichSelectCellEditor', cellEditorPopup: true,
      cellEditorParams: { values: ['', ...(edit?.subgroups ?? [])], allowTyping: true, filterList: true, searchType: 'matchAny', highlightMatch: true, valueListMaxHeight: 320,
                          formatValue: (value: string) => value || 'SIN CLASIFICAR' } },
    { colId: 'actions', headerName: 'Acciones', width: 214, pinned: wide ? 'right' : undefined, cellRenderer: PartActions, suppressHeaderMenuButton: true, resizable: false },
  ];
}

// ---------------------------------------------------------------- Existencias por proveedor
function SupplierCell({ data }: Cell<ManagedStockItem>) {
  return data ? <div className="admin-grid-stack"><strong>{data.supplier_name}</strong><small>{data.source === 'apiag' ? 'apiag-cloud' : 'Carga manual'}</small></div> : null;
}
function StockCodeCell({ data }: Cell<ManagedStockItem>) {
  return data ? <div className="admin-grid-stack"><strong>{data.codigo}</strong><small>{data.brand}</small></div> : null;
}
function LinkedSkuCell({ data }: Cell<ManagedStockItem>) {
  return data ? <div className="admin-grid-stack"><strong className={data.part_sku ? '' : 'admin-grid-muted'}>{data.part_sku || 'Sin vincular'}</strong>{data.part_name && data.part_name !== data.part_sku && <small>{data.part_name}</small>}</div> : null;
}
function MatchCell({ data, context }: Cell<ManagedStockItem>) {
  if (!data) return null;
  return <div className="admin-grid-actions start"><span className={`status ${data.matching_status}`}>{({ matched: 'Vinculado', pending: 'Pendiente', review: 'Por revisar' } as Record<string, string>)[data.matching_status]}</span>
    <button type="button" className="button text small" aria-label={`Revisar coincidencia ${data.supplier_name} ${data.supplier_invent_id}`} onClick={() => context.onEditMatch(data)}><SearchCheck size={13} aria-hidden="true"/>Revisar</button></div>;
}
export const stockOrdering: Record<string, string> = { supplier: 'supplier__name', codigo: 'codigo', part: 'part__sku', invent: 'supplier_invent_id', available: 'available', reserved: 'reserved_quantity', match: 'matching_status' };
export const stockColumns: ColDef<ManagedStockItem>[] = [
  { colId: 'supplier', headerName: 'Proveedor', width: 200, cellRenderer: SupplierCell },
  { colId: 'codigo', headerName: 'Código / marca', flex: 1, minWidth: 190, cellRenderer: StockCodeCell, tooltipValueGetter: ({ data }) => data?.description },
  { colId: 'part', headerName: 'SKU interno', flex: 1, minWidth: 190, cellRenderer: LinkedSkuCell },
  { colId: 'invent', headerName: 'ID del proveedor', field: 'supplier_invent_id', width: 160, cellClass: 'admin-grid-text' },
  { colId: 'available', headerName: 'Disponibles', field: 'available_quantity', valueFormatter: ({ value }) => number(value), width: 124, type: 'rightAligned', cellClass: ['ag-right-aligned-cell', 'admin-grid-number'] },
  { colId: 'reserved', headerName: 'Reservadas', field: 'reserved_quantity', valueFormatter: ({ value }) => number(value), width: 120, type: 'rightAligned' },
  { colId: 'match', headerName: 'Coincidencia', width: 210, cellRenderer: MatchCell },
];

// ---------------------------------------------------------------- Alternos
function AlternateSkuCell({ data }: Cell<ManagedAlternate>) {
  return data ? <div className="admin-grid-stack"><strong>{data.part_sku}</strong>{data.part_name && data.part_name !== data.part_sku && <small>{data.part_name}</small>}</div> : null;
}
function AlternateCodeCell({ data }: Cell<ManagedAlternate>) {
  return data ? <div className="admin-grid-stack"><strong>{data.code}</strong><small>{referenceLabel(data)}</small></div> : null;
}
function AlternateActions({ data, context }: Cell<ManagedAlternate>) {
  if (!data) return null;
  return <div className="admin-grid-actions">
    <button type="button" className="button soft small" aria-label={`Editar alterno ${data.code}`} onClick={() => context.onEditAlternate(data)}><Pencil size={13} aria-hidden="true"/>Editar</button>
    <button type="button" className="button text small danger-text" aria-label={`Retirar alterno ${data.code}`} onClick={() => context.onRemoveAlternate(data)}><Trash2 size={13} aria-hidden="true"/>Retirar</button>
  </div>;
}
export const alternateOrdering: Record<string, string> = { part: 'part__sku', code: 'code', brand: 'brand', type: 'ref_type' };
export function alternateColumns(wide: boolean): ColDef<ManagedAlternate>[] {
  return [
    { colId: 'part', headerName: 'SKU interno', width: 240, pinned: wide ? 'left' : undefined, cellRenderer: AlternateSkuCell },
    { colId: 'code', headerName: 'Código alterno', flex: 1, minWidth: 200, cellRenderer: AlternateCodeCell },
    { colId: 'brand', headerName: 'Marca del código', valueGetter: ({ data }) => data?.brand || 'Todas las marcas', width: 190, cellClass: 'admin-grid-text' },
    { colId: 'type', headerName: 'Tipo', valueGetter: ({ data }) => data ? referenceLabel(data).split(' · ')[0] : '', width: 140, cellClass: 'admin-grid-text muted' },
    { colId: 'source', headerName: 'Fuente', field: 'reference_source', flex: 1.2, minWidth: 220, cellClass: 'admin-grid-text muted', tooltipField: 'reference_source' },
    { colId: 'actions', headerName: 'Acciones', width: 200, pinned: wide ? 'right' : undefined, cellRenderer: AlternateActions, suppressHeaderMenuButton: true, resizable: false },
  ];
}
