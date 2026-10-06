'use client';

import { FormEvent, useEffect, useRef, useState } from 'react';
import { request } from '@/lib/types';

export type ImportProgress = { batch_size: number; total_batches: number; completed_batches: number; total_rows: number; processed_rows: number; next_batch: number | null };
// The shared shape of a staged, owner-bound import job (stock or prices).
export type ResumableJob = { job_id: string; status: string; valid: boolean; imported: boolean; expires_at: string; progress: ImportProgress; summary: { rejected_rows: number } };

class ImportJobError extends Error {
  constructor(message: string, readonly status: number) { super(message); }
}
async function jobRequest<R>(url: string, body?: Record<string, unknown>): Promise<R> {
  let response: Response;
  try { response = await fetch(url, { cache: 'no-store', headers: { 'Content-Type': 'application/json' }, ...(body ? { method: 'POST', body: JSON.stringify(body) } : {}) }); }
  catch { throw new Error('Se perdió la conexión. Los lotes guardados se conservan; reanuda para recuperar el progreso.'); }
  let data;
  try { data = await response.json(); } catch { throw new Error('No se pudo confirmar el progreso. Reanuda la importación para recuperarlo.'); }
  if (!response.ok) throw new ImportJobError(typeof data.detail === 'string' ? data.detail : 'No se pudo continuar la importación.', response.status);
  return data;
}

/**
 * Upload, review, pause and resume a batched import job. Only the job id is kept in the browser (per account and import kind), so a
 * reload recovers the durable server progress; every retry first reads that progress, because a lost response may have saved its batch.
 */
export function useResumableImport<R extends ResumableJob>({ base, storageKey, emptyMessage, batchBody, onCompleted }: {
  base: string; storageKey: string; emptyMessage: string;
  // Extra fields sent with every batch (for example acknowledgements); read when each batch is sent.
  batchBody?: () => Record<string, unknown>;
  onCompleted: (result: R) => void;
}) {
  const [file, setFile] = useState<File | null>(null);
  const [result, setResult] = useState<R | null>(null);
  const [pendingJob, setPendingJob] = useState<string | null>(null);
  const [busy, setBusy] = useState<'review' | 'recover' | 'import' | null>(null);
  const [error, setError] = useState('');
  const [needsReview, setNeedsReview] = useState(false);
  const [pausing, setPausing] = useState(false);
  const [interrupted, setInterrupted] = useState(false);
  const mounted = useRef(false);
  const operating = useRef(false);
  const pauseRequested = useRef(false);
  const notified = useRef(false);
  const callbacks = useRef({ onCompleted, batchBody });
  callbacks.current = { onCompleted, batchBody };
  const jobUrl = (id: string) => `${base}/jobs/${encodeURIComponent(id)}`;

  function rememberJob(id: string | null) {
    try { if (id) localStorage.setItem(storageKey, id); else localStorage.removeItem(storageKey); } catch { /* Importing also works when browser storage is unavailable. */ }
  }

  function finish(next: R) {
    if (!mounted.current || next.status !== 'completed' || !next.imported || notified.current) return;
    notified.current = true;
    // Reopening a completed job with rejected rows must still offer its correction workbook.
    if (!next.summary.rejected_rows) rememberJob(null);
    callbacks.current.onCompleted(next);
  }

  function reportError(caught: unknown) {
    if (!mounted.current) return;
    if (caught instanceof ImportJobError && (caught.status === 404 || caught.status === 403)) {
      rememberJob(null); setPendingJob(null); setResult(null);
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
      const next = await jobRequest<R>(jobUrl(id));
      if (!mounted.current) return;
      setResult(next); setInterrupted(next.progress.completed_batches > 0); finish(next);
    } catch (caught) { reportError(caught); }
    finally { operating.current = false; if (mounted.current) setBusy(null); }
  }

  useEffect(() => {
    mounted.current = true;
    let id: string | null = null;
    try { id = localStorage.getItem(storageKey); } catch { /* Browser storage is optional. */ }
    if (id && /^[a-f0-9-]{36}$/i.test(id)) { setPendingJob(id); void recover(id); }
    return () => { mounted.current = false; pauseRequested.current = true; };
    // Callers key their dialog by account, so a different account gets its own job.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function review(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!file || operating.current) return;
    if (!file.name.toLowerCase().endsWith('.xlsx') || file.size > 5 * 1024 * 1024) { setError('Selecciona un archivo .xlsx de hasta 5 MB.'); return; }
    operating.current = true; setBusy('review'); setError('');
    const form = new FormData(); form.set('file', file);
    try {
      const next = await request<R>(base, { method: 'POST', body: form });
      if (!mounted.current) return;
      setResult(next); setPendingJob(next.job_id); rememberJob(next.job_id); setNeedsReview(false);
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
      let next = await jobRequest<R>(jobUrl(pendingJob));
      if (!mounted.current) return;
      setResult(next);
      if (next.status === 'completed' && next.imported) { finish(next); return; }
      if (!next.valid || !next.progress.total_batches) throw new Error(emptyMessage);
      while (mounted.current && !pauseRequested.current) {
        const before = next.progress;
        if (before.next_batch === null) throw new Error('No se pudo confirmar el siguiente lote. Reanuda para recuperar el progreso.');
        const saved = await jobRequest<R>(jobUrl(pendingJob), { batch_index: before.next_batch, ...callbacks.current.batchBody?.() });
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
    rememberJob(null); setFile(next); setResult(null); setPendingJob(null);
    setError(''); setNeedsReview(false); setInterrupted(false); notified.current = false;
  }

  function requestPause() { pauseRequested.current = true; setPausing(true); }

  const completed = result?.status === 'completed' && result.imported;
  const expired = !!result && new Date(result.expires_at).getTime() <= Date.now();
  return {
    file, result, pendingJob, busy, error, needsReview, pausing, interrupted, completed, expired, jobUrl,
    // True while an upload, recovery or batch is in flight: callers keep their dialog open until it ends.
    isOperating: () => operating.current,
    review, recover, importBatches, selectFile, requestPause,
  };
}
