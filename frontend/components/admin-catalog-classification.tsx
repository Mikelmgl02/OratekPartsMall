'use client';

import { useEffect, useRef, useState } from 'react';
import { BrainCircuit, Pause, Play } from 'lucide-react';
import { CatalogImportResult, request } from '@/lib/types';
import { UppercaseInput } from './uppercase-field';

type Suggestion = {
  sku: string; row: number; target_sku: string; category: string; subcategory: string;
  reason: string; confidence: number; needs_review: boolean;
  evidence: { source_reference: string; target_reference: string };
  candidate_skus: string[]; applied: boolean;
  applied_decision: { sku?: string; target_sku?: string; category?: string; subcategory?: string };
};
export type ClassificationResult = {
  configured: boolean; model: string; batch_size: number;
  completed_batches: number; total_batches: number; status: 'ready' | 'running' | 'completed';
  count: number; applied_count?: number; offset: number; next_offset: number | null; results: Suggestion[];
};
type Decision = Pick<Suggestion, 'sku' | 'target_sku' | 'category' | 'subcategory'>;

export default function CatalogClassification({ jobId, onUpdated, disabled, onBusyChange, onStatusChange }: {
  jobId: string; onUpdated: (result: CatalogImportResult) => void; disabled: boolean;
  onBusyChange?: (busy: boolean) => void;
  onStatusChange?: (result: ClassificationResult | null) => void;
}) {
  const [result, setResult] = useState<ClassificationResult | null>(null);
  const [busy, setBusy] = useState<'classify' | 'apply' | 'load' | null>('load');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [edits, setEdits] = useState<Record<string, Decision>>({});
  const [pauseRequested, setPauseRequested] = useState(false);
  const pause = useRef(false);
  const mounted = useRef(true);
  const url = `/api/management/catalog/import/jobs/${jobId}/classification/`;
  const busyCallback = useRef(onBusyChange);
  busyCallback.current = onBusyChange;
  const statusCallback = useRef(onStatusChange);
  statusCallback.current = onStatusChange;

  useEffect(() => { statusCallback.current?.(result); }, [result]);

  useEffect(() => {
    mounted.current = true;
    busyCallback.current?.(true);
    const controller = new AbortController();
    request<ClassificationResult>(url, { signal: controller.signal }).then(data => {
      if (mounted.current) setResult(data);
    }).catch(reason => {
      if (!controller.signal.aborted && mounted.current) setError(reason.message);
    }).finally(() => { if (mounted.current) { setBusy(null); busyCallback.current?.(false); } });
    return () => { mounted.current = false; pause.current = true; controller.abort(); busyCallback.current?.(false); };
  }, [url]);

  const locked = disabled || !!busy;
  const changeBusy = (value: typeof busy) => { setBusy(value); busyCallback.current?.(!!value); };
  const decision = (item: Suggestion): Decision => edits[item.sku] || {
    sku: item.sku, target_sku: item.target_sku, category: item.category, subcategory: item.subcategory,
  };
  const edit = (item: Suggestion, field: keyof Decision, value: string) => {
    setEdits(current => ({ ...current, [item.sku]: { ...decision(item), [field]: value.toUpperCase() } }));
  };
  const select = (sku: string, checked: boolean) => setSelected(current => {
    const next = new Set(current); if (checked) next.add(sku); else next.delete(sku); return next;
  });

  async function load(offset: number) {
    changeBusy('load'); setError(''); setNotice('');
    try { setResult(await request<ClassificationResult>(`${url}?offset=${offset}`)); setSelected(new Set()); setEdits({}); }
    catch (reason) { setError((reason as Error).message); }
    finally { changeBusy(null); }
  }

  async function classify() {
    if (!result) return;
    changeBusy('classify'); setError(''); setNotice(''); setPauseRequested(false); pause.current = false;
    let current = result;
    try {
      current = await request<ClassificationResult>(url);
      if (!mounted.current) return;
      setResult(current);
      while (current.completed_batches < current.total_batches && !pause.current && mounted.current) {
        current = await request<ClassificationResult>(url, { method: 'POST', body: JSON.stringify({ mode: 'classify', batch_index: current.completed_batches }) });
        if (!mounted.current) return;
        setResult(current); setSelected(new Set()); setEdits({});
      }
      if (mounted.current) setNotice(pause.current ? 'Clasificación pausada. El progreso está guardado.' : 'Clasificación terminada. Revisa las propuestas antes de aplicarlas.');
    } catch (reason) {
      if (mounted.current) {
        setError((reason as Error).message);
        // Recover the checkpoint if the provider or the network dropped a reply.
        try { setResult(await request<ClassificationResult>(url)); } catch { /* The last saved state remains visible. */ }
      }
    } finally { if (mounted.current) { changeBusy(null); setPauseRequested(false); } }
  }

  async function apply() {
    if (!result) return;
    const decisions = result.results.filter(item => selected.has(item.sku) && !item.applied).map(decision);
    if (!decisions.length) return;
    changeBusy('apply'); setError(''); setNotice('');
    try {
      const updated = await request<CatalogImportResult>(url, { method: 'POST', body: JSON.stringify({ mode: 'apply', decisions }) });
      onUpdated(updated);
      setResult(await request<ClassificationResult>(`${url}?offset=${result.offset}`));
      setSelected(new Set()); setEdits({});
      setNotice('Revisión aplicada a la vista previa. Confirma la importación para guardar los SKU y alternos.');
    } catch (reason) {
      setError((reason as Error).message);
      // A saved review can outlive a dropped response. Refresh both the review
      // rows and the import plan before allowing another confirmation.
      const recovered = await Promise.allSettled([
        request<ClassificationResult>(`${url}?offset=${result.offset}`),
        request<CatalogImportResult>(`/api/management/catalog/import/jobs/${jobId}/`),
      ]);
      if (recovered[0].status === 'fulfilled') {
        setResult(recovered[0].value); setSelected(new Set()); setEdits({});
      }
      if (recovered[1].status === 'fulfilled') onUpdated(recovered[1].value);
      const savedRows = recovered[0].status === 'fulfilled' ? recovered[0].value.results : [];
      if (recovered[0].status === 'fulfilled' && recovered[1].status === 'fulfilled' && decisions.every(item => savedRows.some(row => row.sku === item.sku && row.applied))) {
        setError(''); setNotice('La revisión quedó guardada. Recuperamos la vista previa de la importación.');
      }
    }
    finally { changeBusy(null); }
  }

  return <section className="catalog-classification" aria-label="Clasificación con IA">
    <div className="admin-toolbar-actions"><h3><BrainCircuit size={18}/> Clasificación con IA</h3>
      {result?.configured && result.completed_batches < result.total_batches && busy !== 'classify' &&
        <button type="button" className="button soft small" onClick={classify} disabled={locked}>
          <Play size={14}/>{result.completed_batches ? 'Reanudar clasificación' : 'Clasificar con IA'}
        </button>}
      {busy === 'classify' && <button type="button" className="button soft small" disabled={pauseRequested} onClick={() => { pause.current = true; setPauseRequested(true); }}><Pause size={14}/>{pauseRequested ? 'Pausando al terminar el lote…' : 'Pausar clasificación'}</button>}
    </div>
    <p className="fine-print">Leer el Excel prepara los datos; la IA se ejecuta al seleccionar «Clasificar con IA». Después debes revisar y aplicar sus propuestas para agrupar los SKU y asignar categorías.</p>
    {!result && !error && <p className="fine-print">Cargando clasificación…</p>}
    {result && !result.configured && <p className="notice">La IA aún no está configurada. Configura GEMINI_API_KEY en el servidor para clasificar; puedes importar el archivo sin IA.</p>}
    {result?.configured && !result.completed_batches && <p className="notice import-ai-status" role="status"><strong>Agrupación y categorías: IA sin ejecutar.</strong> Cada SKU distinto del Excel se guardará separado si importas ahora.</p>}
    {result?.configured && result.completed_batches > 0 && <p className="notice import-ai-status" role="status"><strong>{result.completed_batches < result.total_batches ? 'Clasificación parcial.' : 'Clasificación terminada.'}</strong> {result.applied_count ?? result.results.filter(item => item.applied).length} de {result.count} propuestas revisadas y aplicadas.{(result.applied_count ?? result.results.filter(item => item.applied).length) < result.count ? ' Las propuestas pendientes no cambiarán la importación.' : ''}</p>}
    {result?.configured && !result.completed_batches && <p className="fine-print">Al clasificar se enviarán a Gemini los códigos y las descripciones del archivo, en lotes de 50 SKU.</p>}
    {result && result.total_batches > 0 && <div className="import-progress" aria-live="polite">
      <p className="fine-print">{result.completed_batches.toLocaleString('es-PA')} de {result.total_batches.toLocaleString('es-PA')} lotes clasificados · {result.count.toLocaleString('es-PA')} propuestas para revisar</p>
      <progress aria-label="Progreso de clasificación" value={result.completed_batches} max={result.total_batches}/>
    </div>}
    {error && <p className="form-error" role="alert">{error}</p>}
    {notice && <p className="notice" role="status">{notice}</p>}
    {!!result?.results.length && <>
      <p className="fine-print">Selecciona únicamente las filas que revisaste. Puedes corregir el SKU de destino y las categorías antes de aplicarlas.</p>
      <div className="table-scroll"><table className="admin-table classification-table">
        <thead><tr><th>Revisado</th><th>SKU de origen</th><th>SKU de destino</th><th>Categoría</th><th>Propuesta</th></tr></thead>
        <tbody>{result.results.map(item => {
          const value = item.applied ? { sku: item.sku, target_sku: item.applied_decision.target_sku || item.target_sku, category: item.applied_decision.category || item.category, subcategory: item.applied_decision.subcategory || item.subcategory } : decision(item);
          return <tr key={item.sku}>
            <td>{item.applied ? <span className="pill">Aplicado</span> : <input type="checkbox" aria-label={`Revisión confirmada para ${item.sku}`} checked={selected.has(item.sku)} disabled={locked} onChange={event => select(item.sku, event.target.checked)}/>}</td>
            <td><strong>{item.sku}</strong><small>Fila {item.row}</small></td>
            <td><UppercaseInput aria-label={`SKU de destino para ${item.sku}`} maxLength={200} value={value.target_sku} disabled={locked || item.applied} onChange={event => edit(item, 'target_sku', event.target.value)}/><small>{value.target_sku === item.sku ? 'Conservar separado' : 'Agrupar como alterno'}</small>{!!item.candidate_skus.length && <small>Candidatos: {item.candidate_skus.join(', ')}</small>}</td>
            <td><UppercaseInput aria-label={`Categoría para ${item.sku}`} placeholder="CATEGORÍA" maxLength={120} value={value.category} disabled={locked || item.applied} onChange={event => edit(item, 'category', event.target.value)}/><UppercaseInput aria-label={`Subcategoría para ${item.sku}`} placeholder="SUBCATEGORÍA" maxLength={120} value={value.subcategory} disabled={locked || item.applied} onChange={event => edit(item, 'subcategory', event.target.value)}/></td>
            <td><span className="pill">Por revisar · {Math.round(item.confidence * 100)}% confianza</span><small>{item.reason}</small>{item.evidence.source_reference && <small>Referencia: {item.evidence.source_reference}</small>}</td>
          </tr>;
        })}</tbody>
      </table></div>
      <div className="admin-toolbar-actions">
        <button type="button" className="button soft small" disabled={locked || !result.offset} onClick={() => load(Math.max(0, result.offset - 50))}>Anterior</button>
        <span className="fine-print">Propuestas {result.offset + 1}–{result.offset + result.results.length} de {result.count}</span>
        <button type="button" className="button soft small" disabled={locked || result.next_offset === null} onClick={() => load(result.next_offset!)}>Siguiente</button>
        <button type="button" className="button primary small" disabled={locked || !selected.size} onClick={apply}>{busy === 'apply' ? 'Aplicando revisión…' : `Aplicar ${selected.size} propuestas revisadas`}</button>
      </div>
    </>}
  </section>;
}
