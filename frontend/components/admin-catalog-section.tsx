'use client';

import { referenceLabel } from '@/lib/reference-label';

import { useEffect, useState } from 'react';
import { Boxes, BrainCircuit, Check, FileSpreadsheet, FileWarning, Layers3, LoaderCircle, Plus, Repeat2, Search } from 'lucide-react';
import { CatalogImportIssuePage, ManagedAlternate, ManagedPart, ManagedStockItem, Page, request } from '@/lib/types';
import { AlternateEditor, MatchEditor, PartEditor, RemoveAlternate } from './admin-catalog-editors';
import { UppercaseInput } from './uppercase-field';
import CatalogImport from './admin-catalog-import';
import ImportIssues from './admin-import-issues';
import CatalogGrouping from './admin-catalog-grouping';
import InventoryAssistant from './admin-inventory-assistant';
import CatalogImages from './admin-catalog-images';
import SmartMatching from './admin-smart-matching';
import { PartTechnicalEditor } from './part-technical';

type Kind = 'catalog' | 'inventory' | 'alternates';
type Row = ManagedPart | ManagedStockItem | ManagedAlternate;

export default function AdminCatalogSection({ section }: { section: 'inventory' | 'alternates' }) {
  const [technicalPart, setTechnicalPart] = useState<ManagedPart | null>(null);
  const [matchingOpen, setMatchingOpen] = useState(false);
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
    <div className="admin-toolbar"><div><h2>{section === 'inventory' ? 'Inventario' : 'Alternos'}</h2><p>{section === 'inventory' ? 'SKU internos que agrupan repuestos idénticos y existencias por proveedor.' : 'Biblioteca de referencias OEM y de fabricantes equivalentes a cada SKU maestro.'}</p></div>{kind !== 'inventory' && <div className="admin-toolbar-actions">{kind === 'catalog' && <><button className="button soft" onClick={() => setGroupingOpen(true)}><Layers3 size={17}/>Agrupar SKU</button><button className="button soft" aria-label="Errores de importación" onClick={() => setIssuesOpen(true)}><FileWarning size={17}/>Errores de importación{pendingIssueCount != null && <span className="import-issues-count" aria-hidden="true">{pendingIssueCount}</span>}</button><button className="button soft" onClick={() => setImportOpen(true)}><FileSpreadsheet size={17}/>Importar Excel</button></>}<button className="button primary" onClick={() => { if (kind === 'alternates') setAlternateEditor(null); else setPartEditor(null); }}><Plus size={17}/>{kind === 'alternates' ? 'Crear alterno' : 'Crear SKU'}</button></div>}</div>
    {section === 'inventory' && <div className="admin-inventory-view-toolbar"><button className="button soft" onClick={()=>setMatchingOpen(true)}><Layers3 size={17}/>Agrupación inteligente</button><div className="admin-inventory-tabs" role="group" aria-label="Vistas de inventario"><button className={!stockView ? 'selected' : ''} aria-pressed={!stockView} onClick={() => setStockView(false)}>Inventario interno</button><button className={stockView ? 'selected' : ''} aria-pressed={stockView} onClick={() => setStockView(true)}>Existencias por proveedor</button></div>{!stockView && <button type="button" className="button soft" onClick={()=>setAssistantOpen(true)}><BrainCircuit size={17}/>Asistente IA</button>}</div>}
    {notice && <div className="notice success admin-notice" role="status"><Check size={16}/>{notice}</div>}
    <Collection onTechnical={setTechnicalPart} onImages={setImagePart} key={kind} kind={kind} revision={revision} onEditPart={setPartEditor} onEditAlternate={setAlternateEditor} onEditMatch={setMatchEditor} onRemoveAlternate={setRemoveAlternate}/>
    <p className="admin-footnote">{kind === 'alternates' ? 'Los alternos de un SKU también aparecen en su editor de inventario interno. Cada artículo del proveedor mantiene su propio inventario e historial.' : kind === 'catalog' ? 'El SKU maestro no tiene marca. Sus referencias identifican piezas equivalentes; los artículos del proveedor conservan sus propios códigos y existencias.' : 'Los registros conservan el ID de inventario de cada proveedor y su vínculo con el SKU interno.'}</p>
    {matchingOpen && <SmartMatching onClose={()=>setMatchingOpen(false)} onSaved={()=>saved('Coincidencias actualizadas.')}/>}
    {technicalPart && <PartTechnicalEditor part={technicalPart} onClose={() => setTechnicalPart(null)} onSaved={() => { setTechnicalPart(null); saved('Ficha técnica y aplicaciones guardadas.'); }}/>}
    {imagePart && <CatalogImages part={imagePart} onClose={() => setImagePart(null)} onChanged={() => setRevision(value => value + 1)}/>}
    {partEditor !== undefined && <PartEditor part={partEditor} onClose={() => setPartEditor(undefined)} onSaved={() => { setPartEditor(undefined); saved('SKU y alternos guardados.'); }}/>}
    {importOpen && <CatalogImport onClose={() => { setImportOpen(false); setRevision(value => value + 1); }} onSaved={summary => { setImportOpen(false); saved(`Importación completada: ${summary.created_skus} SKU nuevos, ${summary.updated_skus} SKU actualizados y ${summary.added_codes} alternos nuevos.${summary.rejected_rows ? ` ${summary.rejected_rows} ${summary.rejected_rows === 1 ? 'fila pendiente' : 'filas pendientes'} de corregir en «Errores de importación».` : summary.skipped_rows ? ` ${summary.skipped_rows} ${summary.skipped_rows === 1 ? 'fila excluida' : 'filas excluidas'}.` : ''}`); }}/>} 
    {issuesOpen && <ImportIssues onClose={() => { setIssuesOpen(false); setRevision(value => value + 1); }} onSaved={() => saved('Fila corregida y guardada en el inventario interno.')}/>}
    {groupingOpen && <CatalogGrouping onClose={() => setGroupingOpen(false)} onSaved={(targetSku, count) => saved(`${count} ${count === 1 ? 'SKU agrupado' : 'SKU agrupados'} bajo ${targetSku}.`)}/>}
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
    <div className="catalog-search admin-search"><Search size={19}/><label className="sr-only" htmlFor="catalog-admin-search">{label}</label><UppercaseInput id="catalog-admin-search" value={search} onChange={event => setSearch(event.target.value)} placeholder={kind === 'catalog' ? 'SKU, nombre o código alterno…' : kind === 'inventory' ? 'Proveedor, marca o código del repuesto…' : 'SKU interno, código alterno o marca…'}/></div>
    {kind === 'catalog' && <div className="admin-toolbar-actions"><label>TIPO DE SKU <select aria-label="Tipo de SKU" value={oemFilter} onChange={event => { setOemFilter(event.target.value); setPage(1); }}><option value="">TODOS</option><option value="true">OEM</option><option value="false">SIN MARCAR COMO OEM</option></select></label></div>}
    {error && <div className="notice error" role="alert">{error}<button onClick={() => setRetry(value => value + 1)}>Intentar de nuevo</button></div>}
    {!data && !error && <div className="loading"><LoaderCircle className="spin" size={20}/>Cargando {kind === 'alternates' ? 'alternos' : 'inventario'}…</div>}
    {data && !data.results.length && <div className="empty-state">{kind === 'alternates' ? <Repeat2 size={30}/> : <Boxes size={30}/>}<h3>{query || oemFilter ? 'No encontramos resultados.' : kind === 'alternates' ? 'Todavía no hay alternos registrados.' : kind === 'catalog' ? 'Todavía no hay SKU en el inventario interno.' : 'Todavía no hay existencias reportadas.'}</h3><p>{query || oemFilter ? 'Prueba con otra búsqueda o filtro.' : kind === 'alternates' ? 'Selecciona Crear alterno para agregar un código a un SKU interno.' : kind === 'catalog' ? 'Selecciona Crear SKU para agregarlo junto con sus alternos.' : 'Los registros aparecerán cuando los proveedores publiquen su inventario.'}</p></div>}
    {!!data?.results.length && <div className="table-scroll"><table className="admin-catalog-table">
      <thead>{kind === 'catalog' ? <tr><th>SKU interno / nombre</th><th>Alternos</th><th>Estado</th><th>Registros de existencias</th><th>Acciones</th></tr> : kind === 'inventory' ? <tr><th>Proveedor</th><th>Código / marca</th><th>SKU interno</th><th>ID del proveedor</th><th>Disponibles</th><th>Reservadas</th><th>Coincidencia</th></tr> : <tr><th>SKU interno</th><th>Código alterno</th><th>Marca del código</th><th>Acciones</th></tr>}</thead>
      <tbody>{data?.results.map(row => {
        if (kind === 'catalog') { const part = row as ManagedPart; return <tr key={part.id}><td><strong>{part.sku}</strong>{<span>{part.is_OEM ? 'MAIN OEM' : part.identity?.ref_type === 'company' ? `MAIN INTERNO · ${part.identity.brand}` : 'MAIN INTERNO'}{!part.is_OEM ? part.identity?.status === 'choose_oem' ? ' · ELEGIR OEM' : ' · OEM PENDIENTE' : ''}</span>}{part.name && part.name !== part.sku && <span>{part.name}</span>}{(part.category || part.subcategory) && <span>{[part.category, part.subcategory].filter(Boolean).join(' / ')}</span>}</td><td><div className="admin-role-list">{part.codes.length ? part.codes.map(code => <span key={`${code.brand}:${code.code}`} title={code.brand || 'Todas las marcas'}>{code.code} · {referenceLabel(code)}</span>) : <span>Solo el código del SKU</span>}</div></td><td><span className={`status ${part.active ? 'matched' : ''}`}>{part.active ? 'Activo' : 'Inactivo'}</span></td><td>{part.stock_record_count}</td><td><div className="admin-row-actions"><button className="button soft small" aria-label={`Editar SKU ${part.sku}`} onClick={() => onEditPart(part)}>Editar</button><button className="button soft small" aria-label={`Ficha técnica de ${part.sku}`} onClick={() => onTechnical(part)}>Ficha técnica</button><button className="button soft small" aria-label={`Imágenes de ${part.sku}`} onClick={() => onImages(part)}>Imágenes ({part.images?.length || 0})</button></div></td></tr>; }
        if (kind === 'inventory') { const item = row as ManagedStockItem; return <tr key={item.id}><td><strong>{item.supplier_name}</strong><span>{item.source === 'apiag' ? 'apiag-cloud' : 'Carga manual'}</span></td><td><strong>{item.codigo}</strong><span>{item.brand}</span></td><td><strong>{item.part_sku || 'Sin vincular'}</strong>{item.part_name && item.part_name !== item.part_sku && <span>{item.part_name}</span>}</td><td>{item.supplier_invent_id}</td><td className="number-cell">{item.available_quantity}</td><td className="number-cell">{item.reserved_quantity}</td><td><div className="admin-match-actions"><span className={`status ${item.matching_status}`}>{{matched: 'Vinculado', pending: 'Pendiente', review: 'Por revisar'}[item.matching_status]}</span><button className="button text small" aria-label={`Revisar coincidencia ${item.supplier_name} ${item.supplier_invent_id}`} onClick={() => onEditMatch(item)}>Revisar</button></div></td></tr>; }
        const alternate = row as ManagedAlternate; return <tr key={alternate.id}><td><strong>{alternate.part_sku}</strong>{alternate.part_name && alternate.part_name !== alternate.part_sku && <span>{alternate.part_name}</span>}</td><td><strong>{alternate.code}</strong><span>{referenceLabel(alternate)}</span>{alternate.reference_source && <span>{alternate.reference_source}</span>}</td><td>{alternate.brand || 'Todas las marcas'}</td><td><div className="admin-row-actions"><button className="button soft small" aria-label={`Editar alterno ${alternate.code}`} onClick={() => onEditAlternate(alternate)}>Editar</button><button className="button text small danger-text" aria-label={`Retirar alterno ${alternate.code}`} onClick={() => onRemoveAlternate(alternate)}>Retirar</button></div></td></tr>;
      })}</tbody>
    </table></div>}
    {data && <div className="pagination"><span>{data.count} {data.count === 1 ? 'resultado' : 'resultados'}</span><div><button className="button soft small" disabled={!data.previous} onClick={() => setPage(page - 1)}>Anterior</button><span>Página {page}</span><button className="button soft small" disabled={!data.next} onClick={() => setPage(page + 1)}>Siguiente</button></div></div>}
  </>;
}
