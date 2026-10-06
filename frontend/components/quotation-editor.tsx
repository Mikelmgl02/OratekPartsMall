'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { LoaderCircle, Send } from 'lucide-react';
import { AgGridReact, type CustomCellEditorProps, type CustomCellRendererProps } from 'ag-grid-react';
import type { ColDef, GetRowIdParams, GridReadyEvent, ValueSetterParams } from 'ag-grid-community';
import { gridLocale, motionGridTheme } from '@/lib/ag-grid';
import type { Deal } from '@/lib/deal-types';
import { decimal, moneyFormatter, priceCents, quantityValue } from '@/lib/money';
import { UppercaseTextarea } from './uppercase-field';

type QuoteRow = { order_line_id: string; codigo: string; description: string; requested: number; quantity: string; unit_price: string };
const rowId = ({ data }: GetRowIdParams<QuoteRow>) => data.order_line_id;

function rowCents(row: QuoteRow) { return BigInt(quantityValue(row.quantity) ?? 0) * (priceCents(row.unit_price) ?? BigInt(0)); }

function NumericEditor({ value, onValueChange, data, colDef, stopEditing, api, node, column }: CustomCellEditorProps<QuoteRow, string>) {
  const input = useRef<HTMLInputElement>(null);
  const price = colDef.field === 'unit_price';
  useEffect(() => { input.current?.focus(); input.current?.select(); }, []);
  return <input ref={input} className="quotation-cell-input" type="text" inputMode={price ? 'decimal' : 'numeric'}
    aria-label={`${price ? 'Precio' : 'Unidades ofrecidas'} de ${data.codigo}`} autoComplete="off" autoCorrect="off" spellCheck={false}
    value={value ?? ''} onChange={event => onValueChange(event.target.value)}
    onKeyDownCapture={event => { if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); api.stopEditing(true); if (node.rowIndex !== null) api.setFocusedCell(node.rowIndex, column, node.rowPinned); } }}
    onKeyDown={event => { if (event.key === 'Enter') { event.preventDefault(); event.stopPropagation(); stopEditing(); } }}/>;
}
function PartCell({ data }: CustomCellRendererProps<QuoteRow>) {
  return data ? <div className="quotation-part-cell"><strong>{data.codigo}</strong>{data.description && <small title={data.description}>{data.description}</small>}</div> : null;
}

export default function QuotationEditor({ deal, disabled, onSend }: { deal: Deal; disabled: boolean; onSend: (values: Record<string, unknown>) => void }) {
  const [rows, setRows] = useState<QuoteRow[]>(() => deal.lines.map(line => {
    const prior = deal.quotation?.lines.find(value => value.order_line_id === line.id);
    return { order_line_id: line.id, codigo: line.codigo, description: line.description || line.name,
      requested: line.quantity, quantity: String(prior?.quantity ?? line.quantity), unit_price: prior?.unit_price ?? '' };
  }));
  const rowsRef = useRef(rows);
  const grid = useRef<AgGridReact<QuoteRow>>(null);
  const host = useRef<HTMLDivElement>(null);
  const [currency, setCurrency] = useState(deal.quotation?.currency || 'USD');
  const [terms, setTerms] = useState(deal.quotation?.terms || '');
  const [error, setError] = useState('');
  const formatMoney = useMemo(() => moneyFormatter(currency), [currency]);
  const total = rows.reduce((sum, row) => sum + rowCents(row), BigInt(0));
  const completed = rows.filter(row => {
    const quantity = quantityValue(row.quantity);
    return quantity !== null && ((quantity === 0 && !row.unit_price.trim()) || priceCents(row.unit_price) !== null);
  }).length;
  const edited = useCallback((event: ValueSetterParams<QuoteRow>) => {
    const field = event.colDef.field;
    if (disabled || !event.data || (field !== 'quantity' && field !== 'unit_price')) return false;
    const value = String(event.newValue ?? '').trim();
    if (event.data[field] === value) return false;
    event.data[field] = value;
    const next = rowsRef.current.map(row => row.order_line_id === event.data.order_line_id ? { ...row, [field]: value } : row);
    // Value setters run synchronously, including when Send finishes an active edit.
    rowsRef.current = next; setRows(next); setError('');
    return true;
  }, [disabled]);
  const columns = useMemo<ColDef<QuoteRow>[]>(() => [
    { field: 'codigo', headerName: 'Código / descripción', minWidth: 230, flex: 1.6, filter: 'agTextColumnFilter', cellRenderer: PartCell, tooltipField: 'description' },
    { field: 'requested', headerName: 'Solicitadas', width: 130, minWidth: 125, type: 'numericColumn', filter: 'agNumberColumnFilter' },
    { field: 'quantity', headerName: 'Ofrecidas', width: 125, minWidth: 120, type: 'numericColumn', editable: !disabled, cellEditor: NumericEditor, valueSetter: edited,
      cellClass: params => ['ag-right-aligned-cell', quantityValue(params.value || '') === null ? 'quotation-invalid-cell' : 'quotation-editable-cell'],
      comparator: (a, b) => (quantityValue(a || '') ?? -1) - (quantityValue(b || '') ?? -1) },
    { field: 'unit_price', headerName: 'Precio unitario', width: 165, minWidth: 155, flex: 1, type: 'numericColumn', editable: !disabled, cellEditor: NumericEditor, valueSetter: edited,
      valueFormatter: params => priceCents(params.value || '') === null ? (params.value ? params.value : 'Ingresar precio') : formatMoney(priceCents(params.value)!),
      cellClass: params => ['ag-right-aligned-cell', params.value && priceCents(params.value) === null ? 'quotation-invalid-cell' : 'quotation-editable-cell'],
      comparator: (a, b) => Number((priceCents(a || '') ?? BigInt(-1)) - (priceCents(b || '') ?? BigInt(-1))) },
    { colId: 'line_total', headerName: 'Importe', width: 155, minWidth: 145, type: 'numericColumn', valueGetter: params => params.data ? rowCents(params.data) : BigInt(0),
      valueFormatter: params => formatMoney(params.value ?? BigInt(0)), comparator: (a: bigint, b: bigint) => a === b ? 0 : a < b ? -1 : 1 },
  ], [disabled, formatMoney, edited]);
  const defaultColumn = useMemo<ColDef<QuoteRow>>(() => ({ sortable: true, resizable: true, cellDataType: false, wrapHeaderText: true, autoHeaderHeight: true }), []);
  useEffect(() => { if (disabled) grid.current?.api?.stopEditing(true); }, [disabled]);
  function ready(event: GridReadyEvent<QuoteRow>) {
    const dialog = host.current?.closest('dialog');
    if (dialog) event.api.setGridOption('popupParent', dialog);
  }
  function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (disabled) return;
    grid.current?.api?.stopEditing();
    const lines = [];
    for (const row of rowsRef.current) {
      const quantity = quantityValue(row.quantity);
      const cents = quantity === 0 && !row.unit_price.trim() ? BigInt(0) : priceCents(row.unit_price);
      if (quantity === null || cents === null) {
        setError(quantity === null ? `Revisa las unidades de ${row.codigo}. Deben ser un número entero entre 0 y 9.999.` : `Revisa el precio de ${row.codigo}. Introduce un importe positivo o cero, con un máximo de dos decimales.`);
        const node = grid.current?.api?.getRowNode(row.order_line_id);
        if (node?.rowIndex != null) { grid.current?.api?.ensureIndexVisible(node.rowIndex); grid.current?.api?.ensureColumnVisible(quantity === null ? 'quantity' : 'unit_price'); grid.current?.api?.setFocusedCell(node.rowIndex, quantity === null ? 'quantity' : 'unit_price'); }
        return;
      }
      lines.push({ order_line_id: row.order_line_id, quantity, unit_price: decimal(cents) });
    }
    if (!lines.some(line => line.quantity > 0)) { setError('Ofrece al menos una unidad para enviar la cotización.'); return; }
    onSend({ currency, terms, lines });
  }
  return <form className="deal-quote-editor" onSubmit={submit} autoComplete="off">
    <h3>{deal.quotation ? 'Preparar versión ajustada' : 'Convertir orden en cotización'}</h3>
    <p>Edita las unidades ofrecidas y el precio de cada artículo. Usa 0 para artículos no disponibles. Al enviar, confirmas la cotización y sus condiciones quedan bloqueadas.</p>
    <div className="quotation-grid-guide"><span>Haz clic en una celda para editar · Tab para avanzar</span><strong>{completed} de {rows.length} {rows.length === 1 ? 'artículo completo' : 'artículos completos'}</strong></div>
    <div ref={host} className="quotation-grid" aria-label="Artículos de la cotización" style={{ height: Math.min(450, Math.max(170, rows.length * 56 + 60)) }}>
      <AgGridReact<QuoteRow> ref={grid} theme={motionGridTheme} localeText={gridLocale} rowData={rows} columnDefs={columns} defaultColDef={defaultColumn}
        getRowId={rowId} singleClickEdit stopEditingWhenCellsLoseFocus
        onGridReady={ready} cellSelection={!disabled} suppressClipboardPaste={disabled}
        ensureDomOrder suppressColumnVirtualisation enableBrowserTooltips loadThemeGoogleFonts={false}/>
    </div>
    {error && <div className="notice error" role="alert">{error}</div>}
    <div className="deal-editor-total"><label>Moneda<select aria-label="Moneda de la cotización" disabled={disabled} value={currency} onChange={event => setCurrency(event.target.value as 'USD' | 'PAB')}><option value="USD">USD</option><option value="PAB">PAB</option></select></label><strong>Total: {formatMoney(total)}</strong></div>
    <label>Condiciones de entrega y cotización<UppercaseTextarea disabled={disabled} maxLength={5000} value={terms} onChange={event => setTerms(event.target.value)} placeholder="PLAZO DE ENTREGA, RETIRO Y OTRAS CONDICIONES…"/></label>
    <button type="submit" className="button primary" disabled={disabled}>{disabled ? <LoaderCircle size={16} className="spin"/> : <Send size={16}/>}Confirmar y enviar cotización</button>
  </form>;
}
