'use client';
import { useCallback, useEffect, useRef, useState } from 'react';
import { ArrowRight, BrainCircuit, RefreshCw, LoaderCircle, Check, Search } from 'lucide-react';
import Modal from './modal';
import { UppercaseInput } from './uppercase-field';
import { PartPicker } from './admin-catalog-editors';
import { request } from '@/lib/types';

type Code = {brand:string;code:string};
// A case row: a supplier's item (sku is its codigo) or an OEM line (sku is its codigo; estado from S6).
type Row = {id:string;sku:string;description:string;name?:string;brand?:string;estado?:string;supplier_invent_id?:string;references?:Code[];codes?:Code[];codigo_pendiente?:string|null;brand_pendiente?:string|null};
// The Reference a case is about (S6): the item's own for a code change, the declared one's line for a reference case.
type CaseReference = {id:number;code:string;brand:string;tipo?:string;line?:string;line_sku?:string};
type Case = {id:string;kind:string;sources:Row[];candidates:Row[];target_sku:string;reason:string;status:string;fingerprint:string;supplier_name:string;
  ai:{target_sku?:string;reason?:string;error?:string;confidence?:number};
  lines?:Row[];new_code?:string;new_brand?:string;reference?:CaseReference|null;guard?:string;route?:string;batch_key?:string;evidence?:Record<string,unknown>};
type Summary = {catalog_count?:number; oem_skus?:number; non_oem_skus?:number; supplier_total?:number; supplier_already_matched?:number; supplier_examined?:number; marked_exact_oems?:number; grouped_skus?:number; matched_items?:number; placed_items?:number; ai_analyzed?:number; ai_status?:string};
type Overview = {running:boolean;queued:boolean;state?:string;stage?:string;last_run:string|null;summary:Summary;error:string;counts:Record<string,number>;kinds?:Record<string,number>;count:number;next_offset:number|null;results:Case[]};
// What a decision sends to matching/<id>, with the case's fingerprint (a stale one answers 409).
type Decision = {action:'approve'|'dismiss'|'merge'|'keep_apart'|'separar_referencia';line?:string;target_sku?:string};

const labels:Record<string,string> = {review:'POR REVISAR',unmatched:'SIN COINCIDENCIA',applied:'APLICADAS',dismissed:'DESCARTADAS'};
// The case kinds of matching on References (S6); supplier, catalog, catalog_reference and seed are the history before it.
const CASE_KINDS = ['code_change','reference','variant','line','merge'];
const kindLabels:Record<string,string> = {code_change:'CAMBIO DE CÓDIGO',reference:'REFERENCIA DECLARADA',variant:'VARIANTE EN REVISIÓN',line:'ELEGIR LÍNEA OEM',merge:'UNIFICAR LÍNEAS',
  supplier:'ARTÍCULO DE PROVEEDOR',catalog:'FAMILIA DEL CATÁLOGO',catalog_reference:'ALTERNO DEL CATÁLOGO',seed:'FAMILIA SIN SKU PRINCIPAL'};
const guardLabels:Record<string,string> = {prefix:'El prefijo del código no coincide.',noun:'La descripción no coincide.',side:'El lado no coincide (izquierdo / derecho, delantero / trasero).',
  make:'La marca del vehículo no coincide.',subgrupo:'Las líneas tienen subgrupos distintos.',spec_conflict:'Las especificaciones no coinciden con las de la línea OEM.',
  separated:'Estas líneas se mantuvieron separadas antes.',disputa:'La referencia está en disputa.'};
const estadoLabels:Record<string,string> = {preparacion:'EN PREPARACIÓN',referencia:'REFERENCIA',rechazada:'RECHAZADA',retirada:'RETIRADA',unificada:'UNIFICADA'};
const batchLabels:Record<string,string> = {'legacy-auto-merge':'UNIFICACIONES AUTOMÁTICAS ANTERIORES'};
const evidenceLabels:Record<string,string> = {numbers:'NÚMEROS OEM',codes:'CÓDIGOS',citations:'FUENTES'};
const stages:Record<string,string> = {catalog:'Revisando referencias principales y casos de unificación…',oem:'Buscando números OEM del catálogo…',suppliers:'Vinculando artículos de proveedores por sus referencias…',proposals:'Llevando artículos sin línea OEM a Productos nuevos…',ai:'Revisando casos ambiguos con IA…'};
const aiLabels:Record<string,string> = {no_pending_cases:'IA no utilizada: no hay casos ambiguos pendientes de analizar.',not_configured:'IA no disponible: falta configurar el proveedor.',disabled:'IA desactivada para esta ejecución.',failed:'La IA no pudo completar este lote. Las coincidencias automáticas se conservaron; puedes reintentar.',completed:'La IA generó propuestas para revisión.'};
const done:Record<string,string> = {
  'code_change:approve':'Cambio de código aprobado.',
  'code_change:dismiss':'Cambio de código rechazado. El artículo conserva su código actual.',
  'reference:approve':'Referencia aprobada en la línea OEM.',
  'reference:dismiss':'Referencia rechazada. No se repetirá mientras sus datos sean iguales.',
  approve:'Artículo vinculado a la línea OEM elegida.',
  separar_referencia:'Referencia separada: el código queda como interno de este proveedor y el artículo pasa a Productos nuevos.',
  merge:'Líneas unificadas. Puedes separarlas después con Separar SKU.',
  keep_apart:'Las líneas se mantendrán separadas y no se volverán a proponer juntas.',
};
const number = (value:number) => value.toLocaleString('es-PA');
const refList = (refs:Code[]) => refs.map(ref=>[ref.brand,ref.code].filter(Boolean).join(': ')).join(' · ');
const isOpen = (item:Case) => ['review','unmatched'].includes(item.status);
const statusLabels:Record<string,string> = {...labels,resolved:'RESUELTA'};
// The lines a case decides between: its own lines (S6), else the candidates (and, for a merge, the sources too), once each.
function caseLines(item:Case):Row[] {
  const rows = item.lines?.length ? item.lines : item.kind==='merge' ? [...item.sources,...item.candidates] : item.candidates;
  return rows.filter((row,index)=>row.id && rows.findIndex(other=>other.id===row.id)===index);
}
// The card's accessible name: its kind and what it decides (the item's code, or the lines a merge joins).
const caseTitle = (item:Case) => `${kindLabels[item.kind]??item.kind.toUpperCase()} ${item.kind==='merge'?caseLines(item).map(row=>row.sku).join(' + '):item.sources[0]?.sku??''}`.trim();
// Evidence values may be codes, numbers or citations: strings, or objects with a code and a brand.
function evidenceText(value:unknown):string {
  if (typeof value==='string' || typeof value==='number') return String(value);
  if (Array.isArray(value)) return value.map(evidenceText).filter(Boolean).join(' · ');
  if (value && typeof value==='object') {
    const row = value as Record<string,unknown>;
    return [row.brand??row.manufacturer,row.code??row.number,row.catalog??row.citation].filter(part=>typeof part==='string' && part).join(': ');
  }
  return '';
}

export default function SmartMatching({onClose,onSaved}:{onClose:()=>void;onSaved:()=>void}) {
  const requestId = useRef(0);
  const lastRun = useRef<string|null|undefined>(undefined);
  const [data,setData] = useState<Overview|null>(null);
  const [status,setStatus] = useState('review');
  const [kind,setKind] = useState('');
  const [offset,setOffset] = useState(0);
  const [targets,setTargets] = useState<Record<string,string>>({});    // legacy cases: the SKU typed or picked
  const [lines,setLines] = useState<Record<string,string>>({});        // S6 cases: the line chosen, by id
  const [searching,setSearching] = useState<Record<string,boolean>>({}); // "Buscar otra línea OEM" open
  const [picking,setPicking] = useState<Record<string,boolean>>({});     // a reference case's "Elegir línea" open
  const [busy,setBusy] = useState('');
  const [error,setError] = useState('');
  const [notice,setNotice] = useState('');
  const load = useCallback(async()=>{
    const id=++requestId.current;
    try {const result=await request<Overview>(`/api/management/matching?status=${status}&offset=${offset}${kind?`&kind=${kind}`:''}`);if(id===requestId.current)setData(result);}
    catch(error) {setError(error instanceof Error ? error.message : 'No se pudo cargar el análisis.');}
  },[status,offset,kind]);
  useEffect(()=>{let active=true; const update=()=>{if(active)void load();};update();const timer=setInterval(update,5000);return()=>{active=false;++requestId.current;clearInterval(timer);};},[load]);
  useEffect(()=>{if(data){if(lastRun.current!==undefined&&data.last_run!==lastRun.current)onSaved();lastRun.current=data.last_run;}},[data,onSaved]);
  async function analyze() {
    setBusy('analyze');setError('');
    try {await request('/api/management/matching',{method:'POST',body:JSON.stringify({retry_ai:true})});setNotice('');await load();}
    catch(error) {setError(error instanceof Error ? error.message : 'No se pudo iniciar el análisis.');}
    finally {setBusy('');}
  }
  async function decide(item:Case,decision:Decision,message:string) {
    setBusy(item.id);setError('');
    try {
      await request(`/api/management/matching/${item.id}`,{method:'POST',body:JSON.stringify({...decision,fingerprint:item.fingerprint})});
      setNotice(message);
      await load();onSaved();
    } catch(error) {setError(error instanceof Error ? error.message : 'No se pudo guardar la revisión.');}
    finally {setBusy('');}
  }
  // The history before S6: a free SKU for a supplier item, the proposed parent for a family.
  function legacy(item:Case,action:'approve'|'dismiss') {
    return decide(item,{action,target_sku:targets[`${item.id}:${item.fingerprint}`]??item.target_sku},
      action==='approve'?'Coincidencia aprobada y guardada para próximas cargas.':'Propuesta descartada. No se repetirá mientras sus datos sean iguales.');
  }
  const tiles = [[data?.summary.matched_items??0,'ARTÍCULOS VINCULADOS'],
    ...(data?.summary.placed_items!==undefined?[[data.summary.placed_items,'LLEVADOS A PRODUCTOS NUEVOS'] as const]:[]),
    ...(data?.summary.grouped_skus?[[data.summary.grouped_skus,'SKU AGRUPADOS'] as const]:[]),
    ...(data?.summary.marked_exact_oems?[[data.summary.marked_exact_oems,'SKU MARCADOS OEM POR LISTA'] as const]:[])] as const;
  const kindTotal = Object.values(data?.kinds??{}).reduce((sum,value)=>sum+value,0);
  return <Modal title="Agrupación inteligente" wide className="smart-matching-modal" onClose={onClose}>
    <p className="modal-description">Este análisis vincula los artículos de los proveedores a una línea OEM por sus referencias: el mismo código y marca, o el código base cuando el sufijo es una marca conocida (58411-1R000-GMB → 58411-1R000) y la descripción coincide. KAIYI y CHERY comparten una línea solo cuando el código es el mismo. Lo que no puede decidir queda aquí como un caso; los artículos sin línea OEM pasan a Productos nuevos.</p>
    <div className="admin-toolbar-actions"><button className="button primary" disabled={!!busy||data?.running||data?.queued} onClick={analyze}><BrainCircuit size={17}/>{data?.running?'Analizando…':data?.queued?'En cola…':'Analizar catálogo y proveedores'}</button><button className="button soft" onClick={load}><RefreshCw size={16}/>Actualizar</button></div>
    <p className="form-footnote">La IA solo ordena los casos de elegir línea y de unificación: hasta 20 por ejecución, como propuestas para revisar. Este botón no busca OEM en internet ni clasifica categorías.</p>
    <div className="notice matching-oem-help"><strong>¿BUSCAS EL OEM DE UN CÓDIGO DE EMPRESA?</strong><p>Abre Editar SKU → Buscar referencias OEM con IA. Revisa la fuente y agrega la equivalencia; al elegirla como MAIN se conserva el código de empresa como alterno y se marca el SKU como OEM.</p></div>
    {error&&<div className="notice error" role="alert">{error}</div>}{data?.error&&<div className="notice error" role="alert">{data.error}</div>}{notice&&<div className="notice success" role="status">{notice}</div>}
    {(data?.running||data?.queued||busy==='analyze')&&<div className="notice matching-progress" role="status"><LoaderCircle size={17} className="spin"/> {data?.running ? stages[data.stage||'']||'Analizando coincidencias…' : data?.state==='waiting_import' ? 'Esperando a que termine la importación del catálogo…' : data?.state==='retrying' ? 'Reintento pendiente…' : 'Análisis en cola…'}<p>Continuará aunque cierres esta ventana.</p></div>}
    {data?.last_run&&<section className="matching-run-summary" aria-label="Resultado del último análisis">
      <strong><Check size={17}/> {data.running||data.queued?'ÚLTIMO ANÁLISIS COMPLETADO':'ANÁLISIS COMPLETADO'}</strong>
      <small>{new Date(data.last_run).toLocaleString('es-PA')}</small>
      {data.summary.catalog_count!==undefined&&<p>{number(data.summary.catalog_count)} SKU en el catálogo al iniciar · {number(data.summary.supplier_total??0)} artículos de proveedores · {number(data.summary.supplier_already_matched??0)} ya vinculados · {number(data.summary.supplier_examined??0)} pendientes examinados.</p>}
      <dl>{tiles.map(([value,label])=><div key={label}><dt>{label}</dt><dd>{number(value)}</dd></div>)}</dl>
      {!tiles.some(([value])=>!!value)&&<p>Sin cambios en el catálogo ni en los vínculos. Las referencias disponibles no produjeron nuevas coincidencias automáticas.</p>}
      <p className={data.summary.ai_status==='failed'?'danger-text':''}>{number(data.summary.ai_analyzed??0)} casos analizados por IA. {aiLabels[data.summary.ai_status||'']||''}</p>
      {data.summary.non_oem_skus!==undefined&&<p><strong>{number(data.summary.oem_skus??0)} SKU OEM · {number(data.summary.non_oem_skus)} SKU internos.</strong> Un artículo vinculado a un SKU no significa que su MAIN sea OEM.</p>}
    </section>}
    <p className="form-footnote">Las pestañas muestran el historial acumulado de coincidencias, no solo el último análisis.</p>
    {/* SIN COINCIDENCIA is the history before S6: shown only while it holds cases. */}
    <div className="admin-inventory-tabs" role="group" aria-label="Estado de coincidencias">{Object.entries(labels).filter(([value])=>value!=='unmatched'||status===value||!!data?.counts.unmatched).map(([value,label])=><button key={value} className={status===value?'selected':''} aria-pressed={status===value} onClick={()=>{setStatus(value);setKind('');setOffset(0);setTargets({});setLines({});setSearching({});setPicking({});}}>{label} · {data?.counts[value]??0}</button>)}</div>
    {data?.kinds&&<div className="admin-inventory-tabs matching-kinds" role="group" aria-label="Tipo de caso"><button className={!kind?'selected':''} aria-pressed={!kind} onClick={()=>{setKind('');setOffset(0);}}>TODOS · {kindTotal}</button>{Object.entries(data.kinds).filter(([value,count])=>count>0||kind===value).map(([value,count])=><button key={value} className={kind===value?'selected':''} aria-pressed={kind===value} onClick={()=>{setKind(value);setOffset(0);}}>{kindLabels[value]??value.toUpperCase()} · {count}</button>)}</div>}
    {!data&&<p role="status">Cargando coincidencias…</p>}
    {data&&!data.results.length&&<div className="notice">No hay coincidencias en este estado.</div>}
    <div className="inventory-ai-proposals">{data?.results.map(item=>{
      const key=`${item.id}:${item.fingerprint}`;
      const known=CASE_KINDS.includes(item.kind);
      return <article className={`inventory-ai-proposal ${item.status}`} key={key} aria-label={caseTitle(item)}>
        <div className="inventory-ai-proposal-heading"><strong>{item.supplier_name||(item.kind==='merge'?'LÍNEAS DEL CATÁLOGO':'FAMILIA DEL CATÁLOGO')}</strong><span className="matching-case-pills">{known&&<span className="pill">{kindLabels[item.kind]}</span>}{item.route==='lote'&&<span className="pill">LOTE · {batchLabels[item.batch_key||'']??item.batch_key}</span>}<span className="pill">{statusLabels[item.status]??item.status.toUpperCase()}</span></span></div>
        {known ? <CaseCard item={item} busy={!!busy} line={lines[key]??''} onLine={value=>setLines(current=>({...current,[key]:value}))}
            searching={!!searching[key]} onSearch={()=>setSearching(current=>({...current,[key]:true}))}
            picking={!!picking[key]} onPick={()=>setPicking(current=>({...current,[key]:!current[key]}))}
            onDecide={(decision,message)=>decide(item,decision,message)}/>
          : <>
            {item.sources.map(source=><p key={source.id||source.sku}><strong>{source.sku}</strong>{source.supplier_invent_id&&<small> · ID {source.supplier_invent_id}</small>}<br/>{source.description||'SIN DESCRIPCIÓN'}{!!source.references?.length&&<small><br/>REFERENCIAS DECLARADAS: {refList(source.references)}</small>}</p>)}
            <p>{item.reason}</p>
            {item.ai.reason&&<div className="notice"><strong>PROPUESTA IA: {item.ai.target_sku}</strong><p>{item.ai.reason}</p></div>}
            {item.ai.error&&<div className="notice error">{item.ai.error}</div>}
            {isOpen(item)&&<>
              <label>SKU interno de destino{item.kind==='catalog'?<strong> {item.target_sku} · {item.candidates[0]?.id?'EXISTENTE':'CREAR PADRE'}</strong>:<UppercaseInput aria-label={`Destino para ${item.sources[0]?.sku}`} value={targets[key]??item.target_sku} onChange={event=>setTargets({...targets,[key]:event.target.value})} placeholder="SKU INTERNO"/>}</label>
              {!!item.candidates.length&&<div className="admin-toolbar-actions">{item.candidates.map(candidate=><button className="button soft small" key={candidate.sku} title={candidate.description} onClick={()=>setTargets({...targets,[key]:candidate.sku})}><strong>{candidate.sku}</strong><small>{candidate.description}</small>{!!candidate.codes?.length&&<small>REFERENCIAS: {refList(candidate.codes)}</small>}</button>)}</div>}
              <div className="admin-toolbar-actions"><button className="button primary small" disabled={!!busy||!(targets[key]??item.target_sku)} onClick={()=>legacy(item,'approve')}>Confirmar coincidencia</button><button className="button soft small" disabled={!!busy} onClick={()=>legacy(item,'dismiss')}>Mantener separado</button></div>
            </>}
          </>}
      </article>;
    })}</div>
    <div className="admin-toolbar-actions"><button className="button soft small" disabled={!offset||!!busy} onClick={()=>setOffset(Math.max(0,offset-30))}>Anterior</button><span>{data?.count??0} casos</span><button className="button soft small" disabled={data?.next_offset==null||!!busy} onClick={()=>setOffset(data!.next_offset!)}>Siguiente</button></div>
  </Modal>;
}

// One card per S6 kind (plan S6.3). Variants waiting on a code change or a variant case are out of stock, so each one is decidable here.
function CaseCard({item,busy,line,onLine,searching,onSearch,picking,onPick,onDecide}:{item:Case;busy:boolean;line:string;onLine:(value:string)=>void;
    searching:boolean;onSearch:()=>void;picking:boolean;onPick:()=>void;onDecide:(decision:Decision,message:string)=>void}) {
  const choices = caseLines(item);
  const proposed = choices.find(row=>row.sku===item.target_sku)?.id ?? '';
  const aiLine = item.ai.target_sku ? choices.find(row=>row.sku===item.ai.target_sku)?.id : undefined;
  const source = item.sources[0];
  const evidence = Object.entries(evidenceLabels).map(([field,label])=>[label,evidenceText(item.evidence?.[field])] as const).filter(([,text])=>text);
  const lineChoices = (legend:string,selected:string,searchable:boolean) => <fieldset className="matching-lines"><legend>{legend}</legend>
    {choices.map(row=><label key={row.id} className={`matching-line${selected===row.id?' selected':''}`}>
      <input type="radio" name={`linea-${item.id}`} value={row.id} checked={selected===row.id} disabled={!isOpen(item)||busy} onChange={()=>onLine(row.id)}/>
      <span><strong>{row.sku}{row.estado&&row.estado!=='publicada'&&<em className="pill">{estadoLabels[row.estado]??row.estado.toUpperCase()}</em>}{row.id===aiLine&&<em className="pill">PROPUESTA IA</em>}</strong>
        <small>{row.description||(row.name!==row.sku?row.name:'')||'SIN DESCRIPCIÓN'}</small>{!!row.codes?.length&&<small>REFERENCIAS: {refList(row.codes)}</small>}</span>
    </label>)}
    {searchable&&isOpen(item)&&(searching
      ? <PartPicker label="línea OEM" value={choices.some(row=>row.id===selected)?'':selected} onChange={onLine}/>
      : <button type="button" className="button text small" disabled={busy} onClick={onSearch}><Search size={14} aria-hidden="true"/>Buscar otra línea OEM</button>)}
  </fieldset>;
  const itemSummary = source&&<p><strong>{source.sku}</strong>{source.brand&&<> · {source.brand}</>}{source.supplier_invent_id&&<small> · ID {source.supplier_invent_id}</small>}<br/>{source.description||'SIN DESCRIPCIÓN'}
    {!!source.references?.length&&item.kind!=='reference'&&<small><br/>REFERENCIAS DECLARADAS: {refList(source.references)}</small>}
    {item.reference?.line_sku&&<small><br/>LÍNEA OEM DEL ARTÍCULO: {item.reference.line_sku}</small>}</p>;
  const why = <>{item.reason&&<p>{item.reason}</p>}{item.guard&&<p className="matching-guard">{guardLabels[item.guard]??item.guard}</p>}
    {evidence.map(([label,text])=><p key={label} className="matching-evidence"><small>{label}: {text}</small></p>)}
    {item.ai.reason&&<div className="notice"><strong>PROPUESTA IA{item.ai.target_sku?`: ${item.ai.target_sku}`:''}</strong><p>{item.ai.reason}</p></div>}
    {item.ai.error&&<div className="notice error">{item.ai.error}</div>}</>;
  const hold = <p className="matching-hold">Mientras se decide, este artículo no cuenta en las existencias.</p>;
  const say = (action:Decision['action']) => done[`${item.kind}:${action}`]??done[action];

  if (item.kind==='code_change') {
    const fresh = item.new_code||source?.codigo_pendiente||'';
    const freshBrand = item.new_brand||source?.brand_pendiente||source?.brand||'';
    return <>
      {itemSummary}
      <div className="matching-code-change" role="group" aria-label="Cambio de código">
        <p><small>CÓDIGO ACTUAL</small><strong>{source?.sku}</strong><span>{source?.brand||'SIN MARCA'}</span></p>
        <ArrowRight size={18} aria-hidden="true"/>
        <p><small>CÓDIGO NUEVO</small><strong>{fresh}</strong><span>{freshBrand||'SIN MARCA'}</span></p>
      </div>
      {!!choices.length&&<p className="matching-evidence"><small>EL CÓDIGO NUEVO YA ESTÁ EN: {choices.map(row=>row.sku).join(' · ')}</small></p>}
      {why}{isOpen(item)&&<>{hold}<div className="admin-toolbar-actions">
        <button className="button primary small" disabled={busy} onClick={()=>onDecide({action:'approve'},say('approve'))}>Aprobar</button>
        <button className="button soft small" disabled={busy} onClick={()=>onDecide({action:'dismiss'},say('dismiss'))}>Rechazar</button>
      </div></>}
    </>;
  }
  if (item.kind==='reference') {
    const chosen = picking ? line : '';
    return <>
      {itemSummary}
      <p className="matching-code"><small>REFERENCIA DECLARADA</small><strong>{item.new_code}</strong><span>{item.new_brand||'TODAS LAS MARCAS'}</span></p>
      {why}
      {isOpen(item)&&<>
        {picking&&lineChoices('Línea OEM que recibe la referencia',chosen,true)}
        <div className="admin-toolbar-actions">
          <button className="button primary small" disabled={busy||(picking&&!chosen)} onClick={()=>onDecide(chosen?{action:'approve',line:chosen}:{action:'approve'},say('approve'))}>{picking?'Aprobar en la línea elegida':'Aprobar'}</button>
          <button className="button soft small" disabled={busy} aria-pressed={picking} onClick={onPick}>{picking?'Cancelar elección':'Elegir línea'}</button>
          <button className="button soft small" disabled={busy} onClick={()=>onDecide({action:'dismiss'},say('dismiss'))}>Rechazar</button>
        </div>
      </>}
    </>;
  }
  if (item.kind==='merge') {
    const keep = isOpen(item) ? line||proposed||choices[0]?.id||'' : proposed;  // decided: only the line it was proposed to keep
    return <>
      {why}
      {lineChoices(isOpen(item)?'Línea OEM que se conserva':'Líneas OEM',keep,false)}
      {isOpen(item)&&<div className="admin-toolbar-actions">
        <button className="button primary small" disabled={busy||choices.length<2||!keep} onClick={()=>onDecide({action:'merge',line:keep},say('merge'))}>Unificar</button>
        <button className="button soft small" disabled={busy} onClick={()=>onDecide({action:'keep_apart'},say('keep_apart'))}>Mantener separadas</button>
      </div>}
    </>;
  }
  // variant and line: the item waits for a line, chosen by id (Vincular), or keeps its code as its own (Separar referencia).
  const chosen = line||proposed;
  return <>
    {itemSummary}{why}
    {lineChoices(isOpen(item)?'Línea OEM de destino':'Líneas OEM propuestas',chosen,true)}
    {isOpen(item)&&<>{hold}<div className="admin-toolbar-actions">
      <button className="button primary small" disabled={busy||!chosen} onClick={()=>onDecide({action:'approve',line:chosen},say('approve'))}>Vincular</button>
      <button className="button soft small" disabled={busy} onClick={()=>onDecide({action:'separar_referencia'},say('separar_referencia'))}>Separar referencia</button>
    </div></>}
  </>;
}
