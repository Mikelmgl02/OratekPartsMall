'use client';
import { useEffect, useRef, useState } from 'react';
import { LoaderCircle, RefreshCw } from 'lucide-react';
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
  return <section className="integration-note apiag-live" aria-label="Conexión con apiag-cloud">
    <span className="apiag-mark">A</span>
    <div className="apiag-live-body">
      <div className="apiag-live-head"><h3>Conexión con apiag-cloud</h3><span className={`status ${stateClass[status.state] || ''}`}>{stateLabel[status.state]}</span></div>
      <p>{status.server}{status.last_success_at ? ` · Última sincronización: ${when(status.last_success_at)}` : ' · Todavía no se ha sincronizado.'}</p>
      {status.state === 'running'
        ? <p>{units(counts.received)} artículos recibidos hasta ahora.</p>
        : status.last_mode && <p>Sincronización {modeLabel[status.last_mode]}: {units(counts.created)} nuevos · {units(counts.updated)} actualizados · {units(counts.withdrawn)} retirados{counts.refused || counts.conflicts ? ` · ${units((counts.refused ?? 0) + (counts.conflicts ?? 0))} no aplicados` : ''}.</p>}
      <p>{status.sucursales.length ? `Sucursales publicadas: ${status.sucursales.map(sucursal => sucursal.nombre).join(', ')}.`
        : 'Ninguna sucursal publicada: elige en apiag-cloud, en la pestaña MotionPartes, de qué sucursales ve existencias MotionPartes.'}</p>
      {!status.token_readable && <div className="notice error" role="alert">El token guardado ya no se puede leer: pide a MotionPartes que lo vuelva a ingresar.</div>}
      {status.error && status.state !== 'running' && <div className="notice error" role="alert">{status.error.message}</div>}
      {error && <div className="notice error" role="alert">{error}</div>}
      {notice && <div className="notice success" role="status">{notice}</div>}
      {status.samples.length > 0 && status.state !== 'running' && <details className="apiag-live-samples"><summary>Artículos no aplicados en la última sincronización</summary><ul>{status.samples.map(sample => <li key={sample}>{sample}</li>)}</ul></details>}
      <p className="form-footnote">Se sincroniza sola cada {status.interval_minutes} minutos y repasa las últimas 24 horas una vez al día. Los artículos de apiag-cloud no se editan con Excel ni con «Actualizar existencias».</p>
    </div>
    <button type="button" className="button soft small" disabled={!status.can_sync || busy} onClick={sync}>
      {live || busy ? <LoaderCircle className="spin" size={15}/> : <RefreshCw size={15}/>}{status.state === 'running' ? 'Sincronizando…' : status.state === 'queued' ? 'En cola…' : 'Sincronizar ahora'}</button>
  </section>;
}
