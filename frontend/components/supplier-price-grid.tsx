'use client';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { History, LoaderCircle, RefreshCw, RotateCcw, Save, Search, X } from 'lucide-react';
import { AgGridReact, type CustomCellRendererProps } from 'ag-grid-react';
import type { ColDef, GetRowIdParams, ValueSetterParams } from 'ag-grid-community';
import Modal from './modal';
import { gridLocale, motionGridTheme } from '@/lib/ag-grid';
import { decimal, moneyFormatter, priceCents } from '@/lib/money';
import { Account, ApiError, Page, request } from '@/lib/types';
import type { ItemPricingInput, PriceChangeInput, PriceHistoryEntry, PriceList, PriceRow, PriceWriteConflict, PriceWriteResult } from '@/lib/pricing-types';

type Status = '' | 'priced' | 'missing' | 'below_floor';
// Cells are keyed discount_group, floor_price or price:<CODE>; edits keep what the supplier typed until it is saved.
type Edits = Map<string, Record<string, string>>;
const statuses: { value: Status; label: string }[] = [{ value: '', label: 'Todos' }, { value: 'priced', label: 'Con precio' }, { value: 'missing', label: 'Sin precio' }, { value: 'below_floor', label: 'Bajo el mínimo' }];
const rowId = ({ data }: GetRowIdParams<PriceRow>) => data.supplier_item_id;
const date = (value: string) => new Date(value).toLocaleString('es-PA', { timeZone: 'America/Panama', dateStyle: 'medium', timeStyle: 'short' });
const failure = (error: unknown, fallback: string) => error instanceof TypeError ? 'Se perdió la conexión. Tus cambios siguen en pantalla; vuelve a guardarlos.' : error instanceof Error ? error.message : fallback;
const BATCH = 500;

function serverValue(row: PriceRow | undefined, key: string) {
  if (!row) return '';
  if (key === 'discount_group') return row.discount_group;
  if (key === 'floor_price') return row.floor_price ?? '';
  return row.prices[key.slice(6)]?.unit_price ?? '';
}
function same(key: string, a: string, b: string) {
  if (key === 'discount_group') return a === b;
  if (!a || !b) return a === b;
  const left = priceCents(a), right = priceCents(b);
  return left !== null && left === right;
}
// A list price is blank (remove) or above zero; a floor is blank or zero and up; a LINEA has at most 60 characters.
function invalid(key: string, value: string) {
  if (key === 'discount_group') return value.length > 60;
  if (!value) return false;
  const cents = priceCents(value);
  return cents === null || (key !== 'floor_price' && cents === BigInt(0));
}
function PartCell({ data }: CustomCellRendererProps<PriceRow>) {
  return data ? <div className="quotation-part-cell"><strong>{data.codigo}</strong>{data.description && <small title={data.description}>{data.description}</small>}</div> : null;
}

export default function SupplierPriceGrid({ account, lists, canConfigure, onSaved }: { account: Account; lists: PriceList[]; canConfigure: boolean; onSaved: () => void }) {
  const active = useMemo(() => lists.filter(list => list.active), [lists]);
  const fallbackList = active.find(list => list.is_default) || active[0];
  const [search, setSearch] = useState('');
  const [applied, setApplied] = useState('');
  const [status, setStatus] = useState<Status>('');
  const [filterList, setFilterList] = useState(fallbackList?.id || '');
  const [page, setPage] = useState(1);
  const [revision, setRevision] = useState(0);
  const [data, setData] = useState<Page<PriceRow> | null>(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [saving, setSaving] = useState(false);
  const [historyRow, setHistoryRow] = useState<PriceRow | null>(null);
  // Every row seen keeps its last server values (and revisions), so edits made on another page are still compared and saved.
  const base = useRef(new Map<string, PriceRow>());
  const edits = useRef<Edits>(new Map());
  const [, setEditRevision] = useState(0);
  const grid = useRef<AgGridReact<PriceRow>>(null);
  const editable = canConfigure && !saving;
  useEffect(() => { const timer = setTimeout(() => { if (search.trim() !== applied) { setApplied(search.trim()); setPage(1); } }, 350); return () => clearTimeout(timer); }, [search, applied]);
  useEffect(() => { if (!active.some(list => list.id === filterList)) setFilterList(fallbackList?.id || ''); }, [active, filterList, fallbackList]);
  useEffect(() => {
    let cancelled = false; setError('');
    const query = new URLSearchParams({ page: String(page) });
    if (applied) query.set('search', applied);
    if (status) query.set('status', status);
    if (status && filterList) query.set('list', filterList);
    request<Page<PriceRow>>(`/api/market/accounts/${account.id}/prices?${query}`).then(value => {
      if (cancelled) return;
      for (const row of value.results) base.current.set(row.supplier_item_id, row);
      setData(value);
    }).catch(caught => { if (!cancelled) setError(failure(caught, 'No se pudieron cargar tus precios.')); });
    return () => { cancelled = true; };
  }, [account.id, applied, status, filterList, page, revision]);
  const pending = [...edits.current.values()].reduce((sum, edit) => sum + Object.keys(edit).length, 0);
  const wrong = [...edits.current.values()].reduce((sum, edit) => sum + Object.entries(edit).filter(([key, value]) => invalid(key, value)).length, 0);
  useEffect(() => {
    if (!pending) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ''; };
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [pending]);
  const refresh = () => { setEditRevision(value => value + 1); grid.current?.api?.refreshCells({ force: true }); };
  const cell = useCallback((row: PriceRow | undefined, key: string) => {
    const edit = row && edits.current.get(row.supplier_item_id);
    return edit && key in edit ? edit[key] : serverValue(row && base.current.get(row.supplier_item_id) || row, key);
  }, []);
  // Typing or pasting a value equal to the saved one cancels that cell's edit.
  const edited = useCallback((event: ValueSetterParams<PriceRow>) => {
    const key = event.colDef.colId!, row = event.data;
    if (!row || !canConfigure) return false;
    let value = String(event.newValue ?? '').trim();
    if (key === 'discount_group') value = value.toUpperCase();
    else value = value.replace(/^(?:B\/\.|\$|USD|PAB)\s*/i, '');
    const edit = { ...(edits.current.get(row.supplier_item_id) || {}) };
    if (same(key, value, serverValue(base.current.get(row.supplier_item_id) || row, key))) delete edit[key]; else edit[key] = value;
    if (Object.keys(edit).length) edits.current.set(row.supplier_item_id, edit); else edits.current.delete(row.supplier_item_id);
    setNotice(''); setEditRevision(value => value + 1);
    return true;
  }, [canConfigure]);
  const columns = useMemo<ColDef<PriceRow>[]>(() => {
    const classes = (key: string) => (params: { data?: PriceRow }) => {
      const edit = params.data && edits.current.get(params.data.supplier_item_id), value = cell(params.data, key);
      return [key === 'discount_group' ? '' : 'ag-right-aligned-cell', edit && key in edit ? (invalid(key, value) ? 'quotation-invalid-cell' : 'price-grid-edited') : canConfigure ? 'quotation-editable-cell' : ''];
    };
    const money = (currency: string) => { const format = moneyFormatter(currency); return (value: string) => { const cents = value ? priceCents(value) : null; return cents === null ? value : format(cents); }; };
    return [
      { field: 'codigo', headerName: 'Código / descripción', minWidth: 220, flex: 1.5, cellRenderer: PartCell, tooltipField: 'description' },
      { field: 'supplier_invent_id', headerName: 'Tu ID de inventario', minWidth: 150, flex: 0.8 },
      { field: 'brand', headerName: 'Marca', minWidth: 110, flex: 0.6 },
      { colId: 'discount_group', headerName: 'Línea', minWidth: 130, flex: 0.7, editable, valueGetter: params => cell(params.data, 'discount_group'), valueSetter: edited, cellClass: classes('discount_group'),
        tooltipValueGetter: params => invalid('discount_group', cell(params.data, 'discount_group')) ? 'Usa como máximo 60 caracteres' : undefined },
      ...active.map((list): ColDef<PriceRow> => {
        const key = `price:${list.code}`, format = money(list.currency);
        return { colId: key, headerName: `Precio ${list.code}`, headerTooltip: `${list.name} · ${list.currency}${list.is_default ? ' · predeterminada' : ''}`, minWidth: 140, flex: 0.8, type: 'numericColumn',
          editable, valueGetter: params => cell(params.data, key), valueSetter: edited, valueFormatter: params => params.value ? format(params.value) : 'Sin precio', cellClass: classes(key),
          tooltipValueGetter: params => invalid(key, cell(params.data, key)) ? 'Escribe un precio mayor que 0 con hasta dos decimales, o deja la celda vacía para quitarlo' : undefined };
      }),
      { colId: 'floor_price', headerName: 'Precio mínimo', minWidth: 130, flex: 0.7, type: 'numericColumn', editable, valueGetter: params => cell(params.data, 'floor_price'), valueSetter: edited,
        valueFormatter: params => params.value ? money(fallbackList?.currency || 'USD')(params.value) : '—', cellClass: classes('floor_price'),
        tooltipValueGetter: params => invalid('floor_price', cell(params.data, 'floor_price')) ? 'Escribe un importe de 0 o más con hasta dos decimales' : undefined },
      { colId: 'updated_at', headerName: 'Actualizado', minWidth: 150, flex: 0.8, valueGetter: params => params.data?.updated_at ? date(params.data.updated_at) : '—' },
      { colId: 'history', headerName: '', width: 64, sortable: false, resizable: false, cellRenderer: ({ data }: CustomCellRendererProps<PriceRow>) => data ? <button type="button" className="icon-button"
          aria-label={`Historial de precio de ${data.codigo}`} onClick={() => setHistoryRow(data)}><History size={16}/></button> : null },
    ];
  }, [active, editable, canConfigure, cell, edited, fallbackList]);
  const defaultColumn = useMemo<ColDef<PriceRow>>(() => ({ sortable: false, resizable: true, cellDataType: false, wrapHeaderText: true, autoHeaderHeight: true }), []);
  function adopt(rows: PriceRow[]) {
    for (const row of rows) base.current.set(row.supplier_item_id, row);
    setData(value => value && { ...value, results: value.results.map(row => base.current.get(row.supplier_item_id) || row) });
  }
  async function save() {
    grid.current?.api?.stopEditing();
    if (!pending || wrong || saving) return;
    const byCode = Object.fromEntries(lists.map(list => [list.code, list]));
    // One request per batch of up to 500 prices and 500 items; each is all-or-nothing on the server.
    const batches: { ids: string[]; changes: PriceChangeInput[]; items: ItemPricingInput[] }[] = [];
    for (const [id, edit] of edits.current) {
      const row = base.current.get(id)!, changes: PriceChangeInput[] = [], item: ItemPricingInput = { supplier_item_id: id, expected_revision: row.item_pricing_revision };
      for (const [key, value] of Object.entries(edit)) {
        if (key === 'discount_group') { item.discount_group = value; continue; }
        const amount = value ? decimal(priceCents(value)!) : null;
        if (key === 'floor_price') item.floor_price = amount;
        else changes.push({ list_id: byCode[key.slice(6)].id, supplier_item_id: id, unit_price: amount, expected_revision: row.prices[key.slice(6)]?.revision ?? null });
      }
      const items = 'discount_group' in item || 'floor_price' in item ? [item] : [];
      let batch = batches.at(-1);
      if (!batch || batch.changes.length + changes.length > BATCH || batch.items.length + items.length > BATCH) batches.push(batch = { ids: [], changes: [], items: [] });
      batch.ids.push(id); batch.changes.push(...changes); batch.items.push(...items);
    }
    setSaving(true); setError(''); setNotice('');
    let saved = 0;
    try {
      for (const batch of batches) {
        try {
          const result = await request<PriceWriteResult>(`/api/market/accounts/${account.id}/prices`, { method: 'POST',
            body: JSON.stringify({ ...(batch.changes.length ? { changes: batch.changes } : {}), ...(batch.items.length ? { items: batch.items } : {}) }) });
          for (const id of batch.ids) edits.current.delete(id);
          adopt(result.rows);
          saved += batch.changes.length + batch.items.length;
        } catch (caught) {
          const body = caught instanceof ApiError && caught.status === 409 ? caught.body as PriceWriteConflict : null;
          if (!body?.conflicts) throw caught;
          // Rows another member changed take their current values; the other edits stay on screen to save again.
          adopt(body.rows);
          let reloaded = 0;
          for (const conflict of body.conflicts) {
            const edit = edits.current.get(conflict.supplier_item_id);
            if (!edit) continue;
            const keys = conflict.list_id ? [`price:${lists.find(list => list.id === conflict.list_id)?.code}`] : ['discount_group', 'floor_price'];
            for (const key of keys) if (key in edit) { delete edit[key]; reloaded++; }
            if (!Object.keys(edit).length) edits.current.delete(conflict.supplier_item_id);
          }
          setError(`Otro usuario cambió ${reloaded} ${reloaded === 1 ? 'precio' : 'precios'}. Se recargaron sus valores actuales.`);
          return;
        }
      }
      setNotice(`Se guardaron ${saved} ${saved === 1 ? 'cambio' : 'cambios'}.`);
    } catch (caught) { setError(failure(caught, 'No se pudieron guardar los precios.')); }
    finally { setSaving(false); refresh(); if (saved) onSaved(); }
  }
  function discard() { edits.current.clear(); setNotice(''); setError(''); refresh(); }
  const rows = data?.results || [];
  return <div className="price-grid-panel" aria-label="Precios por artículo" role="region">
    <div className="panel-heading"><div><h3>Precios por artículo</h3><p>{canConfigure ? 'Selecciona una celda y escribe, o pega un rango copiado de Excel · Una celda vacía quita el precio' : 'Solo lectura'}</p></div>
      <button type="button" className="button text" onClick={() => setRevision(value => value + 1)}><RefreshCw size={15}/>Actualizar</button></div>
    <div className="price-grid-controls">
      <div className="supplier-requests-search price-grid-search"><Search size={18}/><input aria-label="Buscar artículos" value={search} maxLength={200} placeholder="Código, ID, marca o línea…" autoComplete="off" onChange={event => setSearch(event.target.value)}/>{search && <button type="button" className="icon-button" aria-label="Limpiar búsqueda de artículos" onClick={() => setSearch('')}><X size={16}/></button>}</div>
      <div className="supplier-request-filters" role="group" aria-label="Filtrar precios">{statuses.map(item => <button type="button" key={item.value} aria-pressed={status === item.value} className={status === item.value ? 'selected' : ''} onClick={() => { setStatus(item.value); setPage(1); }}>{item.label}</button>)}</div>
      {active.length > 1 && status && <label className="price-grid-list">Lista<select value={filterList} onChange={event => { setFilterList(event.target.value); setPage(1); }}>{active.map(list => <option key={list.id} value={list.id}>{list.code}</option>)}</select></label>}
    </div>
    {notice && <div className="notice success" role="status">{notice}</div>}
    {error && <div className="notice error" role="alert">{error}</div>}
    {!data && !error ? <div className="loading"><LoaderCircle className="spin"/>Cargando tus precios…</div> : <>
      {!rows.length ? <p className="empty-state compact">{applied || status ? 'No hay artículos para estos filtros.' : 'Tu inventario todavía no tiene artículos. Carga primero sus existencias.'}</p>
        : <div className="quotation-grid price-grid" style={{ height: Math.min(620, rows.length * 56 + 64) }}>
          <AgGridReact<PriceRow> ref={grid} theme={motionGridTheme} localeText={gridLocale} rowData={rows} columnDefs={columns} defaultColDef={defaultColumn} getRowId={rowId}
            stopEditingWhenCellsLoseFocus cellSelection={canConfigure} suppressClipboardPaste={!editable} ensureDomOrder suppressColumnVirtualisation enableBrowserTooltips loadThemeGoogleFonts={false}/>
        </div>}
      {canConfigure && <div className="price-grid-actions">
        <span>{wrong ? 'Corrige las celdas marcadas para guardar.' : pending ? `${pending} ${pending === 1 ? 'cambio sin guardar' : 'cambios sin guardar'}` : 'Sin cambios pendientes'}</span>
        <button type="button" className="button soft small" disabled={!pending || saving} onClick={discard}><RotateCcw size={14}/>Descartar</button>
        <button type="button" className="button primary small" disabled={!pending || !!wrong || saving} onClick={() => void save()}>{saving ? <LoaderCircle size={14} className="spin"/> : <Save size={14}/>}Guardar cambios ({pending})</button>
      </div>}
      {data && <div className="pagination"><span>{data.count} {data.count === 1 ? 'artículo' : 'artículos'}</span><div><button type="button" className="button soft small" disabled={!data.previous} onClick={() => setPage(value => value - 1)}>Anterior</button><span>Página {page}</span><button type="button" className="button soft small" disabled={!data.next} onClick={() => setPage(value => value + 1)}>Siguiente</button></div></div>}
    </>}
    {historyRow && <PriceHistory account={account} row={historyRow} lists={lists} onClose={() => setHistoryRow(null)}/>}
  </div>;
}

function PriceHistory({ account, row, lists, onClose }: { account: Account; row: PriceRow; lists: PriceList[]; onClose: () => void }) {
  const [page, setPage] = useState(1);
  const [data, setData] = useState<Page<PriceHistoryEntry> | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    let cancelled = false; setData(null); setError('');
    request<Page<PriceHistoryEntry>>(`/api/market/accounts/${account.id}/prices/${row.supplier_item_id}/history?page=${page}`).then(value => { if (!cancelled) setData(value); })
      .catch(caught => { if (!cancelled) setError(failure(caught, 'No se pudo cargar el historial.')); });
    return () => { cancelled = true; };
  }, [account.id, row.supplier_item_id, page]);
  const currency = (code?: string) => lists.find(list => list.code === code)?.currency || lists.find(list => list.is_default)?.currency || 'USD';
  const show = (value: string | null, code?: string) => value === null ? 'sin precio' : moneyFormatter(currency(code))(priceCents(value)!);
  return <Modal title={`Historial de precio · ${row.codigo}`} onClose={onClose}>
    <p className="modal-description">Cada cambio de precio, precio mínimo o línea de este artículo, del más reciente al más antiguo.</p>
    {error && <div className="notice error" role="alert">{error}</div>}
    {!data && !error && <div className="loading"><LoaderCircle className="spin"/>Cargando el historial…</div>}
    {data && !data.results.length && <p className="empty-state compact">Todavía no hay cambios de precio.</p>}
    {data && <ol className="pricing-audit-list" aria-label={`Historial de precio de ${row.codigo}`}>{data.results.map(entry => <li key={entry.id}><div>
      <strong>{entry.kind === 'list_price' ? `Precio ${entry.price_list?.code}` : entry.kind === 'floor_price' ? 'Precio mínimo' : 'Línea'}</strong>
      <span>{entry.kind === 'discount_group' ? `${entry.old_text || '—'} → ${entry.new_text || '—'}` : `${show(entry.old_value, entry.price_list?.code)} → ${show(entry.new_value, entry.price_list?.code)}`}</span></div>
      <small>{date(entry.created_at)} · {entry.actor.name} · {entry.source === 'import' ? 'Importación' : entry.source === 'api' ? 'Integración' : 'Manual'}</small></li>)}</ol>}
    {data && (data.next || data.previous) && <div className="pagination"><button type="button" className="button soft small" disabled={!data.previous} onClick={() => setPage(value => value - 1)}>Anterior</button><span>Página {page}</span><button type="button" className="button soft small" disabled={!data.next} onClick={() => setPage(value => value + 1)}>Siguiente</button></div>}
  </Modal>;
}
