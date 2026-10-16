'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import Link from 'next/link';
import type { CellDoubleClickedEvent, CellValueChangedEvent, GridApi } from 'ag-grid-community';
import { Boxes, BrainCircuit, Check, Combine, FileSpreadsheet, FileWarning, History, Layers3, LoaderCircle, PencilLine, Plus, Repeat2, Search, Sparkles, Tags, Ungroup, Warehouse } from 'lucide-react';
import { CatalogImportIssuePage, ManagedAlternate, ManagedPart, ManagedStockItem, Page, request } from '@/lib/types';
import { asManagedPart, LINE_TABS, lineTipoLabels } from '@/lib/line-types';
import type { LineTipo, OEMLine } from '@/lib/line-types';
import { PartialSaveError, useGridEdits } from '@/lib/use-grid-edits';
import { subgrupoOptions, useGrupos } from '@/lib/use-grupos';
import ServerGrid from './server-grid';
import { alternateColumns, alternateOrdering, CatalogGridActions, catalogColumns, catalogContextMenu, catalogFieldColumns, catalogOrdering, pastedCodes,
  stockColumns, stockOrdering } from './admin-catalog-grids';
import { AlternateEditor, MatchEditor, RemoveAlternate } from './admin-catalog-editors';
import { EditorTab, LineEditor } from './admin-line-editor';
import { MainReferenceDialog } from './admin-references-editor';
import { UppercaseInput } from './uppercase-field';
import CatalogImport from './admin-catalog-import';
import ImportIssues from './admin-import-issues';
import CatalogSplit from './admin-catalog-split';
import CatalogMergeWith from './admin-catalog-merge';
import InventoryAssistant from './admin-inventory-assistant';
import CatalogImages from './admin-catalog-images';
import SmartMatching from './admin-smart-matching';
import CatalogSuffixes from './admin-catalog-suffixes';
import AdminNewProducts from './admin-new-products';
import AdminSupplierLedger from './admin-supplier-ledger';

type Kind = 'catalog' | 'inventory' | 'alternates';
const errorText = (error: unknown) => error instanceof Error ? error.message : 'No se pudo guardar el cambio. Inténtalo de nuevo.';

export default function AdminCatalogSection({ section }: { section: 'inventory' | 'alternates' }) {
  const [matchingOpen, setMatchingOpen] = useState(false);
  const [suffixesOpen, setSuffixesOpen] = useState(false);
  const [imagePart, setImagePart] = useState<ManagedPart | null>(null);
  const [mainLine, setMainLine] = useState<OEMLine | null>(null);  // Marcar referencia principal, from the grid's right click
  const [stockView, setStockView] = useState(false);
  // Productos nuevos: SKU proposed from supplier items that match nothing (?vista=nuevos deep-links it).
  const [newView, setNewView] = useState(false);
  const [newCount, setNewCount] = useState<number | null>(null);
  // Libro de movimientos: one supplier's stock movements and loads (?vista=movimientos&proveedor=<id> deep-links it).
  const [ledgerView, setLedgerView] = useState(false);
  const kind: Kind = section === 'alternates' ? 'alternates' : stockView ? 'inventory' : 'catalog';
  // The OEM line's side panel (null line: Crear SKU) and the tab it opens on: Ficha opens its Ficha técnica.
  const [partEditor, setPartEditor] = useState<{ part: OEMLine | null; tab: EditorTab } | undefined>();
  const editPart = useCallback((part: OEMLine) => setPartEditor({ part, tab: 'general' }), []);
  const editTechnical = useCallback((part: OEMLine) => setPartEditor({ part, tab: 'specs' }), []);
  const showImages = useCallback((part: OEMLine) => setImagePart(asManagedPart(part)), []);
  const mergeLines = useCallback((lines: OEMLine[]) => setMergeParts(lines.map(asManagedPart)), []);
  const [alternateEditor, setAlternateEditor] = useState<ManagedAlternate | null | undefined>();
  const [matchEditor, setMatchEditor] = useState<ManagedStockItem | null>(null);
  const [removeAlternate, setRemoveAlternate] = useState<ManagedAlternate | null>(null);
  const [importOpen, setImportOpen] = useState(false);
  const [issuesOpen, setIssuesOpen] = useState(false);
  // Separar SKU: null opens its list, a SKU opens that SKU's split.
  const [splitTarget, setSplitTarget] = useState<{ id: string; sku: string } | null | undefined>();
  const [mergeParts, setMergeParts] = useState<ManagedPart[] | null>(null);  // Agrupar con…, from the grid's right click
  const [assistantOpen, setAssistantOpen] = useState(false);
  const [pendingIssueCount, setPendingIssueCount] = useState<number | null>(null);
  const [notice, setNotice] = useState(''); const [revision, setRevision] = useState(0);
  useEffect(() => {
    if (kind !== 'catalog') return;
    let cancelled = false;
    request<CatalogImportIssuePage>('/api/management/catalog/import/issues?status=pending&page=1').then(result => { if (!cancelled) setPendingIssueCount(result.pending_count); }).catch(() => { if (!cancelled) setPendingIssueCount(null); });
    return () => { cancelled = true; };
  }, [kind, revision]);
  useEffect(() => {
    if (section !== 'inventory') return;
    const view = new URL(window.location.href).searchParams.get('vista');
    if (view === 'nuevos') setNewView(true); else if (view === 'existencias') setStockView(true); else if (view === 'movimientos') setLedgerView(true);
    let cancelled = false;
    request<{ pending: number }>('/api/management/catalog/proposals/summary').then(result => { if (!cancelled) setNewCount(result.pending); }).catch(() => { if (!cancelled) setNewCount(null); });
    return () => { cancelled = true; };
  }, [section]);
  function showInventory(view: 'catalog' | 'stock' | 'new' | 'ledger') {
    setStockView(view === 'stock'); setNewView(view === 'new'); setLedgerView(view === 'ledger'); setNotice('');
    const url = new URL(window.location.href);
    if (view !== 'ledger') url.searchParams.delete('proveedor');
    if (view === 'catalog') url.searchParams.delete('vista'); else url.searchParams.set('vista', view === 'new' ? 'nuevos' : view === 'ledger' ? 'movimientos' : 'existencias');
    window.history.replaceState(window.history.state, '', url);
  }
  const reviewChanged = useCallback(() => setRevision(value => value + 1), []);
  function saved(text: string) { setRevision(value => value + 1); setNotice(text); }
  return <section className="inventory-panel admin-panel" aria-label={section === 'inventory' ? 'Inventario' : 'Alternos'}>
    <div className="admin-toolbar inventory-header"><div><h2>{section === 'inventory' ? 'Inventario' : 'Alternos'}</h2><p>{section === 'inventory' ? 'SKU internos que agrupan repuestos idénticos y existencias por proveedor.' : 'Referencias OEM y de fabricantes equivalentes a cada SKU maestro.'}</p></div>{kind !== 'inventory' && !newView && !ledgerView && <div className="admin-toolbar-actions">{kind === 'catalog' && <button className="button soft" onClick={() => setImportOpen(true)}><FileSpreadsheet size={17}/>Importar Excel</button>}<button className="button primary" onClick={() => { if (kind === 'alternates') setAlternateEditor(null); else setPartEditor({ part: null, tab: 'general' }); }}><Plus size={17}/>{kind === 'alternates' ? 'Crear alterno' : 'Crear SKU'}</button></div>}</div>
    {section === 'inventory' && <>
      <div className="inventory-view-tabs" role="group" aria-label="Vistas de inventario"><button className={!stockView && !newView && !ledgerView ? 'selected' : ''} aria-pressed={!stockView && !newView && !ledgerView} onClick={() => showInventory('catalog')}><Boxes size={15} aria-hidden="true"/>Inventario interno</button><button className={stockView ? 'selected' : ''} aria-pressed={stockView} onClick={() => showInventory('stock')}><Warehouse size={15} aria-hidden="true"/>Existencias por proveedor</button><button className={newView ? 'selected' : ''} aria-pressed={newView} onClick={() => showInventory('new')}><Sparkles size={15} aria-hidden="true"/>Productos nuevos{!!newCount && <span className="import-issues-count" aria-label={`${newCount} productos nuevos por revisar`}>{newCount}</span>}</button><button className={ledgerView ? 'selected' : ''} aria-pressed={ledgerView} onClick={() => showInventory('ledger')}><History size={15} aria-hidden="true"/>Libro de movimientos</button></div>
      <div className="inventory-tools" role="group" aria-label="Herramientas del inventario">
        <div className="inventory-tool-group"><span>Calidad del catálogo</span><div><Link className="inventory-tool" href="/administracion?seccion=casos"><Layers3 size={15}/>Casos de coincidencia</Link>{!stockView && <button className="inventory-tool" onClick={() => setSplitTarget(null)}><Ungroup size={15}/>Separar SKU</button>}<button className="inventory-tool" onClick={()=>setMatchingOpen(true)}><Combine size={15}/>Agrupación inteligente</button>{!stockView && <button className={`inventory-tool${pendingIssueCount ? ' attention' : ''}`} aria-label="Errores de importación" onClick={() => setIssuesOpen(true)}><FileWarning size={15}/>Errores de importación{!!pendingIssueCount && <span className="import-issues-count" aria-hidden="true">{pendingIssueCount}</span>}</button>}</div></div>
        <div className="inventory-tool-group"><span>Códigos</span><div><button className="inventory-tool" onClick={()=>setSuffixesOpen(true)}><Tags size={15}/>Sufijos</button></div></div>
        {!stockView && <div className="inventory-tool-group"><span>Asistencia</span><div><button type="button" className="inventory-tool" onClick={()=>setAssistantOpen(true)}><BrainCircuit size={15}/>Asistente IA</button></div></div>}
      </div>
    </>}
    {notice && <div className="notice success admin-notice" role="status"><Check size={16}/>{notice}</div>}
    {section === 'inventory' && ledgerView ? <AdminSupplierLedger/>
      : section === 'inventory' && newView ? <AdminNewProducts onCount={setNewCount} onChanged={reviewChanged}/>
      : <Collection onTechnical={editTechnical} onImages={showImages} key={kind} kind={kind} revision={revision} onEditPart={editPart} onEditAlternate={setAlternateEditor} onEditMatch={setMatchEditor} onRemoveAlternate={setRemoveAlternate} onSplit={setSplitTarget} onMerge={mergeLines} onMain={setMainLine}/>}
    <p className="admin-footnote">{ledgerView ? 'El libro muestra unidades, nunca precios: cada movimiento de existencias del proveedor (cotizaciones confirmadas y cargas) y lo que cada carga Excel, sincronización con apiag-cloud o actualización manual cambió en sus artículos.' : newView ? 'Cada propuesta reúne los artículos de proveedores con el mismo código y marca que no coinciden con ningún SKU. Nada llega al catálogo hasta que lo apruebes; un SKU aprobado se vincula con sus artículos y los de otros proveedores con el mismo código se vinculan solos en el siguiente análisis.' : kind === 'alternates' ? 'Los alternos de un SKU también aparecen en su editor de inventario interno. Cada artículo del proveedor mantiene su propio inventario e historial.' : kind === 'catalog' ? 'El SKU maestro no tiene marca. Sus referencias identifican piezas equivalentes; los artículos del proveedor conservan sus propios códigos y existencias.' : 'Los registros conservan el ID de inventario de cada proveedor y su vínculo con el SKU interno.'}</p>
    {matchingOpen && <SmartMatching onClose={()=>setMatchingOpen(false)} onSaved={()=>saved('Coincidencias actualizadas.')}/>}
    {suffixesOpen && <CatalogSuffixes onClose={()=>{ setSuffixesOpen(false); setRevision(value => value + 1); }}/>}
    {imagePart && <CatalogImages part={imagePart} onClose={() => setImagePart(null)} onChanged={() => setRevision(value => value + 1)}/>}
    {partEditor !== undefined && <LineEditor key={partEditor.part?.id ?? 'new'} line={partEditor.part} initialTab={partEditor.tab} onClose={() => setPartEditor(undefined)}
      onSaved={(_, text) => { setRevision(value => value + 1); if (text) setNotice(text); }}/>}
    {mainLine && <MainReferenceDialog line={mainLine} onClose={() => setMainLine(null)} onSaved={(_, text) => { setMainLine(null); saved(text); }}/>}
    {importOpen && <CatalogImport onClose={() => { setImportOpen(false); setRevision(value => value + 1); }} onSaved={summary => { setImportOpen(false); saved(`Importación completada: ${summary.created_skus} SKU nuevos (líneas OEM), ${summary.updated_skus} SKU actualizados y ${summary.added_codes} referencias nuevas.${summary.rejected_rows ? ` ${summary.rejected_rows} ${summary.rejected_rows === 1 ? 'fila pendiente' : 'filas pendientes'} de corregir en «Errores de importación».` : summary.skipped_rows ? ` ${summary.skipped_rows} ${summary.skipped_rows === 1 ? 'fila excluida' : 'filas excluidas'}.` : ''}`); }}/>} 
    {issuesOpen && <ImportIssues onClose={() => { setIssuesOpen(false); setRevision(value => value + 1); }} onSaved={() => saved('Fila corregida y guardada en el inventario interno.')}/>}
    {splitTarget !== undefined && <CatalogSplit part={splitTarget} onClose={() => setSplitTarget(undefined)} onChanged={saved}/>}
    {mergeParts && <CatalogMergeWith key={mergeParts.map(part => part.id).join()} parts={mergeParts} onClose={() => setMergeParts(null)} onSaved={text => { setMergeParts(null); saved(text); }}/>}
    {assistantOpen && <InventoryAssistant onClose={()=>setAssistantOpen(false)} onSaved={()=>saved('Clasificación de inventario actualizada desde el asistente IA.')}/>}
    {alternateEditor !== undefined && <AlternateEditor alternate={alternateEditor} onClose={() => setAlternateEditor(undefined)} onSaved={() => { setAlternateEditor(undefined); saved('Alterno guardado.'); }}/>}
    {matchEditor && <MatchEditor item={matchEditor} onClose={() => setMatchEditor(null)} onSaved={text => { setMatchEditor(null); saved(text || 'Coincidencia actualizada.'); }}/>}
    {removeAlternate && <RemoveAlternate alternate={removeAlternate} onClose={() => setRemoveAlternate(null)} onSaved={() => { setRemoveAlternate(null); saved('Código alterno retirado.'); }}/>}
  </section>;
}

const ALL_ESTADOS = 'referencia,preparacion,publicada,rechazada,retirada,unificada';
const errorMessage = (error: unknown) => error instanceof Error ? error.message : 'No se pudo guardar el cambio.';
type CollectionProps = { onTechnical: (line: OEMLine) => void; onImages: (line: OEMLine) => void; kind: Kind; revision: number; onEditPart: (line: OEMLine) => void;
  onEditAlternate: (alternate: ManagedAlternate) => void; onEditMatch: (item: ManagedStockItem) => void; onRemoveAlternate: (alternate: ManagedAlternate) => void;
  onSplit: (part: { id: string; sku: string }) => void; onMerge: (lines: OEMLine[]) => void; onMain: (line: OEMLine) => void };

function Collection({ onTechnical, onImages, kind, revision, onEditPart, onEditAlternate, onEditMatch, onRemoveAlternate, onSplit, onMerge, onMain }: CollectionProps) {
  const [tipoFilter, setTipoFilter] = useState('');
  // The OEM lines by estado (plan S8.2): the catalog (publicada and retirada) first; ?estado= deep-links a tab.
  const [estadoTab, setEstadoTab] = useState(LINE_TABS[0].key);
  const tab = LINE_TABS.find(item => item.key === estadoTab) ?? LINE_TABS[0];
  useEffect(() => {
    if (kind !== 'catalog') return;
    const wanted = new URL(window.location.href).searchParams.get('estado');
    if (wanted && LINE_TABS.some(item => item.key === wanted)) setEstadoTab(wanted);
  }, [kind]);
  function showTab(key: string) {
    grid.current?.stopEditing();
    setEstadoTab(key); setCellStatus(null);
    if (key === 'unificada') setEditing(false);
    const url = new URL(window.location.href);
    if (key === LINE_TABS[0].key) url.searchParams.delete('estado'); else url.searchParams.set('estado', key);
    window.history.replaceState(window.history.state, '', url);
  }
  const [search, setSearch] = useState(''); const [query, setQuery] = useState('');
  const [count, setCount] = useState<number | null>(null);
  // Columns pin to the sides only where there is room for them next to the scrolling middle.
  const [wide] = useState(() => typeof window === 'undefined' || window.innerWidth >= 900);
  // Editar celdas, as EssaMobileApp's inventory grid (lib/use-grid-edits): edits and pastes save together, the saved cells flash, a
  // refused row puts its cells back, and the rows on screen are re-read so changes made elsewhere arrive (and flash) too. Each row saves
  // with the version it was read at (management/lines/<id>, its estado through /transition): one changed meanwhile is refused (409).
  const [editing, setEditing] = useState(false);
  const { grupos, error: gruposError } = useGrupos(editing);
  const options = useMemo(() => grupos ? subgrupoOptions(grupos) : null, [grupos]);
  const grid = useRef<GridApi<OEMLine> | null>(null);
  const reread = useCallback((ids: string[]) => request<Page<OEMLine>>(`/api/management/lines?ids=${ids.join(',')}&estado=${ALL_ESTADOS}`).then(page => page.results), []);
  const save = useCallback(async (rows: (Record<string, unknown> & { id: string })[]) => {
    const saved: OEMLine[] = [], failed: string[] = [];
    for (const row of rows) {
      const current = grid.current?.getRowNode(row.id)?.data;
      let version = current?.version;
      const label = current?.sku ?? 'SKU';
      let line: OEMLine | null = null;
      try {
        const { id, estado, ...fields } = row;
        if (Object.keys(fields).length) {
          line = await request<OEMLine>(`/api/management/lines/${id}`, { method: 'PATCH', body: JSON.stringify({ ...fields, expected_version: version }) });
          version = line.version;
        }
        if (estado) line = await request<OEMLine>(`/api/management/lines/${id}/transition`, { method: 'POST', body: JSON.stringify({ estado, expected_version: version }) });
        if (line) saved.push(line);
      } catch (error) {
        if (line) saved.push(line);  // its other cells were saved before its estado was refused: the row keeps them, with the server's estado
        failed.push(`${label}: ${errorMessage(error)}`);
      }
    }
    if (failed.length) {
      const kept = saved.map(line => line.sku);
      throw new PartialSaveError<OEMLine>(`${kept.length ? `${kept.length === 1 ? 'Se guardó' : 'Se guardaron'} ${kept.join(', ')}; no se guardó ` : 'No se guardó ningún cambio: '}${failed.join(' · ')}`, saved);
    }
    return saved;
  }, []);
  const { status: cellStatus, setStatus: setCellStatus, isSaving, refreshRows, queue, flush } = useGridEdits<OEMLine>({ grid, fieldColumns: catalogFieldColumns,
    reread, save, cueColumns: ['description', 'subgroup', 'estado', 'codes'], poll: kind === 'catalog', noun: { one: 'SKU', many: 'SKU' } });
  useEffect(() => { const timer = setTimeout(() => setQuery(search.trim()), 300); return () => clearTimeout(timer); }, [search]);
  useEffect(() => { if (editing && gruposError) { setEditing(false); setCellStatus({ kind: 'error', text: gruposError }); } }, [editing, gruposError, setCellStatus]);
  const onCodesChanged = useCallback((lineId: string) => { refreshRows([lineId]).catch(error => setCellStatus({ kind: 'error', text: errorText(error) })); }, [refreshRows, setCellStatus]);
  const actions = useMemo<CatalogGridActions>(() => ({ onEditPart, onTechnical, onImages, onEditAlternate, onRemoveAlternate, onEditMatch, onCodesChanged, onSplit, onMerge, onMain }),
    [onEditPart, onTechnical, onImages, onEditAlternate, onRemoveAlternate, onEditMatch, onCodesChanged, onSplit, onMerge, onMain]);
  const params = useMemo<Record<string, string>>(() => (kind === 'catalog' ? { search: query, estado: tab.estado, tipo: tipoFilter } : { search: query }) as Record<string, string>, [query, tipoFilter, kind, tab.estado]);
  const subgroupOf = useCallback((label: string) => { const option = options?.find(row => row.label === label); return option ? { grupo: option.grupo, subgrupo: { id: option.id, name: option.name } } : null; }, [options]);
  const edit = useMemo(() => editing && options ? { subgroups: options.map(option => option.label), subgroupOf, isSaving, estados: tab.key === 'catalogo' } : null,
    [editing, options, subgroupOf, isSaving, tab.key]);
  const partColumns = useMemo(() => catalogColumns(wide, edit), [wide, edit]);
  const pasteCodes = useCallback(async (line: OEMLine, codes: string[]) => {
    setCellStatus({ kind: 'saving', text: `Agregando ${codes.length} ${codes.length === 1 ? 'referencia' : 'referencias'} a ${line.sku}…` });
    const failed: string[] = [];
    for (const code of codes) {
      try { await request(`/api/management/lines/${line.id}/references`, { method: 'POST', body: JSON.stringify({ code, brand: '', tipo: 'otro' }) }); }
      catch (error) { failed.push(`${code}: ${errorText(error)}`); }
    }
    await refreshRows([line.id]).catch(() => 0);
    const added = codes.length - failed.length;
    setCellStatus(failed.length ? { kind: 'error', text: `${line.sku}: ${added} ${added === 1 ? 'referencia agregada' : 'referencias agregadas'}; no se agregaron ${failed.join(' · ')}` }
      : { kind: 'saved', text: `${line.sku}: ${added} ${added === 1 ? 'referencia agregada' : 'referencias agregadas'}.` });
  }, [refreshRows, setCellStatus]);
  const onCellValueChanged = useCallback((event: CellValueChangedEvent<OEMLine>) => {
    if (!['edit', 'paste', 'undo', 'redo'].includes(String(event.source))) return;  // grid patches never save back
    const line = event.data, column = event.column.getColId();
    if (!line) return;
    if (column === 'codes') {  // a paste into Referencias: each new code becomes a Reference of the line right away
      const codes = pastedCodes.get(line) ?? [];
      pastedCodes.delete(line);
      if (codes.length) void pasteCodes(line, codes);
      return;
    }
    if (column === 'description') queue(line.id, { description: line.description }, { description: event.oldValue ?? '' });
    else if (column === 'subgroup') {
      const before = subgroupOf(String(event.oldValue ?? ''));
      queue(line.id, { subgrupo: line.subgrupo?.id ?? null }, { grupo: before?.grupo ?? null, subgrupo: before?.subgrupo ?? null,
        category: before?.grupo.nombre ?? '', subcategory: before?.subgrupo.name ?? '' });
    } else if (column === 'estado') queue(line.id, { estado: line.estado }, { estado: event.oldValue });
  }, [queue, pasteCodes, subgroupOf]);
  const catalogGridProps = useMemo(() => ({ onCellValueChanged, stopEditingWhenCellsLoseFocus: true, undoRedoCellEditing: true, undoRedoCellEditingLimit: 20,
    getContextMenuItems: catalogContextMenu,
    onCellDoubleClicked: (event: CellDoubleClickedEvent<OEMLine>) => { if (event.column.getColId() === 'sku' && event.data) onEditPart(event.data); } }),
    [onCellValueChanged, onEditPart]);
  function toggleEditing() {
    grid.current?.stopEditing();
    void flush();
    setEditing(value => !value); setCellStatus(null);
  }
  const codeColumns = useMemo(() => alternateColumns(wide), [wide]);
  const label = kind === 'catalog' ? 'Buscar repuestos' : kind === 'inventory' ? 'Buscar existencias' : 'Buscar alternos';
  const filtered = !!(query || tipoFilter);
  const emptyCatalog = tab.key === 'catalogo' ? 'Todavía no hay SKU en el inventario interno.' : `No hay líneas OEM ${tab.key === 'referencia' ? 'en referencia' : tab.label.toLowerCase()}.`;
  const empty = <div className="empty-state">{kind === 'alternates' ? <Repeat2 size={30}/> : <Boxes size={30}/>}<h3>{filtered ? 'No encontramos resultados.' : kind === 'alternates' ? 'Todavía no hay alternos registrados.' : kind === 'catalog' ? emptyCatalog : 'Todavía no hay existencias reportadas.'}</h3><p>{filtered ? 'Prueba con otra búsqueda o filtro.' : kind === 'alternates' ? 'Selecciona Crear alterno para agregar un código a un SKU interno.' : kind === 'catalog' ? 'Selecciona Crear SKU para agregarlo junto con sus referencias.' : 'Los registros aparecerán cuando los proveedores publiquen su inventario.'}</p></div>;
  return <>
    {kind === 'catalog' && <div className="admin-inventory-tabs line-estado-tabs" role="group" aria-label="Estado de la línea OEM">{LINE_TABS.map(item =>
      <button key={item.key} type="button" className={item.key === tab.key ? 'selected' : ''} aria-pressed={item.key === tab.key} onClick={() => showTab(item.key)}>{item.label}</button>)}</div>}
    <div className="inventory-filters">
      <div className="catalog-search admin-search"><Search size={19}/><label className="sr-only" htmlFor="catalog-admin-search">{label}</label><UppercaseInput id="catalog-admin-search" value={search} onChange={event => setSearch(event.target.value)} placeholder={kind === 'catalog' ? 'SKU, nombre o código de referencia…' : kind === 'inventory' ? 'Proveedor, marca o código del repuesto…' : 'SKU interno, código alterno o marca…'}/></div>
      {kind === 'catalog' && <label className="inventory-filter-select"><span>Tipo</span><select aria-label="Tipo de línea OEM" value={tipoFilter} onChange={event => setTipoFilter(event.target.value)}><option value="">TODOS</option>{(Object.keys(lineTipoLabels) as LineTipo[]).map(value => <option key={value} value={value}>{lineTipoLabels[value]}</option>)}</select></label>}
      {kind === 'catalog' && tab.key !== 'unificada' && <button type="button" className={`inventory-edit-toggle${editing ? ' selected' : ''}`} aria-pressed={editing} onClick={toggleEditing}><PencilLine size={15} aria-hidden="true"/>{editing ? 'Editando celdas' : 'Editar celdas'}</button>}
      <span className="inventory-count" aria-live="polite">{count === null ? <><LoaderCircle className="spin" size={13}/>Cargando…</> : `${count.toLocaleString('es-PA')} ${count === 1 ? 'resultado' : 'resultados'}`}</span>
    </div>
    {kind === 'catalog' && editing && <p className="inventory-edit-hint">{options ? `Escribe sobre una celda punteada o haz doble clic; también puedes pegar desde Excel. Los cambios se guardan juntos al soltar, cada SKU con la versión que leíste, y las celdas que cambian destellan. Esc cancela, Ctrl+Z deshace. Doble clic en Referencias para agregar o retirar códigos; el SKU sigue a la referencia principal.${tab.key === 'catalogo' ? '' : ' El estado se cambia en el editor de la línea OEM.'}` : 'Cargando grupos y subgrupos…'}
      {cellStatus && cellStatus.kind !== 'error' && <span className={`inventory-edit-status ${cellStatus.kind}`} role="status">{cellStatus.text}</span>}</p>}
    {cellStatus?.kind === 'error' && <div className="notice error" role="alert">{cellStatus.text}<button type="button" onClick={() => setCellStatus(null)}>Cerrar</button></div>}
    {kind === 'catalog' ? <ServerGrid<OEMLine> storageKey="admin-lines" label="Inventario interno" path="/api/management/lines" params={params} columns={partColumns} rowId={line => line.id} ordering={catalogOrdering} revision={revision} rowHeight={64} context={actions} empty={empty} onCount={setCount}
      gridProps={catalogGridProps} onReady={api => { grid.current = api; }}/>
      : kind === 'inventory' ? <ServerGrid<ManagedStockItem> storageKey="admin-stock" label="Existencias por proveedor" path="/api/management/inventory" params={params} columns={stockColumns} rowId={item => String(item.id)} ordering={stockOrdering} revision={revision} rowHeight={58} context={actions} empty={empty} onCount={setCount}/>
      : <ServerGrid<ManagedAlternate> storageKey="admin-alternates" label="Alternos" path="/api/management/alternates" params={params} columns={codeColumns} rowId={alternate => String(alternate.id)} ordering={alternateOrdering} revision={revision} rowHeight={58} context={actions} empty={empty} onCount={setCount}/>}
  </>;
}
