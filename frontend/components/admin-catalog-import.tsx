'use client';

import { FormEvent, useEffect, useId, useRef, useState } from 'react';
import { Download, FileSpreadsheet, LoaderCircle, Pause, Play } from 'lucide-react';
import Modal from './modal';
import CatalogClassification, { ClassificationResult } from './admin-catalog-classification';
import { UppercaseInput } from './uppercase-field';
import { CatalogImportRecoveryRow, CatalogImportResult, request } from '@/lib/types';

const STORAGE_KEY = 'motionpartes.catalog-import-job';
function rememberJob(id: string | null) {
  try { if (id) localStorage.setItem(STORAGE_KEY, id); else localStorage.removeItem(STORAGE_KEY); } catch { /* Browser storage is optional. */ }
}
function jobUrl(id: string) { return `/api/management/catalog/import/jobs/${encodeURIComponent(id)}`; }
function recoveryKey(item: Pick<CatalogImportRecoveryRow, 'row' | 'column'>) { return `${item.row}:${item.column}`; }
class JobReadError extends Error {
  constructor(message: string, readonly status: number) { super(message); }
}
async function readJob(id: string): Promise<CatalogImportResult> {
  const response = await fetch(jobUrl(id), { cache: 'no-store', headers: { Accept: 'application/json' } });
  let data;
  try { data = await response.json(); } catch { throw new Error('No se pudo recuperar el progreso. Inténtalo de nuevo.'); }
  if (!response.ok) throw new JobReadError(typeof data.detail === 'string' ? data.detail : 'No se pudo recuperar la importación.', response.status);
  return data;
}

export default function CatalogImport({ onClose, onSaved }: { onClose: () => void; onSaved: (summary: CatalogImportResult['summary']) => void }) {
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<CatalogImportResult | null>(null);
  const [pendingJob, setPendingJob] = useState<string | null>(null);
  const [busy, setBusy] = useState<'preview' | 'import' | 'recover' | null>(null);
  const [paused, setPaused] = useState(true);
  const [pausing, setPausing] = useState(false);
  const [classificationBusy, setClassificationBusy] = useState(false);
  const [classificationStatus, setClassificationStatus] = useState<ClassificationResult | null>(null);
  const [classificationRevision, setClassificationRevision] = useState(0);
  const [recoveryValues, setRecoveryValues] = useState<Record<string, string>>({});
  const [skippedRows, setSkippedRows] = useState<number[]>([]);
  const [recoveryDirty, setRecoveryDirty] = useState(false);
  const [error, setError] = useState('');
  const fileInput = useRef<HTMLInputElement>(null);
  const fileId = useId();
  const mounted = useRef(false);
  const operating = useRef(false);
  const pauseRequested = useRef(false);
  const notified = useRef(false);
  const savedCallback = useRef(onSaved);
  savedCallback.current = onSaved;

  function finish(result: CatalogImportResult) {
    if (!mounted.current || result.status !== 'completed' || !result.imported || notified.current) return;
    notified.current = true;
    rememberJob(null);
    const summary = result.applied_summary || result.summary;
    savedCallback.current({ ...summary, skipped_rows: result.skipped_row_count ?? summary.skipped_rows, rejected_rows: result.pending_error_count ?? result.rejected_row_count ?? summary.rejected_rows });
  }

  function showPreview(result: CatalogImportResult) {
    setPreview(result);
    setRecoveryValues(Object.fromEntries((result.recovery_rows || []).map(item => [recoveryKey(item), item.value || item.suggested_code || ''])));
    setSkippedRows([...new Set([...(result.skipped_rows || []), ...(result.recovery_rows || []).filter(item => item.skipped).map(item => item.row)])]);
    setRecoveryDirty(false);
  }

  function reportError(error: unknown, fallback: string) {
    if (!mounted.current) return;
    if (error instanceof JobReadError && error.status === 404) {
      rememberJob(null); setPendingJob(null); setPreview(null);
      setError('Esta importación ya no está disponible. Selecciona el archivo para crear una nueva vista previa.');
      return;
    }
    setError(error instanceof Error ? error.message : fallback);
  }

  async function recover(id: string) {
    if (operating.current) return;
    operating.current = true;
    setBusy('recover'); setError('');
    try {
      const result = await readJob(id);
      if (!mounted.current) return;
      showPreview(result); setPaused(true);
      finish(result);
    } catch (error) {
      reportError(error, 'No se pudo recuperar la importación. Inténtalo de nuevo.');
    } finally { operating.current = false; if (mounted.current) setBusy(null); }
  }

  useEffect(() => {
    mounted.current = true;
    let id: string | null = null;
    try { id = localStorage.getItem(STORAGE_KEY); } catch { /* Browser storage is optional. */ }
    if (id && /^[a-f0-9-]{36}$/i.test(id)) { setPendingJob(id); void recover(id); }
    return () => { mounted.current = false; pauseRequested.current = true; };
  }, []);

  async function reviewFile(applyCorrections = false) {
    if (!file || operating.current || classificationBusy || (preview?.progress?.completed_batches || 0) > 0) return;
    const form = new FormData(); form.set('file', file); form.set('mode', 'preview');
    if (pendingJob) form.set('job_id', pendingJob);
    if (applyCorrections) {
      const corrections = (preview?.recovery_rows || []).filter(item => !skippedRows.includes(item.row) && recoveryValues[recoveryKey(item)]?.trim()).map(item => ({ row: item.row, column: item.column, value: recoveryValues[recoveryKey(item)].trim().toUpperCase() }));
      form.set('corrections', JSON.stringify(corrections));
      form.set('skip_rows', JSON.stringify(skippedRows));
    }
    operating.current = true;
    setBusy('preview'); setError(''); if (!applyCorrections) setPreview(null);
    try {
      const result = await request<CatalogImportResult>('/api/management/catalog/import', { method: 'POST', body: form });
      if (!mounted.current) return;
      showPreview(result);
      setClassificationStatus(null); setClassificationRevision(value => value + 1);
      if (result.job_id) {
        setPendingJob(result.job_id); rememberJob(result.job_id); setPaused(true);
      }
    } catch (error) {
      if (mounted.current) setError(error instanceof Error ? error.message : 'No se pudo revisar el archivo. Inténtalo de nuevo.');
    } finally { operating.current = false; if (mounted.current) setBusy(null); }
  }

  async function review(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    await reviewFile();
  }

  async function importBatches() {
    if (!pendingJob || operating.current || classificationBusy || recoveryDirty) return;
    operating.current = true;
    pauseRequested.current = false;
    setBusy('import'); setPaused(false); setPausing(false); setError('');
    try {
      // Recover durable progress before every retry, including a lost response from the previous batch.
      let result = await readJob(pendingJob);
      if (!mounted.current) return;
      setPreview(result);
      if (result.status === 'completed' && result.imported) { finish(result); return; }
      while (mounted.current && !pauseRequested.current) {
        const progress = result.progress;
        if (!progress || progress.completed_batches >= progress.total_batches) throw new Error('No se pudo confirmar el estado de la importación. Recupera el progreso antes de continuar.');
        const next = await request<CatalogImportResult>(jobUrl(pendingJob), { method: 'POST', body: JSON.stringify({ batch_index: progress.completed_batches }) });
        if (!mounted.current) return;
        setPreview(next);
        if (next.status === 'completed' && next.imported) { finish(next); return; }
        if (!next.progress || next.progress.completed_batches <= progress.completed_batches) throw new Error('No se pudo confirmar el avance del lote. Reanuda la importación para recuperar su estado.');
        result = next;
      }
    } catch (error) {
      reportError(error, 'No se pudo guardar el lote. Los lotes anteriores siguen registrados. Reanuda para continuar.');
    } finally {
      operating.current = false;
      if (mounted.current) { setBusy(null); setPaused(true); setPausing(false); }
    }
  }

  function selectAnotherFile() {
    if (operating.current || classificationBusy) return;
    rememberJob(null); setPendingJob(null); setPreview(null); setFile(null); setError(''); setPaused(true);
    setClassificationStatus(null);
    setRecoveryValues({}); setSkippedRows([]); setRecoveryDirty(false);
    if (fileInput.current) fileInput.current.value = '';
  }

  const hasChanges = !!preview && preview.summary.created_skus + preview.summary.updated_skus > 0;
  const progress = preview?.progress;
  const started = !!progress && progress.completed_batches > 0;
  const warnings = preview?.warnings?.slice(0, 100) || [];
  const warningCount = preview?.warning_count ?? warnings.length;
  const recoveryRows = preview?.recovery_rows || [];
  const numericWarningCount = preview?.numeric_warning_count ?? (recoveryRows.length ? 0 : warningCount);
  const recoveryEditable = !!file && !started;
  const recoveryDisabled = !!busy || classificationBusy || !recoveryEditable;
  const skippedRowCount = preview?.skipped_row_count ?? preview?.skipped_rows?.length ?? 0;
  const rejectedRowCount = preview?.rejected_row_count ?? preview?.summary.rejected_rows ?? 0;
  const pendingErrorCount = preview?.pending_error_count ?? rejectedRowCount;
  const canImport = !!preview?.valid && !!pendingJob && !!progress?.total_batches && (hasChanges || rejectedRowCount > 0);
  const aiNotRun = !!classificationStatus?.configured && classificationStatus.completed_batches === 0;
  const aiPartiallyReviewed = !!classificationStatus?.configured && !aiNotRun && (classificationStatus.completed_batches < classificationStatus.total_batches || (classificationStatus.applied_count ?? classificationStatus.results.filter(item => item.applied).length) < classificationStatus.count);
  const automaticRecoveryRows = recoveryRows.filter(item => item.automatic && item.resolved && !skippedRows.includes(item.row));
  const automaticRecoveryKeys = new Set(automaticRecoveryRows.map(recoveryKey));
  const manualRecoveryRows = recoveryRows.filter(item => !automaticRecoveryKeys.has(recoveryKey(item))).sort((a, b) => {
    const priority = (item: CatalogImportRecoveryRow) => skippedRows.includes(item.row) ? 2 : item.resolved ? 1 : 0;
    return priority(a) - priority(b) || a.row - b.row;
  });
  const pendingRecoveryCount = manualRecoveryRows.filter(item => !item.resolved && !skippedRows.includes(item.row)).length;
  const reviewedRecoveryCount = manualRecoveryRows.filter(item => item.resolved && !skippedRows.includes(item.row)).length;
  const recoverySummary = [
    automaticRecoveryRows.length ? `${automaticRecoveryRows.length} ${automaticRecoveryRows.length === 1 ? 'código recuperado' : 'códigos recuperados'} automáticamente` : '',
    `${pendingRecoveryCount} por revisar`,
    reviewedRecoveryCount ? `${reviewedRecoveryCount} ${reviewedRecoveryCount === 1 ? 'código revisado' : 'códigos revisados'}` : '',
    skippedRows.length ? `${skippedRows.length} ${skippedRows.length === 1 ? 'fila excluida' : 'filas excluidas'}` : '',
  ].filter(Boolean).join(' · ');

  function recoveryCard(item: CatalogImportRecoveryRow) {
    const key = recoveryKey(item);
    const skipped = skippedRows.includes(item.row);
    return <div className="import-code-row" key={key}>
      <div className="import-code-context"><strong>Fila {item.row} · {item.column === 'SKU' ? 'SKU interno' : 'Código alterno'}</strong><span>{item.description || 'Sin descripción'}</span><small>Excel: {item.excel_value}</small><small>{item.reason}</small><small className="import-code-status">{skipped ? 'Fila excluida' : item.resolved ? item.automatic ? 'Recuperado automáticamente' : 'Corrección revisada' : 'Requiere revisión'}</small></div>
      <div className="import-code-control">
        <label>Código recuperado<UppercaseInput aria-label={`Código recuperado fila ${item.row} ${item.column}`} type="text" value={recoveryValues[key] || ''} disabled={recoveryDisabled || skipped} maxLength={item.column === 'SKU' ? 200 : 120} name={`recovered-code-${fileId}-${item.row}-${item.column}`} onChange={event => { setRecoveryValues(current => ({ ...current, [key]: event.target.value })); setRecoveryDirty(true); }}/></label>
        {recoveryEditable && item.candidates.length > 1 && <div className="import-code-candidates" aria-label={`Opciones para fila ${item.row}`}>{item.candidates.filter(code => code !== recoveryValues[key]).map(code => <button key={code} type="button" className="button soft small" disabled={recoveryDisabled || skipped} onClick={() => { setRecoveryValues(current => ({ ...current, [key]: code })); setRecoveryDirty(true); }}>{code}</button>)}</div>}
        <label className="admin-checkbox"><input type="checkbox" aria-label={`Excluir fila ${item.row} ${item.column}`} checked={skipped} disabled={recoveryDisabled} onChange={event => { setSkippedRows(current => event.target.checked ? [...new Set([...current, item.row])] : current.filter(row => row !== item.row)); setRecoveryDirty(true); }}/><span>Excluir esta fila</span></label>
      </div>
    </div>;
  }
  return <Modal title="Importar Excel" onClose={() => { if (!operating.current && !classificationBusy) onClose(); }} wide>
    <p className="modal-description">Importa SKU y sus alternos al inventario interno. Usa una fila por código alterno y repite el SKU para agregar más códigos al mismo repuesto.</p>
    <a className="button soft small import-template" href="/templates/inventario-interno.xlsx" download><Download size={16}/>Descargar plantilla Excel</a>
    {!pendingJob && <form className="stack-form catalog-import-form" autoComplete="off" onSubmit={review}>
      <div className="stack-form"><label htmlFor={fileId}>Archivo Excel</label><div className="import-file-picker"><input ref={fileInput} id={fileId} className="sr-only" aria-label="Archivo Excel" type="file" accept=".xlsx" required disabled={!!busy || classificationBusy} tabIndex={-1} autoComplete="off" onChange={event => { setFile(event.target.files?.[0] || null); setPreview(null); setError(''); setRecoveryValues({}); setSkippedRows([]); setRecoveryDirty(false); }}/><button type="button" className="button soft small" disabled={!!busy || classificationBusy} onClick={() => fileInput.current?.click()}>Seleccionar archivo</button><span aria-live="polite">{file?.name || 'Ningún archivo seleccionado'}</span></div></div>
      <p className="form-footnote">Formato .xlsx, hasta 5 MB, sin límite de filas. Se guardan hasta 500 SKU por lote. Los códigos numéricos se convierten a texto; guarda los códigos con ceros iniciales como texto en Excel para conservarlos. Sustituye las filas de ejemplo de la plantilla por tus datos.</p>
      <button className="button soft" disabled={!file || !!busy}>{busy === 'preview' ? <><LoaderCircle size={16} className="spin"/>Revisando…</> : <><FileSpreadsheet size={16}/>Revisar archivo</>}</button>
    </form>}
    {pendingJob && <div className="import-job-header"><p>{preview?.filename || file?.name || 'Importación pendiente'}</p><button type="button" className="button soft small" disabled={!!busy || classificationBusy} onClick={selectAnotherFile}>Seleccionar otro archivo</button></div>}
    {error && <div className="notice error" role="alert">{error}</div>}
    {pendingJob && !preview && <div className="import-recovery"><p className="form-footnote">Recupera el progreso guardado para continuar sin volver a cargar el archivo.</p><button className="button soft" disabled={!!busy} onClick={() => recover(pendingJob)}>{busy === 'recover' ? <><LoaderCircle size={16} className="spin"/>Recuperando progreso…</> : 'Recuperar importación'}</button></div>}
    {preview && <section className="catalog-import-preview" aria-label="Vista previa de importación">
      <h3>Vista previa</h3>
      {pendingErrorCount > 0 && <div className="notice" role="status">{preview.summary.rows} {preview.summary.rows === 1 ? 'fila válida lista' : 'filas válidas listas'} para importar. {pendingErrorCount} {pendingErrorCount === 1 ? 'fila con errores guardada' : 'filas con errores guardadas'} en «Errores de importación» para corregir después.{preview.valid ? ' Puedes importar las filas válidas ahora.' : ' No hay filas válidas para importar; puedes corregir las pendientes desde el inventario interno.'}</div>}
      {recoveryRows.length > 0 && <details className="import-code-recovery" aria-label="Recuperación de códigos">
        <summary>Revisar códigos convertidos en fechas · {recoverySummary}</summary>
        <section className="import-code-recovery-body">
        <h3>Códigos que Excel convirtió en fechas</h3>
        <p className="notice" role="status">{recoverySummary}</p>
        <p className="form-footnote">Puedes revisar y aplicar correcciones aquí si lo prefieres. Las filas que sigan con errores se guardan para corregirlas después en «Errores de importación».</p>
        {!file && <p className="notice" role="status">Esta vista conserva las decisiones guardadas. Para cambiar los códigos, selecciona de nuevo el archivo Excel.</p>}
        {started && <p className="form-footnote">Las decisiones ya forman parte de la importación iniciada.</p>}
        {manualRecoveryRows.length > 0 && <div className="import-recovery-list">{manualRecoveryRows.map(recoveryCard)}</div>}
        {automaticRecoveryRows.length > 0 && <details className="import-automatic-recovery"><summary>Ver {automaticRecoveryRows.length} {automaticRecoveryRows.length === 1 ? 'código recuperado' : 'códigos recuperados'} automáticamente</summary><div className="import-recovery-list">{automaticRecoveryRows.map(recoveryCard)}</div></details>}
        {(preview.recovery_count || 0) > recoveryRows.length && <p className="form-footnote">Se muestran {recoveryRows.length} de {preview.recovery_count} códigos que requieren revisión.</p>}
        {recoveryEditable && <div className="import-recovery-actions"><button className="button soft" type="button" disabled={recoveryDisabled} onClick={() => reviewFile(true)}>{busy === 'preview' ? <><LoaderCircle size={16} className="spin"/>Revisando correcciones…</> : 'Aplicar correcciones y revisar'}</button>{recoveryDirty && <button className="button soft" type="button" disabled={recoveryDisabled} onClick={() => showPreview(preview)}>Descartar cambios</button>}</div>}
        </section>
      </details>}
      {recoveryDirty && <p className="notice" role="status">Tienes correcciones sin aplicar. Aplica las correcciones o descarta los cambios antes de importar.<button type="button" className="button soft small" disabled={recoveryDisabled} onClick={() => showPreview(preview)}>Descartar cambios sin aplicar</button></p>}
      {skippedRowCount > 0 && <div className="notice" role="status">Se {skippedRowCount === 1 ? 'excluirá 1 fila' : `excluirán ${skippedRowCount} filas`} de esta importación. {preview.source_rows != null ? `${preview.summary.rows} de ${preview.source_rows} filas del archivo se incluyen en la vista previa.` : 'Las demás filas se procesarán por lotes.'}</div>}
      {warningCount > 0 && <>
        <div className="notice" role="status">{numericWarningCount === warningCount ? <>{numericWarningCount} {numericWarningCount === 1 ? 'código numérico convertido' : 'códigos numéricos convertidos'} a texto. Revisa las conversiones antes de importar.</> : <>{warningCount} {warningCount === 1 ? 'conversión de código' : 'conversiones de códigos'}. {numericWarningCount > 0 ? `${numericWarningCount} ${numericWarningCount === 1 ? 'código numérico convertido' : 'códigos numéricos convertidos'} a texto. ` : ''}Revisa los códigos recuperados antes de importar.</>}</div>
        {!!warnings.length && <details><summary>Ver conversiones a texto</summary>
          <div className="table-scroll"><table className="admin-catalog-table"><thead><tr><th>Fila de Excel</th><th>Columna</th><th>Conversión</th></tr></thead><tbody>{warnings.map((item, index) => <tr key={index}><td>{item.row}</td><td>{item.column}</td><td>{item.message}</td></tr>)}</tbody></table></div>
          {warningCount > warnings.length && <p className="form-footnote">Se muestran las primeras {warnings.length} conversiones.</p>}
        </details>}
      </>}
      {preview.error_count > 0 && <details className="import-deferred-errors">
        <summary>Ver {preview.error_count} {preview.error_count === 1 ? 'error guardado para después' : 'errores guardados para después'}</summary>
        <div className="table-scroll"><table className="admin-catalog-table"><thead><tr><th>Fila de Excel</th><th>Columna</th><th>Error</th></tr></thead><tbody>{preview.errors.map((item, index) => <tr key={index}><td>{item.row}</td><td>{item.column}</td><td>{item.message}</td></tr>)}</tbody></table></div>
        {preview.error_count > preview.errors.length && <p className="form-footnote">Se muestran los primeros {preview.errors.length} errores.</p>}
      </details>}
      {!preview.valid && !pendingErrorCount && <div className="notice error" role="alert">No se pudo preparar el archivo. Revisa los errores indicados y vuelve a cargarlo.</div>}
      {preview.valid && <>
        <div className="import-summary"><span><strong>{preview.summary.created_skus}</strong> SKU nuevos</span><span><strong>{preview.summary.updated_skus}</strong> SKU por actualizar</span><span><strong>{preview.summary.added_codes}</strong> alternos nuevos</span><span><strong>{preview.summary.unchanged_skus}</strong> SKU sin cambios</span></div>
        <p className="form-footnote">Los campos vacíos conservan los datos actuales de los SKU existentes. Se agregan los alternos nuevos y se conservan los ya registrados.</p>
        {pendingJob && progress?.completed_batches === 0 && <CatalogClassification key={`${pendingJob}:${classificationRevision}`} jobId={pendingJob} disabled={!!busy || recoveryDirty} onBusyChange={setClassificationBusy} onStatusChange={setClassificationStatus} onUpdated={setPreview}/>}
        {progress && <div className="import-progress">
          <h3>Importación por lotes</h3>
          <progress aria-label="SKU procesados" value={progress.processed_skus} max={Math.max(progress.total_skus, 1)}/>
          <p role="status" aria-live="polite">{progress.processed_skus} de {progress.total_skus} SKU procesados · {progress.completed_batches} de {progress.total_batches} lotes guardados{busy === 'import' ? pausing ? ' · Pausando al terminar el lote…' : ' · Importando…' : started && paused ? ' · En pausa' : ''}</p>
          {preview.applied_summary && started && <p className="form-footnote">Guardados: {preview.applied_summary.created_skus} SKU nuevos, {preview.applied_summary.updated_skus} actualizados y {preview.applied_summary.added_codes} alternos nuevos.</p>}
          <p className="form-footnote">Cada lote guarda hasta {progress.batch_size} SKU. Puedes pausar y reanudar. Los lotes ya guardados permanecen registrados si un lote posterior falla o seleccionas otro archivo.</p>
        </div>}
        <div className="table-scroll"><table className="admin-catalog-table"><thead><tr><th>SKU interno</th><th>Nombre / descripción</th><th>Estado</th><th>Acción</th><th>Alternos nuevos</th></tr></thead><tbody>{preview.preview.map(item => <tr key={item.sku}><td><strong>{item.sku}</strong></td><td><strong>{item.name || 'Sin nombre'}</strong>{item.description && <span>{item.description}</span>}{(item.category || item.subcategory) && <span>{[item.category, item.subcategory].filter(Boolean).join(' / ')}</span>}</td><td>{item.active ? 'Activo' : 'Inactivo'}</td><td>{{ create: 'Crear', update: 'Actualizar', unchanged: 'Sin cambios' }[item.action]}</td><td><div className="admin-role-list">{item.codes.map(code => <span key={`${code.brand}:${code.code}`}>{code.code}{code.brand ? ` · ${code.brand}` : ''}</span>)}</div>{item.added_codes > item.codes.length ? `${item.added_codes - item.codes.length} alternos más` : !item.added_codes ? 'Sin alternos nuevos' : ''}</td></tr>)}</tbody></table></div>
        {preview.preview_count > preview.preview.length && <p className="form-footnote">Se muestran {preview.preview.length} de {preview.preview_count} SKU. La importación incluye todos los SKU revisados.</p>}
        {!hasChanges && <p className="notice" role="status">Las filas válidas ya están registradas.{rejectedRowCount > 0 ? ' Completa la importación para continuar después con las filas pendientes.' : ' No hay cambios por importar.'}</p>}
      </>}
      {!started && canImport && (aiNotRun || aiPartiallyReviewed) && <p className="form-footnote">{aiNotRun ? 'Importar sin IA conserva los SKU y alternos tal como aparecen en el Excel.' : 'Importar con revisión parcial guarda solo las propuestas ya aplicadas; los demás SKU conservan los datos del Excel.'} Puedes revisar agrupaciones después desde «Agrupar SKU».</p>}
      {busy === 'import' ? <button type="button" className="button soft full" disabled={pausing} onClick={() => { pauseRequested.current = true; setPausing(true); }}><Pause size={16}/>{pausing ? 'Pausando al terminar el lote…' : 'Pausar importación'}</button> : <button className="button primary full" disabled={!canImport || !!busy || classificationBusy || recoveryDirty} onClick={importBatches}>{busy === 'recover' ? <><LoaderCircle size={16} className="spin"/>Recuperando progreso…</> : started ? <><Play size={16}/>Reanudar importación</> : aiNotRun ? rejectedRowCount > 0 ? 'Importar filas válidas sin IA' : 'Importar sin IA' : aiPartiallyReviewed ? 'Importar con revisión parcial' : error ? <><Play size={16}/>Reanudar importación</> : rejectedRowCount > 0 ? 'Importar filas válidas' : 'Confirmar importación'}</button>}
    </section>}
  </Modal>;
}
