'use client';

import { useEffect, useRef, useState } from 'react';
import { ArrowLeft, BrainCircuit, Check, Layers3, LoaderCircle, Pause, Play, Search } from 'lucide-react';
import { request } from '@/lib/types';
import Modal from './modal';
import { UppercaseInput, UppercaseTextarea } from './uppercase-field';

type CatalogRow = {proposed_parent?:boolean;id:string; sku:string; name:string; description:string; category:string; subcategory:string; codes:{brand:string;code:string}[]};
type Decision = {target_sku:string;category:string;subcategory:string};
type Suggestion = Decision & {method?:'rule'|'cache'|'ai'|'review';id:string;source_sku:string;reason:string;confidence:number;source_reference:string;target_reference:string;status:'pending'|'applied'|'dismissed';decision:Decision;source:CatalogRow;candidates:CatalogRow[]};
type JobSummary = {task?:'categories'|'grouping';metrics?:Record<string,number>;page_size?:number;id:string;scope:'unclassified'|'all';search:string;instructions:string;model:string;total_items:number;batch_size:number;completed_batches:number;total_batches:number;status:'ready'|'running'|'completed';count:number;pending_count:number;applied_count:number;dismissed_count:number;created_at:string};
type CategoryGroup = {category:string;subcategory:string;count:number};
type Job = JobSummary & {category_groups?:CategoryGroup[];applied_batch?:number;configured:boolean;offset:number;next_offset:number|null;results:Suggestion[]};
type Overview = {configured:boolean;eligible_count:number;jobs:JobSummary[]};
const base = '/api/management/catalog/assistant';
const errorMessage = (error:unknown) => error instanceof Error ? error.message : 'No se pudo completar el análisis.';
const count = (value:number) => value.toLocaleString('es-PA');

export default function InventoryAssistant({onClose, onSaved}: {onClose:()=>void;onSaved:()=>void}) {
  const [task, setTask] = useState<'categories'|'grouping'>('categories');
  const [categoryGroup, setCategoryGroup] = useState('');
  const [groupReviewed, setGroupReviewed] = useState(false);
  const [scope, setScope] = useState<'unclassified'|'all'>('unclassified');
  const [search, setSearch] = useState('');
  const [instructions, setInstructions] = useState('');
  const [overview, setOverview] = useState<Overview|null>(null);
  const [job, setJob] = useState<Job|null>(null);
  const [busy, setBusy] = useState<'start'|'classify'|'load'|'apply'|'dismiss'|'apply_category'|null>(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [edits, setEdits] = useState<Record<string,Decision>>({});
  const [revision, setRevision] = useState(0);
  const [pauseRequested, setPauseRequested] = useState(false);
  const operating = useRef(false);
  const pause = useRef(false);
  const requestId = useRef<string|null>(null);
  const locked = !!busy;

  useEffect(() => {
    if (job) return;
    const abort = new AbortController();
    setOverview(null);
    const timer = setTimeout(() => {
      request<Overview>(`${base}?scope=${scope}&search=${encodeURIComponent(search.trim())}`, {signal:abort.signal})
        .then(result=>{if (!abort.signal.aborted) setOverview(result);}).catch(reason => {if (!abort.signal.aborted) setError(errorMessage(reason));});
    }, 300);
    return () => {clearTimeout(timer);abort.abort();};
  }, [scope, search, revision, job]);

  function accept(result:Job) {setJob(result);setSelected(new Set());setEdits({});setGroupReviewed(false);}
  async function load(id:string, offset=0) {
    if (operating.current) return;
    operating.current = true; setBusy('load'); setError(''); setNotice('');
    try {accept(await request<Job>(`${base}/${id}?offset=${offset}`));}
    catch (reason) {setError(errorMessage(reason));}
    finally {operating.current = false;setBusy(null);}
  }
  async function classify(current:Job) {
    pause.current = false;setPauseRequested(false);setBusy('classify');
    try {
      while (current.completed_batches < current.total_batches && !pause.current) {
        current = await request<Job>(`${base}/${current.id}`, {method:'POST', body:JSON.stringify({mode:'classify',batch_index:current.completed_batches})});
        accept(current);
      }
      setNotice(pause.current && current.completed_batches < current.total_batches ? 'Análisis pausado. El progreso y las propuestas quedaron guardados.' : 'Análisis terminado. Revisa las propuestas antes de aplicarlas.');
    } catch (reason) {
      setError(errorMessage(reason));
      // A lost response may have completed on the server. Recover its saved
      // batch index before offering resume, rather than replaying a later batch.
      try {accept(await request<Job>(`${base}/${current.id}`));} catch {}
    } finally {operating.current = false;setBusy(null);setPauseRequested(false);}
  }
  async function start() {
    if (operating.current || (!overview?.configured && task==='grouping') || !overview?.eligible_count) return;
    operating.current = true;setBusy('start');setError('');setNotice('');
    requestId.current ||= crypto.randomUUID();
    try {
      const result = await request<Job>(base, {method:'POST',body:JSON.stringify({id:requestId.current,scope,task,search:search.trim(),instructions:instructions.trim()})});
      accept(result);requestId.current = null;
      await classify(result);
    } catch (reason) {setError(errorMessage(reason));operating.current = false;setBusy(null);}
  }
  async function resume() {
    if (!job || operating.current) return;
    operating.current = true;setError('');setNotice('');
    await classify(job);
  }
  function decision(item:Suggestion):Decision {return edits[item.id] || {target_sku:item.target_sku,category:item.category,subcategory:item.subcategory};}
  function edit(item:Suggestion, field:keyof Decision, value:string) {
    setGroupReviewed(false);
    setEdits(current => ({...current,[item.id]:{...decision(item),[field]:value}}));
    setSelected(current => {const next = new Set(current);next.delete(item.id);return next;});
  }
  async function review(mode:'apply'|'dismiss') {
    if (!job || operating.current || !selected.size) return;
    operating.current = true;setBusy(mode);setError('');setNotice('');
    const decisions = job.results.filter(item => selected.has(item.id)).map(item => ({id:item.id,...decision(item)}));
    try {
      accept(await request<Job>(`${base}/${job.id}?offset=${job.offset}`, {method:'POST',body:JSON.stringify(mode === 'apply' ? {mode,decisions} : {mode,ids:decisions.map(item=>item.id)})}));
      if (mode === 'apply') onSaved();
      setNotice(mode === 'apply' ? 'Propuestas revisadas aplicadas al inventario.' : 'Propuestas descartadas. El inventario conserva sus datos.');
    } catch (reason) {setError(errorMessage(reason));}
    finally {operating.current = false;setBusy(null);}
  }
  const groups = job?.category_groups || [];
  const chosenGroup = groups.find(group=>JSON.stringify([group.category,group.subcategory])===categoryGroup);
  async function applyGroup() {
    if (!job || !chosenGroup || !groupReviewed || Object.keys(edits).length || operating.current) return;
    operating.current=true;setBusy('apply_category');setError('');setNotice('');
    const {category,subcategory}=chosenGroup;
    try {
      let current=job;
      do {
        current=await request<Job>(`${base}/${job.id}?offset=${job.offset}`, {method:'POST',body:JSON.stringify({mode:'apply_category',category,subcategory})});
        accept(current);onSaved();
      } while (current.applied_batch && current.category_groups?.some(g=>g.category===category && g.subcategory===subcategory));
      setNotice('Categoría aplicada. Las propuestas de baja confianza quedan para revisión individual.');
    } catch(reason) {
      setError(errorMessage(reason));
      try {accept(await request<Job>(`${base}/${job.id}?offset=${job.offset}`));} catch {}
    } finally {operating.current=false;setBusy(null);}
  }
  const date = (value:string) => new Date(value).toLocaleString('es-PA',{timeZone:'America/Panama',dateStyle:'short',timeStyle:'short'});

  return <Modal title="Asistente IA de inventario" wide className="inventory-ai-modal" onClose={() => {if (!operating.current) onClose();}}>
    <div className="inventory-ai-intro"><span className="inventory-ai-icon"><BrainCircuit size={26}/></span><div><h3>Organiza tu catálogo de repuestos</h3><p>Clasifica por tipo de repuesto y reutiliza resultados para ahorrar tiempo. Tú revisas y decides qué guardar.</p></div></div>
    {error && <div className="notice error" role="alert">{error}<button type="button" disabled={locked} onClick={() => job ? load(job.id,job.offset) : setRevision(value=>value+1)}>Actualizar análisis</button></div>}
    {notice && <p className="notice success" role="status"><Check size={16}/>{notice}</p>}
    {!job ? <>
      <div className="inventory-ai-fields inventory-ai-mode"><label>Tipo de análisis<select aria-label="Tipo de análisis" value={task} disabled={locked} onChange={event=>{setTask(event.target.value as typeof task);requestId.current=null;}}><option value="categories">Categorías · rápido y económico</option><option value="grouping">Categorías y equivalencias · análisis completo</option></select></label><p className="form-footnote">{task==='categories' ? 'Sugiere grupo y subgrupo. Los SKU se conservan separados.' : 'Busca también candidatos equivalentes en todo el catálogo. Este análisis utiliza más tiempo y tokens.'}</p></div>
      <div className="inventory-ai-fields"><label>Artículos para analizar<select aria-label="Artículos para analizar" value={scope} disabled={locked} onChange={event => {setScope(event.target.value as typeof scope);requestId.current=null;setError('');}}><option value="unclassified">Sin grupo o subgrupo</option><option value="all">Todo el inventario interno</option></select></label><label>Filtrar SKU o descripción<div className="inventory-ai-search"><Search size={16}/><UppercaseInput aria-label="Filtrar SKU o descripción" maxLength={200} value={search} disabled={locked} placeholder="TODOS LOS SKU" onChange={event=>{setSearch(event.target.value);requestId.current=null;setError('');}}/></div></label></div>
      <label className="inventory-ai-instructions">Indicaciones para la IA (opcional)<UppercaseTextarea aria-label="Indicaciones para la IA (opcional)" value={instructions} maxLength={2000} rows={3} disabled={locked} placeholder="EJ.: USAR FRENOS COMO GRUPO Y SEPARAR DISCOS, TAMBORES Y PASTILLAS EN SUBGRUPOS." onChange={event=>{setInstructions(event.target.value);requestId.current=null;}}/></label>
      {task==='categories' && instructions.trim() && <p className="form-footnote">Las indicaciones personalizadas se consultarán con IA y pueden aumentar el consumo. Deja este campo vacío para aprovechar las reglas locales.</p>}
      <div className="inventory-ai-scope" aria-live="polite"><strong>{overview ? `${count(overview.eligible_count)} SKU para analizar` : 'Buscando artículos…'}</strong><p>{task==='categories' ? 'Lotes de 200 SKU. Los tipos reconocidos se resuelven sin IA; solo se envían descripciones pendientes, sin repetir las idénticas. Puedes pausar y continuar después.' : 'Se analizan lotes de 50 SKU junto con sus posibles equivalencias. Puedes pausar y continuar después.'}</p></div>
      {overview && !overview.configured && <p className="notice">{task==='categories' ? 'La IA no está configurada. Se usarán las reglas locales; el resto quedará para revisión.' : 'La IA todavía no está disponible. Revisa la configuración del servidor para activar el asistente.'}</p>}
      <button type="button" className="button primary full" disabled={locked || (!overview?.configured && task==='grouping') || !overview?.eligible_count} onClick={start}>{busy==='start' ? <LoaderCircle size={17} className="spin"/> : <BrainCircuit size={17}/>} {busy==='start' ? 'Preparando análisis…' : 'Analizar inventario con IA'}</button>
      {!!overview?.jobs.length && <section className="inventory-ai-history" aria-label="Análisis guardados"><h3>Análisis guardados</h3>{overview.jobs.map(item=><button type="button" className="inventory-ai-history-row" key={item.id} disabled={locked} onClick={()=>load(item.id)}><div><strong>{item.search || (item.scope==='unclassified' ? 'SIN GRUPO O SUBGRUPO' : 'TODO EL INVENTARIO')}</strong><small>{date(item.created_at)} · {count(item.total_items)} SKU · {item.task==='categories' ? 'CATEGORÍAS' : 'ANÁLISIS COMPLETO'}</small></div><span>{item.completed_batches}/{item.total_batches} lotes · {count(item.pending_count)} por revisar</span></button>)}</section>}
    </> : <>
      <div className="inventory-ai-job-heading"><button type="button" className="button text small" disabled={locked} onClick={()=>{setJob(null);setError('');setNotice('');setRevision(value=>value+1);}}><ArrowLeft size={15}/>Volver a análisis</button><span>{job.search || (job.scope==='unclassified' ? 'SIN GRUPO O SUBGRUPO' : 'TODO EL INVENTARIO')} · {count(job.total_items)} SKU</span></div>
      {job.instructions && <p className="form-footnote">Indicaciones: {job.instructions}</p>}
      <div className="inventory-ai-progress" aria-live="polite"><div><strong>{job.completed_batches} de {job.total_batches} lotes analizados</strong><span>{count(job.pending_count)} por revisar · {count(job.applied_count)} aplicadas · {count(job.dismissed_count)} descartadas</span></div><progress aria-label="Progreso del asistente" max={job.total_batches} value={job.completed_batches}/></div>
      {job.task==='categories' && <div className="inventory-ai-savings" aria-label="Uso del análisis"><span><strong>{count(job.metrics?.rule_items || 0)}</strong> por reglas · sin tokens</span><span><strong>{count(job.metrics?.cached_items || 0)}</strong> reutilizados · sin tokens</span><span><strong>{count(job.metrics?.ai_descriptions || 0)}</strong> descripciones enviadas a IA</span><span><strong>{count(job.metrics?.total_tokens || 0)}</strong> tokens reportados</span><small>Consumo reportado por el proveedor en los lotes guardados. Las descripciones repetidas se envían una sola vez por lote.</small></div>}
      <div className="admin-toolbar-actions inventory-ai-controls">
        {busy==='classify' ? <button type="button" className="button soft small" disabled={pauseRequested} onClick={()=>{pause.current=true;setPauseRequested(true);}}><Pause size={15}/>{pauseRequested ? 'Pausando al terminar el lote…' : 'Pausar análisis'}</button> : job.completed_batches<job.total_batches && <button type="button" className="button soft small" disabled={locked || (!job.configured && job.task!=='categories')} onClick={resume}><Play size={15}/>Reanudar análisis</button>}
        {busy==='classify' && <span className="fine-print"><LoaderCircle size={14} className="spin"/> Analizando el siguiente lote…</span>}
      </div>
      {job.task==='categories' && !!groups.length && <section className="inventory-ai-category-review" aria-label="Revisión por categoría"><h3>Aplicar por categoría</h3><p>Revisa el grupo y subgrupo antes de aprobar. Se aplicarán sus propuestas pendientes con confianza de al menos 85 %, incluidas las de otras páginas. Los casos inciertos se revisan individualmente.</p><div className="inventory-ai-fields"><label>Categoría para revisar<select aria-label="Categoría para revisar" value={chosenGroup ? categoryGroup : ''} disabled={locked} onChange={event=>{setCategoryGroup(event.target.value);setGroupReviewed(false);}}><option value="">Selecciona una categoría</option>{groups.map(group=><option key={JSON.stringify([group.category,group.subcategory])} value={JSON.stringify([group.category,group.subcategory])}>{group.category} / {group.subcategory} · {count(group.count)} SKU</option>)}</select></label></div>{!!Object.keys(edits).length && <p>Aplica primero tus correcciones individuales para incluirlas en la revisión por categoría.</p>}{chosenGroup && <><label className="admin-checkbox"><input type="checkbox" checked={groupReviewed} disabled={locked || !!Object.keys(edits).length} onChange={event=>setGroupReviewed(event.target.checked)}/>Revisé esta categoría y apruebo sus {count(chosenGroup.count)} propuestas.</label><button className="button primary small" type="button" disabled={locked || !groupReviewed || !!Object.keys(edits).length} onClick={applyGroup}>{busy==='apply_category' ? 'Aplicando por lotes…' : `Aplicar categoría a ${count(chosenGroup.count)} SKU`}</button></>}</section>}
      {!!job.results.length && <>
        <p className="form-footnote">{job.task==='categories' ? 'Revisa o corrige las propuestas. Las categorías no agrupan SKU ni cambian las existencias.' : 'Revisa cada propuesta y marca «Revisión confirmada». Un código parecido no demuestra que las piezas sean equivalentes. Si corriges una propuesta, confirma su revisión de nuevo.'}</p>
        <button className="button soft small" type="button" disabled={locked} onClick={()=>setSelected(new Set(job.results.filter(item=>item.status==='pending').map(item=>item.id)))}>Marcar esta página como revisada</button>
        <div className="inventory-ai-proposals">{job.results.map(item=> {
          const value = item.status==='applied' ? item.decision : decision(item);
          return <article className={`inventory-ai-proposal ${item.status}`} key={item.id} aria-label={`Propuesta ${item.source_sku}`}>
            <div className="inventory-ai-proposal-heading"><strong>{item.source_sku}</strong><span className="pill">{item.status==='applied' ? 'APLICADA' : item.status==='dismissed' ? 'DESCARTADA' : `${Math.round(item.confidence*100)}% DE CONFIANZA`}</span></div>
            {item.method && <small className="inventory-ai-method">{({rule:'REGLA LOCAL',cache:'RESULTADO REUTILIZADO',ai:'SUGERENCIA IA',review:'REVISIÓN MANUAL'})[item.method]}</small>}
            <p className="inventory-ai-description">{item.source.description || item.source.name || 'SIN DESCRIPCIÓN'}</p><small className="inventory-ai-current">Actual: {[item.source.category,item.source.subcategory].filter(Boolean).join(' / ') || 'SIN CLASIFICAR'}</small>
            <div className="inventory-ai-fields">{job.task!=='categories' && <label>SKU interno de destino<select aria-label={`Destino para ${item.source_sku}`} value={value.target_sku} disabled={locked || item.status!=='pending'} onChange={event=>edit(item,'target_sku',event.target.value)}><option value={item.source_sku}>{item.source_sku} · CONSERVAR SEPARADO</option>{item.candidates.map(candidate=><option key={candidate.sku} value={candidate.sku}>{candidate.sku} · {candidate.proposed_parent ? 'CREAR SKU PADRE' : 'AGRUPAR COMO ALTERNO'}</option>)}</select></label>}<div className="inventory-ai-fields"><label>Grupo<UppercaseInput aria-label={`Grupo para ${item.source_sku}`} maxLength={120} value={value.category} disabled={locked || item.status!=='pending'} onChange={event=>edit(item,'category',event.target.value)}/></label><label>Subgrupo<UppercaseInput aria-label={`Subgrupo para ${item.source_sku}`} maxLength={120} value={value.subcategory} disabled={locked || item.status!=='pending'} onChange={event=>edit(item,'subcategory',event.target.value)}/></label></div></div>
            {value.target_sku!==item.source_sku && <div className="inventory-ai-grouping"><Layers3 size={15}/><div><strong>El código {item.source_sku} será un alterno de {value.target_sku}.</strong>{item.candidates.find(candidate=>candidate.sku===value.target_sku)?.proposed_parent && <p>Se creará este SKU padre al aplicar la revisión.</p>}<p>{item.candidates.find(candidate=>candidate.sku===value.target_sku)?.description || 'SIN DESCRIPCIÓN DEL DESTINO'}</p><small>Se conservan los artículos de proveedores y su historial de existencias.</small></div></div>}
            <div className="inventory-ai-reason"><p>{item.reason}</p>{item.source_reference && <small>Referencia origen: {item.source_reference} · Referencia destino: {item.target_reference}</small>}</div>
            {item.status==='pending' && <label className="admin-checkbox"><input type="checkbox" aria-label={`Revisión confirmada para ${item.source_sku}`} checked={selected.has(item.id)} disabled={locked} onChange={event=>setSelected(current=>{const next=new Set(current);if(event.target.checked) next.add(item.id);else next.delete(item.id);return next;})}/>Revisión confirmada{value.target_sku!==item.source_sku ? ': son el mismo repuesto' : ''}</label>}
          </article>;
        })}</div>
        <div className="inventory-ai-review-actions"><div className="admin-toolbar-actions"><button type="button" className="button soft small" disabled={locked || !job.offset} onClick={()=>load(job.id,Math.max(0,job.offset-(job.page_size || 50)))}>Anterior</button><span>{job.offset+1}–{job.offset+job.results.length} de {count(job.count)}</span><button type="button" className="button soft small" disabled={locked || job.next_offset===null} onClick={()=>load(job.id,job.next_offset!)}>Siguiente</button></div><div className="admin-toolbar-actions"><button type="button" className="button soft small" disabled={locked || !selected.size} onClick={()=>review('dismiss')}>Descartar seleccionadas</button><button type="button" className="button primary small" disabled={locked || !selected.size} onClick={()=>review('apply')}>{busy==='apply' ? 'Aplicando…' : `Aplicar ${selected.size} propuestas revisadas`}</button></div></div>
      </>}
      {!job.results.length && busy!=='classify' && <p className="notice">Todavía no hay propuestas. Reanuda el análisis para clasificar el primer lote.</p>}
    </>}
  </Modal>;
}
