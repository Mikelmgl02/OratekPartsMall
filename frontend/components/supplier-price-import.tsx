'use client';

import { useId, useRef, useState } from 'react';
import { CheckCircle2, Download, FileSpreadsheet, LoaderCircle, Pause, Play, TriangleAlert } from 'lucide-react';
import Modal from './modal';
import { useResumableImport } from './use-resumable-import';
import type { Account } from '@/lib/types';
import type { PriceImportChange, PriceImportResult } from '@/lib/pricing-types';

const actions: Record<PriceImportChange['action'], string> = { create: 'Crear', update: 'Actualizar', delete: 'Eliminar' };
const target = (change: PriceImportChange) => change.kind === 'list_price' ? `Lista ${change.list}` : change.kind === 'floor_price' ? 'Precio mínimo' : 'Línea';
const units = (value: number) => value.toLocaleString('es-PA');

// Supplier-only: previews and applies the account's private prices; nothing here is shown to clients.
export default function SupplierPriceImport({ account, onClose, onSaved }: { account: Account; onClose: () => void; onSaved: (result: PriceImportResult) => void }) {
  const fileInput = useRef<HTMLInputElement>(null);
  const fileId = useId();
  // Each confirmation names the job it was given for: a fresh review (after a 409, or the same file again) must be confirmed again.
  const [reviewedBig, setReviewedBig] = useState('');
  const [createLists, setCreateLists] = useState('');
  const base = `/api/market/accounts/${account.id}/prices/import`;
  const { file, result, pendingJob, busy, error, needsReview, pausing, interrupted, completed, expired, jobUrl, isOperating, review, recover, importBatches, selectFile, requestPause } =
    useResumableImport<PriceImportResult>({ base, storageKey: `motionpartes.price-import-job:${account.id}`, onCompleted: onSaved,
      emptyMessage: 'El archivo no trae cambios de precio para aplicar.',
      batchBody: (): Record<string, boolean> => ({ acknowledge_big_changes: bigReviewed, confirm_new_lists: listsConfirmed }) });
  function choose(next: File | null) {
    if (isOperating()) return;
    setReviewedBig(''); setCreateLists(''); selectFile(next);
  }
  const progress = result?.progress;
  const requires = result?.requires;
  const bigReviewed: boolean = !!result && reviewedBig === result.job_id, listsConfirmed: boolean = !!result && createLists === result.job_id;
  const confirmed = !!requires && (!requires.acknowledge_big_changes || bigReviewed) && (!requires.confirm_new_lists || listsConfirmed);
  const canImport = !!result?.valid && !!pendingJob && !!progress?.total_batches && !completed && !needsReview && !expired && confirmed;
  const close = () => { if (!isOperating()) onClose(); };
  const summary = result?.summary, applied = result?.applied_summary;
  const notices = result ? [
    ...result.lists_to_create.map(list => `Se creará la lista ${list.code} (${list.currency})${list.is_default ? ' como tu lista predeterminada' : ''}.`),
    ...result.lists.filter(list => list.skipped).map(list => `La columna ${list.header} no trae precios nuevos: no se creará la lista ${list.code}.`),
    ...(summary && (summary.line_updates || summary.floor_updates) ? [`También cambian ${units(summary.line_updates)} ${summary.line_updates === 1 ? 'línea' : 'líneas'} y ${units(summary.floor_updates)} ${summary.floor_updates === 1 ? 'precio mínimo' : 'precios mínimos'}.`] : []),
    ...(result.ignored_columns.length ? [`Columnas ignoradas: ${result.ignored_columns.join(', ')}.`] : []),
  ] : [];
  return <Modal title="Importar precios desde Excel" wide onClose={close}><div className="supplier-import price-import">
    <p className="modal-description">Sube la exportación de precios de tu ERP tal como está. La revisión no cambia ningún precio: verás cada cambio antes de aplicarlo. Estos precios son privados y nunca se muestran en el catálogo.</p>
    <div className="supplier-import-template"><a className="button soft small" href={`${base}/template`} download><Download size={16}/>Descargar plantilla</a><p>Obligatoria: <strong>ID_INVENTARIO_PROVEEDOR</strong>. <strong>PRECIO</strong> es tu lista predeterminada y <strong>PRECIO_&lt;CÓDIGO&gt;</strong> cada una de tus otras listas; puedes traer varias listas en el mismo archivo. LINEA y PRECIO_MINIMO son opcionales. CODIGO solo se compara con tu inventario. Las demás columnas, como COSTO, se ignoran y nunca se guardan.</p></div>
    <form onSubmit={review} autoComplete="off" className="supplier-import-form">
      <label htmlFor={fileId}>Archivo Excel de precios</label>
      <div className="supplier-import-picker"><input ref={fileInput} id={fileId} type="file" accept=".xlsx" className="sr-only" disabled={!!busy} onChange={event => choose(event.target.files?.[0] || null)}/><button className="button soft small" type="button" disabled={!!busy} onClick={() => fileInput.current?.click()}><FileSpreadsheet size={16}/>{result || pendingJob ? 'Elegir otro archivo' : 'Seleccionar archivo'}</button><span>{file?.name || result?.filename || 'Ningún archivo seleccionado'}</span></div>
      <p className="form-footnote">Las celdas vacías conservan el precio actual. Escribe BORRAR para quitar un precio. Un precio 0 se ignora. Acepta 12.50, 12,50, $ 12.50, 1,234.50 y 1.234,50; usa como máximo dos decimales. Los artículos que no estén en el archivo conservan sus precios.</p>
      <button className="button soft full" disabled={!file || !!busy}>{busy === 'review' ? <LoaderCircle className="spin" size={17}/> : <FileSpreadsheet size={17}/>}Revisar archivo</button>
    </form>
    {busy === 'recover' && <div className="loading" role="status"><LoaderCircle className="spin"/>Recuperando tu importación…</div>}
    {error && <div className="notice error" role="alert">{error}{needsReview && <p>Selecciona el archivo y revísalo de nuevo. Los lotes ya guardados se conservarán.</p>}</div>}
    {pendingJob && !result && !busy && <button className="button soft" onClick={() => recover(pendingJob)}>Recuperar progreso</button>}
    {result && summary && applied && <div className="supplier-import-preview">
      <div className="supplier-import-preview-heading"><h3>{completed ? 'Importación completada' : 'Revisa los cambios'}</h3><span>{result.filename}</span></div>
      {completed && <div className="notice success" role="status"><CheckCircle2 size={18}/><span>Se aplicaron {units(applied.created)} precios nuevos, {units(applied.updated)} actualizados y {units(applied.removed)} eliminados.{applied.line_updates + applied.floor_updates > 0 && ` También se guardaron ${units(applied.line_updates)} líneas y ${units(applied.floor_updates)} precios mínimos.`}{summary.rejected_rows > 0 && ` ${units(summary.rejected_rows)} filas siguen pendientes; puedes descargar sus errores y cargar el archivo corregido.`}</span></div>}
      <div className="supplier-import-summary price-import-summary">
        <span className="credit"><strong>{units(summary.new_prices)}</strong>Precios nuevos</span><span><strong>{units(summary.increases)}</strong>Suben</span><span><strong>{units(summary.decreases)}</strong>Bajan</span>
        <span><strong>{units(summary.unchanged)}</strong>Sin cambios</span><span><strong>{units(summary.removed)}</strong>Se eliminan</span>
        <span className={summary.big_changes ? 'debit' : ''}><strong>{units(summary.big_changes)}</strong>Cambios grandes</span><span className={summary.rejected_rows ? 'debit' : ''}><strong>{units(summary.rejected_rows)}</strong>Errores</span>
      </div>
      {notices.length > 0 && <ul className="price-import-notices">{notices.map(notice => <li key={notice}>{notice}</li>)}</ul>}
      {!result.valid && !completed && summary.valid_rows > 0 && <p className="notice">El archivo no trae cambios: sus precios coinciden con los actuales.</p>}
      {summary.rejected_rows > 0 && <div className="supplier-import-errors"><div className="notice error">{units(summary.rejected_rows)} {summary.rejected_rows === 1 ? 'fila con errores queda' : 'filas con errores quedan'} fuera de esta importación. Puedes aplicar las válidas y corregir las demás después.</div><a className="button soft small" href={`${jobUrl(result.job_id)}/errors`} download><Download size={16}/>Descargar filas con errores</a><p className="form-footnote">El archivo PRECIOS contiene las filas pendientes con una columna ERRORES. Corrígelo y cárgalo aquí de nuevo.</p><details><summary>Ver errores{result.error_count > result.errors.length ? ` · primeros ${result.errors.length} de ${units(result.error_count)}` : ''}</summary><div className="table-scroll"><table><thead><tr><th>Fila</th><th>Columna</th><th>Error</th></tr></thead><tbody>{result.errors.map((item, index) => <tr key={`${item.row}-${item.column}-${index}`}><td>{item.row}</td><td>{item.column}</td><td>{item.message}</td></tr>)}</tbody></table></div></details></div>}
      {result.warning_count > 0 && <details className="supplier-import-warnings"><summary>{units(result.warning_count)} advertencias</summary><ul>{result.warnings.map((item, index) => <li key={`${item.row}-${index}`}>Fila {item.row} · {item.column}: {item.message}</li>)}</ul>{result.warning_count > result.warnings.length && <p>Se muestran las primeras {result.warnings.length} advertencias.</p>}</details>}
      {!!progress?.total_batches && <div className="supplier-import-progress" aria-live="polite"><div><strong>{busy === 'import' ? pausing ? 'Pausando después de este lote…' : 'Aplicando precios…' : completed ? 'Todos los lotes guardados' : progress.completed_batches ? 'Importación en pausa' : 'Lista para aplicar'}</strong><span>{units(progress.processed_rows)} de {units(progress.total_rows)} filas · {progress.completed_batches} de {progress.total_batches} lotes</span></div><progress aria-label="Progreso de importación de precios" value={progress.completed_batches} max={progress.total_batches}/>{busy === 'import' && <button type="button" className="button soft small" disabled={pausing} onClick={requestPause}><Pause size={15}/>Pausar después del lote</button>}</div>}
      {expired && !completed && <p className="notice error">Esta revisión ha caducado. Selecciona el archivo y revísalo de nuevo para aplicarlo; los lotes ya guardados se conservan.</p>}
      {!completed && requires && (requires.acknowledge_big_changes || requires.confirm_new_lists) && <div className="price-import-confirm">
        {requires.acknowledge_big_changes && <label className="admin-checkbox"><input type="checkbox" checked={bigReviewed} disabled={!!busy} onChange={event => setReviewedBig(event.target.checked ? result.job_id : '')}/>Revisé los cambios mayores al 50 %</label>}
        {requires.confirm_new_lists && <label className="admin-checkbox"><input type="checkbox" checked={listsConfirmed} disabled={!!busy} onChange={event => setCreateLists(event.target.checked ? result.job_id : '')}/>Crear las listas nuevas</label>}
      </div>}
      {result.preview.length > 0 && <><p className="supplier-import-preview-caption">{result.preview_count > result.preview.length ? `Se muestran los primeros ${result.preview.length} de ${units(result.preview_count)} cambios, empezando por los cambios grandes.` : `${units(result.preview_count)} ${result.preview_count === 1 ? 'cambio' : 'cambios'} en la revisión.`}</p>
        <div className="table-scroll supplier-import-table price-import-table"><table><thead><tr><th>Fila / ID del proveedor</th><th>Código / repuesto</th><th>Cambio</th><th>Actual</th><th>Nuevo</th><th>Variación</th><th>Acción</th></tr></thead>
          <tbody>{result.preview.map(item => <tr key={`${item.row}-${item.kind}-${item.list}`} className={item.big_change ? 'big' : ''}><td><span>Fila {item.row}</span><strong>{item.supplier_invent_id}</strong></td><td><strong>{item.codigo}</strong>{item.description && <small>{item.description}</small>}</td><td>{target(item)}</td>
            <td className="number-cell">{item.current ?? '—'}</td><td className="number-cell"><strong>{item.new ?? '—'}</strong></td>
            <td className="number-cell">{item.change_percent === null ? '—' : <>{Number(item.change_percent) > 0 ? '+' : ''}{item.change_percent} %{item.big_change && <small className="price-import-big"><TriangleAlert size={12}/>Cambio grande</small>}</>}</td><td>{actions[item.action]}</td></tr>)}</tbody></table></div></>}
      {completed ? <button type="button" className="button primary full" onClick={close}>Cerrar</button> : <button type="button" className="button primary full" disabled={!canImport || !!busy} onClick={importBatches}>{busy === 'import' ? <LoaderCircle className="spin" size={17}/> : interrupted || progress?.completed_batches ? <Play size={17}/> : <CheckCircle2 size={17}/>} {interrupted || progress?.completed_batches ? 'Reanudar importación' : summary.rejected_rows ? 'Aplicar precios válidos' : 'Aplicar precios'}</button>}
      {busy === 'import' && <p className="form-footnote">Espera a que termine o pausa después del lote antes de cerrar. Puedes reanudar el progreso guardado al volver a abrir este panel.</p>}
    </div>}
  </div></Modal>;
}
