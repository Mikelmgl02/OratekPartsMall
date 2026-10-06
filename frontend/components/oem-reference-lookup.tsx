'use client';

import { useState } from 'react';
import { Search, LoaderCircle, Plus } from 'lucide-react';
import { ManagedPart, PartReference, request } from '@/lib/types';
import { UppercaseInput } from './uppercase-field';

type Suggestion = PartReference & { relationship: 'equivalent' | 'component' | 'uncertain'; source_url: string; reason: string };
type Lookup = { references: Suggestion[]; note: string; cached: boolean; sources: { url: string; title: string }[]; search_suggestions: string; metrics: {total_tokens?: number} };
const relationLabel = { equivalent: 'POSIBLE EQUIVALENCIA', component: 'REFERENCIA AL CONJUNTO', uncertain: 'POR VERIFICAR' };

export default function OEMReferenceLookup({ part, onAdd }: { part: ManagedPart; onAdd: (rows: PartReference[]) => void }) {
  const companies = part.codes.filter(row => row.ref_type === 'company' || row.kind === 'manufacturer');
  const inferred = companies.find(row => !/-FEB(?:EST)?$/.test(row.code)) || companies[0];
  const [company, setCompany] = useState(inferred?.brand || (/-FEB(?:EST)?$/.test(part.sku) ? 'FEBEST' : ''));
  const [code, setCode] = useState(inferred?.code || part.sku.replace(/-FEB(?:EST)?$/, ''));
  const [result, setResult] = useState<Lookup | null>(null);
  const [busy, setBusy] = useState(false); const [error, setError] = useState('');
  const [selected, setSelected] = useState<number[]>([]); const [reviewed, setReviewed] = useState(false);
  const [added, setAdded] = useState(false);
  function reset() { setResult(null); setSelected([]); setReviewed(false); setAdded(false); }
  async function search() {
    reset(); setBusy(true); setError('');
    try { setResult(await request<Lookup>(`/api/management/catalog/${part.id}/oem-lookup`, {method:'POST', body:JSON.stringify({ company, code })})); }
    catch(error) { setError(error instanceof Error ? error.message : 'No se pudo consultar el OEM.'); }
    finally { setBusy(false); }
  }
  return <details className="oem-lookup"><summary>Buscar referencias OEM con IA</summary><div className="stack-form">
    <p>Consulta fuentes del fabricante para este artículo. Reutilizamos las búsquedas guardadas para ahorrar tokens. Las propuestas se revisan antes de agregarlas.</p>
    <div className="form-row"><label>Empresa del código<UppercaseInput disabled={busy} value={company} onChange={event => {setCompany(event.target.value); reset();}} maxLength={120} placeholder="EJ.: FEBEST"/></label><label>Código de empresa<UppercaseInput disabled={busy} value={code} onChange={event => {setCode(event.target.value); reset();}} maxLength={120}/></label></div>
    <button type="button" className="button soft" disabled={busy || !code.trim()} onClick={search}>{busy ? <LoaderCircle size={16} className="spin"/> : <Search size={16}/>} {busy ? 'Consultando fuentes…' : 'Buscar OEM'}</button>
    {error && <div role="alert" className="notice error">{error}</div>}
    {result && <><p>{result.note}</p><small>{result.cached ? 'BÚSQUEDA GUARDADA · SIN NUEVA LLAMADA A IA' : `BÚSQUEDA CON FUENTES · ${result.metrics.total_tokens ?? 0} TOKENS`}</small>
      {!result.references.length && <p>No hay referencias OEM propuestas. Puedes registrar una referencia verificada en la biblioteca de equivalencias.</p>}
      {result.references.map((row, index) => <div key={`${row.brand}:${row.code}`} className="oem-result"><label className="admin-checkbox"><input type="checkbox" disabled={row.relationship !== 'equivalent' || added} checked={selected.includes(index)} onChange={event => {setSelected(values => event.target.checked ? [...values, index] : values.filter(value => value !== index)); setReviewed(false);}}/><strong>{row.code} · {row.brand}</strong></label><span className={`status ${row.relationship === 'equivalent' ? '' : 'review'}`}>{relationLabel[row.relationship]}</span><p>{row.reason}</p><a href={row.source_url} target="_blank" rel="noopener noreferrer">Revisar ficha de origen ↗</a></div>)}
      {!!result.sources.length && <details><summary>Fuentes consultadas</summary><ul>{result.sources.map((source, index) => <li key={index}><a href={source.url} target="_blank" rel="noopener noreferrer">{source.title} ↗</a></li>)}</ul></details>}
      {result.search_suggestions && <iframe title="Sugerencias de búsqueda de Google" sandbox="allow-popups allow-popups-to-escape-sandbox" referrerPolicy="no-referrer" srcDoc={`<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src https:">${result.search_suggestions}`} className="oem-search-suggestions"/>}
      {!!result.references.length && <><label className="admin-checkbox"><input type="checkbox" checked={reviewed} disabled={!selected.length || added} onChange={event => setReviewed(event.target.checked)}/>Verifiqué en las fuentes que las referencias seleccionadas identifican la misma pieza, con sus medidas y contenido.</label><button type="button" className="button soft" disabled={!reviewed || !selected.length || added} onClick={() => {onAdd(selected.map(index => { const row=result.references[index]; return {brand:row.brand, code:row.code, ref_type:'oem', reference_source:row.reference_source}; }));setAdded(true);}}><Plus size={16}/>{added ? 'Agregadas al formulario · guarda el SKU para aplicar' : `Agregar ${selected.length} referencias revisadas al formulario`}</button></>}
    </>}
  </div></details>;
}
