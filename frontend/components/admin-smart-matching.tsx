'use client';
import { useCallback, useEffect, useRef, useState } from 'react';
import { BrainCircuit, RefreshCw, LoaderCircle, Check } from 'lucide-react';
import Modal from './modal';
import { UppercaseInput } from './uppercase-field';
import { request } from '@/lib/types';

type Row = {id:string;sku:string;description:string;supplier_invent_id?:string;references?:{brand:string;code:string}[];codes?:{brand:string;code:string}[]};
type Case = {id:string;kind:string;sources:Row[];candidates:Row[];target_sku:string;reason:string;status:string;fingerprint:string;supplier_name:string;ai:{target_sku?:string;reason?:string;error?:string;confidence?:number}};
type Summary = {catalog_count?:number; oem_skus?:number; non_oem_skus?:number; supplier_total?:number; supplier_already_matched?:number; supplier_examined?:number; promoted_oems?:number; grouped_skus?:number; created_parents?:number; matched_items?:number; ai_analyzed?:number; ai_status?:string};
type Overview = {running:boolean;queued:boolean;state?:string;stage?:string;last_run:string|null;summary:Summary;error:string;counts:Record<string,number>;count:number;next_offset:number|null;results:Case[]};
const labels:Record<string,string> = {review:'POR REVISAR',unmatched:'SIN COINCIDENCIA',applied:'APLICADAS',dismissed:'DESCARTADAS'};
const stages:Record<string,string> = {catalog:'Comparando SKU y referencias del catálogo…',parents:'Buscando familias de artículos sin SKU principal…',suppliers:'Vinculando artículos de proveedores pendientes…',ai:'Revisando casos ambiguos con IA…'};
const aiLabels:Record<string,string> = {no_pending_cases:'IA no utilizada: no hay casos ambiguos de proveedores pendientes de analizar.',not_configured:'IA no disponible: falta configurar el proveedor.',disabled:'IA desactivada para esta ejecución.',failed:'La IA no pudo completar este lote. Las coincidencias automáticas se conservaron; puedes reintentar.',completed:'La IA generó propuestas para revisión.'};
const number = (value:number) => value.toLocaleString('es-PA');

export default function SmartMatching({onClose,onSaved}:{onClose:()=>void;onSaved:()=>void}) {
  const requestId = useRef(0);
  const lastRun = useRef<string|null|undefined>(undefined);
  const [data,setData] = useState<Overview|null>(null);
  const [status,setStatus] = useState('review');
  const [offset,setOffset] = useState(0);
  const [targets,setTargets] = useState<Record<string,string>>({});
  const [busy,setBusy] = useState('');
  const [error,setError] = useState('');
  const [notice,setNotice] = useState('');
  const load = useCallback(async()=>{
    const id=++requestId.current;
    try {const result=await request<Overview>(`/api/management/matching?status=${status}&offset=${offset}`);if(id===requestId.current)setData(result);}
    catch(error) {setError(error instanceof Error ? error.message : 'No se pudo cargar el análisis.');}
  },[status,offset]);
  useEffect(()=>{let active=true; const update=()=>{if(active)void load();};update();const timer=setInterval(update,5000);return()=>{active=false;++requestId.current;clearInterval(timer);};},[load]);
  useEffect(()=>{if(data){if(lastRun.current!==undefined&&data.last_run!==lastRun.current)onSaved();lastRun.current=data.last_run;}},[data,onSaved]);
  async function analyze() {
    setBusy('analyze');setError('');
    try {await request('/api/management/matching',{method:'POST',body:JSON.stringify({retry_ai:true})});setNotice('');await load();}
    catch(error) {setError(error instanceof Error ? error.message : 'No se pudo iniciar el análisis.');}
    finally {setBusy('');}
  }
  async function review(item:Case,action:'approve'|'dismiss') {
    setBusy(item.id);setError('');
    try {
      await request(`/api/management/matching/${item.id}`,{method:'POST',body:JSON.stringify({action,fingerprint:item.fingerprint,target_sku:targets[`${item.id}:${item.fingerprint}`]??item.target_sku})});
      setNotice(action==='approve'?'Coincidencia aprobada y guardada para próximas cargas.':'Propuesta descartada. No se repetirá mientras sus datos sean iguales.');
      await load();onSaved();
    } catch(error) {setError(error instanceof Error ? error.message : 'No se pudo guardar la revisión.');}
    finally {setBusy('');}
  }
  return <Modal title="Agrupación inteligente" wide className="smart-matching-modal" onClose={onClose}>
    <p className="modal-description">Este análisis compara los SKU y alternos ya registrados, agrupa coincidencias fuertes y busca un destino para los artículos de proveedores pendientes. Conserva los vínculos de los artículos que ya están asociados.</p>
    <div className="admin-toolbar-actions"><button className="button primary" disabled={!!busy||data?.running||data?.queued} onClick={analyze}><BrainCircuit size={17}/>{data?.running?'Analizando…':data?.queued?'En cola…':'Analizar catálogo y proveedores'}</button><button className="button soft" onClick={load}><RefreshCw size={16}/>Actualizar</button></div>
    <p className="form-footnote">La IA solo interviene si quedan casos ambiguos de proveedores: hasta 20 por ejecución, con propuestas para revisar. Este botón no busca OEM en internet ni clasifica categorías.</p>
    <div className="notice matching-oem-help"><strong>¿BUSCAS EL OEM DE UN CÓDIGO DE EMPRESA?</strong><p>Abre Editar SKU → Buscar referencias OEM con IA. Revisa la fuente y agrega la equivalencia; al elegirla como MAIN se conserva el código de empresa como alterno y se marca el SKU como OEM.</p></div>
    {error&&<div className="notice error" role="alert">{error}</div>}{data?.error&&<div className="notice error" role="alert">{data.error}</div>}{notice&&<div className="notice success" role="status">{notice}</div>}
    {(data?.running||data?.queued||busy==='analyze')&&<div className="notice matching-progress" role="status"><LoaderCircle size={17} className="spin"/> {data?.running ? stages[data.stage||'']||'Analizando coincidencias…' : data?.state==='waiting_import' ? 'Esperando a que termine la importación del catálogo…' : data?.state==='retrying' ? 'Reintento pendiente…' : 'Análisis en cola…'}<p>Continuará aunque cierres esta ventana.</p></div>}
    {data?.last_run&&<section className="matching-run-summary" aria-label="Resultado del último análisis">
      <strong><Check size={17}/> {data.running||data.queued?'ÚLTIMO ANÁLISIS COMPLETADO':'ANÁLISIS COMPLETADO'}</strong>
      <small>{new Date(data.last_run).toLocaleString('es-PA')}</small>
      {data.summary.catalog_count!==undefined&&<p>{number(data.summary.catalog_count)} SKU en el catálogo al iniciar · {number(data.summary.supplier_total??0)} artículos de proveedores · {number(data.summary.supplier_already_matched??0)} ya vinculados · {number(data.summary.supplier_examined??0)} pendientes examinados.</p>}
      <dl>{([[data.summary.matched_items??0,'ARTÍCULOS VINCULADOS'],[data.summary.grouped_skus??0,'SKU AGRUPADOS'],[data.summary.created_parents??0,'PADRES CREADOS'],[data.summary.promoted_oems??0,'MAIN CAMBIADOS A OEM']] as const).map(([value,label])=><div key={label}><dt>{label}</dt><dd>{number(value)}</dd></div>)}</dl>
      {![data.summary.matched_items,data.summary.grouped_skus,data.summary.created_parents,data.summary.promoted_oems].some(value=>!!value)&&<p>Sin cambios en el catálogo ni en los vínculos. Las referencias disponibles no produjeron nuevas coincidencias automáticas.</p>}
      <p className={data.summary.ai_status==='failed'?'danger-text':''}>{number(data.summary.ai_analyzed??0)} casos analizados por IA. {aiLabels[data.summary.ai_status||'']||''}</p>
      {data.summary.non_oem_skus!==undefined&&<p><strong>{number(data.summary.oem_skus??0)} SKU marcados como OEM · {number(data.summary.non_oem_skus)} sin marcar.</strong> Un artículo vinculado a un SKU no significa que su MAIN sea OEM.</p>}
    </section>}
    <p className="form-footnote">Las pestañas muestran el historial acumulado de coincidencias, no solo el último análisis.</p>
    <div className="admin-inventory-tabs" role="group" aria-label="Estado de coincidencias">{Object.entries(labels).map(([value,label])=><button key={value} className={status===value?'selected':''} aria-pressed={status===value} onClick={()=>{setStatus(value);setOffset(0);setTargets({});}}>{label} · {data?.counts[value]??0}</button>)}</div>
    {!data&&<p role="status">Cargando coincidencias…</p>}
    {data&&!data.results.length&&<div className="notice">No hay coincidencias en este estado.</div>}
    <div className="inventory-ai-proposals">{data?.results.map(item=><article className="inventory-ai-proposal" key={`${item.id}:${item.fingerprint}`}>
      <div className="inventory-ai-proposal-heading"><strong>{item.supplier_name||'FAMILIA DEL CATÁLOGO'}</strong><span className="pill">{labels[item.status]}</span></div>
      {item.sources.map(source=><p key={source.id||source.sku}><strong>{source.sku}</strong>{source.supplier_invent_id&&<small> · ID {source.supplier_invent_id}</small>}<br/>{source.description||'SIN DESCRIPCIÓN'}{!!source.references?.length&&<small><br/>REFERENCIAS DECLARADAS: {source.references.map(ref=>[ref.brand,ref.code].filter(Boolean).join(': ')).join(' · ')}</small>}</p>)}
      <p>{item.reason}</p>
      {item.ai.reason&&<div className="notice"><strong>PROPUESTA IA: {item.ai.target_sku}</strong><p>{item.ai.reason}</p></div>}
      {item.ai.error&&<div className="notice error">{item.ai.error}</div>}
      {['review','unmatched'].includes(item.status)&&<>
        <label>SKU interno de destino{item.kind==='catalog'?<strong> {item.target_sku} · {item.candidates[0]?.id?'EXISTENTE':'CREAR PADRE'}</strong>:<UppercaseInput aria-label={`Destino para ${item.sources[0]?.sku}`} value={targets[`${item.id}:${item.fingerprint}`]??item.target_sku} onChange={event=>setTargets({...targets,[`${item.id}:${item.fingerprint}`]:event.target.value})} placeholder="SKU INTERNO"/>}</label>
        {!!item.candidates.length&&<div className="admin-toolbar-actions">{item.candidates.map(candidate=><button className="button soft small" key={candidate.sku} title={candidate.description} onClick={()=>setTargets({...targets,[`${item.id}:${item.fingerprint}`]:candidate.sku})}><strong>{candidate.sku}</strong><small>{candidate.description}</small>{!!candidate.codes?.length&&<small>REFERENCIAS: {candidate.codes.map(ref=>[ref.brand,ref.code].filter(Boolean).join(': ')).join(' · ')}</small>}</button>)}</div>}
        <div className="admin-toolbar-actions"><button className="button primary small" disabled={!!busy||!(targets[`${item.id}:${item.fingerprint}`]??item.target_sku)} onClick={()=>review(item,'approve')}>Confirmar coincidencia</button><button className="button soft small" disabled={!!busy} onClick={()=>review(item,'dismiss')}>Mantener separado</button></div>
      </>}
    </article>)}</div>
    <div className="admin-toolbar-actions"><button className="button soft small" disabled={!offset||!!busy} onClick={()=>setOffset(Math.max(0,offset-30))}>Anterior</button><span>{data?.count??0} casos</span><button className="button soft small" disabled={data?.next_offset==null||!!busy} onClick={()=>setOffset(data!.next_offset!)}>Siguiente</button></div>
  </Modal>;
}
