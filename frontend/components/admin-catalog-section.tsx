'use client';

import { useEffect, useMemo, useState } from 'react';
import { Boxes, BrainCircuit, Check, Combine, FileSpreadsheet, FileWarning, Layers3, Library, LoaderCircle, Plus, Repeat2, Search, Tags, Wand2, Warehouse } from 'lucide-react';
import { CatalogGroupingCandidate, CatalogImportIssuePage, ManagedAlternate, ManagedPart, ManagedStockItem, request } from '@/lib/types';
import AdminServerGrid from './admin-server-grid';
import { alternateColumns, alternateOrdering, CatalogGridActions, catalogColumns, catalogOrdering, stockColumns, stockOrdering } from './admin-catalog-grids';
import { AlternateEditor, MatchEditor, PartEditor, RemoveAlternate } from './admin-catalog-editors';
import { UppercaseInput } from './uppercase-field';
import CatalogImport from './admin-catalog-import';
import ImportIssues from './admin-import-issues';
import CatalogGrouping from './admin-catalog-grouping';
import InventoryAssistant from './admin-inventory-assistant';
import CatalogImages from './admin-catalog-images';
import SmartMatching from './admin-smart-matching';
import CatalogSuffixes from './admin-catalog-suffixes';
import OEMLibrary from './admin-oem-library';
import OEMRuns from './admin-oem-runs';
import { PartTechnicalEditor } from './part-technical';

type Kind = 'catalog' | 'inventory' | 'alternates';

export default function AdminCatalogSection({ section }: { section: 'inventory' | 'alternates' }) {
  const [technicalPart, setTechnicalPart] = useState<ManagedPart | null>(null);
  const [matchingOpen, setMatchingOpen] = useState(false);
  const [suffixesOpen, setSuffixesOpen] = useState(false);
  const [suffixSearch, setSuffixSearch] = useState('');
  const [oemLibraryOpen, setOemLibraryOpen] = useState(false);
  const [groupingFamily, setGroupingFamily] = useState<CatalogGroupingCandidate | null>(null);
  const [oemRunsOpen, setOemRunsOpen] = useState(false);
  const [imagePart, setImagePart] = useState<ManagedPart | null>(null);
  const [stockView, setStockView] = useState(false);
  const kind: Kind = section === 'alternates' ? 'alternates' : stockView ? 'inventory' : 'catalog';
  const [partEditor, setPartEditor] = useState<ManagedPart | null | undefined>();
  const [alternateEditor, setAlternateEditor] = useState<ManagedAlternate | null | undefined>();
  const [matchEditor, setMatchEditor] = useState<ManagedStockItem | null>(null);
  const [removeAlternate, setRemoveAlternate] = useState<ManagedAlternate | null>(null);
  const [importOpen, setImportOpen] = useState(false);
  const [issuesOpen, setIssuesOpen] = useState(false);
  const [groupingOpen, setGroupingOpen] = useState(false);
  const [assistantOpen, setAssistantOpen] = useState(false);
  const [pendingIssueCount, setPendingIssueCount] = useState<number | null>(null);
  const [notice, setNotice] = useState(''); const [revision, setRevision] = useState(0);
  useEffect(() => {
    if (kind !== 'catalog') return;
    let cancelled = false;
    request<CatalogImportIssuePage>('/api/management/catalog/import/issues?status=pending&page=1').then(result => { if (!cancelled) setPendingIssueCount(result.pending_count); }).catch(() => { if (!cancelled) setPendingIssueCount(null); });
    return () => { cancelled = true; };
  }, [kind, revision]);
  function saved(text: string) { setRevision(value => value + 1); setNotice(text); }
  return <section className="inventory-panel admin-panel" aria-label={section === 'inventory' ? 'Inventario' : 'Alternos'}>
    <div className="admin-toolbar inventory-header"><div><h2>{section === 'inventory' ? 'Inventario' : 'Alternos'}</h2><p>{section === 'inventory' ? 'SKU internos que agrupan repuestos idénticos y existencias por proveedor.' : 'Biblioteca de referencias OEM y de fabricantes equivalentes a cada SKU maestro.'}</p></div>{kind !== 'inventory' && <div className="admin-toolbar-actions">{kind === 'catalog' && <button className="button soft" onClick={() => setImportOpen(true)}><FileSpreadsheet size={17}/>Importar Excel</button>}<button className="button primary" onClick={() => { if (kind === 'alternates') setAlternateEditor(null); else setPartEditor(null); }}><Plus size={17}/>{kind === 'alternates' ? 'Crear alterno' : 'Crear SKU'}</button></div>}</div>
    {section === 'inventory' && <>
      <div className="inventory-view-tabs" role="group" aria-label="Vistas de inventario"><button className={!stockView ? 'selected' : ''} aria-pressed={!stockView} onClick={() => setStockView(false)}><Boxes size={15} aria-hidden="true"/>Inventario interno</button><button className={stockView ? 'selected' : ''} aria-pressed={stockView} onClick={() => setStockView(true)}><Warehouse size={15} aria-hidden="true"/>Existencias por proveedor</button></div>
      <div className="inventory-tools" role="group" aria-label="Herramientas del inventario">
        <div className="inventory-tool-group"><span>Calidad del catálogo</span><div>{!stockView && <button className="inventory-tool" onClick={() => setGroupingOpen(true)}><Layers3 size={15}/>Agrupar SKU</button>}<button className="inventory-tool" onClick={()=>setMatchingOpen(true)}><Combine size={15}/>Agrupación inteligente</button>{!stockView && <button className={`inventory-tool${pendingIssueCount ? ' attention' : ''}`} aria-label="Errores de importación" onClick={() => setIssuesOpen(true)}><FileWarning size={15}/>Errores de importación{!!pendingIssueCount && <span className="import-issues-count" aria-hidden="true">{pendingIssueCount}</span>}</button>}</div></div>
        <div className="inventory-tool-group"><span>OEM</span><div><button className="inventory-tool" onClick={()=>setOemLibraryOpen(true)}><Library size={15}/>Biblioteca OEM</button><button className="inventory-tool" onClick={()=>setOemRunsOpen(true)}><Wand2 size={15}/>Aplicación OEM</button><button className="inventory-tool" onClick={()=>setSuffixesOpen(true)}><Tags size={15}/>Sufijos</button></div></div>
        {!stockView && <div className="inventory-tool-group"><span>Asistencia</span><div><button type="button" className="inventory-tool" onClick={()=>setAssistantOpen(true)}><BrainCircuit size={15}/>Asistente IA</button></div></div>}
      </div>
    </>}
    {section === 'alternates' && <div className="inventory-tools-spacer"/>}
    {notice && <div className="notice success admin-notice" role="status"><Check size={16}/>{notice}</div>}
    <Collection onTechnical={setTechnicalPart} onImages={setImagePart} key={kind} kind={kind} revision={revision} onEditPart={setPartEditor} onEditAlternate={setAlternateEditor} onEditMatch={setMatchEditor} onRemoveAlternate={setRemoveAlternate}/>
    <p className="admin-footnote">{kind === 'alternates' ? 'Los alternos de un SKU también aparecen en su editor de inventario interno. Cada artículo del proveedor mantiene su propio inventario e historial.' : kind === 'catalog' ? 'El SKU maestro no tiene marca. Sus referencias identifican piezas equivalentes; los artículos del proveedor conservan sus propios códigos y existencias.' : 'Los registros conservan el ID de inventario de cada proveedor y su vínculo con el SKU interno.'}</p>
    {matchingOpen && <SmartMatching onClose={()=>setMatchingOpen(false)} onSaved={()=>saved('Coincidencias actualizadas.')} onOpenSuffixes={token => { setSuffixSearch(token); setSuffixesOpen(true); }} onOpenGrouping={family => { setGroupingFamily(family); setGroupingOpen(true); }}/>}
    {suffixesOpen && <CatalogSuffixes initialSearch={suffixSearch} onClose={()=>{ setSuffixesOpen(false); setSuffixSearch(''); setRevision(value => value + 1); }}/>}
    {oemLibraryOpen && <OEMLibrary onClose={()=>setOemLibraryOpen(false)}/>}
    {oemRunsOpen && <OEMRuns onClose={()=>setOemRunsOpen(false)} onChanged={()=>setRevision(value => value + 1)}/>}
    {technicalPart && <PartTechnicalEditor part={technicalPart} onClose={() => setTechnicalPart(null)} onSaved={() => { setTechnicalPart(null); saved('Ficha técnica y aplicaciones guardadas.'); }}/>}
    {imagePart && <CatalogImages part={imagePart} onClose={() => setImagePart(null)} onChanged={() => setRevision(value => value + 1)}/>}
    {partEditor !== undefined && <PartEditor part={partEditor} onClose={() => setPartEditor(undefined)} onSaved={() => { setPartEditor(undefined); saved('SKU y alternos guardados.'); }}/>}
    {importOpen && <CatalogImport onClose={() => { setImportOpen(false); setRevision(value => value + 1); }} onSaved={summary => { setImportOpen(false); saved(`Importación completada: ${summary.created_skus} SKU nuevos, ${summary.updated_skus} SKU actualizados y ${summary.added_codes} alternos nuevos.${summary.rejected_rows ? ` ${summary.rejected_rows} ${summary.rejected_rows === 1 ? 'fila pendiente' : 'filas pendientes'} de corregir en «Errores de importación».` : summary.skipped_rows ? ` ${summary.skipped_rows} ${summary.skipped_rows === 1 ? 'fila excluida' : 'filas excluidas'}.` : ''}`); }}/>} 
    {issuesOpen && <ImportIssues onClose={() => { setIssuesOpen(false); setRevision(value => value + 1); }} onSaved={() => saved('Fila corregida y guardada en el inventario interno.')}/>}
    {groupingOpen && <CatalogGrouping initialFamily={groupingFamily} onClose={() => { setGroupingOpen(false); setGroupingFamily(null); }} onSaved={(targetSku, count) => saved(`${count} ${count === 1 ? 'SKU agrupado' : 'SKU agrupados'} bajo ${targetSku}.`)}/>}
    {assistantOpen && <InventoryAssistant onClose={()=>setAssistantOpen(false)} onSaved={()=>saved('Clasificación de inventario actualizada desde el asistente IA.')}/>}
    {alternateEditor !== undefined && <AlternateEditor alternate={alternateEditor} onClose={() => setAlternateEditor(undefined)} onSaved={() => { setAlternateEditor(undefined); saved('Alterno guardado.'); }}/>}
    {matchEditor && <MatchEditor item={matchEditor} onClose={() => setMatchEditor(null)} onSaved={() => { setMatchEditor(null); saved('Coincidencia actualizada.'); }}/>}
    {removeAlternate && <RemoveAlternate alternate={removeAlternate} onClose={() => setRemoveAlternate(null)} onSaved={() => { setRemoveAlternate(null); saved('Código alterno retirado.'); }}/>}
  </section>;
}

function Collection({ onTechnical, onImages, kind, revision, onEditPart, onEditAlternate, onEditMatch, onRemoveAlternate }: { onTechnical: (part: ManagedPart) => void; onImages: (part: ManagedPart) => void; kind: Kind; revision: number; onEditPart: (part: ManagedPart) => void; onEditAlternate: (alternate: ManagedAlternate) => void; onEditMatch: (item: ManagedStockItem) => void; onRemoveAlternate: (alternate: ManagedAlternate) => void }) {
  const [oemFilter, setOemFilter] = useState('');
  const [search, setSearch] = useState(''); const [query, setQuery] = useState('');
  const [count, setCount] = useState<number | null>(null);
  // Columns pin to the sides only where there is room for them next to the scrolling middle.
  const [wide] = useState(() => typeof window === 'undefined' || window.innerWidth >= 900);
  useEffect(() => { const timer = setTimeout(() => setQuery(search.trim()), 300); return () => clearTimeout(timer); }, [search]);
  const actions = useMemo<CatalogGridActions>(() => ({ onEditPart, onTechnical, onImages, onEditAlternate, onRemoveAlternate, onEditMatch }), [onEditPart, onTechnical, onImages, onEditAlternate, onRemoveAlternate, onEditMatch]);
  const params = useMemo(() => ({ search: query, is_OEM: kind === 'catalog' ? oemFilter : '' }), [query, oemFilter, kind]);
  const partColumns = useMemo(() => catalogColumns(wide), [wide]);
  const codeColumns = useMemo(() => alternateColumns(wide), [wide]);
  const label = kind === 'catalog' ? 'Buscar repuestos' : kind === 'inventory' ? 'Buscar existencias' : 'Buscar alternos';
  const filtered = !!(query || oemFilter);
  const empty = <div className="empty-state">{kind === 'alternates' ? <Repeat2 size={30}/> : <Boxes size={30}/>}<h3>{filtered ? 'No encontramos resultados.' : kind === 'alternates' ? 'Todavía no hay alternos registrados.' : kind === 'catalog' ? 'Todavía no hay SKU en el inventario interno.' : 'Todavía no hay existencias reportadas.'}</h3><p>{filtered ? 'Prueba con otra búsqueda o filtro.' : kind === 'alternates' ? 'Selecciona Crear alterno para agregar un código a un SKU interno.' : kind === 'catalog' ? 'Selecciona Crear SKU para agregarlo junto con sus alternos.' : 'Los registros aparecerán cuando los proveedores publiquen su inventario.'}</p></div>;
  return <>
    <div className="inventory-filters">
      <div className="catalog-search admin-search"><Search size={19}/><label className="sr-only" htmlFor="catalog-admin-search">{label}</label><UppercaseInput id="catalog-admin-search" value={search} onChange={event => setSearch(event.target.value)} placeholder={kind === 'catalog' ? 'SKU, nombre o código alterno…' : kind === 'inventory' ? 'Proveedor, marca o código del repuesto…' : 'SKU interno, código alterno o marca…'}/></div>
      {kind === 'catalog' && <label className="inventory-filter-select"><span>Tipo de SKU</span><select aria-label="Tipo de SKU" value={oemFilter} onChange={event => setOemFilter(event.target.value)}><option value="">TODOS</option><option value="true">OEM</option><option value="false">SIN MARCAR COMO OEM</option></select></label>}
      <span className="inventory-count" aria-live="polite">{count === null ? <><LoaderCircle className="spin" size={13}/>Cargando…</> : `${count.toLocaleString('es-PA')} ${count === 1 ? 'resultado' : 'resultados'}`}</span>
    </div>
    {kind === 'catalog' ? <AdminServerGrid<ManagedPart> storageKey="admin-catalog" label="Inventario interno" path="/api/management/catalog" params={params} columns={partColumns} rowId={part => part.id} ordering={catalogOrdering} revision={revision} rowHeight={64} context={actions} empty={empty} onCount={setCount}/>
      : kind === 'inventory' ? <AdminServerGrid<ManagedStockItem> storageKey="admin-stock" label="Existencias por proveedor" path="/api/management/inventory" params={params} columns={stockColumns} rowId={item => String(item.id)} ordering={stockOrdering} revision={revision} rowHeight={58} context={actions} empty={empty} onCount={setCount}/>
      : <AdminServerGrid<ManagedAlternate> storageKey="admin-alternates" label="Alternos" path="/api/management/alternates" params={params} columns={codeColumns} rowId={alternate => String(alternate.id)} ordering={alternateOrdering} revision={revision} rowHeight={58} context={actions} empty={empty} onCount={setCount}/>}
  </>;
}
