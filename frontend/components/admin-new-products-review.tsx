'use client';
import { useCallback, useEffect, useRef, useState } from 'react';
import { ArrowLeft, Ban, BrainCircuit, Check, Download, Link2, LoaderCircle, OctagonPause, PackageCheck, Pause, Play, X } from 'lucide-react';
import Modal from './modal';
import { request } from '@/lib/types';
import { ActionableRow, BulkResult, ReviewOverview, ReviewReport, ReviewTab, ReviewVerdictRow, rejectReasons, verdictLabels } from '@/lib/new-product-types';

const base = '/api/management/catalog/proposals';
const CHUNK = 50;   // proposals per bulk call: each approval creates a SKU, well inside the proxy's 120 s
const number = (value: number) => value.toLocaleString('es-PA');
const percent = (value: number) => `${Math.round(value * 100)} %`;
const usd = (micro: number) => `US$ ${(micro / 1_000_000).toFixed(2)}`;
const errorText = (error: unknown) => error instanceof Error ? error.message : 'No se pudo completar. Inténtalo de nuevo.';
const date = (value: string) => new Date(value).toLocaleString('es-PA', { timeZone: 'America/Panama', dateStyle: 'short', timeStyle: 'short' });
const tabs: { value: ReviewTab; label: string }[] = [{ value: 'good', label: 'Buenas' }, { value: 'unsure', label: 'Dudosas' }, { value: 'bad', label: 'Malas' },
  { value: 'stale', label: 'Ya no vigentes' }, { value: 'decided', label: 'Ya decididas' }];
const fieldLabels: Record<string, string> = { part_type: 'grupo/subgrupo', description: 'descripción', internal_code: 'código interno', oem: 'OEM quitado' };
const signalLabels: Record<string, string> = { descripcion_de_relleno: 'DESCRIPCIÓN DE RELLENO', sin_marca: 'SIN MARCA', marca_es_el_proveedor: 'MARCA = PROVEEDOR',
  sin_existencias: 'SIN EXISTENCIAS', proveedor_ya_no_lo_usa: 'NO USO' };
type Action = 'approve' | 'reject' | 'link';

function download(name: string, body: string) {
  const url = URL.createObjectURL(new Blob([body], { type: 'text/csv;charset=utf-8' }));
  const link = document.createElement('a');
  link.href = url; link.download = name; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}
// Text starting with = + - @ would run as a formula in a spreadsheet: an apostrophe keeps it text (mall.oem_apply.csv_cell does the same).
const cell = (value: string | number) => { const text = String(value); return `"${(/^[=+\-@\t\r]/.test(text) ? `'${text}` : text).replace(/"/g, '""')}"`; };

// Revisión IA: the AI reviews the pending Productos nuevos and separates the good ones (approve as they are) from the bad ones (reject, or link
// to the SKU the catalog already has) and the doubtful ones (a person decides). It fills grupo/subgrupo, typo fixes and «código interno» from our
// own data. The administrator reads the report and decides, one by one (Detalle) or in batch.
// refresh and message come from the drawer a report card opened: a decision or an edit there reloads the report and says what happened.
export default function NewProductsReview({ search, onClose, onChanged, onOpen, refresh = 0, message = '' }: { search: string; onClose: () => void; onChanged: () => void;
  onOpen: (id: string) => void; refresh?: number; message?: string }) {
  const [overview, setOverview] = useState<ReviewOverview | null>(null);
  const [report, setReport] = useState<ReviewReport | null>(null);
  const [tab, setTab] = useState<ReviewTab>('good');
  const [onlyUnreviewed, setOnlyUnreviewed] = useState(true);
  const [busy, setBusy] = useState<'' | 'start' | 'review' | 'load' | 'export'>('');
  const [pauseRequested, setPauseRequested] = useState(false);
  const [error, setError] = useState(''); const [notice, setNotice] = useState('');
  const [action, setAction] = useState<Action | null>(null);
  const [batchRunning, setBatchRunning] = useState(false);   // a batch approve, reject or link in progress: nothing closes the modal under it
  const batchRef = useRef(false);   // read by the close guard, which may run before React renders the new state
  const running = useCallback((value: boolean) => { batchRef.current = value; setBatchRunning(value); }, []);
  const [revision, setRevision] = useState(0);
  const pause = useRef(false); const requestId = useRef<string | null>(null); const operating = useRef(false);
  useEffect(() => {
    let live = true;
    request<ReviewOverview>(`${base}/review?search=${encodeURIComponent(search)}`).then(data => { if (live) setOverview(data); }).catch(e => { if (live) setError(errorText(e)); });
    return () => { live = false; };
  }, [search, revision]);
  const load = useCallback(async (id: string, wanted: ReviewTab, offset = 0) => {
    const data = await request<ReviewReport>(`${base}/review/${id}?verdict=${wanted}&offset=${offset}`);
    setReport(data); setTab(wanted);
    return data;
  }, []);
  async function open(id: string, wanted: ReviewTab = tab, offset = 0) {
    if (operating.current) return;
    operating.current = true; setBusy('load'); setError('');
    try { await load(id, wanted, offset); } catch (e) { setError(errorText(e)); }
    finally { operating.current = false; setBusy(''); }
  }
  useEffect(() => {   // the drawer decided or saved a proposal: the report shows it now
    if (!refresh || !report || operating.current) return;
    if (message) setNotice(message);
    load(report.id, tab, report.offset).then(data => {   // the last row of a page decided: show the page before it
      if (!data.results.length && data.offset > 0) return load(data.id, tab, Math.max(0, data.offset - CHUNK));
    }).catch(() => { /* the report keeps what it showed */ });
  }, [refresh]);   // eslint-disable-line react-hooks/exhaustive-deps
  async function review(current: ReviewReport, wanted: ReviewTab = tab) {
    pause.current = false; setPauseRequested(false); setBusy('review'); setNotice('');
    try {
      while (current.completed_batches < current.total_batches && !pause.current) {
        current = await request<ReviewReport>(`${base}/review/${current.id}?verdict=${wanted}`, { method: 'POST', body: JSON.stringify({ mode: 'review', batch_index: current.completed_batches }) });
        setReport(current);
      }
      setNotice(current.completed_batches < current.total_batches ? 'Revisión en pausa. Lo revisado quedó guardado.' : 'Revisión terminada. Lee el informe y decide.');
      onChanged();
    } catch (e) {
      setError(errorText(e));
      try { await load(current.id, wanted); } catch { /* the error above says enough */ }   // a lost answer may have saved the batch
    } finally { operating.current = false; setBusy(''); setPauseRequested(false); }
  }
  async function start() {
    if (operating.current || !overview) return;
    operating.current = true; setBusy('start'); setError(''); setNotice('');
    requestId.current ||= crypto.randomUUID();
    try {
      const created = await request<ReviewReport>(`${base}/review`, { method: 'POST', body: JSON.stringify({ id: requestId.current, search, only_unreviewed: onlyUnreviewed }) });
      requestId.current = null; setReport(created); setTab('good');
      await review(created, 'good');
    } catch (e) { setError(errorText(e)); operating.current = false; setBusy(''); }
  }
  async function resume() {
    if (!report || operating.current) return;
    operating.current = true; setError('');
    await review(report);
  }
  async function exportReport() {
    if (!report || operating.current) return;
    operating.current = true; setBusy('export'); setError('');
    try {
      const rows: ReviewVerdictRow[] = [];
      for (const wanted of tabs.map(item => item.value)) {
        let offset: number | null = 0;
        while (offset !== null) {
          const page: ReviewReport = await request<ReviewReport>(`${base}/review/${report.id}?verdict=${wanted}&offset=${offset}`);
          rows.push(...page.results); offset = page.next_offset;
        }
      }
      const head = ['SKU', 'Descripción', 'Marca', 'Veredicto', 'Estado', 'Confianza', 'Motivo sugerido', 'Razones', 'Grupo / subgrupo', 'Completado por IA', 'Vincular a', 'Existencias'];
      const lines = rows.map(row => [row.current.sku, row.current.description, row.current.brand, verdictLabels[row.verdict],
        row.state === 'fresh' ? 'VIGENTE' : row.state === 'stale' ? 'CAMBIÓ' : row.state === 'superseded' ? 'REVISADA DE NUEVO' : row.current.status_label.toUpperCase(),
        percent(row.confidence), row.reason_label,
        row.reasons.join(' / '), row.current.part_type ? `${row.current.part_type.category} / ${row.current.part_type.name}` : '',
        (row.applied.written ?? []).map(field => fieldLabels[field] ?? field).join(', '), row.link_part?.sku ?? '', row.stock].map(cell).join(','));
      download(`revision-ia-productos-nuevos-${report.created_at.slice(0, 10)}.csv`, `﻿${[head.map(cell).join(','), ...lines].join('\n')}`);
    } catch (e) { setError(errorText(e)); }
    finally { operating.current = false; setBusy(''); }
  }
  const locked = !!busy || batchRunning;
  const enabled = !!overview && overview.configured && overview.cap_micro_usd > 0;
  const toReview = overview ? (onlyUnreviewed ? overview.unreviewed_count : overview.eligible_count) : 0;
  return <Modal title="Revisión IA de productos nuevos" wide className="inventory-ai-modal review-ai-modal" onClose={() => { if (!operating.current && !batchRef.current) onClose(); }}>
    <div className="inventory-ai-intro"><span className="inventory-ai-icon"><BrainCircuit size={26}/></span><div><h3>Separa los buenos de los malos</h3>
      <p>La IA revisa los productos por revisar con los datos del catálogo y de los proveedores (sin buscar en internet), completa grupo/subgrupo y errores evidentes, y te deja un informe. Tú decides qué aprobar o rechazar.</p></div></div>
    {error && <div className="notice error" role="alert">{error}</div>}
    {notice && <p className="notice success" role="status"><Check size={16}/>{notice}</p>}
    {!report ? <>
      {!overview && !error && <div className="loading"><LoaderCircle className="spin"/>Cargando…</div>}
      {overview && <>
        <div className="inventory-ai-scope" aria-live="polite"><strong>{number(toReview)} {toReview === 1 ? 'producto por revisar' : 'productos por revisar'}{search ? ` con «${search}»` : ''}</strong>
          <p>Lotes de 40. Las reglas resuelven sin IA los códigos que el catálogo ya tiene, los «NO USO» y los datos de relleno. Puedes pausar y continuar después.
            Gasto de este mes: {usd(overview.month_spend_micro_usd)} de {usd(overview.cap_micro_usd)}.</p></div>
        <label className="admin-checkbox"><input type="checkbox" checked={onlyUnreviewed} disabled={locked} onChange={event => { setOnlyUnreviewed(event.target.checked); requestId.current = null; }}/>
          Solo los que no tienen una revisión vigente ({number(overview.unreviewed_count)} de {number(overview.eligible_count)})</label>
        {!overview.configured && <p className="notice">La IA no está configurada en el servidor (GEMINI_API_KEY).</p>}
        {overview.configured && overview.cap_micro_usd <= 0 && <p className="notice">La revisión IA está desactivada: su límite mensual (SKU_REVIEW_MONTHLY_USD) es 0.</p>}
        <button type="button" className="button primary full" disabled={locked || !enabled || !toReview} onClick={start}>
          {busy === 'start' ? <LoaderCircle size={17} className="spin"/> : <BrainCircuit size={17}/>}{busy === 'start' ? 'Preparando la revisión…' : `Revisar ${number(toReview)} con IA`}</button>
        {!!overview.runs.length && <section className="inventory-ai-history" aria-label="Revisiones guardadas"><h3>Revisiones guardadas</h3>
          {overview.runs.map(run => <button type="button" className="inventory-ai-history-row" key={run.id} disabled={locked} onClick={() => open(run.id, 'good')}>
            <div><strong>{run.search || 'TODOS LOS PENDIENTES'}</strong><small>{date(run.created_at)} · {run.created_by} · {number(run.total_items)} productos</small></div>
            <span>{run.status === 'completed' ? `${number(run.metrics.good ?? 0)} buenas · ${number(run.metrics.unsure ?? 0)} dudosas · ${number(run.metrics.bad ?? 0)} malas` : `${run.completed_batches}/${run.total_batches} lotes`}</span></button>)}</section>}
      </>}
    </> : <>
      <div className="inventory-ai-job-heading"><button type="button" className="button text small" disabled={locked} onClick={() => { setReport(null); setAction(null); setNotice(''); setError(''); setRevision(value => value + 1); }}><ArrowLeft size={15}/>Volver</button>
        <span>{report.search || 'TODOS LOS PENDIENTES'} · {number(report.total_items)} productos · {date(report.created_at)}</span></div>
      <div className="inventory-ai-progress" aria-live="polite"><div><strong>{report.completed_batches} de {report.total_batches} lotes revisados</strong>
        <span>{number(report.metrics.rule_items ?? 0)} por reglas · {number(report.metrics.ai_items ?? 0)} con IA · {usd(report.metrics.cost_micro_usd ?? 0)}</span></div>
        <progress aria-label="Progreso de la revisión" max={report.total_batches} value={report.completed_batches}/></div>
      <div className="admin-toolbar-actions inventory-ai-controls">
        {busy === 'review' ? <button type="button" className="button soft small" disabled={pauseRequested} onClick={() => { pause.current = true; setPauseRequested(true); }}><Pause size={15}/>{pauseRequested ? 'Pausando al terminar el lote…' : 'Pausar revisión'}</button>
          : report.completed_batches < report.total_batches && <button type="button" className="button soft small" disabled={locked || !enabled} onClick={resume}><Play size={15}/>Continuar revisión</button>}
        {busy === 'review' && <span className="fine-print"><LoaderCircle size={14} className="spin"/> Revisando el siguiente lote…</span>}
        <button type="button" className="button text small" disabled={locked || !report.count && !Object.values(report.counts).some(Boolean)} onClick={exportReport}>{busy === 'export' ? <LoaderCircle size={14} className="spin"/> : <Download size={14}/>}Descargar informe (CSV)</button>
      </div>
      <div className="review-ai-tiles" aria-label="Resumen de la revisión">
        <span className="good"><strong>{number(report.counts.good)}</strong>Buenas</span><span className="unsure"><strong>{number(report.counts.unsure)}</strong>Dudosas</span>
        <span className="bad"><strong>{number(report.counts.bad)}</strong>Malas{report.counts.link ? ` · ${number(report.counts.link)} ya en el catálogo` : ''}</span>
        <span><strong>{number(report.counts.stale)}</strong>Ya no vigentes</span><span><strong>{number(report.counts.decided)}</strong>Ya decididas</span></div>
      {!action && busy !== 'review' && <div className="oem-bulk-bar review-ai-actions" role="group" aria-label="Decidir en lote">
        <button type="button" className="button primary small" disabled={locked || !report.counts.good} onClick={() => setAction('approve')}><PackageCheck size={14}/>Aprobar {number(report.counts.good)} buenas…</button>
        <button type="button" className="button soft small danger-text" disabled={locked || !report.counts.reject} onClick={() => setAction('reject')}><Ban size={14}/>Rechazar {number(report.counts.reject)} malas…</button>
        {!!report.counts.link && <button type="button" className="button soft small" disabled={locked} onClick={() => setAction('link')}><Link2 size={14}/>Vincular {number(report.counts.link)} al catálogo…</button>}
        <span className="fine-print">Las dudosas se deciden una por una desde su detalle.</span></div>}
      {action && <ReviewBatch run={report.id} action={action} onRunning={running} onClose={() => setAction(null)}
        onDone={text => { setAction(null); running(false); setNotice(text); onChanged(); void open(report.id, tab); }}
        onPartial={() => { onChanged(); load(report.id, tab, report.offset).catch(() => {}); }}/>}
      <div className="admin-inventory-tabs" role="group" aria-label="Veredictos">
        {tabs.map(item => <button type="button" key={item.value} className={tab === item.value ? 'selected' : ''} aria-pressed={tab === item.value} disabled={locked}
          onClick={() => open(report.id, item.value)}>{item.label} · {number(report.counts[item.value])}</button>)}</div>
      {report.verdict === tab && <div className="review-ai-cards">
        {report.results.map(row => <ReviewCard key={row.id} row={row} onOpen={() => onOpen(row.proposal)}/>)}
        {!report.results.length && <p className="notice">{busy === 'review' ? 'Revisando…' : 'No hay productos en esta pestaña.'}</p>}</div>}
      {(report.offset > 0 || report.next_offset !== null) && <div className="inventory-ai-review-actions"><div className="admin-toolbar-actions">
        <button type="button" className="button soft small" disabled={locked || !report.offset} onClick={() => open(report.id, tab, Math.max(0, report.offset - CHUNK))}>Anterior</button>
        <span>{report.offset + 1}–{report.offset + report.results.length} de {number(report.count)}</span>
        <button type="button" className="button soft small" disabled={locked || report.next_offset === null} onClick={() => report.next_offset !== null && open(report.id, tab, report.next_offset)}>Siguiente</button></div></div>}
    </>}
  </Modal>;
}

function ReviewCard({ row, onOpen }: { row: ReviewVerdictRow; onOpen: () => void }) {
  const { current, suggestions } = row;
  const written = row.applied.written ?? [];
  const pill = row.state === 'stale' || row.state === 'superseded' ? 'stale' : row.verdict;
  const label = row.state === 'decided' ? current.status_label.toUpperCase() : row.state === 'superseded' ? 'REVISADA DE NUEVO' : verdictLabels[pill];
  return <article className={`review-ai-card ${row.state === 'fresh' ? row.verdict : row.state}`} aria-label={`Revisión de ${current.sku}`}>
    <div className="review-ai-card-heading"><strong>{current.sku}</strong>
      <span className={`review-ai-pill ${pill}`}>{label} · {percent(row.confidence)}</span>
      <small>{row.method === 'rule' ? 'REGLA' : 'IA'}</small></div>
    <p className="review-ai-description">{current.description || 'SIN DESCRIPCIÓN'}{current.brand ? <small> · {current.brand}</small> : null}</p>
    {row.supplier_description && row.supplier_description !== current.description && <small className="review-ai-before">Del proveedor: {row.supplier_description}</small>}
    <ul className="review-ai-reasons">{row.reasons.map(reason => <li key={reason}>{reason}</li>)}</ul>
    <div className="review-ai-chips">
      {row.verdict === 'bad' && row.reason_label && <span>MOTIVO: {row.reason_label.toUpperCase()}</span>}
      {row.link_part && <span>VINCULAR A {row.link_part.sku}</span>}
      {current.part_type && <span className={written.includes('part_type') ? 'ai' : ''}>{current.part_type.category} / {current.part_type.name}{written.includes('part_type') ? ' · IA' : ''}</span>}
      {!current.part_type && suggestions.part_type && <span className="hint">¿{suggestions.part_type.category} / {suggestions.part_type.name}? {percent(suggestions.part_type.confidence)}</span>}
      {current.oem && <span className="oem">OEM {current.oem.brand}: {current.oem.code}</span>}
      {written.includes('description') && <span className="ai">DESCRIPCIÓN CORREGIDA · IA</span>}
      {suggestions.description && !suggestions.description.drafted && !written.includes('description') && <span className="hint">¿{suggestions.description.value}? {percent(suggestions.description.confidence)}</span>}
      {suggestions.internal_code && !current.internal_code && <span className="hint">¿CÓDIGO INTERNO, NO OEM? {percent(suggestions.internal_code.confidence)}</span>}
      {current.internal_code && <span className={written.includes('internal_code') ? 'ai' : ''}>CÓDIGO INTERNO · NO OEM</span>}
      {suggestions.description?.drafted && <span className="hint">¿{suggestions.description.value}? (de un artículo con el mismo número)</span>}
      {suggestions.duplicate_of && <span className="hint">¿IGUAL A {suggestions.duplicate_of.kind === 'proposal' ? 'EL PRODUCTO NUEVO' : 'LA LÍNEA OEM'} {suggestions.duplicate_of.sku}?</span>}
      {(row.applied.kept ?? []).length > 0 && <span>SE RESPETÓ LO QUE EDITASTE</span>}
      {row.signals.map(signal => signalLabels[signal] && <span key={signal} className="signal">{signalLabels[signal]}</span>)}
      {row.stock > 0 && <span className="signal">{number(row.stock)} EN EXISTENCIA</span>}
    </div>
    <div className="review-ai-card-actions"><button type="button" className="button ghost small" aria-label={`Ver detalle de ${current.sku}`} onClick={onOpen}>Detalle</button></div>
  </article>;
}

function ReviewBatch({ run, action, onClose, onDone, onRunning, onPartial }: { run: string; action: Action; onClose: () => void; onDone: (text: string) => void;
  onRunning: (running: boolean) => void; onPartial: () => void }) {
  const [rows, setRows] = useState<ActionableRow[] | null>(null);
  const [checked, setChecked] = useState(false);
  const [error, setError] = useState(''); const [running, setRunning] = useState(false);
  const [progress, setProgress] = useState({ done: 0, sent: 0 }); const [failures, setFailures] = useState<{ sku: string; message: string }[]>([]);
  const stop = useRef(false);
  const verdict = action === 'approve' ? 'good' : action === 'reject' ? 'bad' : 'link';
  const [reload, setReload] = useState(0);   // after a failure the list is read again: only what is still pending goes in a retry
  useEffect(() => {
    let live = true;
    request<{ results: ActionableRow[] }>(`${base}/review/${run}?mode=actionable&verdict=${verdict}`).then(data => { if (live) setRows(data.results); }).catch(e => { if (live) setError(errorText(e)); });
    return () => { live = false; };
  }, [run, verdict, reload]);
  const title = action === 'approve' ? 'Aprobar las buenas' : action === 'reject' ? 'Rechazar las malas' : 'Vincular al catálogo';
  async function start() {
    if (!rows?.length || running) return;
    setRunning(true); onRunning(true); setError(''); stop.current = false;
    let done = 0, sent = 0; const failed: { sku: string; message: string }[] = [];
    try {
      for (let index = 0; index < rows.length && !stop.current; index += CHUNK) {
        const chunk = rows.slice(index, index + CHUNK);
        const proposals = chunk.map(row => action === 'approve' ? { id: row.id, version: row.version }
          : action === 'reject' ? { id: row.id, version: row.version, reason: row.reason, note: row.note } : { id: row.id, version: row.version, part: row.part });
        const result = await request<BulkResult>(`${base}/bulk`, { method: 'POST', body: JSON.stringify({ action, proposals }) });
        done += result.done ?? result.approved; sent += chunk.length;
        failed.push(...result.results.filter(entry => !entry.ok).map(entry => ({ sku: chunk.find(row => row.id === entry.id)?.sku ?? entry.id, message: entry.message ?? '' })));
        setProgress({ done, sent }); setFailures([...failed]);
      }
      const verb = action === 'approve' ? (done === 1 ? 'SKU creado' : 'SKU creados') : action === 'reject' ? (done === 1 ? 'propuesta rechazada' : 'propuestas rechazadas') : (done === 1 ? 'propuesta vinculada' : 'propuestas vinculadas');
      onDone(`${number(done)} ${verb}.${failed.length ? ` ${number(failed.length)} no se pudieron: ${failed.slice(0, 5).map(entry => `${entry.sku} (${entry.message})`).join('; ')}${failed.length > 5 ? '…' : ''}` : ''}${stop.current && sent < rows.length ? ` Se detuvo con ${number(rows.length - sent)} sin enviar.` : ''}`);
    } catch (e) {
      setError(errorText(e)); setRunning(false); onRunning(false); setChecked(false);
      onPartial();   // a lost answer may have decided some on the server: the report and the grid read them again
      setRows(null); setReload(value => value + 1);
    }
  }
  const withOem = rows?.filter(row => row.oem && !row.internal_code).length ?? 0;
  return <section className="oem-bulk review-ai-batch" aria-label={title}>
    <div className="oem-bulk-heading"><h3>{title}</h3>{!running && <button className="icon-button" aria-label="Cerrar" onClick={onClose}><X size={16}/></button>}</div>
    {error && <div className="notice error" role="alert">{error}</div>}
    {!rows && !error && <div className="loading"><LoaderCircle className="spin" size={18}/>Preparando la lista…</div>}
    {rows && <>
      <p><strong>{number(rows.length)} {rows.length === 1 ? 'producto' : 'productos'}</strong>{action === 'approve' ? ': se crea cada SKU con lo que la IA completó y se vinculan sus artículos.'
        : action === 'reject' ? ': se rechazan con el motivo que sugirió la IA y sus razones como nota.' : ': sus artículos se vinculan al SKU que el catálogo ya tiene.'} Solo entran los que no cambiaron desde la revisión; cada uno se vuelve a comprobar al decidirlo.</p>
      {action === 'approve' && withOem > 0 && <p className="review-ai-oem-note">{number(withOem)} {withOem === 1 ? 'se creará' : 'se crearán'} con su código como número OEM de la marca (marcados «OEM»). Si alguno es mercancía o un código interno, márcalo en su detalle antes de aprobar.</p>}
      {!!rows.length && <ul className="review-ai-sample">{rows.slice(0, 8).map(row => <li key={row.id}><strong>{row.sku}</strong> {row.description}{action === 'reject' && row.reason ? ` · ${rejectReasons[row.reason as keyof typeof rejectReasons] ?? row.reason}` : ''}{action === 'approve' && row.oem && !row.internal_code ? ` · OEM ${row.oem.brand}` : ''}{row.internal_code ? ' · CÓDIGO INTERNO' : ''}</li>)}
        {rows.length > 8 && <li>… y {number(rows.length - 8)} más.</li>}</ul>}
      {!running && <label className="admin-checkbox"><input type="checkbox" checked={checked} onChange={event => setChecked(event.target.checked)}/>Revisé el informe</label>}
      {running && <><div className="oem-progress" role="progressbar" aria-label={`Progreso: ${title}`} aria-valuemin={0} aria-valuemax={rows.length} aria-valuenow={progress.sent}><span style={{ width: `${rows.length ? Math.round(progress.sent / rows.length * 100) : 100}%` }}/></div>
        <p role="status">{number(progress.sent)} / {number(rows.length)} · {number(progress.done)} hechas{failures.length ? ` · ${number(failures.length)} no se pudieron` : ''}</p></>}
      <div className="suffix-actions">
        {running ? <button className="button soft small" onClick={() => { stop.current = true; }}><OctagonPause size={14}/>Detener después de este lote</button>
          : <button className={`button small ${action === 'reject' ? 'soft danger-text' : 'primary'}`} disabled={!checked || !rows.length} onClick={() => void start()}>
            {action === 'approve' ? <PackageCheck size={14}/> : action === 'reject' ? <Ban size={14}/> : <Link2 size={14}/>}
            {action === 'approve' ? `Aprobar ${number(rows.length)}` : action === 'reject' ? `Rechazar ${number(rows.length)}` : `Vincular ${number(rows.length)}`}</button>}
        {!running && <button className="button text small" onClick={onClose}>Cancelar</button>}</div>
    </>}
  </section>;
}
