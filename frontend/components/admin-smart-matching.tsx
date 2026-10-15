'use client';
import { useCallback, useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { ArrowRight, BrainCircuit, RefreshCw, LoaderCircle, Check } from 'lucide-react';
import Modal from './modal';
import { request } from '@/lib/types';
import { CASE_KINDS, caseKindLabels, caseStatusLabels } from '@/lib/line-types';

type Summary = {catalog_count?:number; oem_skus?:number; non_oem_skus?:number; supplier_total?:number; supplier_already_matched?:number; supplier_examined?:number; marked_exact_oems?:number; grouped_skus?:number; matched_items?:number; placed_items?:number; ai_analyzed?:number; ai_status?:string};
type Overview = {running:boolean;queued:boolean;state?:string;stage?:string;last_run:string|null;summary:Summary;error:string;counts:Record<string,number>;kinds?:Record<string,number>;count:number};

const stages:Record<string,string> = {catalog:'Revisando referencias principales y casos de unificación…',oem:'Buscando números OEM del catálogo…',suppliers:'Vinculando artículos de proveedores por sus referencias…',proposals:'Llevando artículos sin línea OEM a Productos nuevos…',ai:'Revisando casos ambiguos con IA…'};
const aiLabels:Record<string,string> = {no_pending_cases:'IA no utilizada: no hay casos ambiguos pendientes de analizar.',not_configured:'IA no disponible: falta configurar el proveedor.',disabled:'IA desactivada para esta ejecución.',failed:'La IA no pudo completar este lote. Las coincidencias automáticas se conservaron; puedes reintentar.',completed:'La IA generó propuestas para revisión.'};
const number = (value:number) => value.toLocaleString('es-PA');
// The queue's kind order; the kinds from before S6 last.
const order = (kind:string) => { const index=(CASE_KINDS as string[]).indexOf(kind); return index<0?CASE_KINDS.length:index; };

// Agrupación inteligente (plan S8.4): the matching pass's state, its last run and what it left to decide. The cases themselves are decided
// in Casos de coincidencia (Administración → Catálogo), which replaced the list this window had.
export default function SmartMatching({onClose,onSaved}:{onClose:()=>void;onSaved:()=>void}) {
  const requestId = useRef(0);
  const lastRun = useRef<string|null|undefined>(undefined);
  const [data,setData] = useState<Overview|null>(null);
  const [busy,setBusy] = useState('');
  const [error,setError] = useState('');
  const load = useCallback(async()=>{
    const id=++requestId.current;
    try {const result=await request<Overview>('/api/management/matching?status=review&offset=0');if(id===requestId.current)setData(result);}
    catch(error) {setError(error instanceof Error ? error.message : 'No se pudo cargar el análisis.');}
  },[]);
  useEffect(()=>{let active=true; const update=()=>{if(active)void load();};update();const timer=setInterval(update,5000);return()=>{active=false;++requestId.current;clearInterval(timer);};},[load]);
  useEffect(()=>{if(data){if(lastRun.current!==undefined&&data.last_run!==lastRun.current)onSaved();lastRun.current=data.last_run;}},[data,onSaved]);
  async function analyze() {
    setBusy('analyze');setError('');
    try {await request('/api/management/matching',{method:'POST',body:JSON.stringify({retry_ai:true})});await load();}
    catch(error) {setError(error instanceof Error ? error.message : 'No se pudo iniciar el análisis.');}
    finally {setBusy('');}
  }
  const tiles = [[data?.summary.matched_items??0,'ARTÍCULOS VINCULADOS'],
    ...(data?.summary.placed_items!==undefined?[[data.summary.placed_items,'LLEVADOS A PRODUCTOS NUEVOS'] as const]:[]),
    ...(data?.summary.grouped_skus?[[data.summary.grouped_skus,'SKU AGRUPADOS'] as const]:[]),
    ...(data?.summary.marked_exact_oems?[[data.summary.marked_exact_oems,'SKU MARCADOS OEM POR LISTA'] as const]:[])] as const;
  const kinds = Object.entries(data?.kinds??{}).filter(([,count])=>count>0).sort(([a],[b])=>order(a)-order(b));
  return <Modal title="Agrupación inteligente" wide className="smart-matching-modal" onClose={onClose}>
    <p className="modal-description">Este análisis vincula los artículos de los proveedores a una línea OEM por sus referencias: el mismo código y marca, o el código base cuando el sufijo es una marca conocida (58411-1R000-GMB → 58411-1R000) y la descripción coincide. KAIYI y CHERY comparten una línea solo cuando el código es el mismo. Lo que no puede decidir queda aquí como un caso; los artículos sin línea OEM pasan a Productos nuevos.</p>
    <div className="admin-toolbar-actions"><button className="button primary" disabled={!!busy||data?.running||data?.queued} onClick={analyze}><BrainCircuit size={17}/>{data?.running?'Analizando…':data?.queued?'En cola…':'Analizar catálogo y proveedores'}</button><button className="button soft" onClick={load}><RefreshCw size={16}/>Actualizar</button></div>
    <p className="form-footnote">La IA solo ordena los casos de elegir línea OEM y de unificación: hasta 20 por ejecución, como propuestas para revisar. Este botón no busca OEM en internet ni clasifica categorías.</p>
    <div className="notice matching-oem-help"><strong>¿BUSCAS EL OEM DE UN CÓDIGO DE EMPRESA?</strong><p>Abre la línea OEM → Referencias → Buscar referencias OEM con IA. Revisa la fuente y agrega la referencia; si es la pieza misma, hazla principal y el SKU pasa a ser ese código.</p></div>
    {error&&<div className="notice error" role="alert">{error}</div>}{data?.error&&<div className="notice error" role="alert">{data.error}</div>}
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
    <section className="matching-pending" aria-label="Casos por decidir">
      <strong>CASOS POR DECIDIR · {number(data?.counts.review??0)}</strong>
      {kinds.length ? <ul>{kinds.map(([kind,count])=><li key={kind}><span>{caseKindLabels[kind]??kind.toUpperCase()}</span><strong>{number(count)}</strong></li>)}</ul> : data && <p>No hay casos por decidir.</p>}
      <p className="form-footnote">{Object.entries(data?.counts??{}).filter(([status])=>status!=='review').map(([status,count])=>`${caseStatusLabels[status]??status.toUpperCase()} · ${number(count)}`).join(' · ')}</p>
      <Link className="button primary small" href="/administracion?seccion=casos">Abrir Casos de coincidencia<ArrowRight size={15} aria-hidden="true"/></Link>
    </section>
  </Modal>;
}
