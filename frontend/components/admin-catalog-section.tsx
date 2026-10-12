'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { CellDoubleClickedEvent, CellValueChangedEvent, GridApi } from 'ag-grid-community';
import { Boxes, BrainCircuit, Check, ClipboardCheck, Combine, FileSpreadsheet, FileWarning, History, Layers3, Library, LoaderCircle, PencilLine, Plus, Repeat2, Search, Sparkles, Tags, Ungroup, Wand2, Warehouse } from 'lucide-react';
import { CatalogGroupingCandidate, CatalogImportIssuePage, ManagedAlternate, ManagedPart, ManagedStockItem, Page, request } from '@/lib/types';
import { useGridEdits } from '@/lib/use-grid-edits';
import ServerGrid from './server-grid';
import { alternateColumns, alternateOrdering, CatalogGridActions, catalogColumns, catalogContextMenu, catalogFieldColumns, catalogOrdering, parseSubgroup, pastedCodes,
  stockColumns, stockOrdering } from './admin-catalog-grids';
import { AlternateEditor, EditorTab, MatchEditor, PartEditor, RemoveAlternate } from './admin-catalog-editors';
import { UppercaseInput } from './uppercase-field';
import CatalogImport from './admin-catalog-import';
import ImportIssues from './admin-import-issues';
import CatalogGrouping from './admin-catalog-grouping';
import CatalogSplit from './admin-catalog-split';
import CatalogMergeWith from './admin-catalog-merge';
import CatalogMarkOEM, { OEMMarkMode } from './admin-catalog-oem-mark';
import InventoryAssistant from './admin-inventory-assistant';
import CatalogImages from './admin-catalog-images';
import SmartMatching from './admin-smart-matching';
import CatalogSuffixes from './admin-catalog-suffixes';
import OEMLibrary from './admin-oem-library';
import OEMRuns from './admin-oem-runs';
import AlternateReview from './admin-alternate-review';
import AdminNewProducts from './admin-new-products';
import AdminSupplierLedger from './admin-supplier-ledger';
import type { AlternateReviewList } from '@/lib/oem-reference-types';

type Kind = 'catalog' | 'inventory' | 'alternates';
const errorText = (error: unknown) => error instanceof Error ? error.message : 'No se pudo guardar el cambio. Inténtalo de nuevo.';

export default function AdminCatalogSection({ section }: { section: 'inventory' | 'alternates' }) {
  const [matchingOpen, setMatchingOpen] = useState(false);
  const [suffixesOpen, setSuffixesOpen] = useState(false);
  const [suffixSearch, setSuffixSearch] = useState('');
  const [oemLibraryOpen, setOemLibraryOpen] = useState(false);
  const [groupingFamily, setGroupingFamily] = useState<CatalogGroupingCandidate | null>(null);
  const [oemRunsOpen, setOemRunsOpen] = useState(false);
  const [imagePart, setImagePart] = useState<ManagedPart | null>(null);
  const [stockView, setStockView] = useState(false);
  // Productos nuevos: SKU proposed from supplier items that match nothing (?vista=nuevos deep-links it).
  const [newView, setNewView] = useState(false);
  const [newCount, setNewCount] = useState<number | null>(null);
  // Libro de movimientos: one supplier's stock movements and loads (?vista=movimientos&proveedor=<id> deep-links it).
  const [ledgerView, setLedgerView] = useState(false);
  // Alternos: the library, or the catalog copies no OEM number of their SKU carries (?vista=revisar deep-links it).
  const [reviewView, setReviewView] = useState(false);
  const [reviewCount, setReviewCount] = useState<number | null>(null);
  const kind: Kind = section === 'alternates' ? 'alternates' : stockView ? 'inventory' : 'catalog';
  // The SKU editor's side panel (null part: Crear SKU) and the tab it opens on: Ficha opens its Ficha técnica.
  const [partEditor, setPartEditor] = useState<{ part: ManagedPart | null; tab: EditorTab } | undefined>();
  const editPart = useCallback((part: ManagedPart) => setPartEditor({ part, tab: 'general' }), []);
  const editTechnical = useCallback((part: ManagedPart) => setPartEditor({ part, tab: 'specs' }), []);
  const [alternateEditor, setAlternateEditor] = useState<ManagedAlternate | null | undefined>();
  const [matchEditor, setMatchEditor] = useState<ManagedStockItem | null>(null);
  const [removeAlternate, setRemoveAlternate] = useState<ManagedAlternate | null>(null);
  const [importOpen, setImportOpen] = useState(false);
  const [issuesOpen, setIssuesOpen] = useState(false);
  const [groupingOpen, setGroupingOpen] = useState(false);
  // Separar SKU: null opens its list, a SKU opens that SKU's split.
  const [splitTarget, setSplitTarget] = useState<{ id: string; sku: string } | null | undefined>();
  const [mergeParts, setMergeParts] = useState<ManagedPart[] | null>(null);  // Agrupar con…, from the grid's right click
  const [oemMark, setOemMark] = useState<{ mode: OEMMarkMode; parts: ManagedPart[] } | null>(null);  // Marcar como OEM / Crear OEM desde SKU / Quitar marca OEM
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
  useEffect(() => {
    if (section !== 'alternates') return;
    if (new URL(window.location.href).searchParams.get('vista') === 'revisar') setReviewView(true);
    let cancelled = false;
    request<AlternateReviewList>('/api/management/alternate-review').then(result => { if (!cancelled) setReviewCount(result.count); }).catch(() => { if (!cancelled) setReviewCount(null); });
    return () => { cancelled = true; };
  }, [section]);
  function showReview(next: boolean) {
    setReviewView(next); setNotice('');
    const url = new URL(window.location.href);
    if (next) url.searchParams.set('vista', 'revisar'); else url.searchParams.delete('vista');
    window.history.replaceState(window.history.state, '', url);
  }
  const reviewChanged = useCallback(() => setRevision(value => value + 1), []);
  function saved(text: string) { setRevision(value => value + 1); setNotice(text); }
  return <section className="inventory-panel admin-panel" aria-label={section === 'inventory' ? 'Inventario' : 'Alternos'}>
    <div className="admin-toolbar inventory-header"><div><h2>{section === 'inventory' ? 'Inventario' : 'Alternos'}</h2><p>{section === 'inventory' ? 'SKU internos que agrupan repuestos idénticos y existencias por proveedor.' : 'Biblioteca de referencias OEM y de fabricantes equivalentes a cada SKU maestro.'}</p></div>{kind !== 'inventory' && !(kind === 'alternates' && reviewView) && !newView && !ledgerView && <div className="admin-toolbar-actions">{kind === 'catalog' && <button className="button soft" onClick={() => setImportOpen(true)}><FileSpreadsheet size={17}/>Importar Excel</button>}<button className="button primary" onClick={() => { if (kind === 'alternates') setAlternateEditor(null); else setPartEditor({ part: null, tab: 'general' }); }}><Plus size={17}/>{kind === 'alternates' ? 'Crear alterno' : 'Crear SKU'}</button></div>}</div>
    {section === 'inventory' && <>
      <div className="inventory-view-tabs" role="group" aria-label="Vistas de inventario"><button className={!stockView && !newView && !ledgerView ? 'selected' : ''} aria-pressed={!stockView && !newView && !ledgerView} onClick={() => showInventory('catalog')}><Boxes size={15} aria-hidden="true"/>Inventario interno</button><button className={stockView ? 'selected' : ''} aria-pressed={stockView} onClick={() => showInventory('stock')}><Warehouse size={15} aria-hidden="true"/>Existencias por proveedor</button><button className={newView ? 'selected' : ''} aria-pressed={newView} onClick={() => showInventory('new')}><Sparkles size={15} aria-hidden="true"/>Productos nuevos{!!newCount && <span className="import-issues-count" aria-label={`${newCount} productos nuevos por revisar`}>{newCount}</span>}</button><button className={ledgerView ? 'selected' : ''} aria-pressed={ledgerView} onClick={() => showInventory('ledger')}><History size={15} aria-hidden="true"/>Libro de movimientos</button></div>
      <div className="inventory-tools" role="group" aria-label="Herramientas del inventario">
        <div className="inventory-tool-group"><span>Calidad del catálogo</span><div>{!stockView && <button className="inventory-tool" onClick={() => setGroupingOpen(true)}><Layers3 size={15}/>Agrupar SKU</button>}{!stockView && <button className="inventory-tool" onClick={() => setSplitTarget(null)}><Ungroup size={15}/>Separar SKU</button>}<button className="inventory-tool" onClick={()=>setMatchingOpen(true)}><Combine size={15}/>Agrupación inteligente</button>{!stockView && <button className={`inventory-tool${pendingIssueCount ? ' attention' : ''}`} aria-label="Errores de importación" onClick={() => setIssuesOpen(true)}><FileWarning size={15}/>Errores de importación{!!pendingIssueCount && <span className="import-issues-count" aria-hidden="true">{pendingIssueCount}</span>}</button>}</div></div>
        <div className="inventory-tool-group"><span>OEM</span><div><button className="inventory-tool" onClick={()=>setOemLibraryOpen(true)}><Library size={15}/>Biblioteca OEM</button><button className="inventory-tool" onClick={()=>setOemRunsOpen(true)}><Wand2 size={15}/>Aplicación OEM</button><button className="inventory-tool" onClick={()=>setSuffixesOpen(true)}><Tags size={15}/>Sufijos</button></div></div>
        {!stockView && <div className="inventory-tool-group"><span>Asistencia</span><div><button type="button" className="inventory-tool" onClick={()=>setAssistantOpen(true)}><BrainCircuit size={15}/>Asistente IA</button></div></div>}
      </div>
    </>}
    {section === 'alternates' && <div className="inventory-view-tabs" role="group" aria-label="Vistas de alternos">
      <button className={!reviewView ? 'selected' : ''} aria-pressed={!reviewView} onClick={() => showReview(false)}><Repeat2 size={15} aria-hidden="true"/>Biblioteca de alternos</button>
      <button className={reviewView ? 'selected' : ''} aria-pressed={reviewView} onClick={() => showReview(true)}><ClipboardCheck size={15} aria-hidden="true"/>Por revisar{!!reviewCount && <span className="import-issues-count" aria-label={`${reviewCount} SKU por revisar`}>{reviewCount}</span>}</button>
    </div>}
    {notice && <div className="notice success admin-notice" role="status"><Check size={16}/>{notice}</div>}
    {section === 'inventory' && ledgerView ? <AdminSupplierLedger/>
      : section === 'inventory' && newView ? <AdminNewProducts onCount={setNewCount} onChanged={reviewChanged}/>
      : kind === 'alternates' && reviewView ? <AlternateReview onCount={setReviewCount} onChanged={reviewChanged}/>
      : <Collection onTechnical={editTechnical} onImages={setImagePart} key={kind} kind={kind} revision={revision} onEditPart={editPart} onEditAlternate={setAlternateEditor} onEditMatch={setMatchEditor} onRemoveAlternate={setRemoveAlternate} onSplit={setSplitTarget} onMerge={setMergeParts} onMarkOEM={(parts, mode) => setOemMark({ parts, mode })}/>}
    <p className="admin-footnote">{ledgerView ? 'El libro muestra unidades, nunca precios: cada movimiento de existencias del proveedor (cotizaciones confirmadas y cargas) y lo que cada carga Excel, sincronización con apiag-cloud o actualización manual cambió en sus artículos.' : newView ? 'Cada propuesta reúne los artículos de proveedores con el mismo código y marca que no coinciden con ningún SKU. Nada llega al catálogo hasta que lo apruebes; un SKU aprobado se vincula con sus artículos y los de otros proveedores con el mismo código se vinculan solos en el siguiente análisis.' : kind === 'alternates' && reviewView ? 'Conservar marca el código como confirmado por ti y la limpieza de copias nunca lo retira. Retirar lo quita del SKU. Los clientes siguen viendo los códigos que llegan por número OEM.' : kind === 'alternates' ? 'Los alternos de un SKU también aparecen en su editor de inventario interno. Cada artículo del proveedor mantiene su propio inventario e historial.' : kind === 'catalog' ? 'El SKU maestro no tiene marca. Sus referencias identifican piezas equivalentes; los artículos del proveedor conservan sus propios códigos y existencias.' : 'Los registros conservan el ID de inventario de cada proveedor y su vínculo con el SKU interno.'}</p>
    {matchingOpen && <SmartMatching onClose={()=>setMatchingOpen(false)} onSaved={()=>saved('Coincidencias actualizadas.')} onOpenSuffixes={token => { setSuffixSearch(token); setSuffixesOpen(true); }} onOpenGrouping={family => { setGroupingFamily(family); setGroupingOpen(true); }}/>}
    {suffixesOpen && <CatalogSuffixes initialSearch={suffixSearch} onClose={()=>{ setSuffixesOpen(false); setSuffixSearch(''); setRevision(value => value + 1); }}/>}
    {oemLibraryOpen && <OEMLibrary onClose={()=>setOemLibraryOpen(false)}/>}
    {oemRunsOpen && <OEMRuns onClose={()=>setOemRunsOpen(false)} onChanged={()=>setRevision(value => value + 1)}/>}
    {imagePart && <CatalogImages part={imagePart} onClose={() => setImagePart(null)} onChanged={() => setRevision(value => value + 1)}/>}
    {partEditor !== undefined && <PartEditor key={partEditor.part?.id ?? 'new'} part={partEditor.part} initialTab={partEditor.tab} onClose={() => setPartEditor(undefined)}
      onSaved={() => { setPartEditor(undefined); saved('SKU y alternos guardados.'); }}/>}
    {importOpen && <CatalogImport onClose={() => { setImportOpen(false); setRevision(value => value + 1); }} onSaved={summary => { setImportOpen(false); saved(`Importación completada: ${summary.created_skus} SKU nuevos, ${summary.updated_skus} SKU actualizados y ${summary.added_codes} alternos nuevos.${summary.rejected_rows ? ` ${summary.rejected_rows} ${summary.rejected_rows === 1 ? 'fila pendiente' : 'filas pendientes'} de corregir en «Errores de importación».` : summary.skipped_rows ? ` ${summary.skipped_rows} ${summary.skipped_rows === 1 ? 'fila excluida' : 'filas excluidas'}.` : ''}`); }}/>} 
    {issuesOpen && <ImportIssues onClose={() => { setIssuesOpen(false); setRevision(value => value + 1); }} onSaved={() => saved('Fila corregida y guardada en el inventario interno.')}/>}
    {splitTarget !== undefined && <CatalogSplit part={splitTarget} onClose={() => setSplitTarget(undefined)} onChanged={saved}/>}
    {oemMark && <CatalogMarkOEM key={`${oemMark.mode}:${oemMark.parts.map(part => part.id).join()}`} mode={oemMark.mode} parts={oemMark.parts} onClose={() => setOemMark(null)}
      onSwitch={(parts, mode) => setOemMark({ parts, mode })}
      onSaved={text => { setOemMark(null); saved(text); }}/>}
    {mergeParts && <CatalogMergeWith key={mergeParts.map(part => part.id).join()} parts={mergeParts} onClose={() => setMergeParts(null)} onSaved={text => { setMergeParts(null); saved(text); }}/>}
    {groupingOpen && <CatalogGrouping initialFamily={groupingFamily} onClose={() => { setGroupingOpen(false); setGroupingFamily(null); }} onSaved={(targetSku, count) => saved(`${count} ${count === 1 ? 'SKU agrupado' : 'SKU agrupados'} bajo ${targetSku}.`)}/>}
    {assistantOpen && <InventoryAssistant onClose={()=>setAssistantOpen(false)} onSaved={()=>saved('Clasificación de inventario actualizada desde el asistente IA.')}/>}
    {alternateEditor !== undefined && <AlternateEditor alternate={alternateEditor} onClose={() => setAlternateEditor(undefined)} onSaved={() => { setAlternateEditor(undefined); saved('Alterno guardado.'); }}/>}
    {matchEditor && <MatchEditor item={matchEditor} onClose={() => setMatchEditor(null)} onSaved={() => { setMatchEditor(null); saved('Coincidencia actualizada.'); }}/>}
    {removeAlternate && <RemoveAlternate alternate={removeAlternate} onClose={() => setRemoveAlternate(null)} onSaved={() => { setRemoveAlternate(null); saved('Código alterno retirado.'); }}/>}
  </section>;
}

function Collection({ onTechnical, onImages, kind, revision, onEditPart, onEditAlternate, onEditMatch, onRemoveAlternate, onSplit, onMerge, onMarkOEM }: { onTechnical: (part: ManagedPart) => void; onImages: (part: ManagedPart) => void; kind: Kind; revision: number; onEditPart: (part: ManagedPart) => void; onEditAlternate: (alternate: ManagedAlternate) => void; onEditMatch: (item: ManagedStockItem) => void; onRemoveAlternate: (alternate: ManagedAlternate) => void; onSplit: (part: { id: string; sku: string }) => void; onMerge: (parts: ManagedPart[]) => void; onMarkOEM: (parts: ManagedPart[], mode: OEMMarkMode) => void }) {
  const [oemFilter, setOemFilter] = useState('');
  const [search, setSearch] = useState(''); const [query, setQuery] = useState('');
  const [count, setCount] = useState<number | null>(null);
  // Columns pin to the sides only where there is room for them next to the scrolling middle.
  const [wide] = useState(() => typeof window === 'undefined' || window.innerWidth >= 900);
  // Editar celdas, as EssaMobileApp's inventory grid (lib/use-grid-edits): edits and pastes save together, the saved cells flash, a
  // refused batch puts every cell back, and the rows on screen are re-read so changes made elsewhere arrive (and flash) too.
  const [editing, setEditing] = useState(false);
  const [subgroups, setSubgroups] = useState<string[] | null>(null);
  const grid = useRef<GridApi<ManagedPart> | null>(null);
  const reread = useCallback((ids: string[]) => request<Page<ManagedPart>>(`/api/management/catalog?ids=${ids.join(',')}`).then(page => page.results), []);
  const save = useCallback((rows: Record<string, unknown>[]) => request<{ updated: ManagedPart[] }>('/api/management/catalog/bulk-edit', { method: 'POST',
    body: JSON.stringify({ rows }) }).then(result => result.updated), []);
  const { status: cellStatus, setStatus: setCellStatus, isSaving, refreshRows, queue, flush } = useGridEdits<ManagedPart>({ grid, fieldColumns: catalogFieldColumns,
    reread, save, cueColumns: ['description', 'subgroup', 'active', 'codes'], poll: kind === 'catalog', noun: { one: 'SKU', many: 'SKU' } });
  useEffect(() => { const timer = setTimeout(() => setQuery(search.trim()), 300); return () => clearTimeout(timer); }, [search]);
  useEffect(() => {
    if (!editing || subgroups) return;
    let live = true;
    request<{ results: { category: string; subcategory: string }[] }>('/api/management/catalog/taxonomy')
      .then(result => { if (live) setSubgroups(result.results.map(row => `${row.category} / ${row.subcategory}`)); })
      .catch(error => { if (live) { setEditing(false); setCellStatus({ kind: 'error', text: errorText(error) }); } });
    return () => { live = false; };
  }, [editing, subgroups, setCellStatus]);
  const onCodesChanged = useCallback((partId: string) => { refreshRows([partId]).catch(error => setCellStatus({ kind: 'error', text: errorText(error) })); }, [refreshRows, setCellStatus]);
  const actions = useMemo<CatalogGridActions>(() => ({ onEditPart, onTechnical, onImages, onEditAlternate, onRemoveAlternate, onEditMatch, onCodesChanged,
    onSplit: (part: ManagedPart) => onSplit({ id: part.id, sku: part.sku }), onMerge, onMarkOEM }),
    [onEditPart, onTechnical, onImages, onEditAlternate, onRemoveAlternate, onEditMatch, onCodesChanged, onSplit, onMerge, onMarkOEM]);
  const params = useMemo(() => ({ search: query, is_OEM: kind === 'catalog' ? oemFilter : '' }), [query, oemFilter, kind]);
  const edit = useMemo(() => editing && subgroups ? { subgroups, isSaving } : null, [editing, subgroups, isSaving]);
  const partColumns = useMemo(() => catalogColumns(wide, edit), [wide, edit]);
  const pasteCodes = useCallback(async (part: ManagedPart, codes: string[]) => {
    setCellStatus({ kind: 'saving', text: `Agregando ${codes.length} ${codes.length === 1 ? 'alterno' : 'alternos'} a ${part.sku}…` });
    const failed: string[] = [];
    for (const code of codes) {
      try { await request('/api/management/alternates', { method: 'POST', body: JSON.stringify({ part: part.id, code, brand: '', ref_type: 'unknown' }) }); }
      catch (error) { failed.push(`${code}: ${errorText(error)}`); }
    }
    await refreshRows([part.id]).catch(() => 0);
    const added = codes.length - failed.length;
    setCellStatus(failed.length ? { kind: 'error', text: `${part.sku}: ${added} ${added === 1 ? 'alterno agregado' : 'alternos agregados'}; no se agregaron ${failed.join(' · ')}` }
      : { kind: 'saved', text: `${part.sku}: ${added} ${added === 1 ? 'alterno agregado' : 'alternos agregados'}.` });
  }, [refreshRows, setCellStatus]);
  const onCellValueChanged = useCallback((event: CellValueChangedEvent<ManagedPart>) => {
    if (!['edit', 'paste', 'undo', 'redo'].includes(String(event.source))) return;  // grid patches never save back
    const part = event.data, column = event.column.getColId();
    if (!part) return;
    if (column === 'codes') {  // a paste into Alternos: each new code becomes an alterno right away
      const codes = pastedCodes.get(part) ?? [];
      pastedCodes.delete(part);
      if (codes.length) void pasteCodes(part, codes);
      return;
    }
    if (column === 'description') queue(part.id, { description: part.description }, { description: event.oldValue ?? '' });
    else if (column === 'subgroup') queue(part.id, { category: part.category, subcategory: part.subcategory }, parseSubgroup(event.oldValue));
    else if (column === 'active') queue(part.id, { active: part.active }, { active: event.oldValue });
  }, [queue, pasteCodes]);
  const catalogGridProps = useMemo(() => ({ onCellValueChanged, stopEditingWhenCellsLoseFocus: true, undoRedoCellEditing: true, undoRedoCellEditingLimit: 20,
    getContextMenuItems: catalogContextMenu,
    onCellDoubleClicked: (event: CellDoubleClickedEvent<ManagedPart>) => { if (event.column.getColId() === 'sku' && event.data) onEditPart(event.data); } }),
    [onCellValueChanged, onEditPart]);
  function toggleEditing() {
    grid.current?.stopEditing();
    void flush();
    setEditing(value => !value); setCellStatus(null);
  }
  const codeColumns = useMemo(() => alternateColumns(wide), [wide]);
  const label = kind === 'catalog' ? 'Buscar repuestos' : kind === 'inventory' ? 'Buscar existencias' : 'Buscar alternos';
  const filtered = !!(query || oemFilter);
  const empty = <div className="empty-state">{kind === 'alternates' ? <Repeat2 size={30}/> : <Boxes size={30}/>}<h3>{filtered ? 'No encontramos resultados.' : kind === 'alternates' ? 'Todavía no hay alternos registrados.' : kind === 'catalog' ? 'Todavía no hay SKU en el inventario interno.' : 'Todavía no hay existencias reportadas.'}</h3><p>{filtered ? 'Prueba con otra búsqueda o filtro.' : kind === 'alternates' ? 'Selecciona Crear alterno para agregar un código a un SKU interno.' : kind === 'catalog' ? 'Selecciona Crear SKU para agregarlo junto con sus alternos.' : 'Los registros aparecerán cuando los proveedores publiquen su inventario.'}</p></div>;
  return <>
    <div className="inventory-filters">
      <div className="catalog-search admin-search"><Search size={19}/><label className="sr-only" htmlFor="catalog-admin-search">{label}</label><UppercaseInput id="catalog-admin-search" value={search} onChange={event => setSearch(event.target.value)} placeholder={kind === 'catalog' ? 'SKU, nombre o código alterno…' : kind === 'inventory' ? 'Proveedor, marca o código del repuesto…' : 'SKU interno, código alterno o marca…'}/></div>
      {kind === 'catalog' && <label className="inventory-filter-select"><span>Tipo de SKU</span><select aria-label="Tipo de SKU" value={oemFilter} onChange={event => setOemFilter(event.target.value)}><option value="">TODOS</option><option value="true">OEM</option><option value="false">SIN MARCAR COMO OEM</option></select></label>}
      {kind === 'catalog' && <button type="button" className={`inventory-edit-toggle${editing ? ' selected' : ''}`} aria-pressed={editing} onClick={toggleEditing}><PencilLine size={15} aria-hidden="true"/>{editing ? 'Editando celdas' : 'Editar celdas'}</button>}
      <span className="inventory-count" aria-live="polite">{count === null ? <><LoaderCircle className="spin" size={13}/>Cargando…</> : `${count.toLocaleString('es-PA')} ${count === 1 ? 'resultado' : 'resultados'}`}</span>
    </div>
    {kind === 'catalog' && editing && <p className="inventory-edit-hint">{subgroups ? 'Escribe sobre una celda punteada o haz doble clic; también puedes pegar desde Excel. Los cambios se guardan juntos al soltar y las celdas que cambian destellan. Esc cancela, Ctrl+Z deshace. Doble clic en Alternos para agregar o retirar códigos; el código del SKU se edita con Editar.' : 'Cargando grupos y subgrupos…'}
      {cellStatus && cellStatus.kind !== 'error' && <span className={`inventory-edit-status ${cellStatus.kind}`} role="status">{cellStatus.text}</span>}</p>}
    {cellStatus?.kind === 'error' && <div className="notice error" role="alert">{cellStatus.text}<button type="button" onClick={() => setCellStatus(null)}>Cerrar</button></div>}
    {kind === 'catalog' ? <ServerGrid<ManagedPart> storageKey="admin-catalog" label="Inventario interno" path="/api/management/catalog" params={params} columns={partColumns} rowId={part => part.id} ordering={catalogOrdering} revision={revision} rowHeight={64} context={actions} empty={empty} onCount={setCount}
      gridProps={catalogGridProps} onReady={api => { grid.current = api; }}/>
      : kind === 'inventory' ? <ServerGrid<ManagedStockItem> storageKey="admin-stock" label="Existencias por proveedor" path="/api/management/inventory" params={params} columns={stockColumns} rowId={item => String(item.id)} ordering={stockOrdering} revision={revision} rowHeight={58} context={actions} empty={empty} onCount={setCount}/>
      : <ServerGrid<ManagedAlternate> storageKey="admin-alternates" label="Alternos" path="/api/management/alternates" params={params} columns={codeColumns} rowId={alternate => String(alternate.id)} ordering={alternateOrdering} revision={revision} rowHeight={58} context={actions} empty={empty} onCount={setCount}/>}
  </>;
}
