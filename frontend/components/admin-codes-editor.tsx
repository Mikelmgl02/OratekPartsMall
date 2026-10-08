'use client';

import { KeyboardEvent, useEffect, useRef, useState } from 'react';
import type { CustomCellEditorProps } from 'ag-grid-react';
import { Check, LoaderCircle, Plus, X } from 'lucide-react';
import { ManagedPart, PartReference, request } from '@/lib/types';
import { referenceLabel } from '@/lib/reference-label';
import { UppercaseInput } from './uppercase-field';

export type CodesEditorContext = { onCodesChanged: (partId: string) => void };
type Props = CustomCellEditorProps<ManagedPart, PartReference[], CodesEditorContext> & { readOnly?: boolean };
const message = (error: unknown) => error instanceof Error ? error.message : 'No se pudo guardar el alterno. Inténtalo de nuevo.';
const tone = (code: PartReference) => code.ref_type || (code.kind === 'oem' ? 'oem' : code.kind === 'manufacturer' ? 'company' : 'unknown');

// The Alternos cell's popup (EssaMobileApp's alternos editor): always opens, so every code can be read and copied; with Editar celdas
// on, a code is added or retired on the spot, each change saved by the alternos API. The grid value never changes here: when the popup
// closes after a change, the grid re-reads the row and flashes what moved.
export default function CodesEditor({ data, value, readOnly = false, stopEditing, context, eGridCell }: Props) {
  const [codes, setCodes] = useState<PartReference[]>(() => [...(value ?? [])]);
  const [code, setCode] = useState(''); const [brand, setBrand] = useState(''); const [type, setType] = useState<'unknown' | 'oem' | 'company'>('unknown');
  const [busy, setBusy] = useState(''); const [error, setError] = useState(''); const [copied, setCopied] = useState('');
  // A grid popup cannot leave the grid: 460 px, or the grid's width on a phone.
  const [width] = useState(() => Math.min(460, (eGridCell?.closest('.ag-root-wrapper')?.clientWidth ?? 476) - 16));
  const changed = useRef(false);
  const input = useRef<HTMLInputElement>(null);
  const partId = data?.id;
  useEffect(() => { input.current?.focus(); }, []);
  useEffect(() => () => { if (changed.current && partId) context.onCodesChanged(partId); }, [context, partId]);

  async function add() {
    const text = code.trim();
    if (!text || !partId || busy) return;
    if (codes.some(entry => entry.code === text && entry.brand === brand.trim())) { setError(`${text} ya es un alterno de este SKU.`); return; }
    setBusy('add'); setError('');
    try {
      const saved = await request<PartReference & { id: number }>('/api/management/alternates', { method: 'POST', body: JSON.stringify({ part: partId, code: text, brand: brand.trim(), ref_type: type }) });
      setCodes(entries => [...entries, saved]);
      changed.current = true;
      setCode(''); setBrand(''); input.current?.focus();
    } catch (e) { setError(message(e)); } finally { setBusy(''); }
  }
  async function remove(entry: PartReference) {
    if (!entry.id || busy) return;
    setBusy(`remove:${entry.id}`); setError('');
    try {
      await request(`/api/management/alternates/${entry.id}`, { method: 'DELETE' });
      setCodes(entries => entries.filter(item => item.id !== entry.id));
      changed.current = true;
    } catch (e) { setError(message(e)); } finally { setBusy(''); }
  }
  function copy(entry: PartReference) {
    void navigator.clipboard?.writeText(entry.code).then(() => setCopied(entry.code)).catch(() => setCopied(''));
  }
  // The grid leaves Enter and Tab to this editor (suppressKeyboardEvent on the column): Enter adds the code instead of closing the popup.
  function keys(event: KeyboardEvent) {
    if (event.key === 'Enter') { event.preventDefault(); void add(); }
  }

  return <div className="codes-editor" style={{ width }} role="dialog" aria-label={`Alternos de ${data?.sku ?? ''}`}>
    <div className="codes-editor-head"><strong>{data?.sku}</strong><span>{readOnly ? 'Solo lectura · clic en un código para copiarlo' : `${codes.length} ${codes.length === 1 ? 'alterno' : 'alternos'} · clic para copiar`}</span>
      <button type="button" className="icon-button" aria-label="Cerrar alternos" onClick={() => stopEditing()}><X size={15}/></button></div>
    {codes.length ? <ul className="codes-editor-list">{codes.map(entry => <li key={entry.id ?? `${entry.brand}:${entry.code}`} className={tone(entry)}>
      <button type="button" className="codes-editor-copy" title="Copiar" onClick={() => copy(entry)}><strong>{entry.code}</strong><small>{referenceLabel(entry)}</small></button>
      {!readOnly && entry.id && <button type="button" className="codes-editor-remove" aria-label={`Retirar alterno ${entry.code}`} disabled={!!busy} onClick={() => void remove(entry)}>{busy === `remove:${entry.id}` ? <LoaderCircle className="spin" size={13}/> : <X size={13}/>}</button>}
    </li>)}</ul> : <p className="codes-editor-empty">Sin alternos: solo el código del SKU.</p>}
    {copied && <p className="codes-editor-copied" role="status"><Check size={13}/>Copiado: {copied}</p>}
    {error && <p className="codes-editor-error" role="alert">{error}</p>}
    {readOnly ? <p className="codes-editor-note">Activa Editar celdas para agregar o retirar alternos.</p> : <div className="codes-editor-add">
      <UppercaseInput ref={input} aria-label="Nuevo código alterno" placeholder="CÓDIGO" value={code} maxLength={120} onChange={event => setCode(event.target.value)} onKeyDown={keys}/>
      <UppercaseInput aria-label="Marca del código" placeholder="MARCA (OPCIONAL)" value={brand} maxLength={120} onChange={event => setBrand(event.target.value)} onKeyDown={keys}/>
      <select aria-label="Tipo de referencia" value={type} onChange={event => setType(event.target.value as typeof type)}><option value="unknown">SIN CLASIFICAR</option><option value="oem">OEM</option><option value="company">EMPRESA</option></select>
      <button type="button" className="button soft small" disabled={!code.trim() || !!busy} onClick={() => void add()}>{busy === 'add' ? <LoaderCircle className="spin" size={14}/> : <Plus size={14}/>}Agregar</button>
    </div>}
  </div>;
}
