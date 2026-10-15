'use client';

import { KeyboardEvent, useEffect, useRef, useState } from 'react';
import type { CustomCellEditorProps } from 'ag-grid-react';
import { Check, LoaderCircle, Plus, X } from 'lucide-react';
import { ApiError, request } from '@/lib/types';
import { OEMCrossReference, OEMReference, OEMReferenceDetail, referenceStatusLabels } from '@/lib/oem-reference-types';
import { UppercaseInput, UppercaseTextarea } from './uppercase-field';

const base = '/api/management/oem-references';
export type OEMGridContext = { open: (id: string) => void; onRowChanged: (id: string) => void };
type Props = CustomCellEditorProps<OEMReference, unknown, OEMGridContext> & { readOnly?: boolean };
const message = (error: unknown) => error instanceof Error ? error.message : 'No se pudo guardar el cambio. Inténtalo de nuevo.';
// A grid popup cannot leave the grid: 460 px, or the grid's width on a phone.
const popupWidth = (cell: HTMLElement | undefined) => Math.min(460, (cell?.closest('.ag-root-wrapper')?.clientWidth ?? 476) - 16);
const order = (rows: OEMCrossReference[]) => [...rows].sort((a, b) => a.brand.localeCompare(b.brand) || a.code.localeCompare(b.code));

// The Alternos cell of Biblioteca OEM: every aftermarket code of the number with the sources that print it, copied on click. With
// Editar celdas on, a code is added by hand or retired on the spot; a retired catalog code stays retired when catalogs are imported
// again. Every SKU linked to the number shows the change. When the popup closes after a change, the grid re-reads the row.
export function OEMAlternosEditor({ data, readOnly = false, stopEditing, context, eGridCell }: Props) {
  const [codes, setCodes] = useState<OEMCrossReference[] | null>(null);
  const [brand, setBrand] = useState(''); const [code, setCode] = useState('');
  const [busy, setBusy] = useState(''); const [error, setError] = useState(''); const [copied, setCopied] = useState('');
  const [retiring, setRetiring] = useState<OEMCrossReference | null>(null);
  const [width] = useState(() => popupWidth(eGridCell));
  const changed = useRef(false);
  const input = useRef<HTMLInputElement>(null);
  const id = data?.id;
  useEffect(() => {
    if (!id) return;
    let live = true;
    request<OEMReferenceDetail>(`${base}/${id}`).then(row => { if (live) setCodes(order(row.cross_references)); }).catch(reason => { if (live) { setCodes([]); setError(message(reason)); } });
    return () => { live = false; };
  }, [id]);
  useEffect(() => { if (!readOnly) input.current?.focus(); }, [readOnly]);
  useEffect(() => () => { if (changed.current && id) context.onRowChanged(id); }, [context, id]);

  async function add() {
    if (!id || !brand.trim() || !code.trim() || busy) return;
    setBusy('add'); setError('');
    try {
      const saved = await request<OEMCrossReference>(`${base}/${id}/cross-references`, { method: 'POST', body: JSON.stringify({ brand: brand.trim(), code: code.trim() }) });
      setCodes(rows => order([...(rows ?? []).filter(row => row.id !== saved.id), saved]));
      changed.current = true;
      setCode(''); input.current?.focus();
    } catch (reason) { setError(message(reason)); } finally { setBusy(''); }
  }
  async function retire(entry: OEMCrossReference) {
    if (!id || busy) return;
    setBusy(`retire:${entry.id}`); setError('');
    try {
      await request(`${base}/${id}/cross-references/${entry.id}`, { method: 'DELETE' });
      setCodes(rows => (rows ?? []).filter(row => row.id !== entry.id));
      changed.current = true; setRetiring(null);
    } catch (reason) { setError(message(reason)); } finally { setBusy(''); }
  }
  function copy(entry: OEMCrossReference) {
    void navigator.clipboard?.writeText(entry.code).then(() => setCopied(entry.code)).catch(() => setCopied(''));
  }
  // The grid leaves Enter and Tab to this editor (suppressKeyboardEvent on the column): Enter adds the code.
  function keys(event: KeyboardEvent) {
    if (event.key === 'Enter') { event.preventDefault(); void add(); }
  }
  const linked = data?.linked_count ?? 0;

  return <div className="codes-editor" style={{ width }} role="dialog" aria-label={`Alternos de ${data?.manufacturer ?? ''} ${data?.code ?? ''}`}>
    <div className="codes-editor-head"><strong>{data?.manufacturer} {data?.code}</strong>
      <span>{readOnly ? 'Solo lectura · clic en un código para copiarlo' : `${codes?.length ?? 0} ${codes?.length === 1 ? 'alterno' : 'alternos'} · clic para copiar`}</span>
      <button type="button" className="icon-button" aria-label="Cerrar alternos" onClick={() => stopEditing()}><X size={15}/></button></div>
    {codes === null ? <p className="codes-editor-empty"><LoaderCircle className="spin" size={13}/> Cargando alternos…</p>
      : codes.length ? <ul className="codes-editor-list">{codes.map(entry => <li key={entry.id} className="company">
        <button type="button" className="codes-editor-copy" title={entry.citations.join('\n')} onClick={() => copy(entry)}><strong>{entry.code}</strong><small>{entry.brand}</small></button>
        {!readOnly && <button type="button" className="codes-editor-remove" aria-label={`Retirar alterno ${entry.brand} ${entry.code}`} disabled={!!busy} onClick={() => { setRetiring(entry); setError(''); }}><X size={13}/></button>}
      </li>)}</ul> : <p className="codes-editor-empty">Ningún catálogo imprime alternos de este número.</p>}
    {retiring && <div className="oem-retire" role="alert">
      <p>¿Retirar {retiring.brand} {retiring.code}? {linked ? `Lo dejan de ver ${linked === 1 ? 'la línea OEM vinculada' : `las ${linked} líneas OEM vinculadas`}` : 'Ninguna línea OEM lo muestra todavía'}, y la importación de catálogos no lo vuelve a agregar.</p>
      <div><button type="button" className="button soft small danger-text" disabled={!!busy} onClick={() => void retire(retiring)}>{busy === `retire:${retiring.id}` ? <LoaderCircle className="spin" size={13}/> : null}Retirar</button>
        <button type="button" className="button text small" disabled={!!busy} onClick={() => setRetiring(null)}>Cancelar</button></div>
    </div>}
    {copied && <p className="codes-editor-copied" role="status"><Check size={13}/>Copiado: {copied}</p>}
    {error && <p className="codes-editor-error" role="alert">{error}</p>}
    {readOnly ? <p className="codes-editor-note">Activa Editar celdas para agregar o retirar alternos.</p> : <div className="codes-editor-add">
      <UppercaseInput ref={input} aria-label="Marca del alterno" placeholder="MARCA (AISIN, GMB…)" value={brand} maxLength={120} onChange={event => setBrand(event.target.value)} onKeyDown={keys}/>
      <UppercaseInput aria-label="Código del alterno" placeholder="CÓDIGO" value={code} maxLength={120} onChange={event => setCode(event.target.value)} onKeyDown={keys}/>
      <button type="button" className="button soft small oem-alternos-add" disabled={!brand.trim() || !code.trim() || !!busy} onClick={() => void add()}>{busy === 'add' ? <LoaderCircle className="spin" size={14}/> : <Plus size={14}/>}Agregar alterno</button>
    </div>}
  </div>;
}

// The Estado cell with Editar celdas on: a number goes into dispute with the reason, or out of it (its status follows its sources
// again). Saved on the spot under the version the grid showed; the grid re-reads the row when the popup closes.
export function OEMStatusEditor({ data, stopEditing, context, eGridCell }: Props) {
  const [note, setNote] = useState(data?.dispute_note ?? '');
  const [busy, setBusy] = useState(false); const [error, setError] = useState('');
  const [width] = useState(() => popupWidth(eGridCell));
  const changed = useRef(false);
  const id = data?.id;
  useEffect(() => () => { if (changed.current && id) context.onRowChanged(id); }, [context, id]);
  if (!data) return null;
  const disputed = data.status === 'disputed';
  async function apply(action: 'dispute' | 'clear_dispute') {
    if (busy || !data) return;
    setBusy(true); setError('');
    try {
      await request(`${base}/${data.id}`, { method: 'PATCH', body: JSON.stringify({ expected_version: data.version, action, ...(action === 'dispute' ? { note: note.trim() } : {}) }) });
      changed.current = true;
      stopEditing();
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 409) changed.current = true;  // the grid shows what changed when this closes
      setError(message(reason));
    } finally { setBusy(false); }
  }
  return <div className="codes-editor oem-status-editor" style={{ width }} role="dialog" aria-label={`Estado de ${data.manufacturer} ${data.code}`}>
    <div className="codes-editor-head"><strong>{data.manufacturer} {data.code}</strong><span>{referenceStatusLabels[data.status]}</span>
      <button type="button" className="icon-button" aria-label="Cerrar estado" onClick={() => stopEditing()}><X size={15}/></button></div>
    {disputed ? <>
      <p className="oem-status-note"><strong>En disputa:</strong> {data.dispute_note || 'sin motivo registrado'}</p>
      <p className="codes-editor-note">Al quitar la disputa, el estado vuelve a seguir a sus fuentes.</p>
      <button type="button" className="button soft small" disabled={busy} onClick={() => void apply('clear_dispute')}>{busy && <LoaderCircle className="spin" size={14}/>}Quitar disputa</button>
    </> : <>
      <label className="oem-status-reason">Motivo de la disputa<UppercaseTextarea aria-label="Motivo de la disputa" value={note} maxLength={2000} rows={3} autoFocus onChange={event => setNote(event.target.value)}/></label>
      <button type="button" className="button soft small danger-text" disabled={busy || !note.trim()} onClick={() => void apply('dispute')}>{busy && <LoaderCircle className="spin" size={14}/>}Marcar en disputa</button>
    </>}
    {error && <p className="codes-editor-error" role="alert">{error}</p>}
  </div>;
}
