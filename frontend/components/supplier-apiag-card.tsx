'use client';
import { useEffect, useId, useRef, useState } from 'react';
import { ChevronDown, ChevronUp, LoaderCircle, RefreshCw } from 'lucide-react';
import { ApiagSyncState, ApiagSyncStatus, request } from '@/lib/types';

const when = (value: string | null) => value ? new Date(value).toLocaleString('es-PA', { timeZone: 'America/Panama', dateStyle: 'medium', timeStyle: 'short' }) : '';
const units = (value: number | undefined) => (value ?? 0).toLocaleString('es-PA');
const stateLabel: Record<ApiagSyncState, string> = { idle: 'Sin sincronizar', queued: 'En cola…', running: 'Sincronizando…', completed: 'Sincronizada', failed: 'Con error', paused: 'En pausa' };
const stateClass: Partial<Record<ApiagSyncState, string>> = { completed: 'matched', failed: 'review', paused: 'review' };
const modeLabel = { full: 'completa', changes: 'de cambios', day: 'de las últimas 24 horas' };

// The supplier's pull from apiag-cloud: state, last result and «Sincronizar ahora». The pull runs in the apiag-sync service, so the
// card only asks for a run and re-reads the state every few seconds while one is queued or running. Without a connection (or when the
// state cannot be read) it renders nothing and tells the panel, which keeps its static note.
export default function SupplierApiagCard({ accountId, revision, onConnected, onSynced }: { accountId: string; revision: number;
  onConnected: (connected: boolean) => void; onSynced: () => void }) {
  const [status, setStatus] = useState<ApiagSyncStatus | null>(null);
  const [busy, setBusy] = useState(false); const [error, setError] = useState(''); const [notice, setNotice] = useState('');
  const [open, setOpen] = useState(false); const detailsId = useId();
  const finished = useRef<string | null | undefined>(undefined);
  const path = `/api/market/accounts/${accountId}/apiag`;
  const live = status?.connected && (status.state === 'queued' || status.state === 'running');
  useEffect(() => { finished.current = undefined; setNotice(''); setError(''); }, [accountId]);
  useEffect(() => {
    let cancelled = false;
    request<ApiagSyncStatus>(path).then(data => { if (!cancelled) setStatus(data); }).catch(() => { if (!cancelled) setStatus({ connected: false }); });
    return () => { cancelled = true; };
  }, [path, revision]);
  useEffect(() => { onConnected(!!status?.connected); }, [status?.connected, onConnected]);
  useEffect(() => {
    if (!status?.connected) return;
    // A run that finished since the last reading changed the inventory: the panel reloads its grid and totals.
    if (finished.current !== undefined && status.last_finished_at !== finished.current) { onSynced(); setNotice(''); }
    finished.current = status.last_finished_at;
  }, [status, onSynced]);
  useEffect(() => {
    if (!live) return;
    const timer = setInterval(() => {
      if (document.visibilityState !== 'visible') return;
      request<ApiagSyncStatus>(path).then(setStatus).catch(() => { /* keep the last state; the next tick tries again */ });
    }, 5000);
    return () => clearInterval(timer);
  }, [live, path]);
  async function sync() {
    setBusy(true); setError('');
    try { setStatus(await request<ApiagSyncStatus>(`${path}/sync`, { method: 'POST', body: '{}' })); setNotice('Sincronización en cola. Continúa aunque cierres esta página.'); }
    catch (error) { setError(error instanceof Error ? error.message : 'No se pudo pedir la sincronización.'); }
    finally { setBusy(false); }
  }
  if (!status?.connected) return null;
  const counts = status.state === 'running' ? status.progress : status.counts;
  const skipped = (counts.refused ?? 0) + (counts.conflicts ?? 0);
  // One line: state, server, last pull, its result and «Sincronizar ahora». The rest (mode, sucursales, schedule, what was not
  // applied) waits behind «Detalles»; errors always show in full.
  return <section className="apiag-bar" aria-label="Conexión con apiag-cloud">
    <div className="apiag-bar-row">
      <span className="apiag-mark small" aria-hidden="true">A</span>
      <strong>apiag-cloud</strong>
      <span className={`status ${stateClass[status.state] || ''}`}>{stateLabel[status.state]}</span>
      <span className="apiag-bar-meta">{status.server}{status.last_success_at ? ` · Última sincronización: ${when(status.last_success_at)}` : ' · Todavía no se ha sincronizado.'}</span>
      {status.state === 'running'
        ? <span className="apiag-bar-meta">{units(counts.received)} artículos recibidos hasta ahora.</span>
        : status.last_mode && <span className="apiag-bar-meta">{units(counts.created)} nuevos · {units(counts.updated)} actualizados · {units(counts.withdrawn)} retirados{skipped ? ` · ${units(skipped)} no aplicados` : ''}</span>}
      {!status.sucursales.length && <span className="status review">Ninguna sucursal publicada</span>}
      <span className="apiag-bar-actions">
        <button type="button" className="button text small" aria-expanded={open} aria-controls={detailsId} onClick={() => setOpen(value => !value)}>
          {open ? <ChevronUp size={14}/> : <ChevronDown size={14}/>}Detalles</button>
        <button type="button" className="button soft small" disabled={!status.can_sync || busy} onClick={sync}>
          {live || busy ? <LoaderCircle className="spin" size={15}/> : <RefreshCw size={15}/>}{status.state === 'running' ? 'Sincronizando…' : status.state === 'queued' ? 'En cola…' : 'Sincronizar ahora'}</button>
      </span>
    </div>
    {!status.token_readable && <div className="notice error" role="alert">El token guardado ya no se puede leer: pide a MotionPartes que lo vuelva a ingresar.</div>}
    {status.error && status.state !== 'running' && <div className="notice error" role="alert">{status.error.message}</div>}
    {error && <div className="notice error" role="alert">{error}</div>}
    {notice && <div className="notice success" role="status">{notice}</div>}
    {open && <div className="apiag-bar-details" id={detailsId}>
      {status.last_mode && status.state !== 'running' && <p>Sincronización {modeLabel[status.last_mode]}: {units(counts.created)} nuevos · {units(counts.updated)} actualizados · {units(counts.withdrawn)} retirados{skipped ? ` · ${units(skipped)} no aplicados` : ''}.</p>}
      <p>{status.sucursales.length ? `Sucursales publicadas: ${status.sucursales.map(sucursal => sucursal.nombre).join(', ')}.`
        : 'Ninguna sucursal publicada: elige en apiag-cloud, en la pestaña MotionPartes, de qué sucursales ve existencias MotionPartes.'}</p>
      {status.samples.length > 0 && status.state !== 'running' && <details className="apiag-live-samples"><summary>Artículos no aplicados en la última sincronización</summary><ul>{status.samples.map(sample => <li key={sample}>{sample}</li>)}</ul></details>}
      <p className="form-footnote">Se sincroniza sola cada {status.interval_minutes} minutos y repasa las últimas 24 horas una vez al día. Esta cuenta carga sus existencias solo desde apiag-cloud: no usa Excel ni «Actualizar existencias».</p>
    </div>}
  </section>;
}
