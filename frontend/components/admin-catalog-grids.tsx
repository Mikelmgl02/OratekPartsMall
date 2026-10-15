'use client';
import type { ColDef, DefaultMenuItem, GetContextMenuItemsParams, GridApi, MenuItemDef, SuppressKeyboardEventParams } from 'ag-grid-community';
import type { CustomCellRendererProps } from 'ag-grid-react';
import { Images, Pencil, SearchCheck, Trash2, Ungroup, Wrench } from 'lucide-react';
import { referenceClass, referenceLabel } from '@/lib/reference-label';
import type { ManagedAlternate, ManagedPart, ManagedStockItem } from '@/lib/types';
import { lineEstadoLabels, lineText, lineTipoLabels, referenceEstadoLabels, subgrupoLabel } from '@/lib/line-types';
import type { LineCode, LineEstado, OEMLine } from '@/lib/line-types';
import CodesEditor from './admin-codes-editor';

export type CatalogGridActions = {
  onEditPart: (line: OEMLine) => void; onTechnical: (line: OEMLine) => void; onImages: (line: OEMLine) => void;
  onEditAlternate: (alternate: ManagedAlternate) => void; onRemoveAlternate: (alternate: ManagedAlternate) => void; onEditMatch: (item: ManagedStockItem) => void;
  onCodesChanged: (lineId: string) => void; onSplit: (target: { id: string; sku: string }) => void; onMerge: (lines: OEMLine[]) => void;
  onMain: (line: OEMLine) => void;
};
// Codes pasted into the Referencias cell (comma, semicolon or line separated) wait here until the grid adds them one by one.
export const pastedCodes = new WeakMap<OEMLine, string[]>();
// A row field and the column that shows it: what flashes when a fresh row comes in.
export const catalogFieldColumns: Record<string, string> = { description: 'description', name: 'description', category: 'subgroup', subcategory: 'subgroup',
  subgrupo: 'subgroup', grupo: 'subgroup', estado: 'estado', codes: 'codes', variant_count: 'stock', sku: 'sku', tipo: 'tipo', brand: 'brand', merged_count: 'sku' };
type Cell<T> = CustomCellRendererProps<T, unknown, CatalogGridActions>;
const ALTERNATES_SHOWN = 4;
const number = (value: number | null | undefined) => (value ?? 0).toLocaleString('es-PA');

// A SKU's MAIN tag, for the screens that still read catalog/ rows (Agrupar con…): OEM, or interno with its brand.
export function identityText(part: ManagedPart) {
  return part.is_OEM ? 'MAIN OEM' : part.identity?.ref_type === 'company' ? `MAIN INTERNO · ${part.identity.brand}` : 'MAIN INTERNO';
}
// An OEM line's main Reference, under its codigo: the line's tipo and the main's brand (TOYOTA, GMB...).
export const lineIdentity = (line: Pick<OEMLine, 'tipo' | 'brand'>) => `${lineTipoLabels[line.tipo] ?? line.tipo}${line.brand ? ` · ${line.brand}` : ''}`;
export const subgroupLabel = (part?: Pick<ManagedPart, 'category' | 'subcategory'>) => [part?.category, part?.subcategory].filter(Boolean).join(' / ');
export const parseSubgroup = (label: unknown) => { const [category = '', subcategory = ''] = String(label || '').split(' / '); return { category, subcategory }; };
const codeText = (line?: OEMLine) => line?.codes?.map(code => `${code.code} · ${referenceLabel(code)}${code.estado === 'disputa' ? ' · EN DISPUTA' : ''}`).join('\n') || '';
const lineEstado = (estado: LineEstado) => lineEstadoLabels[estado] ?? String(estado).toUpperCase();

// ---------------------------------------------------------------- Inventario interno (OEM lines: management/lines)
function SkuCell({ data, context }: Cell<OEMLine>) {
  if (!data) return null;
  const merged = data.merged_count ?? 0;
  // The code opens the line's editor (so does a double click anywhere in the cell); the badge, the lines a merge unified into this one,
  // opens Separar SKU on it. A unificada line names the way back out: Separar SKU on the line it went into.
  if (data.estado === 'unificada') return <div className="admin-grid-stack"><div className="sku-line"><button type="button" className="sku-open" title="Abrir la línea OEM"
      onClick={() => context.onEditPart(data)}><strong>{data.sku}</strong></button></div>
    {data.merged_into ? <button type="button" className="button text small" onClick={() => context.onSplit({ id: data.merged_into!, sku: '' })}><Ungroup size={11} aria-hidden="true"/>Separar de su línea OEM</button>
      : <span className="sku-identity line-estado">UNIFICADA</span>}</div>;
  return <div className="admin-grid-stack"><div className="sku-line"><button type="button" className="sku-open" title="Abrir la línea OEM"
      onClick={() => context.onEditPart(data)}><strong>{data.sku}</strong></button>{merged > 0 && <button type="button" className="sku-merged"
      aria-label={`${merged} ${merged === 1 ? 'SKU agrupado' : 'SKU agrupados'} en ${data.sku}: separar`} title="Líneas OEM unificadas en esta · clic para separarlas"
      onClick={() => context.onSplit({ id: data.id, sku: data.sku })}><Ungroup size={11} aria-hidden="true"/>{merged}</button>}</div>
    <span className={`sku-identity ${data.tipo === 'OEM' ? 'oem' : 'internal'}`}>{lineIdentity(data)}</span></div>;
}
function TipoCell({ data }: Cell<OEMLine>) {
  return data ? <div className="admin-grid-stack"><span className={`sku-identity ${data.tipo === 'OEM' ? 'oem' : 'internal'}`}>{lineTipoLabels[data.tipo] ?? data.tipo}</span></div> : null;
}
function chip(code: LineCode) {
  return <span key={code.id} className={`${referenceClass(code)}${code.estado === 'disputa' ? ' disputa' : ''}`}
    title={`${referenceLabel(code)} · ${referenceEstadoLabels[code.estado] ?? code.estado}`}>{code.code}</span>;
}
function CodesCell({ data }: Cell<OEMLine>) {
  if (!data) return null;
  const codes = data.codes ?? [];
  if (!codes.length) return <span className="admin-grid-muted">{data.reference_count > 1 ? `${data.reference_count - 1} referencias` : 'Solo el código del SKU'}</span>;
  const hidden = codes.length - ALTERNATES_SHOWN;
  // The chips may be clipped by the column width; the count of the rest sits outside the clipped area so it always shows.
  return <div className="admin-grid-codes"><div className="admin-grid-chips">{codes.slice(0, ALTERNATES_SHOWN).map(chip)}</div>{hidden > 0 && <span className="more">+{hidden}</span>}</div>;
}
function EstadoCell({ data }: Cell<OEMLine>) {
  return data ? <span className={`status ${data.estado === 'publicada' ? 'matched' : data.estado === 'preparacion' ? 'preparacion' : ''}`}>{lineEstado(data.estado)}</span> : null;
}
function PartActions({ data, context }: Cell<OEMLine>) {
  if (!data) return null;
  return <div className="admin-grid-actions">
    <button type="button" className="button soft small" aria-label={`Editar línea OEM ${data.sku}`} onClick={() => context.onEditPart(data)}><Pencil size={13} aria-hidden="true"/>Editar</button>
    <button type="button" className="button ghost small" aria-label={`Ficha técnica de ${data.sku}`} disabled={data.estado === 'unificada'} onClick={() => context.onTechnical(data)}><Wrench size={13} aria-hidden="true"/>Ficha</button>
    <button type="button" className="button ghost small" aria-label={`Imágenes de ${data.sku}`} disabled={data.estado === 'unificada'} onClick={() => context.onImages(data)}><Images size={13} aria-hidden="true"/>{data.images?.length || 0}</button>
  </div>;
}
// colId -> the lines/ ordering fields (mall.line_views.LineList.ordering_fields): the subgroup and brand do not sort.
export const catalogOrdering: Record<string, string> = { sku: 'sku', description: 'description', tipo: 'tipo', estado: 'estado', stock: 'variant_count' };
export const MERGE_LIMIT = 51;  // a main SKU and the 50 others one merge takes (catalog/grouping/merge)
// The rows the grid's cell selection covers (drag or Shift+click over several rows), at most the merge limit.
export function selectedParts(api: GridApi<OEMLine>): OEMLine[] {
  const rows = new Map<string, OEMLine>();
  for (const range of api.getCellRanges() ?? []) {
    if (!range.startRow || !range.endRow) continue;
    const [from, to] = [range.startRow.rowIndex, range.endRow.rowIndex].sort((a, b) => a - b);
    for (let index = from; index <= to && rows.size <= MERGE_LIMIT; index++) {
      const data = api.getDisplayedRowAtIndex(index)?.data;
      if (data) rows.set(data.id, data);
    }
  }
  return [...rows.values()];
}
// Right click on a line: group it with others (the selected rows, when the click falls inside a selection of several; catalog lines
// only), split the lines unified into it, or choose its main Reference (its code becomes the SKU); the grid's own items follow.
const menuIcon = (name: string) => `<span class="ag-icon ag-icon-${name}" unselectable="on" role="presentation"></span>`;
export function catalogContextMenu(params: GetContextMenuItemsParams<OEMLine, CatalogGridActions>): (DefaultMenuItem | MenuItemDef<OEMLine, CatalogGridActions>)[] {
  const line = params.node?.data, defaults = params.defaultItems ?? [];
  if (!line) return defaults;
  const live = line.estado !== 'unificada';
  const catalog = line.estado === 'publicada' || line.estado === 'retirada';
  const chosen = selectedParts(params.api).filter(row => row.estado === 'publicada' || row.estado === 'retirada');
  const many = chosen.length > 1 && chosen.some(row => row.id === line.id);
  const merged = line.merged_count ?? 0;
  return [
    ...(catalog ? [many ? { name: `Agrupar ${chosen.length > MERGE_LIMIT ? `más de ${MERGE_LIMIT}` : chosen.length} SKU seleccionados…`, icon: menuIcon('linked'), disabled: chosen.length > MERGE_LIMIT,
             tooltip: chosen.length > MERGE_LIMIT ? `Selecciona hasta ${MERGE_LIMIT} SKU.` : undefined, action: () => params.context.onMerge(chosen) }
      : { name: `Agrupar ${line.sku} con…`, icon: menuIcon('linked'), action: () => params.context.onMerge([line]) }] : []),
    { name: merged ? `Separar SKU agrupados (${merged})` : 'Separar SKU agrupados', icon: menuIcon('unlinked'), disabled: !merged,
      tooltip: merged ? undefined : 'Esta línea OEM no tiene SKU agrupados.', action: () => params.context.onSplit({ id: line.id, sku: line.sku }) },
    { name: 'Marcar referencia principal…', icon: menuIcon('tick'), disabled: !live || line.reference_count < 2,
      tooltip: !live ? 'Una línea OEM unificada no cambia de referencia principal.' : line.reference_count < 2 ? 'La línea OEM solo tiene su referencia principal.' : undefined,
      action: () => params.context.onMain(line) },
    ...(defaults.length ? ['separator' as const, ...defaults] : []),
  ];
}
// edit: Editar celdas is on (the subgrupos as GRUPO / SUBGRUPO labels, whether a batch is being saved, and whether the estado moves here:
// publicada ↔ retirada, the catalog tab); null keeps the grid read-only. Description, group / subgroup and estado edit in place, each row
// saved with the version it was read at (management/lines); references in their popup; the codigo follows the main Reference.
export type SubgroupChoice = { grupo: { id: string; nombre: string }; subgrupo: { id: string; name: string } };
export type CatalogEdit = { subgroups: string[]; subgroupOf: (label: string) => SubgroupChoice | null; isSaving: () => boolean; estados: boolean };
export function catalogColumns(wide: boolean, edit: CatalogEdit | null = null): ColDef<OEMLine>[] {
  const editing = edit !== null;
  const editable = ({ data }: { data?: OEMLine }) => editing && !edit.isSaving() && data?.estado !== 'unificada';
  const estadoEditable = ({ data }: { data?: OEMLine }) => editing && edit.estados && !edit.isSaving() && (data?.estado === 'publicada' || data?.estado === 'retirada');
  const cue = { 'editable-cell-active': (params: { data?: OEMLine }) => editable(params) };
  const estadoCue = { 'editable-cell-active': (params: { data?: OEMLine }) => estadoEditable(params) };
  const title = (text: string) => editing ? `${text} ✎` : text;
  return [
    { colId: 'sku', headerName: 'SKU interno', width: 214, minWidth: 170, pinned: wide ? 'left' : undefined, cellRenderer: SkuCell, tooltipValueGetter: ({ data }) => data?.sku },
    { colId: 'tipo', headerName: 'Tipo', width: 132, cellRenderer: TipoCell, valueGetter: ({ data }) => data ? lineTipoLabels[data.tipo] ?? data.tipo : '' },
    { colId: 'brand', headerName: 'Marca', width: 140, valueGetter: ({ data }) => data?.brand || '—', cellClass: 'admin-grid-text', tooltipValueGetter: ({ data }) => data?.brand || 'Sin marca' },
    { colId: 'description', headerName: title('Descripción'), field: 'description', valueFormatter: ({ data }) => data ? lineText(data) : '', flex: 1.2, minWidth: 200, cellClass: 'admin-grid-text',
      cellClassRules: cue, tooltipValueGetter: ({ data }) => data ? lineText(data) : '', editable, cellEditor: 'agTextCellEditor', cellDataType: false },
    // Always opens (read-only without Editar celdas) so every code can be read and copied. Enter and Tab belong to the popup's own
    // fields (the grid would otherwise close it and move to another cell).
    { colId: 'codes', headerName: title('Referencias'), field: 'codes', cellRenderer: CodesCell, flex: 1, minWidth: 230, tooltipValueGetter: ({ data }) => codeText(data),
      valueFormatter: ({ value }) => (value as LineCode[] | undefined)?.map(code => code.code).join(', ') ?? '', cellDataType: false, cellClassRules: cue,
      editable: ({ data }) => data?.estado !== 'unificada', cellEditor: CodesEditor, cellEditorPopup: true, cellEditorPopupPosition: 'under', cellEditorParams: { readOnly: !editing },
      suppressKeyboardEvent: ({ editing: open, event }: SuppressKeyboardEventParams<OEMLine>) => open && (event.key === 'Enter' || event.key === 'Tab'),
      valueParser: ({ newValue }) => typeof newValue === 'string' ? newValue.split(/[,;\n]+/).map(text => text.trim().toUpperCase()).filter(Boolean) : newValue,
      valueSetter: ({ data, newValue }) => {
        if (!editable({ data }) || !Array.isArray(newValue) || !newValue.every(item => typeof item === 'string')) return false;
        const known = new Set([data.sku, ...(data.codes ?? []).map(code => code.code)].map(code => code.toUpperCase()));
        const fresh = [...new Set(newValue as string[])].filter(text => !known.has(text));
        if (!fresh.length) return false;
        pastedCodes.set(data, fresh);
        return true;
      } },
    { colId: 'stock', headerName: 'Existencias', valueGetter: ({ data }) => data?.variant_count ?? null, valueFormatter: ({ value }) => value === null ? '' : number(value), width: 112, type: 'rightAligned', cellClass: ['ag-right-aligned-cell', 'admin-grid-number'] },
    // The select editor labels its options with the column's valueFormatter (the renderer still draws the cell).
    { colId: 'estado', headerName: title('Estado'), field: 'estado', width: 134, cellRenderer: EstadoCell, valueFormatter: ({ value }) => value ? lineEstado(value as LineEstado) : '',
      cellClassRules: estadoCue, editable: estadoEditable, cellEditor: 'agSelectCellEditor', cellEditorParams: { values: ['publicada', 'retirada'] }, cellDataType: false,
      // A pasted PUBLICADA / RETIRADA (or ACTIVO / INACTIVO, SI / NO); anything else is ignored.
      valueParser: ({ newValue, oldValue }) => {
        const text = String(newValue ?? '').trim().toUpperCase();
        return ['PUBLICADA', 'ACTIVO', 'SI', 'SÍ', 'TRUE', '1'].includes(text) ? 'publicada' : ['RETIRADA', 'INACTIVO', 'NO', 'FALSE', '0'].includes(text) ? 'retirada'
          : ['publicada', 'retirada'].includes(String(newValue)) ? newValue : oldValue;
      } },
    { colId: 'subgroup', headerName: title('Grupo / subgrupo'), valueGetter: ({ data }) => data ? subgrupoLabel(data) : '', valueFormatter: ({ value }) => value || '—', width: 220,
      cellClass: 'admin-grid-text muted', cellClassRules: cue, tooltipValueGetter: ({ data }) => data ? subgrupoLabel(data) : '', cellDataType: false, editable,
      // Only the catalog's grupos / subgrupos (a paste of anything else is ignored), or blank to unclassify.
      valueSetter: ({ data, newValue }) => {
        const label = String(newValue ?? '').trim().toUpperCase();
        const choice = label ? edit?.subgroupOf(label) ?? null : null;
        if ((label && !choice) || label === subgrupoLabel(data)) return false;
        Object.assign(data, { category: choice?.grupo.nombre ?? '', subcategory: choice?.subgrupo.name ?? '', grupo: choice?.grupo ?? null, subgrupo: choice?.subgrupo ?? null });
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
// A code change waiting for review (rule 5b) shows under the code the item still sells under.
function StockCodeCell({ data }: Cell<ManagedStockItem>) {
  if (!data) return null;
  const pending = data.codigo_pendiente || data.brand_pendiente;
  const fresh = [data.codigo_pendiente || data.codigo, data.brand_pendiente].filter(Boolean).join(' · ');
  return <div className="admin-grid-stack"><strong>{data.codigo}</strong>{pending ? <small className="stock-code-pending" title={`Código nuevo en revisión (Casos de coincidencia): ${fresh}`}>EN REVISIÓN: {fresh}</small> : <small>{data.brand}</small>}</div>;
}
// A line that is not publicada (S6: Productos nuevos are lines in preparación) holds the item out of the storefront.
function LinkedSkuCell({ data }: Cell<ManagedStockItem>) {
  if (!data) return null;
  const estado = data.part_sku && data.line_estado && data.line_estado !== 'publicada' ? lineEstado(data.line_estado as LineEstado) : '';
  return <div className="admin-grid-stack"><div className="sku-line"><strong className={data.part_sku ? '' : 'admin-grid-muted'}>{data.part_sku || 'Sin vincular'}</strong>{estado && <span className="sku-identity line-estado">{estado}</span>}</div>{data.part_name && data.part_name !== data.part_sku && <small>{data.part_name}</small>}</div>;
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
