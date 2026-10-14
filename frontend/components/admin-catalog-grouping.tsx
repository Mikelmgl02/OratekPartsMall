'use client';

import { useEffect, useState } from 'react';
import { ArrowLeft, BrainCircuit, Check, Layers3, LoaderCircle, Search } from 'lucide-react';
import { CatalogGroupingCandidate, CatalogGroupingClassification, CatalogGroupingPart, Page, request } from '@/lib/types';
import Modal from './modal';
import { UppercaseInput } from './uppercase-field';

type GroupingPage = Page<CatalogGroupingCandidate> & { configured?: boolean };

export default function CatalogGrouping({ onClose, onSaved }: { onClose: () => void; onSaved: (targetSku: string, sourceCount: number) => void }) {
  const [data, setData] = useState<GroupingPage | null>(null);
  const [search, setSearch] = useState('');
  const [query, setQuery] = useState('');
  const [page, setPage] = useState(1);
  const [revision, setRevision] = useState(0);
  const [editing, setEditing] = useState<CatalogGroupingCandidate | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState('');
  const [error, setError] = useState('');

  useEffect(() => { const timer = setTimeout(() => { setQuery(search.trim()); setPage(1); }, 300); return () => clearTimeout(timer); }, [search]);
  useEffect(() => {
    let cancelled = false; setData(null); setError('');
    request<GroupingPage>(`/api/management/catalog/grouping?search=${encodeURIComponent(query)}&page=${page}`).then(result => {
      if (cancelled) return;
      if (!result.results.length && page > 1) { setPage(value => value - 1); return; }
      setData(result);
    }).catch(reason => { if (!cancelled) setError((reason as Error).message); });
    return () => { cancelled = true; };
  }, [query, page, revision]);

  return <Modal title="Agrupar SKU" wide onClose={() => { if (!busy) onClose(); }}>
    <p className="modal-description">Revisa los SKU que podrían representar el mismo repuesto. El SKU interno elegido conserva su identidad; los códigos de los SKU agrupados pasan a ser sus alternos.</p>
    {notice && <p className="notice success" role="status"><Check size={16}/>{notice}</p>}
    {error && <p className="notice error" role="alert">{error}<button type="button" onClick={() => setRevision(value => value + 1)}>Intentar de nuevo</button></p>}
    {editing ? <GroupingEditor key={editing.target_sku} group={editing} aiConfigured={data?.configured !== false} onBack={() => setEditing(null)} onBusyChange={setBusy} onSaved={(targetSku, sourceCount) => {
      setEditing(null); setRevision(value => value + 1); setNotice(`${sourceCount} ${sourceCount === 1 ? 'SKU agrupado' : 'SKU agrupados'} bajo ${targetSku}. Sus códigos ya son alternos del SKU interno.`); onSaved(targetSku, sourceCount);
    }}/> : <>
      <div className="catalog-search admin-search"><Search size={18}/><UppercaseInput aria-label="Buscar familias de SKU" value={search} placeholder="SKU, código o descripción…" onChange={event => setSearch(event.target.value)}/></div>
      <p className="form-footnote grouping-explanation">Estas familias se detectan por el código base y la descripción. Son candidatos para revisión; no se agrupan automáticamente.</p>
      {!data && !error && <div className="loading"><LoaderCircle className="spin" size={20}/>Buscando familias de SKU…</div>}
      {data && !data.results.length && <div className="empty-state"><Layers3 size={30}/><h3>No encontramos familias pendientes.</h3><p>{query ? 'Prueba otra búsqueda. También puedes editar los alternos del SKU directamente.' : 'Los candidatos aparecerán cuando existan SKU separados con un mismo código base y descripción.'}</p></div>}
      {!!data?.results.length && <div className="grouping-families">{data.results.map(group => <article className="grouping-family" key={group.target_sku}>
        <div><h3>{group.target_sku}</h3><p>{group.target.description || group.target.name || 'Sin descripción'}</p><div className="admin-role-list">{group.source_skus.slice(0, 5).map(sku => <span key={sku}>{sku}</span>)}{group.source_count > 5 && <span>+{group.source_count - 5} más</span>}</div><small>{group.reason}</small>{!!group.warnings?.length && <small className="grouping-warning-label">{group.warnings.length} {group.warnings.length === 1 ? 'advertencia de códigos' : 'advertencias de códigos'} por revisar</small>}</div>
        <button type="button" className="button soft small" aria-label={`Revisar familia ${group.target_sku}`} onClick={() => { setEditing(group); setNotice(''); }}>Revisar {group.source_count} {group.source_count === 1 ? 'candidato' : 'candidatos'}</button>
      </article>)}</div>}
      {data && <div className="pagination grouping-pagination"><span>{data.count} {data.count === 1 ? 'familia' : 'familias'} por revisar</span><div><button type="button" className="button soft small" disabled={!data.previous} onClick={() => setPage(value => value - 1)}>Anterior</button><span>Página {page}</span><button type="button" className="button soft small" disabled={!data.next} onClick={() => setPage(value => value + 1)}>Siguiente</button></div></div>}
    </>}
  </Modal>;
}

function PartContext({ part }: { part: CatalogGroupingPart }) {
  return <div className="grouping-part-context"><strong>{part.sku}</strong>{part.name && part.name !== part.sku && <span>{part.name}</span>}<p>{part.description || 'Sin descripción'}</p>{(part.category || part.subcategory) && <small>{[part.category, part.subcategory].filter(Boolean).join(' / ')}</small>}{!!part.codes.length && <div className="admin-role-list">{part.codes.map(code => <span key={`${code.brand}:${code.code}`}>{code.code}{code.brand ? ` · ${code.brand}` : ''}</span>)}</div>}</div>;
}

function GroupingEditor({ group, aiConfigured, onBack, onBusyChange, onSaved }: { group: CatalogGroupingCandidate; aiConfigured: boolean; onBack: () => void; onBusyChange: (busy: boolean) => void; onSaved: (targetSku: string, sourceCount: number) => void }) {
  const members = [group.target, ...group.sources];
  const [targetSku, setTargetSku] = useState(group.target_sku);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [reviewed, setReviewed] = useState(false);
  const [category, setCategory] = useState('');
  const [subcategory, setSubcategory] = useState('');
  const [classification, setClassification] = useState<CatalogGroupingClassification | null>(null);
  const [busy, setBusy] = useState<'classify' | 'merge' | null>(null);
  const [error, setError] = useState('');
  const target = members.find(part => part.sku === targetSku)!;
  const sources = members.filter(part => part.sku !== targetSku && !part.proposed_parent);
  const locked = !!busy;
  function changeBusy(value: typeof busy) { setBusy(value); onBusyChange(!!value); }
  function select(sku: string, checked: boolean) { setSelected(current => { const next = new Set(current); if (checked) next.add(sku); else next.delete(sku); return next; }); setReviewed(false); }
  function chooseTarget(value: string) { setTargetSku(value); setSelected(new Set()); setReviewed(false); setClassification(null); setCategory(''); setSubcategory(''); setError(''); }

  async function classify() {
    if (busy) return;
    changeBusy('classify'); setError(''); setClassification(null); setReviewed(false);
    try {
      const result = await request<CatalogGroupingClassification>('/api/management/catalog/grouping/classify', { method: 'POST', body: JSON.stringify({ target_sku: targetSku, source_skus: sources.map(part => part.sku) }) });
      setClassification(result); setCategory(result.category || ''); setSubcategory(result.subcategory || '');
    } catch (reason) { setError((reason as Error).message); }
    finally { changeBusy(null); }
  }

  async function merge() {
    if (busy || !selected.size || !reviewed) return;
    changeBusy('merge'); setError('');
    const sourceSkus = sources.filter(part => selected.has(part.sku)).map(part => part.sku);
    try {
      await request('/api/management/catalog/grouping/merge', { method: 'POST', body: JSON.stringify({ target_sku: targetSku, source_skus: sourceSkus, create_target: !!target.proposed_parent, ...(category.trim() ? { category: category.trim() } : {}), ...(subcategory.trim() ? { subcategory: subcategory.trim() } : {}) }) });
      onSaved(targetSku, sourceSkus.length);
    } catch (reason) { setError((reason as Error).message); }
    finally { changeBusy(null); }
  }

  return <section className="grouping-editor" aria-label={`Revisión de familia ${group.target_sku}`}>
    <div className="grouping-heading"><button type="button" className="button text small" disabled={locked} onClick={onBack}><ArrowLeft size={15}/>Volver a familias</button>{aiConfigured && <button type="button" className="button soft small" disabled={locked} onClick={classify}><BrainCircuit size={16}/>{busy === 'classify' ? 'Revisando con IA…' : 'Revisar familia con IA'}</button>}</div>
    <p className="form-footnote">{aiConfigured ? 'La IA revisa solo esta familia. Sus propuestas no guardan cambios ni seleccionan repuestos; confirma cuáles son reemplazos idénticos.' : 'La IA todavía no está configurada. Puedes revisar y agrupar los repuestos manualmente.'}</p>
    {!!group.warnings?.length && <div className="notice grouping-code-warnings" role="status"><strong>Códigos que requieren revisión</strong>{group.warnings.map((warning, index) => <p key={index}>{warning}</p>)}<small>Revisa estos alternos desde su editor. No se trasladarán códigos de otros SKU que no hayas seleccionado.</small></div>}
    {error && <p className="notice error" role="alert">{error}</p>}
    <label className="grouping-target-label">SKU interno de destino<select aria-label="SKU interno de destino" value={targetSku} disabled={locked} onChange={event => chooseTarget(event.target.value)}>{members.map(part => <option key={part.sku} value={part.sku}>{part.sku}{part.proposed_parent ? ' · CREAR SKU PADRE' : ''}</option>)}</select></label>
    {target.proposed_parent && <p className="notice" role="status">Se creará el SKU padre {targetSku} al confirmar la agrupación. Los códigos seleccionados se conservarán como alternos.</p>}
    <div className="grouping-target"><span className="eyebrow">SKU interno</span><PartContext part={target}/></div>
    {classification && <div className="notice grouping-ai-result" role="status"><strong>Propuesta de IA · {Math.round(classification.confidence * 100)}% de confianza</strong><p>{classification.reason}</p>{classification.target_sku !== targetSku && <p>Destino propuesto: {classification.target_sku}. Cambia el destino arriba si confirmas esta propuesta.</p>}<small>La propuesta requiere tu revisión; la coincidencia del código no garantiza compatibilidad.</small></div>}
    <h3>Selecciona los SKU que pasarán a ser alternos</h3>
    <div className="grouping-source-list">{sources.map(part => {
      const suggestion = classification?.suggestions.find(item => item.source_sku === part.sku);
      const joinsTarget = suggestion?.target_sku === targetSku;
      return <article key={part.sku} className={`grouping-source ${selected.has(part.sku) ? 'selected' : ''}`}>
        <label className="grouping-select"><input type="checkbox" aria-label={`Agrupar ${part.sku} bajo ${targetSku}`} checked={selected.has(part.sku)} disabled={locked} onChange={event => select(part.sku, event.target.checked)}/><span>Confirmar como alterno</span></label>
        <PartContext part={part}/>
        {suggestion && <div className="grouping-evidence"><strong>{joinsTarget ? `IA: agrupar bajo ${targetSku}` : suggestion.target_sku === part.sku ? 'IA: conservar separado' : `IA: destino ${suggestion.target_sku}`}</strong><p>{suggestion.reason}</p>{suggestion.source_reference && <small>Referencia del origen: {suggestion.source_reference}</small>}{suggestion.target_reference && <small>Referencia del destino: {suggestion.target_reference}</small>}</div>}
      </article>;
    })}</div>
    {group.source_count > group.sources.length && <p className="form-footnote">Se muestran {group.sources.length} de {group.source_count} candidatos. Después de agrupar este lote podrás revisar los restantes.</p>}
    <div className="grouping-category-fields"><label>Categoría del SKU interno (opcional)<UppercaseInput aria-label="Categoría de agrupación" value={category} maxLength={120} disabled={locked} onChange={event => { setCategory(event.target.value); setReviewed(false); }} placeholder={target.category || 'CONSERVAR CATEGORÍA ACTUAL'}/></label><label>Subcategoría (opcional)<UppercaseInput aria-label="Subcategoría de agrupación" value={subcategory} maxLength={120} disabled={locked} onChange={event => { setSubcategory(event.target.value); setReviewed(false); }} placeholder={target.subcategory || 'CONSERVAR SUBCATEGORÍA ACTUAL'}/></label></div>
    <div className="grouping-confirmation"><p>{selected.size ? <>{selected.size} {selected.size === 1 ? 'SKU dejará' : 'SKU dejarán'} de aparecer separado en el catálogo. Sus códigos, artículos de proveedores e historial se conservarán bajo <strong>{targetSku}</strong>.</> : 'Selecciona únicamente los repuestos que confirmaste como reemplazos idénticos.'}</p><label className="admin-checkbox"><input type="checkbox" checked={reviewed} disabled={locked || !selected.size} onChange={event => setReviewed(event.target.checked)}/>Confirmo que los SKU seleccionados son el mismo repuesto.</label></div>
    <button type="button" className="button primary full" disabled={locked || !selected.size || !reviewed} onClick={merge}>{busy === 'merge' ? <><LoaderCircle size={16} className="spin"/>Agrupando…</> : `Agrupar ${selected.size} ${selected.size === 1 ? 'alterno' : 'alternos'} bajo ${targetSku}`}</button>
  </section>;
}
