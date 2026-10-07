'use client';

import { FormEvent, useEffect, useRef, useState } from 'react';
import { ArrowLeft, Check, Download, LoaderCircle, OctagonPause, Play, Undo2, Wand2 } from 'lucide-react';
import Modal from './modal';
import OEMPending from './admin-oem-pending';
import { request } from '@/lib/types';
import { OEMRevertResult, OEMRun, OEMRunPage, OEMSpotCheck, skipLabels, stoppedLabels, tierLabels } from '@/lib/oem-finder-types';

const base = '/api/management/oem-finder';
const number = (value: number) => value.toLocaleString('es-PA');
const stamp = (value: string) => new Date(value).toLocaleString('es-PA', { timeZone: 'America/Panama' });
const errorMessage = (error: unknown) => error instanceof Error ? error.message : 'No se pudo completar la acción. Inténtalo de nuevo.';

function revertNotice(result: OEMRevertResult) {
  const parts = [`${number(result.reverted)} ${result.reverted === 1 ? 'cambio revertido' : 'cambios revertidos'}`];
  if (result.already_reverted) parts.push(`${number(result.already_reverted)} ya estaban revertidos`);
  if (result.skipped.length) parts.push(`${number(result.skipped.length)} sin revertir (${result.skipped.slice(0, 3).map(row => `${row.sku}: ${row.label}`).join('; ')})`);
  return parts.join(' · ') + '.';
}

function download(name: string, body: string, type: string) {
  const url = URL.createObjectURL(new Blob([body], { type }));
  const link = document.createElement('a');
  link.href = url; link.download = name; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

function scopeText(run: OEMRun) {
  const parts = [run.scope.tier ? tierLabels[run.scope.tier] || run.scope.tier : 'TODOS LOS NIVELES AUTOMÁTICOS'];
  if (run.scope.canary) parts.push(`CANARIO ${number(run.scope.canary)}`);
  else if (run.scope.limit) parts.push(`LÍMITE ${number(run.scope.limit)}`);
  if (run.scope.stage === 'admin') parts.push(`DESDE PENDIENTES${run.actor ? ` · ${run.actor}` : ''}`);
  return parts.join(' · ');
}

export default function OEMRuns({ onClose, onChanged }: { onClose: () => void; onChanged: () => void }) {
  const [data, setData] = useState<OEMRunPage | null>(null); const [error, setError] = useState(''); const [notice, setNotice] = useState('');
  const [page, setPage] = useState(1); const [revision, setRevision] = useState(0); const [busy, setBusy] = useState('');
  const [confirming, setConfirming] = useState<number | null>(null); const [spotRun, setSpotRun] = useState<OEMRun | null>(null);
  const [focus, setFocus] = useState<number | null>(null); const scrolled = useRef<number | null>(null);
  useEffect(() => {
    if (focus === null || scrolled.current === focus || !data?.results.some(run => run.id === focus)) return;
    scrolled.current = focus;
    const row = document.getElementById(`oem-run-${focus}`);
    row?.scrollIntoView({ block: 'center', behavior: 'smooth' }); row?.focus({ preventScroll: true });
  }, [data, focus]);
  function showRun(id: number) { scrolled.current = null; setFocus(id); setPage(1); setRevision(v => v + 1); }
  useEffect(() => {
    let live = true;
    request<OEMRunPage>(`${base}/runs?mode=apply_auto&stage=auto&page=${page}`).then(result => { if (live) setData(result); }).catch(e => { if (live) { setData(null); setError(errorMessage(e)); } });
    return () => { live = false; };
  }, [page, revision]);
  async function revert(run: OEMRun, part?: string) {
    setBusy(part ? `part:${part}` : `run:${run.id}`); setError(''); setNotice('');
    try {
      const result = await request<OEMRevertResult>(`${base}/runs/${run.id}/revert`, { method: 'POST', body: JSON.stringify(part ? { part } : {}) });
      setNotice(revertNotice(result)); setConfirming(null); setRevision(v => v + 1); if (result.reverted) onChanged();
      return true;
    } catch (e) { setError(errorMessage(e)); return false; }
    finally { setBusy(''); }
  }
  async function toggleHalt(event?: FormEvent<HTMLFormElement>) {
    event?.preventDefault();
    const reason = event ? String(new FormData(event.currentTarget).get('reason') || '').trim() : '';
    setBusy('halt'); setError(''); setNotice('');
    try {
      const result = await request<{ halt: OEMRunPage['halt'] }>(`${base}/halt`, { method: 'POST', body: JSON.stringify(event ? { action: 'halt', reason } : { action: 'resume' }) });
      setNotice(result.halt ? 'Aplicación automática detenida. Ninguna ejecución empezará ni continuará hasta levantar la detención.' : 'Detención levantada.');
      setRevision(v => v + 1);
    } catch (e) { setError(errorMessage(e)); }
    finally { setBusy(''); }
  }
  return <Modal title="Aplicación OEM automática" wide className="suffix-modal" onClose={onClose}>
    {spotRun ? <SpotCheck run={spotRun} busy={busy} onBack={() => { setSpotRun(null); setRevision(v => v + 1); }} onRevert={(part) => revert(spotRun, part)} notice={notice} error={error}/> : <>
      <p className="modal-description">Ejecuciones del buscador OEM que marcaron SKU como OEM o los renombraron a su base OEM (mismo SKU interno, existencias, fotos y alternos). Cada ejecución empieza con un canario, aplica primero los SKU con existencias en lotes de hasta 100 y deja una muestra de control de 20 filas. Si más de una fila de la muestra es incorrecta, detén la aplicación y deshaz la ejecución.</p>
      {data?.halt ? <div className="notice error" role="alert"><OctagonPause size={16}/>APLICACIÓN DETENIDA: {data.halt.reason} · {data.halt.created_by || 'SISTEMA'} · {stamp(data.halt.created_at)}<button className="button soft small" disabled={!!busy} onClick={() => void toggleHalt()}><Play size={14}/>Levantar detención</button></div>
        : data && <form className="suffix-inline-form" onSubmit={toggleHalt} aria-label="Regla de detención"><label>Motivo para detener<input name="reason" required maxLength={300} autoComplete="off" placeholder="EJ.: 2 FILAS INCORRECTAS EN LA MUESTRA"/></label><button className="button soft" disabled={!!busy}><OctagonPause size={15}/>Detener aplicación automática</button></form>}
      {error && <div className="notice error" role="alert">{error}{!data && <button onClick={() => { setError(''); setRevision(v => v + 1); }}>Intentar de nuevo</button>}</div>}
      {notice && <div className="notice success" role="status"><Check size={16}/>{notice}</div>}
      <OEMPending revision={revision} onApplied={run => { showRun(run.id); if (run.applied.applied) onChanged(); }} onShowRun={showRun}/>
      <h3 className="oem-runs-heading">Historial de ejecuciones</h3>
      {!data && !error && <div className="loading"><LoaderCircle className="spin" size={20}/>Cargando ejecuciones…</div>}
      {data && !data.results.length && <div className="empty-state"><Wand2 size={30}/><h3>Aún no hay ejecuciones automáticas.</h3><p>Se lanzan desde Pendientes (arriba) o en el servidor con python -m mall.oem_finder --apply-auto --canary 50.</p></div>}
      {!!data?.results.length && <div className="suffix-table-wrap"><table className="suffix-table">
        <thead><tr><th>Ejecución</th><th>Alcance</th><th>Resultado</th><th>Revertidos</th><th>Acciones</th></tr></thead>
        <tbody>{data.results.map(run => { const s = run.applied; return <tr key={run.id} id={`oem-run-${run.id}`} tabIndex={-1} className={focus === run.id ? 'oem-run-focus' : ''}>
          <td data-label="Ejecución"><strong>#{run.id}</strong><span>{stamp(run.started_at)}</span><span>{run.status === 'failed' ? 'FALLIDA' : run.status === 'running' ? 'EN CURSO' : 'COMPLETADA'} · {run.rules_version}</span></td>
          <td data-label="Alcance">{scopeText(run)}<span>{number(s.selected ?? 0)} SELECCIONADOS · {number(s.batches?.length ?? 0)} {s.batches?.length === 1 ? 'LOTE' : 'LOTES'}</span></td>
          <td data-label="Resultado"><strong>{number(s.applied ?? 0)} APLICADOS</strong>{!!s.conflicts && <span>{number(s.conflicts)} A REVISIÓN POR CONFLICTO</span>}{Object.entries(s.skipped ?? {}).map(([reason, n]) => <span key={reason}>{number(n)} OMITIDOS: {skipLabels[reason] || reason}</span>)}{!!s.errors && <span>{number(s.errors)} ERRORES</span>}{s.stopped && <span>{stoppedLabels[s.stopped]}</span>}</td>
          <td data-label="Revertidos" className="number-cell">{number(run.reverted)} / {number(run.changes)}</td>
          <td data-label="Acciones"><div className="suffix-actions">
            <button className="button soft small" aria-label={`Muestra de control de la ejecución ${run.id}`} disabled={!!busy || !s.spot_check?.length} onClick={() => { setNotice(''); setError(''); setSpotRun(run); }}>Muestra de control</button>
            {confirming === run.id ? <>
              <button className="button primary small" aria-label={`Confirmar deshacer la ejecución ${run.id}`} disabled={!!busy} onClick={() => void revert(run)}>{busy === `run:${run.id}` && <LoaderCircle size={14} className="spin"/>}Deshacer {number(run.changes - run.reverted)} cambios</button>
              <button className="button text small" onClick={() => setConfirming(null)}>Cancelar</button>
            </> : <button className="button text small" aria-label={`Deshacer la ejecución ${run.id}`} disabled={!!busy || run.changes === run.reverted} onClick={() => setConfirming(run.id)}><Undo2 size={14}/>Deshacer ejecución</button>}
          </div></td>
        </tr>; })}</tbody>
      </table></div>}
      {data && <div className="pagination"><span>{number(data.count)} {data.count === 1 ? 'ejecución' : 'ejecuciones'}</span><div><button className="button soft small" disabled={!data.previous} onClick={() => setPage(page - 1)}>Anterior</button><span>Página {page}</span><button className="button soft small" disabled={!data.next} onClick={() => setPage(page + 1)}>Siguiente</button></div></div>}
      <p className="form-footnote">Deshacer devuelve el SKU anterior si sigue libre, borra el OEM y los alternos que creó la ejecución, quita la marca OEM y registra el cambio inverso. Las filas en conflicto pasan a la revisión OEM (Agrupar SKU); nunca se renombran en silencio.</p>
    </>}
  </Modal>;
}

function SpotCheck({ run, busy, notice, error, onBack, onRevert }: { run: OEMRun; busy: string; notice: string; error: string; onBack: () => void; onRevert: (part: string) => Promise<boolean> }) {
  const [data, setData] = useState<OEMSpotCheck | null>(null); const [loadError, setLoadError] = useState(''); const [revision, setRevision] = useState(0);
  const [confirming, setConfirming] = useState('');
  useEffect(() => {
    let live = true;
    request<OEMSpotCheck>(`${base}/runs/${run.id}/spot-check`).then(result => { if (live) setData(result); }).catch(e => { if (live) setLoadError(errorMessage(e)); });
    return () => { live = false; };
  }, [run.id, revision]);
  return <div className="suffix-editor">
    <button className="button text small" onClick={onBack}><ArrowLeft size={15}/>Volver a las ejecuciones</button>
    <div className="suffix-editor-heading"><h3>Muestra de control · ejecución #{run.id}</h3>{data && <span className="status review">{number(data.rows.length)} FILAS</span>}</div>
    <p className="form-footnote">Revisa que cada SKU sea realmente el número OEM del fabricante para esa pieza. Descarga el CSV para anotar la columna correcto_si_no; si más de una fila es incorrecta, detén la aplicación y deshaz la ejecución completa.</p>
    {(error || loadError) && <div className="notice error" role="alert">{error || loadError}</div>}
    {notice && <div className="notice success" role="status"><Check size={16}/>{notice}</div>}
    {!data && !loadError && <div className="loading"><LoaderCircle className="spin" size={20}/>Cargando muestra…</div>}
    {data && <div className="suffix-actions"><button className="button soft small" onClick={() => download(`oem-run-${run.id}-spot-check.csv`, '\ufeff' + data.csv, 'text/csv;charset=utf-8')}><Download size={14}/>Descargar CSV</button><button className="button soft small" onClick={() => download(`oem-run-${run.id}-spot-check.json`, JSON.stringify({ run: data.run, rows: data.rows }, null, 2), 'application/json')}><Download size={14}/>Descargar JSON</button></div>}
    {!!data?.rows.length && <div className="suffix-table-wrap"><table className="suffix-table">
      <thead><tr><th>SKU</th><th>Descripción</th><th>OEM</th><th>Evidencia</th><th>Acciones</th></tr></thead>
      <tbody>{data.rows.map(row => <tr key={row.change}>
        <td data-label="SKU"><strong>{row.previous_sku === row.sku ? row.sku : `${row.previous_sku} → ${row.sku}`}</strong><span>{tierLabels[row.tier] || row.tier} · LOTE {row.batch}</span>{row.current_sku !== row.sku && <span>HOY: {row.current_sku}</span>}</td>
        <td data-label="Descripción">{row.description}<span>{number(row.available_quantity)} EN EXISTENCIA</span></td>
        <td data-label="OEM">{row.brand} {row.code}<span>{row.system} · {row.grade}</span></td>
        <td data-label="Evidencia">LÉXICO {(row.lexicon || '').toUpperCase()}{row.lexicon_keys != null && ` · ${number(row.lexicon_keys)} CLAVES`}{row.lexicon_share != null && ` · ${Math.round(row.lexicon_share * 100)} %`}<span>{row.class_heads}</span>{row.units && <span>{row.units.toUpperCase()}</span>}</td>
        <td data-label="Acciones">{row.reverted_at ? <span className="status matched">REVERTIDO</span> : confirming === row.part_id ? <div className="suffix-actions">
          <button className="button primary small" aria-label={`Confirmar deshacer ${row.sku}`} disabled={!!busy} onClick={async () => { if (await onRevert(row.part_id)) { setConfirming(''); setRevision(v => v + 1); } }}>{busy === `part:${row.part_id}` ? <LoaderCircle size={14} className="spin"/> : <Undo2 size={14}/>}Deshacer {row.previous_sku === row.sku ? 'la marca OEM' : `y volver a ${row.previous_sku}`}</button>
          <button className="button text small" onClick={() => setConfirming('')}>Cancelar</button>
        </div> : <button className="button text small" aria-label={`Deshacer ${row.sku}`} disabled={!!busy} onClick={() => setConfirming(row.part_id)}><Undo2 size={14}/>Deshacer este SKU</button>}</td>
      </tr>)}</tbody>
    </table></div>}
    {data && !data.rows.length && <div className="empty-state"><Wand2 size={30}/><h3>La ejecución no aplicó filas.</h3></div>}
  </div>;
}
