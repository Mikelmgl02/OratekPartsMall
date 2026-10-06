'use client';

import { FormEvent, useEffect, useId, useRef, useState } from 'react';
import { ArrowDownLeft, ArrowUpRight, CheckCircle2, Download, FileSpreadsheet, LoaderCircle, Pause, Play } from 'lucide-react';
import Modal from './modal';
import { Account, SupplierInventoryImportResult, request } from '@/lib/types';

function storageKey(accountId: string) { return `motionpartes.supplier-import-job:${accountId}`; }
function rememberJob(accountId: string, id: string | null) {
  try { if (id) localStorage.setItem(storageKey(accountId), id); else localStorage.removeItem(storageKey(accountId)); } catch { /* Importing also works when browser storage is unavailable. */ }
}
class ImportJobError extends Error {
  constructor(message: string, readonly status: number) { super(message); }
}
async function jobRequest(url: string, batchIndex?: number): Promise<SupplierInventoryImportResult> {
  let response: Response;
  try { response = await fetch(url, { cache: 'no-store', headers: { 'Content-Type': 'application/json' }, ...(batchIndex !== undefined ? { method: 'POST', body: JSON.stringify({ batch_index: batchIndex }) } : {}) }); }
  catch { throw new Error('Se perdió la conexión. Los lotes guardados se conservan; reanuda para recuperar el progreso.'); }
  let data;
  try { data = await response.json(); } catch { throw new Error('No se pudo confirmar el progreso. Reanuda la importación para recuperarlo.'); }
  if (!response.ok) throw new ImportJobError(typeof data.detail === 'string' ? data.detail : 'No se pudo continuar la importación.', response.status);
  return data;
}

export default function SupplierInventoryImport({ account, onClose, onSaved }: { account: Account; onClose: () => void; onSaved: (result: SupplierInventoryImportResult) => void }) {
  const [file, setFile] = useState<File | null>(null);
  const [result, setResult] = useState<SupplierInventoryImportResult | null>(null);
  const [pendingJob, setPendingJob] = useState<string | null>(null);
  const [busy, setBusy] = useState<'review' | 'recover' | 'import' | null>(null);
  const [error, setError] = useState('');
  const [needsReview, setNeedsReview] = useState(false);
  const [pausing, setPausing] = useState(false);
  const [interrupted, setInterrupted] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const fileId = useId();
  const mounted = useRef(false);
  const operating = useRef(false);
  const pauseRequested = useRef(false);
  const notified = useRef(false);
  const savedCallback = useRef(onSaved);
  savedCallback.current = onSaved;
  const base = `/api/market/accounts/${account.id}/inventory/import`;
  const jobUrl = (id: string) => `${base}/jobs/${encodeURIComponent(id)}`;

  function finish(next: SupplierInventoryImportResult) {
    if (!mounted.current || next.status !== 'completed' || !next.imported || notified.current) return;
    notified.current = true;
    // Reopening a completed job with rejected rows must still offer its correction workbook.
    if (!next.summary.rejected_rows) rememberJob(account.id, null);
    savedCallback.current(next);
  }

  function reportError(caught: unknown) {
    if (!mounted.current) return;
    if (caught instanceof ImportJobError && (caught.status === 404 || caught.status === 403)) {
      rememberJob(account.id, null); setPendingJob(null); setResult(null);
      setError('Esta importación ya no está disponible para tu cuenta. Selecciona el archivo para revisarlo de nuevo.');
    } else {
      if (caught instanceof ImportJobError && caught.status === 409) setNeedsReview(true);
      setError(caught instanceof TypeError ? 'Se perdió la conexión. Inténtalo de nuevo; los lotes guardados se conservarán.' : caught instanceof Error ? caught.message : 'No se pudo continuar. Los lotes guardados se conservarán.');
    }
  }

  async function recover(id: string) {
    if (operating.current) return;
    operating.current = true; setBusy('recover'); setError('');
    try {
      const next = await jobRequest(jobUrl(id));
      if (!mounted.current) return;
      setResult(next); setInterrupted(next.progress.completed_batches > 0); finish(next);
    } catch (caught) { reportError(caught); }
    finally { operating.current = false; if (mounted.current) setBusy(null); }
  }

  useEffect(() => {
    mounted.current = true;
    let id: string | null = null;
    try { id = localStorage.getItem(storageKey(account.id)); } catch { /* Browser storage is optional. */ }
    if (id && /^[a-f0-9-]{36}$/i.test(id)) { setPendingJob(id); void recover(id); }
    return () => { mounted.current = false; pauseRequested.current = true; };
    // The supplier workspace keys this modal by account, so a different account gets its own job.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function review(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!file || operating.current) return;
    if (!file.name.toLowerCase().endsWith('.xlsx') || file.size > 5 * 1024 * 1024) { setError('Selecciona un archivo .xlsx de hasta 5 MB.'); return; }
    operating.current = true; setBusy('review'); setError('');
    const form = new FormData(); form.set('file', file);
    try {
      const next = await request<SupplierInventoryImportResult>(base, { method: 'POST', body: form });
      if (!mounted.current) return;
      setResult(next); setPendingJob(next.job_id); rememberJob(account.id, next.job_id); setNeedsReview(false);
      setInterrupted(false); notified.current = false;
    } catch (caught) { reportError(caught); }
    finally { operating.current = false; if (mounted.current) setBusy(null); }
  }

  async function importBatches() {
    if (!pendingJob || operating.current || needsReview) return;
    operating.current = true; pauseRequested.current = false;
    setBusy('import'); setPausing(false); setError('');
    try {
      // Read durable progress before a retry: a previous response could have been lost after saving.
      let next = await jobRequest(jobUrl(pendingJob));
      if (!mounted.current) return;
      setResult(next);
      if (next.status === 'completed' && next.imported) { finish(next); return; }
      if (!next.valid || !next.progress.total_batches) throw new Error('No hay filas válidas para importar. Descarga y corrige las filas con errores.');
      while (mounted.current && !pauseRequested.current) {
        const before = next.progress;
        if (before.next_batch === null) throw new Error('No se pudo confirmar el siguiente lote. Reanuda para recuperar el progreso.');
        const saved = await jobRequest(jobUrl(pendingJob), before.next_batch);
        if (!mounted.current) return;
        setResult(saved);
        if (saved.status === 'completed' && saved.imported) { finish(saved); return; }
        if (saved.progress.completed_batches <= before.completed_batches) throw new Error('No se pudo confirmar el avance del lote. Reanuda para recuperar el progreso.');
        next = saved;
      }
    } catch (caught) { reportError(caught); }
    finally {
      operating.current = false;
      if (mounted.current) { setBusy(null); setPausing(false); setInterrupted(true); }
    }
  }

  function selectFile(next: File | null) {
    if (operating.current) return;
    rememberJob(account.id, null); setFile(next); setResult(null); setPendingJob(null);
    setError(''); setNeedsReview(false); setInterrupted(false); notified.current = false;
  }

  const completed = result?.status === 'completed' && result.imported;
  const progress = result?.progress;
  const expired = !!result && new Date(result.expires_at).getTime() <= Date.now();
  const canImport = !!result?.valid && !!pendingJob && !!progress?.total_batches && !completed && !needsReview && !expired;
  const close = () => { if (!operating.current) onClose(); };
  const units = (value: number) => value.toLocaleString('es-PA');
  return <Modal title="Importar existencias desde Excel" wide onClose={close}><div className="supplier-import">
    <p className="modal-description">Carga los saldos totales de {account.name}. La revisión no modifica existencias; verás cada crédito o débito antes de confirmar. Al terminar la carga, los códigos se agrupan automáticamente con el catálogo; las coincidencias dudosas quedan para revisión.</p>
    <div className="supplier-import-template"><a className="button soft small" href={`${base}/template`} download><Download size={16}/>Descargar plantilla</a><p>Columnas obligatorias: <strong>ID_INVENTARIO_PROVEEDOR, CODIGO y EXISTENCIAS</strong>. MARCA, DESCRIPCION y REFERENCIAS son opcionales. En REFERENCIAS separa los alternos con punto y coma: HYUNDAI:51712-1R000; KIA:51712-0U000. El fabricante es opcional. Estas referencias ayudan a encontrar el SKU maestro y no crean artículos adicionales. Sustituye la fila de ejemplo.</p></div>
    <form onSubmit={review} autoComplete="off" className="supplier-import-form">
      <label htmlFor={fileId}>Archivo Excel de existencias</label>
      <div className="supplier-import-picker"><input ref={fileInput} id={fileId} type="file" accept=".xlsx" className="sr-only" disabled={!!busy} onChange={event => selectFile(event.target.files?.[0] || null)}/><button className="button soft small" type="button" disabled={!!busy} onClick={() => fileInput.current?.click()}><FileSpreadsheet size={16}/>{result || pendingJob ? 'Elegir otro archivo' : 'Seleccionar archivo'}</button><span>{file?.name || result?.filename || 'Ningún archivo seleccionado'}</span></div>
      <p className="form-footnote">Formato .xlsx, hasta 5 MB, sin límite de filas. Conserva los ID y códigos como texto. EXISTENCIAS es el saldo total: cero vacía ese artículo; los artículos omitidos conservan su saldo. Los registros de apiag-cloud se actualizan por su integración.</p>
      <button className="button soft full" disabled={!file || !!busy}>{busy === 'review' ? <LoaderCircle className="spin" size={17}/> : <FileSpreadsheet size={17}/>}Revisar archivo</button>
    </form>
    {busy === 'recover' && <div className="loading" role="status"><LoaderCircle className="spin"/>Recuperando tu importación…</div>}
    {error && <div className="notice error" role="alert">{error}{needsReview && <p>Selecciona el archivo y revísalo de nuevo. Los lotes ya guardados se conservarán.</p>}</div>}
    {pendingJob && !result && !busy && <button className="button soft" onClick={() => recover(pendingJob)}>Recuperar progreso</button>}
    {result && <div className="supplier-import-preview">
      <div className="supplier-import-preview-heading"><h3>{completed ? 'Importación completada' : 'Vista previa'}</h3><span>{result.filename}</span></div>
      {completed && <div className="notice success" role="status"><CheckCircle2 size={18}/><span>Se procesaron {units(result.applied_summary.valid_rows)} filas válidas: {units(result.applied_summary.created_items)} artículos nuevos, {units(result.applied_summary.updated_items)} actualizados y {units(result.applied_summary.unchanged_items)} sin cambios.{result.summary.rejected_rows > 0 && ` ${units(result.summary.rejected_rows)} filas siguen pendientes; puedes descargar sus errores y cargar el archivo corregido.`}</span></div>}
      <div className="supplier-import-summary"><span><strong>{units(result.summary.valid_rows)}</strong>Filas válidas</span><span><strong>{units(result.summary.rejected_rows)}</strong>Filas pendientes</span><span><strong>{units(result.summary.created_items)}</strong>Artículos nuevos</span><span><strong>{units(result.summary.updated_items)}</strong>{completed ? 'Artículos actualizados' : 'Artículos por actualizar'}</span><span className="credit"><strong>{units(result.summary.credit_units)}</strong>Unidades de crédito</span><span className="debit"><strong>{units(result.summary.debit_units)}</strong>Unidades de débito</span></div>
      {result.summary.rejected_rows > 0 && <div className="supplier-import-errors"><div className="notice error">{units(result.summary.rejected_rows)} {result.summary.rejected_rows === 1 ? 'fila con errores queda' : 'filas con errores quedan'} fuera de esta importación. Puedes importar las válidas y corregir las demás después.</div><a className="button soft small" href={`${jobUrl(result.job_id)}/errors`} download><Download size={16}/>Descargar filas con errores</a><p className="form-footnote">El archivo contiene todas las filas pendientes y sus errores. Corrígelo y cárgalo aquí de nuevo; no necesitas volver a cargar las filas importadas.</p><details><summary>Ver errores{result.error_count > result.errors.length ? ` · primeros ${result.errors.length} de ${units(result.error_count)}` : ''}</summary><div className="table-scroll"><table><thead><tr><th>Fila</th><th>Columna</th><th>Error</th></tr></thead><tbody>{result.errors.map((item, index) => <tr key={`${item.row}-${item.column}-${index}`}><td>{item.row}</td><td>{item.column}</td><td>{item.message}</td></tr>)}</tbody></table></div></details></div>}
      {result.warning_count > 0 && <details className="supplier-import-warnings"><summary>{units(result.warning_count)} advertencias de lectura</summary><ul>{result.warnings.map((item, index) => <li key={`${item.row}-${index}`}>Fila {item.row} · {item.column}: {item.message}</li>)}</ul>{result.warning_count > result.warnings.length && <p>Se muestran las primeras {result.warnings.length} advertencias.</p>}</details>}
      {!!progress?.total_batches && <div className="supplier-import-progress" aria-live="polite"><div><strong>{busy === 'import' ? pausing ? 'Pausando después de este lote…' : 'Importando existencias…' : completed ? 'Todos los lotes guardados' : progress.completed_batches ? 'Importación en pausa' : 'Lista para importar'}</strong><span>{units(progress.processed_rows)} de {units(progress.total_rows)} filas · {progress.completed_batches} de {progress.total_batches} lotes</span></div><progress aria-label="Progreso de importación de existencias" value={progress.completed_batches} max={progress.total_batches}/>{busy === 'import' && <button type="button" className="button soft small" disabled={pausing} onClick={() => { pauseRequested.current = true; setPausing(true); }}><Pause size={15}/>Pausar después del lote</button>}</div>}
      {expired && !completed && <p className="notice error">Esta revisión ha caducado. Selecciona el archivo y revísalo de nuevo para importar; los lotes ya guardados se conservan.</p>}
      {result.preview.length > 0 && <><p className="supplier-import-preview-caption">{result.preview_count > result.preview.length ? `Se muestran las primeras ${result.preview.length} de ${units(result.preview_count)} filas válidas.` : `${units(result.preview_count)} filas válidas en la vista previa.`} Cada movimiento usa una cantidad positiva; su dirección indica si aumenta o reduce las existencias.</p><div className="table-scroll supplier-import-table"><table><thead><tr><th>Fila / ID del proveedor</th><th>Código / repuesto</th><th>Saldo actual</th><th>Saldo en Excel</th><th>Movimiento</th><th>Cantidad</th><th>Nuevo saldo</th><th>Acción</th></tr></thead><tbody>{result.preview.map(item => <tr key={item.row}><td><span>Fila {item.row}</span><strong>{item.supplier_invent_id}</strong></td><td><strong>{item.codigo}</strong><span>{item.brand || 'SIN MARCA'}{item.description ? ` · ${item.description}` : ''}</span><small>{item.references?.length ? `REFERENCIAS: ${item.references.map(ref => [ref.brand, ref.code].filter(Boolean).join(': ')).join('; ')}` : ''}</small><small>{item.part_sku ? `SKU: ${item.part_sku}` : item.matching_status === 'review' ? 'Coincidencia por revisar' : 'Coincidencia pendiente'}</small></td><td className="number-cell">{units(item.current_quantity)}</td><td className="number-cell">{units(item.uploaded_quantity)}</td><td><span className={`supplier-movement ${item.direction}`}>{item.direction === 'credit' ? <><ArrowDownLeft size={14}/>Crédito</> : item.direction === 'debit' ? <><ArrowUpRight size={14}/>Débito</> : 'Sin cambio'}</span></td><td className="number-cell">{units(item.movement_quantity)}</td><td className="number-cell"><strong>{units(item.new_quantity)}</strong></td><td>{item.action === 'create' ? 'Crear' : item.action === 'update' ? 'Actualizar' : 'Sin cambios'}</td></tr>)}</tbody></table></div></>}
      {completed ? <button type="button" className="button primary full" onClick={close}>Cerrar</button> : <button type="button" className="button primary full" disabled={!canImport || !!busy} onClick={importBatches}>{busy === 'import' ? <LoaderCircle className="spin" size={17}/> : interrupted || progress?.completed_batches ? <Play size={17}/> : <CheckCircle2 size={17}/>} {interrupted || progress?.completed_batches ? 'Reanudar importación' : result.summary.rejected_rows ? 'Importar filas válidas' : 'Confirmar importación'}</button>}
      {busy === 'import' && <p className="form-footnote">Espera a que termine o pausa después del lote antes de cerrar. Puedes reanudar el progreso guardado al volver a abrir este panel.</p>}
    </div>}
  </div></Modal>;
}
