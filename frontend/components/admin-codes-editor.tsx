'use client';

import { KeyboardEvent, useEffect, useRef, useState } from 'react';
import type { CustomCellEditorProps } from 'ag-grid-react';
import { Check, LoaderCircle, Plus, X } from 'lucide-react';
import { request } from '@/lib/types';
import { referenceClass, referenceLabel } from '@/lib/reference-label';
import { referenceEstadoLabels, referenceTipoLabels } from '@/lib/line-types';
import type { LineCode, OEMLine, ReferenceAdded, ReferenceTipo } from '@/lib/line-types';
import { UppercaseInput } from './uppercase-field';

export type CodesEditorContext = { onCodesChanged: (lineId: string) => void };
type Props = CustomCellEditorProps<OEMLine, LineCode[], CodesEditorContext> & { readOnly?: boolean };
const message = (error: unknown) => error instanceof Error ? error.message : 'No se pudo guardar la referencia. Inténtalo de nuevo.';
const TIPOS: ReferenceTipo[] = ['otro', 'oem', 'aftermarket'];

// The Referencias cell's popup (EssaMobileApp's alternos editor): always opens, so every code can be read and copied; with Editar celdas
// on, a Reference is added to the line (management/lines/<id>/references) or retired (management/references/<id>: never deleted) on the
// spot. The grid value never changes here: when the popup closes after a change, the grid re-reads the row and flashes what moved. The
// main Reference is the SKU itself, so it is not listed; its tipo, brand and sources live in the line's References tab.
export default function CodesEditor({ data, value, readOnly = false, stopEditing, context, eGridCell }: Props) {
  const [codes, setCodes] = useState<LineCode[]>(() => [...(value ?? [])]);
  const [code, setCode] = useState(''); const [brand, setBrand] = useState(''); const [tipo, setTipo] = useState<ReferenceTipo>('otro');
  const [busy, setBusy] = useState(''); const [error, setError] = useState(''); const [copied, setCopied] = useState('');
  // A grid popup cannot leave the grid: 460 px, or the grid's width on a phone.
  const [width] = useState(() => Math.min(460, (eGridCell?.closest('.ag-root-wrapper')?.clientWidth ?? 476) - 16));
  const changed = useRef(false);
  const input = useRef<HTMLInputElement>(null);
  const lineId = data?.id;
  useEffect(() => { input.current?.focus(); }, []);
  useEffect(() => () => { if (changed.current && lineId) context.onCodesChanged(lineId); }, [context, lineId]);

  async function add() {
    const text = code.trim();
    if (!text || !lineId || busy) return;
    if (codes.some(entry => entry.code === text && entry.brand === brand.trim())) { setError(`${text} ya es una referencia de esta línea OEM.`); return; }
    setBusy('add'); setError('');
    try {
      const saved = await request<ReferenceAdded>(`/api/management/lines/${lineId}/references`, { method: 'POST', body: JSON.stringify({ code: text, brand: brand.trim(), tipo }) });
      const ref = saved.reference;
      if (!ref.is_main && !codes.some(entry => entry.id === ref.id)) setCodes(entries => [...entries, { id: ref.id, code: ref.code, brand: ref.brand, tipo: ref.tipo, estado: ref.estado }]);
      if (!saved.added) setError(`${ref.code} ya era una referencia de esta línea OEM.`);
      changed.current = true;
      setCode(''); setBrand(''); input.current?.focus();
    } catch (e) { setError(message(e)); } finally { setBusy(''); }
  }
  async function remove(entry: LineCode) {
    if (busy) return;
    setBusy(`remove:${entry.id}`); setError('');
    try {
      await request(`/api/management/references/${entry.id}`, { method: 'DELETE' });
      setCodes(entries => entries.filter(item => item.id !== entry.id));
      changed.current = true;
    } catch (e) { setError(message(e)); } finally { setBusy(''); }
  }
  function copy(entry: LineCode) {
    void navigator.clipboard?.writeText(entry.code).then(() => setCopied(entry.code)).catch(() => setCopied(''));
  }
  // The grid leaves Enter and Tab to this editor (suppressKeyboardEvent on the column): Enter adds the code instead of closing the popup.
  function keys(event: KeyboardEvent) {
    if (event.key === 'Enter') { event.preventDefault(); void add(); }
  }

  return <div className="codes-editor" style={{ width }} role="dialog" aria-label={`Referencias de ${data?.sku ?? ''}`}>
    <div className="codes-editor-head"><strong>{data?.sku}</strong><span>{readOnly ? 'Solo lectura · clic en un código para copiarlo' : `${codes.length} ${codes.length === 1 ? 'referencia' : 'referencias'} · clic para copiar`}</span>
      <button type="button" className="icon-button" aria-label="Cerrar referencias" onClick={() => stopEditing()}><X size={15}/></button></div>
    {codes.length ? <ul className="codes-editor-list">{codes.map(entry => <li key={entry.id} className={`${referenceClass(entry)}${entry.estado === 'disputa' ? ' disputa' : ''}`}>
      <button type="button" className="codes-editor-copy" title="Copiar" onClick={() => copy(entry)}><strong>{entry.code}</strong><small>{referenceLabel(entry)}{entry.estado === 'disputa' ? ` · ${referenceEstadoLabels.disputa}` : ''}</small></button>
      {!readOnly && <button type="button" className="codes-editor-remove" aria-label={`Retirar referencia ${entry.code}`} disabled={!!busy} onClick={() => void remove(entry)}>{busy === `remove:${entry.id}` ? <LoaderCircle className="spin" size={13}/> : <X size={13}/>}</button>}
    </li>)}</ul> : <p className="codes-editor-empty">Sin otras referencias: solo el código del SKU.</p>}
    {copied && <p className="codes-editor-copied" role="status"><Check size={13}/>Copiado: {copied}</p>}
    {error && <p className="codes-editor-error" role="alert">{error}</p>}
    {readOnly ? <p className="codes-editor-note">Activa Editar celdas para agregar o retirar referencias; su tipo, estado y fuentes se editan en la pestaña Referencias de la línea OEM.</p> : <div className="codes-editor-add">
      <UppercaseInput ref={input} aria-label="Nueva referencia" placeholder="CÓDIGO" value={code} maxLength={120} onChange={event => setCode(event.target.value)} onKeyDown={keys}/>
      <UppercaseInput aria-label="Marca de la referencia" placeholder="MARCA (OPCIONAL)" value={brand} maxLength={120} onChange={event => setBrand(event.target.value)} onKeyDown={keys}/>
      <select aria-label="Tipo de referencia" value={tipo} onChange={event => setTipo(event.target.value as ReferenceTipo)}>{TIPOS.map(value => <option key={value} value={value}>{referenceTipoLabels[value]}</option>)}</select>
      <button type="button" className="button soft small" disabled={!code.trim() || !!busy} onClick={() => void add()}>{busy === 'add' ? <LoaderCircle className="spin" size={14}/> : <Plus size={14}/>}Agregar</button>
    </div>}
  </div>;
}
