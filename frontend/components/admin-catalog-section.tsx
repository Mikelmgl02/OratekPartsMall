'use client';

import { referenceLabel } from '@/lib/reference-label';

import { useEffect, useState } from 'react';
import { Boxes, BrainCircuit, Check, Combine, FileSpreadsheet, FileWarning, Images, Layers3, Library, LoaderCircle, Pencil, Plus, Repeat2, Search, Tags, Wand2, Warehouse, Wrench } from 'lucide-react';
import { CatalogGroupingCandidate, CatalogImportIssuePage, ManagedAlternate, ManagedPart, ManagedStockItem, Page, request } from '@/lib/types';
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
type Row = ManagedPart | ManagedStockItem | ManagedAlternate;
const ALTERNATES_SHOWN = 4;

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
  const [data, setData] = useState<Page<Row> | null>(null);
  const [oemFilter, setOemFilter] = useState('');
  const [search, setSearch] = useState(''); const [query, setQuery] = useState(''); const [page, setPage] = useState(1);
  const [error, setError] = useState(''); const [retry, setRetry] = useState(0);
  useEffect(() => { const timer = setTimeout(() => { setQuery(search.trim()); setPage(1); }, 300); return () => clearTimeout(timer); }, [search]);
  useEffect(() => {
    let cancelled = false; setData(null); setError('');
    request<Page<Row>>(`/api/management/${kind}?search=${encodeURIComponent(query)}&page=${page}${kind === 'catalog' && oemFilter ? `&is_OEM=${oemFilter}` : ''}`).then(result => { if (!cancelled) setData(result); }).catch(error => { if (!cancelled) setError(error instanceof Error ? error.message : 'No se pudo cargar la información.'); });
    return () => { cancelled = true; };
  }, [kind, query, page, revision, retry, oemFilter]);
  const label = kind === 'catalog' ? 'Buscar repuestos' : kind === 'inventory' ? 'Buscar existencias' : 'Buscar alternos';
  return <>
    <div className="inventory-filters">
      <div className="catalog-search admin-search"><Search size={19}/><label className="sr-only" htmlFor="catalog-admin-search">{label}</label><UppercaseInput id="catalog-admin-search" value={search} onChange={event => setSearch(event.target.value)} placeholder={kind === 'catalog' ? 'SKU, nombre o código alterno…' : kind === 'inventory' ? 'Proveedor, marca o código del repuesto…' : 'SKU interno, código alterno o marca…'}/></div>
      {kind === 'catalog' && <label className="inventory-filter-select"><span>Tipo de SKU</span><select aria-label="Tipo de SKU" value={oemFilter} onChange={event => { setOemFilter(event.target.value); setPage(1); }}><option value="">TODOS</option><option value="true">OEM</option><option value="false">SIN MARCAR COMO OEM</option></select></label>}
      {data && <span className="inventory-count">{data.count.toLocaleString('es-PA')} {data.count === 1 ? 'resultado' : 'resultados'}</span>}
    </div>
    {error && <div className="notice error" role="alert">{error}<button onClick={() => setRetry(value => value + 1)}>Intentar de nuevo</button></div>}
    {!data && !error && <div className="loading"><LoaderCircle className="spin" size={20}/>Cargando {kind === 'alternates' ? 'alternos' : 'inventario'}…</div>}
    {data && !data.results.length && <div className="empty-state">{kind === 'alternates' ? <Repeat2 size={30}/> : <Boxes size={30}/>}<h3>{query || oemFilter ? 'No encontramos resultados.' : kind === 'alternates' ? 'Todavía no hay alternos registrados.' : kind === 'catalog' ? 'Todavía no hay SKU en el inventario interno.' : 'Todavía no hay existencias reportadas.'}</h3><p>{query || oemFilter ? 'Prueba con otra búsqueda o filtro.' : kind === 'alternates' ? 'Selecciona Crear alterno para agregar un código a un SKU interno.' : kind === 'catalog' ? 'Selecciona Crear SKU para agregarlo junto con sus alternos.' : 'Los registros aparecerán cuando los proveedores publiquen su inventario.'}</p></div>}
    {!!data?.results.length && <div className="table-scroll"><table className="admin-catalog-table">
      <thead>{kind === 'catalog' ? <tr><th>SKU interno / nombre</th><th>Alternos</th><th>Estado</th><th className="number-cell">Existencias</th><th className="actions-cell">Acciones</th></tr> : kind === 'inventory' ? <tr><th>Proveedor</th><th>Código / marca</th><th>SKU interno</th><th>ID del proveedor</th><th>Disponibles</th><th>Reservadas</th><th>Coincidencia</th></tr> : <tr><th>SKU interno</th><th>Código alterno</th><th>Marca del código</th><th>Acciones</th></tr>}</thead>
      <tbody>{data?.results.map(row => {
        if (kind === 'catalog') { const part = row as ManagedPart; const identity = `${part.is_OEM ? 'MAIN OEM' : part.identity?.ref_type === 'company' ? `MAIN INTERNO · ${part.identity.brand}` : 'MAIN INTERNO'}${!part.is_OEM ? part.identity?.status === 'choose_oem' ? ' · ELEGIR OEM' : ' · OEM PENDIENTE' : ''}`; const shown = part.codes.slice(0, ALTERNATES_SHOWN); const hidden = part.codes.slice(ALTERNATES_SHOWN);
          return <tr key={part.id}><td className="sku-cell"><strong>{part.sku}</strong><span className={`sku-identity ${part.is_OEM ? 'oem' : part.identity?.status === 'choose_oem' ? 'choose' : 'pending'}`}>{identity}</span>{part.name && part.name !== part.sku && <span>{part.name}</span>}{(part.category || part.subcategory) && <span className="sku-category">{[part.category, part.subcategory].filter(Boolean).join(' / ')}</span>}</td><td><div className="admin-role-list">{part.codes.length ? <>{shown.map(code => <span key={`${code.brand}:${code.code}`} title={code.brand || 'Todas las marcas'}>{code.code} · {referenceLabel(code)}</span>)}{!!hidden.length && <span className="more" title={hidden.map(code => `${code.code} · ${referenceLabel(code)}`).join('\n')}>+{hidden.length} más</span>}</> : <span className="none">Solo el código del SKU</span>}</div></td><td><span className={`status ${part.active ? 'matched' : ''}`}>{part.active ? 'Activo' : 'Inactivo'}</span></td><td className="number-cell">{part.stock_record_count}</td><td className="actions-cell"><div className="admin-row-actions"><button className="button soft small" aria-label={`Editar SKU ${part.sku}`} onClick={() => onEditPart(part)}><Pencil size={13} aria-hidden="true"/>Editar</button><button className="button ghost small" aria-label={`Ficha técnica de ${part.sku}`} onClick={() => onTechnical(part)}><Wrench size={13} aria-hidden="true"/>Ficha técnica</button><button className="button ghost small" aria-label={`Imágenes de ${part.sku}`} onClick={() => onImages(part)}><Images size={13} aria-hidden="true"/>Imágenes ({part.images?.length || 0})</button></div></td></tr>; }
        if (kind === 'inventory') { const item = row as ManagedStockItem; return <tr key={item.id}><td><strong>{item.supplier_name}</strong><span>{item.source === 'apiag' ? 'apiag-cloud' : 'Carga manual'}</span></td><td><strong>{item.codigo}</strong><span>{item.brand}</span></td><td><strong>{item.part_sku || 'Sin vincular'}</strong>{item.part_name && item.part_name !== item.part_sku && <span>{item.part_name}</span>}</td><td>{item.supplier_invent_id}</td><td className="number-cell">{item.available_quantity}</td><td className="number-cell">{item.reserved_quantity}</td><td><div className="admin-match-actions"><span className={`status ${item.matching_status}`}>{{matched: 'Vinculado', pending: 'Pendiente', review: 'Por revisar'}[item.matching_status]}</span><button className="button text small" aria-label={`Revisar coincidencia ${item.supplier_name} ${item.supplier_invent_id}`} onClick={() => onEditMatch(item)}>Revisar</button></div></td></tr>; }
        const alternate = row as ManagedAlternate; return <tr key={alternate.id}><td><strong>{alternate.part_sku}</strong>{alternate.part_name && alternate.part_name !== alternate.part_sku && <span>{alternate.part_name}</span>}</td><td><strong>{alternate.code}</strong><span>{referenceLabel(alternate)}</span>{alternate.reference_source && <span>{alternate.reference_source}</span>}</td><td>{alternate.brand || 'Todas las marcas'}</td><td><div className="admin-row-actions"><button className="button soft small" aria-label={`Editar alterno ${alternate.code}`} onClick={() => onEditAlternate(alternate)}>Editar</button><button className="button text small danger-text" aria-label={`Retirar alterno ${alternate.code}`} onClick={() => onRemoveAlternate(alternate)}>Retirar</button></div></td></tr>;
      })}</tbody>
    </table></div>}
    {data && <div className="pagination"><span>{data.count} {data.count === 1 ? 'resultado' : 'resultados'}</span><div><button className="button soft small" disabled={!data.previous} onClick={() => setPage(page - 1)}>Anterior</button><span>Página {page}</span><button className="button soft small" disabled={!data.next} onClick={() => setPage(page + 1)}>Siguiente</button></div></div>}
  </>;
}
