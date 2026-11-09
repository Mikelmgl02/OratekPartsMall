'use client';
import { useEffect, useState } from 'react';
import { request } from '@/lib/types';
import type { OEMLine } from '@/lib/line-types';
import type { OEMSuggestion } from './oem-reference-lookup';

type Preview = { preview_token: string; merge: boolean; oem: string; brand: string; previous_code: string; preserve_main?: boolean; selected_oem?: string; selected_brand?: string; matched_equivalences?: OEMSuggestion[]; selected_oems?: (OEMSuggestion & { already_registered: boolean })[];
  source: { id: string; sku: string; description: string; items: number; images: number };
  target: { id: string; sku: string; description: string; items: number; images: number } };

export default function OEMPromotion({ line, proposal, selections, researchToken, onSaved, onBusy }: {
  line: OEMLine; proposal: OEMSuggestion; selections: OEMSuggestion[]; researchToken: string; onSaved: (line: OEMLine) => void; onBusy: (busy: boolean) => void;
}) {
  const [preview, setPreview] = useState<Preview | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const body = { research_token: researchToken, code: proposal.code, brand: proposal.brand, expected_version: line.version, selections: selections.map(row => ({ code: row.code, brand: row.brand })) };
  const path = `/api/management/lines/${line.id}/oem-classification`;
  useEffect(() => {
    const controller = new AbortController();
    request<Preview>(path, { method: 'POST', body: JSON.stringify(body), signal: controller.signal })
      .then(setPreview).catch(error => { if (!controller.signal.aborted) setError(error.message); });
    return () => controller.abort();
  }, [path, researchToken, proposal.code, proposal.brand, line.version]);
  async function apply() {
    if (!preview || !confirmed || busy) return;
    setBusy(true); onBusy(true); setError('');
    try {
      const result = await request<{ line: OEMLine }>(path, { method: 'POST', body: JSON.stringify({ ...body, preview_token: preview.preview_token, confirmed: true }) });
      onSaved(result.line);
    } catch (error) { setError(error instanceof Error ? error.message : 'No se pudo aplicar la clasificación.'); setPreview(null); }
    finally { setBusy(false); onBusy(false); }
  }
  return <section className="classification-final-review stack-form" aria-label="Aplicar clasificación OEM">
    <h3>Revisar y guardar</h3>
    {!preview && !error && <p role="status">Comprobando el catálogo actual…</p>}
    {preview && <>
      <div className="classification-summary">
        <div><small>OEM PRINCIPAL FINAL</small><strong>{preview.oem} · {preview.brand}</strong><p>{preview.preserve_main ? 'Se conserva el principal existente.' : 'Será el principal de la ficha.'}</p>
          <ul>{(preview.selected_oems ?? selections).map(row => <li key={`${row.brand}:${row.code}`}><strong>{row.code} · {row.brand}</strong><span>{'already_registered' in row && row.already_registered ? 'Ya registrado · reutilizar' : 'Agregar OEM'}{row.code === preview.oem && row.brand === preview.brand ? ' · Principal' : ' · Alterna OEM'}</span><a href={row.source_url} target="_blank" rel="noopener noreferrer">Fuente ↗</a></li>)}</ul>
        </div>
        <div><small>AFTERMARKET QUE SE CONSERVA</small><strong>{preview.previous_code}</strong><p>Con sus artículos, proveedores y existencias.</p></div>
      </div>
      {preview.merge && <p className="notice"><strong>Se unificarán dos fichas.</strong> {preview.source.sku} ({preview.source.items} artículos, {preview.source.images} imágenes) pasa a {preview.target.sku} · {preview.target.description} ({preview.target.items} artículos, {preview.target.images} imágenes). La ficha destino conserva sus datos; recibe las referencias, artículos e imágenes. Un conflicto cancela toda la operación.</p>}
      <label className="admin-checkbox"><input type="checkbox" checked={confirmed} disabled={busy} onChange={event => setConfirmed(event.target.checked)}/>Verifiqué las fuentes: todas las OEM seleccionadas son la misma pieza completa y el código de origen es aftermarket.</label>
      <button type="button" className="button primary" disabled={busy || !confirmed} onClick={() => void apply()}>{busy ? 'Guardando…' : 'Guardar clasificación'}</button>
    </>}
    {error && <p className="notice error" role="alert">{error} Cambia la selección y vuelve a revisarla.</p>}
  </section>;
}
