'use client';

import { useEffect, useRef, useState } from 'react';
import { ArrowRight, Check, LoaderCircle } from 'lucide-react';
import { Account, BasketLine, request } from '@/lib/types';
import type { RequestSubmissionResult } from '@/lib/request-types';
import { DraftSubmission, finishDraftSubmission, readSubmissions, requestPayloadLines, submissionStorageKey } from '@/lib/basket-submissions';

export default function BasketSubmission({ lines, account, preview, onPending, onSent }: {
  lines: BasketLine[]; account?: Account; preview: boolean; onPending: (pending: boolean) => void;
  onSent: (receipt: RequestSubmissionResult, remaining: BasketLine[]) => void;
}) {
  const [submissions, setSubmissions] = useState<DraftSubmission[]>([]);
  const [ready, setReady] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [storageNote, setStorageNote] = useState('');
  const active = useRef(true);
  const inFlight = useRef(false);
  const controller = useRef<AbortController | null>(null);
  const storageKey = submissionStorageKey(account?.id || 'preview');
  const payloadLines = requestPayloadLines(lines);
  const fingerprint = JSON.stringify(payloadLines);
  const saved = submissions.findLast(item => item.fingerprint === fingerprint && !item.draftCleared);
  const receipt = saved?.receipt;
  const canSend = !preview && !!account?.capabilities.includes('client');
  const hasSelectedItems = payloadLines.every(line => !!line.supplier_item_id);

  useEffect(() => {
    active.current = true;
    if (preview || !canSend) { setReady(true); return; }
    try {
      const history = readSubmissions(account!.id);
      setSubmissions(history);
      setReady(true);
    } catch { setError('No se pudo recuperar el historial de envío en este navegador. Inténtalo de nuevo al recargar la página.'); }
    return () => { active.current = false; controller.current?.abort(); onPending(false); };
  }, [storageKey, preview, canSend, onPending]);
  useEffect(() => { if (ready) { setError(''); setStorageNote(''); } }, [fingerprint, ready]);

  async function submit() {
    if (inFlight.current || !ready || !canSend || !hasSelectedItems || !lines.length || receipt) return;
    inFlight.current = true;
    setBusy(true); onPending(true); setError(''); setStorageNote('');
    const submission = saved || { fingerprint, submissionId: crypto.randomUUID() };
    const history = saved ? submissions : [...submissions, submission];
    try {
      // Save the key before posting: a lost response can be retried safely, including after reload.
      localStorage.setItem(storageKey, JSON.stringify(history));
      setSubmissions(history);
    } catch {
      setError('Tu navegador no pudo guardar este envío. Habilita el almacenamiento del sitio e inténtalo de nuevo.');
      inFlight.current = false; setBusy(false); onPending(false); return;
    }
    controller.current = new AbortController();
    try {
      const result = await request<RequestSubmissionResult>(`/api/market/accounts/${account!.id}/requests`, {
        method: 'POST', body: JSON.stringify({ submission_id: submission.submissionId, lines: payloadLines }), signal: controller.current.signal,
      });
      if (active.current) {
        setSubmissions(history.map(item => item.submissionId === submission.submissionId ? { ...item, receipt: result } : item));
        try { const remaining = finishDraftSubmission(account!.id, submission, result); onSent(result, remaining); }
        catch { setStorageNote('La solicitud se envió y está guardada en Enviadas. No se pudo limpiar el borrador en este navegador.'); }
      }
    } catch (failure) {
      if (active.current) setError(failure instanceof TypeError ? 'Se perdió la conexión. Inténtalo de nuevo; se recuperará el mismo envío sin duplicarlo.' : failure instanceof Error ? failure.message : 'No se pudo enviar la solicitud. Inténtalo de nuevo.');
    } finally {
      inFlight.current = false;
      if (active.current) { setBusy(false); onPending(false); }
    }
  }

  return <>
    {error && <div className="notice error" role="alert">{error}</div>}
    {receipt && <div className="notice success" role="status"><div><strong>Solicitud enviada</strong><p>Consulta sus artículos y estado en Enviadas.</p>{receipt.requests.map(item => <p key={item.id}>{item.supplier.name} · {item.reference}{item.appended ? ' · Agregada a orden pendiente' : ''}</p>)}</div></div>}
    {storageNote && <p className="basket-page-draft-note" role="status">{storageNote}</p>}
    <button type="button" className="button primary full" disabled={!ready || busy || !canSend || !hasSelectedItems || !!receipt} onClick={submit}>
      {busy ? <><LoaderCircle size={16} className="spin"/>Enviando solicitud…</> : receipt ? <><Check size={16}/>Solicitud enviada</> : <>{preview ? 'Enviar solicitud de ejemplo' : 'Enviar solicitud'}<ArrowRight size={16}/></>}
    </button>
    <p className="basket-page-draft-note">{preview ? 'Inicia sesión con una cuenta de cliente para enviar solicitudes reales.' : !canSend ? 'Selecciona una cuenta con rol de cliente para enviar solicitudes.' : !hasSelectedItems ? 'Vuelve al catálogo y elige el artículo de cada proveedor para completar esta cesta.' : receipt ? 'La solicitud ya está guardada. Abre Enviadas para consultar su estado.' : 'Al enviar, los artículos pasan de Borrador a Enviadas. Cada proveedor recibe una orden. Si tiene una orden tuya pendiente de revisión, agregaremos los artículos a esa orden. El proveedor preparará una cotización privada.'}</p>
  </>;
}
