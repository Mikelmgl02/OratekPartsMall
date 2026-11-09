'use client';
import { useEffect, useRef, useState } from 'react';
import { Search, LoaderCircle } from 'lucide-react';
import { request } from '@/lib/types';
import type { OEMLine, Reference } from '@/lib/line-types';
import type { OEMSuggestion } from './oem-reference-lookup';
import OEMPromotion from './admin-oem-promotion';
import { UppercaseInput } from './uppercase-field';

type Research = { references: OEMSuggestion[]; research_token?: string; catalog_conflict?: boolean; note: string; cached: boolean; sources: { url: string; title: string }[] };
export default function ClassificationWorkbench({ line, onSaved, onBusy }: { line: OEMLine; onSaved: (line: OEMLine) => void; onBusy: (busy: boolean) => void }) {
  const [refs, setRefs] = useState<Reference[]>([]);
  const [refsError, setRefsError] = useState('');
  const [company, setCompany] = useState(line.main?.brand ?? '');
  const [code, setCode] = useState(line.sku);
  const [result, setResult] = useState<Research | null>(null);
  const [selected, setSelected] = useState<number[]>([]);
  const [primary, setPrimary] = useState<number | null>(null);
  const [review, setReview] = useState<{ rows: OEMSuggestion[]; primary: OEMSuggestion; token: string; revision: number } | null>(null);
  const [searching, setSearching] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const searchController = useRef<AbortController | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    request<Reference[]>(`/api/management/lines/${line.id}/references`, { signal: controller.signal }).then(setRefs).catch(error => { if (!controller.signal.aborted) setRefsError(error.message); });
    return () => { controller.abort(); searchController.current?.abort(); };
  }, [line.id]);
  function reset() { setReview(null); setResult(null); setSelected([]); setPrimary(null); }
  async function search() {
    searchController.current?.abort(); const controller = new AbortController(); searchController.current = controller;
    reset(); setError(''); setSearching(true);
    try { setResult(await request<Research>(`/api/management/catalog/${line.id}/oem-lookup`, { method: 'POST', body: JSON.stringify({ company, code }), signal: controller.signal })); }
    catch (error) { if (!controller.signal.aborted) setError(error instanceof Error ? error.message : 'No se pudo investigar el código.'); }
    finally { if (!controller.signal.aborted) setSearching(false); }
  }
  const live = refs.filter(ref => ref.estado !== 'retirada');
  const knownOEM = live.filter(ref => ref.tipo === 'oem');
  const aftermarket = live.filter(ref => ref.tipo === 'aftermarket' && ref.code !== line.sku);
  const ownerMains = [...new Map((result?.references ?? []).filter(row => row.relationship === 'equivalent').flatMap(row => row.catalog_matches ?? [])
    .filter(owner => owner.main?.tipo === 'oem' && ['verificado', 'declarado', 'inferido'].includes(owner.main.estado)).map(owner => [owner.id, owner])).values()];
  const preserveMain = !result?.catalog_conflict && ownerMains.length === 1 ? ownerMains[0] : null;
  const eligible = (result?.references ?? []).flatMap((row, index) => row.relationship === 'equivalent' && !result?.catalog_conflict ? [index] : []);
  const selectedEligible = eligible.filter(index => selected.includes(index)).length;
  const allSelected = eligible.length > 0 && selectedEligible === eligible.length;
  const partlySelected = selectedEligible > 0 && !allSelected;
  function toggleAll(checked: boolean) { setSelected(checked ? eligible : []); if (!checked || (primary !== null && !eligible.includes(primary))) setPrimary(null); setReview(null); }
  function toggle(index: number, checked: boolean) { setSelected(values => checked ? [...values, index] : values.filter(value => value !== index)); if (!checked && primary === index) setPrimary(null); setReview(null); }
  function reviewSelection() {
    if (!result?.research_token || !selected.length || result.catalog_conflict) return;
    const primaryIndex = preserveMain ? selected[0] : primary;
    if (primaryIndex === null || !selected.includes(primaryIndex)) return;
    setReview(current => ({ rows: selected.map(index => result.references[index]), primary: result.references[primaryIndex], token: result.research_token!, revision: (current?.revision ?? 0) + 1 }));
  }
  return <div className="classification-workbench">
    <fieldset className="classification-research" disabled={searching || saving}>
      <legend>Investigar el código de origen</legend>
      <div className="form-row"><label>Código a investigar<UppercaseInput value={code} maxLength={120} onChange={event => { setCode(event.target.value); reset(); }}/></label><label>Marca o empresa<UppercaseInput value={company} maxLength={120} onChange={event => { setCompany(event.target.value); reset(); }} placeholder="Si se conoce"/></label></div>
      <button type="button" className="button soft" disabled={!code.trim()} onClick={() => void search()}>{searching ? <LoaderCircle size={16} className="spin"/> : <Search size={16}/>} {searching ? 'Consultando fuentes…' : 'Buscar OEM'}</button>
      <small>Consulta web con IA; puede consumir tokens. Buscar no modifica el catálogo.</small>
    </fieldset>
    {error && <p role="alert" className="notice error">{error}</p>}
    {result?.catalog_conflict && <p className="notice error" role="alert">Las equivalencias apuntan a fichas distintas. Revisa las fichas registradas; la aplicación está bloqueada.</p>}
    <div className="classification-columns">
      <section className="classification-pane classification-oem" aria-label="OEM">
        <header><span className="classification-kicker">Identidad de la pieza</span><h3>OEM</h3><p>Principal y referencias alternas OEM.</p></header>
        {result && <small>{result.cached ? 'Búsqueda guardada · catálogo comprobado ahora' : 'Resultados con fuentes'}</small>}
        <table className="classification-reference-table" aria-label="Referencias OEM"><thead><tr><th className="classification-check-col"><input className="classification-select" type="checkbox" aria-label="Seleccionar todo" title="Seleccionar todas las equivalencias habilitadas" checked={allSelected} ref={node => { if (node) node.indeterminate = partlySelected; }} disabled={saving || !eligible.length} onChange={event => toggleAll(event.target.checked)}/></th><th>Código OEM</th><th>Función</th><th>Fuentes y catálogo</th></tr></thead><tbody>
          {preserveMain && <tr className="classification-preserved"><td>✓</td><td><strong>{preserveMain.main?.code}</strong><small>{preserveMain.main?.brand}</small></td><td><strong>Principal existente</strong><small>Se conserva</small></td><td>Ficha {preserveMain.sku}<small>{preserveMain.description}</small></td></tr>}
          {knownOEM.map(ref => <tr key={`known-${ref.id}`}><td>✓</td><td><strong>{ref.code}</strong><small>{ref.brand || 'SIN MARCA'}</small></td><td>{ref.is_main ? 'Principal actual' : 'Alterna OEM registrada'}</td><td>{ref.sources.length ? ref.sources.map((source, i) => <small key={i}>{source.citation}</small>) : 'Sin fuente registrada'}</td></tr>)}
          {!result && !knownOEM.length && <tr><td colSpan={4} className="classification-empty">Aún no hay OEM confirmado para esta ficha. Busca para consultar propuestas con fuentes.</td></tr>}
          {result && !result.references.length && <tr><td colSpan={4} className="classification-empty">No se encontraron propuestas OEM verificables. La clasificación actual se conserva.</td></tr>}
          {result?.references.map((row, index) => <tr className={selected.includes(index) ? 'selected' : ''} key={`${row.brand}:${row.code}`}>
            <td><input className="classification-select" type="checkbox" aria-label={`Incluir OEM ${row.code}`} checked={selected.includes(index)} disabled={saving || !!result.catalog_conflict || row.relationship !== 'equivalent'} onChange={event => toggle(index, event.target.checked)}/></td>
            <td><strong>{row.code}</strong><small>{row.brand}</small><small>{row.relationship === 'equivalent' ? 'Equivalencia propuesta' : row.relationship === 'component' ? 'Conjunto · no se incluirá' : 'Incierta · no se incluirá'}</small></td>
            <td>{selected.includes(index) ? <>{!preserveMain && <label className="classification-pick"><input type="radio" name={`oem-principal-${line.id}`} checked={primary === index} disabled={saving} onChange={() => { setPrimary(index); setReview(null); }}/>Usar como principal</label>}{(preserveMain || primary !== index) && <small>Alterna OEM{!preserveMain && primary === null ? ' · puedes elegirla como principal' : ''}</small>}</> : <small>No seleccionada</small>}</td>
            <td><a href={row.source_url} target="_blank" rel="noopener noreferrer">Ver fuente ↗</a><details><summary>Evidencia</summary><p>{row.reason}</p></details>
              {row.catalog_matches?.map(owner => <div className="classification-registered" key={owner.id}><strong>Ya registrado · {owner.sku}</strong><small>Principal: {owner.main?.code ?? owner.sku} · {owner.main?.brand || 'SIN MARCA'}</small><small>{owner.references.some(ref => !ref.is_main) ? 'Coincidencia en alterna OEM' : 'Coincidencia en principal'}</small><small>{owner.description}</small></div>)}
            </td>
          </tr>)}
        </tbody></table>
        {!!result?.sources.length && <details><summary>Todas las fuentes consultadas</summary><ul>{result.sources.map((source, i) => <li key={i}><a href={source.url} target="_blank" rel="noopener noreferrer">{source.title}</a></li>)}</ul></details>}

      </section>
      <section className="classification-pane classification-aftermarket" aria-label="Referencias aftermarket">
        <header><span className="classification-kicker">Códigos asociados</span><h3>Referencias aftermarket</h3><p>Se conservan bajo la misma pieza OEM.</p></header>
        <table className="classification-reference-table" aria-label="Referencias aftermarket"><thead><tr><th>Código</th><th>Estado</th><th>Fuente</th></tr></thead><tbody>
          <tr><td><strong>{line.sku}</strong><small>{line.main?.brand || 'SIN MARCA'}</small></td><td><strong>Origen · se conserva</strong><small>{line.tipo === 'AFTERMARKET' ? 'Aftermarket registrado' : 'Pendiente: aftermarket solo al confirmar'}</small><small>Con artículos y existencias</small></td><td>{live.find(ref => ref.is_main)?.sources.length ? live.find(ref => ref.is_main)?.sources.map((source, i) => <small key={i}>{source.citation}</small>) : <small>Ficha de inventario · sin fuente documental registrada</small>}</td></tr>
          {aftermarket.map(ref => <tr key={ref.id}><td><strong>{ref.code}</strong><small>{ref.brand || 'SIN MARCA'}</small></td><td>Aftermarket registrado</td><td>{ref.sources.length ? ref.sources.map((source, i) => <small key={i}>{source.citation}</small>) : 'Sin fuente registrada'}</td></tr>)}
          {!aftermarket.length && <tr><td colSpan={3} className="classification-empty">No hay otras referencias aftermarket registradas para esta ficha.</td></tr>}
        </tbody></table>
        {refsError && <p className="notice error" role="alert">No se pudieron cargar las referencias registradas: {refsError}</p>}
        <p className="form-footnote">La búsqueda actual propone OEM; no investiga códigos aftermarket adicionales. Un OEM alterno permanece en el panel OEM. El tipo del código no determina la calidad del artículo físico.</p>
      </section>
    </div>
    {!review && <div className="classification-review-bar"><span><strong>{selected.length} OEM seleccionadas</strong>{!preserveMain && selected.length > 0 && primary === null && <small>Elige una principal entre las seleccionadas.</small>}</span><button type="button" className="button primary" disabled={!selected.length || !result?.research_token || !!result.catalog_conflict || (!preserveMain && primary === null)} onClick={reviewSelection}>Revisar selección</button></div>}
    {review && <OEMPromotion key={review.revision} line={line} proposal={review.primary} selections={review.rows} researchToken={review.token} onBusy={busy => { setSaving(busy); onBusy(busy); }} onSaved={onSaved}/>}
  </div>;
}
