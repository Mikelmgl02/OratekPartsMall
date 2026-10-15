'use client';

import { FormEvent, useId, useState } from 'react';
import { LoaderCircle } from 'lucide-react';
import Modal from './modal';
import ReferencesEditor from './admin-references-editor';
import { PartTechnicalForm } from './part-technical';
import { UppercaseInput, UppercaseTextarea } from './uppercase-field';
import { ApiError, ManagedPart, request } from '@/lib/types';
import { asManagedPart, lineEstadoHints, lineEstadoLabels, lineOrigenLabels, lineTipoLabels, lineTransitions, rejectReasonLabels, transitionLabels } from '@/lib/line-types';
import type { LineEstado, OEMLine, RejectReason } from '@/lib/line-types';
import { subgrupoOptions, useGrupos } from '@/lib/use-grupos';

const base = '/api/management';
const message = (error: unknown) => error instanceof Error ? error.message : 'No se pudo guardar el cambio. Inténtalo de nuevo.';
export type EditorTab = 'general' | 'references' | 'specs';
const EDITOR_TABS: [EditorTab, string][] = [['general', 'General'], ['references', 'Referencias'], ['specs', 'Ficha técnica']];

function SubgrupoSelect({ value, onChange, label = 'Subgrupo' }: { value: string; onChange: (value: string) => void; label?: string }) {
  const { grupos, error, reload } = useGrupos();
  const options = grupos ? subgrupoOptions(grupos) : [];
  return <label>{label}<select aria-label={label} value={value} onChange={event => onChange(event.target.value)} disabled={!grupos}>
      <option value="">{grupos ? 'SIN CLASIFICAR' : 'Cargando grupos…'}</option>
      {grupos?.map(grupo => <optgroup key={grupo.id} label={grupo.nombre}>{options.filter(option => option.grupo.id === grupo.id).map(option =>
        <option key={option.id} value={option.id}>{option.label}</option>)}</optgroup>)}
    </select>{error && <span className="danger-text">{error} <button type="button" className="button text small" onClick={reload}>Reintentar</button></span>}</label>;
}

// The OEM line's side panel (plan S8.2–3): General (its own fields, saved with the version it was read at through management/lines/<id>,
// and its estado moves through management/lines/<id>/transition), Referencias (the References editor) and its Ficha técnica. A new line
// (Crear SKU) is created from its codigo through management/catalog and opens on Referencias, where its other codes are added.
export function LineEditor({ line: initial, initialTab = 'general', onClose, onSaved }: { line: OEMLine | null; initialTab?: EditorTab; onClose: () => void;
    onSaved: (line: OEMLine, text: string) => void }) {
  const [line, setLine] = useState<OEMLine | null>(initial);
  const first: EditorTab = initial ? initialTab : 'general';
  const [tab, setTab] = useState<EditorTab>(first);
  const [visited, setVisited] = useState<Set<EditorTab>>(() => new Set([first]));
  const tabs = useId();
  function open(next: EditorTab) { setTab(next); setVisited(current => new Set(current).add(next)); }
  // General's message outlives the form, which starts over on every version of the line (a save, a conflict that reloaded it).
  const [status, setStatus] = useState<{ kind: 'error' | 'success'; text: string } | null>(null);
  function changed(next: OEMLine, text = '') { setLine(next); onSaved(next, text); }
  const panel = (key: EditorTab) => ({ role: 'tabpanel', id: `${tabs}-${key}`, 'aria-labelledby': `${tabs}-${key}-tab`, 'data-tab': key, hidden: tab !== key, className: 'editor-tab-panel' });
  return <Modal title={line ? `Línea OEM · ${line.sku}` : 'Crear SKU'} onClose={onClose} wide drawer className="side-panel">
    <div className="inventory-view-tabs editor-tabs" role="tablist" aria-label="Secciones de la línea OEM">{EDITOR_TABS.map(([key, label]) =>
      <button key={key} type="button" role="tab" id={`${tabs}-${key}-tab`} aria-controls={`${tabs}-${key}`} aria-selected={tab === key} className={tab === key ? 'selected' : ''}
        disabled={key !== 'general' && !line} title={key !== 'general' && !line ? 'Guarda el SKU para completar sus referencias y su ficha técnica.' : undefined} onClick={() => open(key)}>
        {label}{key === 'references' && !!line && line.reference_count > 0 && <span className="editor-tab-count">{line.reference_count}</span>}</button>)}</div>
    <div {...panel('general')}>{line ? <LineGeneral key={`${line.id}:${line.version}`} line={line} onChanged={changed} status={status} onStatus={setStatus}/>
      : <CreateLine onCreated={(created, text) => { changed(created, text); open('references'); }}/>}</div>
    {line && visited.has('references') && <div {...panel('references')}><ReferencesEditor line={line} onLine={next => changed(next)}/></div>}
    {line && visited.has('specs') && <div {...panel('specs')}>{line.estado === 'unificada' ? <p className="notice">Una línea OEM unificada no tiene ficha propia: está en la línea que la recibió.</p>
      : <PartTechnicalForm part={asManagedPart(line)}/>}</div>}
  </Modal>;
}

type Status = { kind: 'error' | 'success'; text: string } | null;
function LineGeneral({ line, onChanged, status, onStatus }: { line: OEMLine; onChanged: (line: OEMLine, text: string) => void; status: Status; onStatus: (status: Status) => void }) {
  const [name, setName] = useState(line.name || '');
  const [description, setDescription] = useState(line.description || '');
  const [aplicaciones, setAplicaciones] = useState(line.aplicaciones_texto || '');
  const [subgrupo, setSubgrupo] = useState(line.subgrupo?.id ?? '');
  const [busy, setBusy] = useState('');
  const setError = (text: string) => onStatus(text ? { kind: 'error', text } : null);
  const [rejecting, setRejecting] = useState(false); const [reason, setReason] = useState<RejectReason>('duplicate'); const [note, setNote] = useState('');
  const locked = line.estado === 'unificada';
  const changes = Object.fromEntries(([['name', name, line.name || ''], ['description', description, line.description || ''], ['aplicaciones_texto', aplicaciones, line.aplicaciones_texto || ''],
    ['subgrupo', subgrupo, line.subgrupo?.id ?? '']] as const).filter(([, value, before]) => value !== before).map(([key, value]) => [key, key === 'subgrupo' ? value || null : value]));
  async function recover(caught: unknown) {
    setError(message(caught));
    if (caught instanceof ApiError && caught.status === 409) {  // the line moved meanwhile: show it as it is now
      try { onChanged(await request<OEMLine>(`${base}/lines/${line.id}`), ''); } catch { /* the message already says what happened */ }
    }
  }
  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!Object.keys(changes).length || busy) return;
    setBusy('save'); setError('');
    try {
      const saved = await request<OEMLine>(`${base}/lines/${line.id}`, { method: 'PATCH', body: JSON.stringify({ ...changes, expected_version: line.version }) });
      onStatus({ kind: 'success', text: `Línea OEM ${saved.sku} guardada.` });
      onChanged(saved, `Línea OEM ${saved.sku} guardada.`);
    } catch (caught) { await recover(caught); } finally { setBusy(''); }
  }
  async function move(estado: LineEstado) {
    setBusy(estado); setError('');
    try {
      const saved = await request<OEMLine>(`${base}/lines/${line.id}/transition`, { method: 'POST',
        body: JSON.stringify({ estado, expected_version: line.version, ...(estado === 'rechazada' ? { reason, note } : {}) }) });
      const text = `Línea OEM ${saved.sku}: ${lineEstadoLabels[saved.estado].toLowerCase()}.`;
      onStatus({ kind: 'success', text });
      onChanged(saved, text);
    } catch (caught) { await recover(caught); } finally { setBusy(''); }
  }
  const moves = lineTransitions[line.estado] ?? [];
  return <div className="stack-form catalog-editor line-general">
    <section className="line-summary" aria-label="Estado de la línea OEM">
      <div><span className={`status ${line.estado === 'publicada' ? 'matched' : line.estado === 'preparacion' ? 'preparacion' : ''}`}>{lineEstadoLabels[line.estado]}</span>
        <span className={`sku-identity ${line.tipo === 'OEM' ? 'oem' : 'internal'}`}>{lineTipoLabels[line.tipo]}{line.brand ? ` · ${line.brand}` : ''}</span>
        <span className="pill">ORIGEN: {lineOrigenLabels[line.origen] ?? line.origen}</span></div>
      <p>{lineEstadoHints[line.estado]}</p>
      <dl><div><dt>SKU</dt><dd>{line.sku}</dd></div><div><dt>Referencias</dt><dd>{line.reference_count}</dd></div><div><dt>Artículos de proveedores</dt><dd>{line.variant_count}</dd></div>
        {line.merged_count > 0 && <div><dt>Unificadas en esta</dt><dd>{line.merged_count}</dd></div>}</dl>
      {line.estado === 'rechazada' && line.decision.reason && <p className="form-footnote">Rechazada: {rejectReasonLabels[line.decision.reason as RejectReason] ?? line.decision.reason}{line.decision.note ? ` · ${line.decision.note}` : ''}{line.decision.decided_by ? ` · ${line.decision.decided_by}` : ''}</p>}
      <p className="form-footnote">El SKU es el código de la referencia principal: para cambiarlo, elige otra principal en Referencias.</p>
    </section>
    {status && <div className={`notice ${status.kind}`} role={status.kind === 'error' ? 'alert' : 'status'}>{status.text}</div>}
    <form className="stack-form" autoComplete="off" onSubmit={save} aria-label="Datos de la línea OEM">
      <label>Nombre del repuesto (opcional)<UppercaseInput name="part_label" value={name} disabled={locked} maxLength={200} onChange={event => setName(event.target.value)} placeholder="Ej.: Disco de freno delantero"/></label>
      <label>Descripción<UppercaseTextarea aria-label="Descripción" name="part_details" value={description} disabled={locked} rows={3} onChange={event => setDescription(event.target.value)} placeholder="Características, aplicación o medidas del repuesto"/></label>
      <label>Aplicaciones<UppercaseTextarea aria-label="Aplicaciones" value={aplicaciones} disabled={locked} rows={3} onChange={event => setAplicaciones(event.target.value)} placeholder="Una aplicación por línea: TOYOTA HILUX 2005-2015"/></label>
      <SubgrupoSelect value={subgrupo} onChange={setSubgrupo}/>
      <p className="form-footnote">El subgrupo define los campos de la ficha técnica: guarda la línea OEM y complétala en la pestaña Ficha técnica.</p>
      {!locked && <button className="button primary full" disabled={!!busy || !Object.keys(changes).length}>{busy === 'save' && <LoaderCircle size={16} className="spin"/>}{busy === 'save' ? 'Guardando…' : 'Guardar línea OEM'}</button>}
    </form>
    {!locked && !!moves.length && <section className="line-moves" aria-label="Cambiar el estado">
      <strong>Estado</strong>
      <div className="admin-toolbar-actions">{moves.map(estado => estado === 'rechazada'
        ? <button key={estado} type="button" className="button soft small" disabled={!!busy} aria-pressed={rejecting} onClick={() => setRejecting(value => !value)}>{transitionLabels[estado]}</button>
        : <button key={estado} type="button" className={`button ${estado === 'publicada' ? 'primary' : 'soft'} small`} disabled={!!busy} onClick={() => void move(estado)}>{busy === estado && <LoaderCircle size={14} className="spin"/>}{transitionLabels[estado]}</button>)}</div>
      {rejecting && <div className="stack-form" role="group" aria-label="Rechazar la línea OEM">
        <label>Motivo<select aria-label="Motivo del rechazo" value={reason} onChange={event => setReason(event.target.value as RejectReason)}>{Object.entries(rejectReasonLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
        <label>Nota (opcional)<input aria-label="Nota del rechazo" value={note} maxLength={2000} onChange={event => setNote(event.target.value)}/></label>
        <button type="button" className="button dark small" disabled={!!busy} onClick={() => void move('rechazada')}>Confirmar rechazo</button>
      </div>}
      <p className="form-footnote">Publicar la pone a la venta; retirarla la saca sin borrar sus códigos ni su historial. Una línea OEM en preparación se publica o rechaza como en Productos nuevos.</p>
    </section>}
    {locked && <p className="notice">Esta línea OEM está unificada en otra: Separar SKU, desde la línea que la recibió, la devuelve.</p>}
  </div>;
}

function CreateLine({ onCreated }: { onCreated: (line: OEMLine, text: string) => void }) {
  const [subgrupo, setSubgrupo] = useState('');
  const [busy, setBusy] = useState(false); const [error, setError] = useState('');
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setError(''); setBusy(true);
    const form = new FormData(event.currentTarget);
    try {
      const created = await request<ManagedPart>(`${base}/catalog`, { method: 'POST', body: JSON.stringify({ sku: form.get('internal_sku'), name: form.get('part_label'),
        description: form.get('part_details'), active: form.has('active'), codes: [], ...(subgrupo ? { part_type: subgrupo } : {}) }) });
      const line = await request<OEMLine>(`${base}/lines/${created.id}`);
      onCreated(line, `SKU ${line.sku} creado. Agrega sus otras referencias.`);
    } catch (caught) { setError(message(caught)); } finally { setBusy(false); }
  }
  return <form autoComplete="off" className="stack-form catalog-editor" onSubmit={submit} aria-label="Crear SKU">
    <div className="form-row"><label>SKU principal (MAIN)<UppercaseInput name="internal_sku" required maxLength={120} placeholder="Ej.: 58411-1R000"/></label>
      <label>Nombre del repuesto (opcional)<UppercaseInput name="part_label" maxLength={200} placeholder="Ej.: Disco de freno delantero"/></label></div>
    <p className="form-footnote">El código del SKU es su referencia principal. Su tipo (OEM, aftermarket u otro), su marca y las demás referencias se completan en la pestaña Referencias al guardarlo.</p>
    <label>Descripción<UppercaseTextarea aria-label="Descripción" name="part_details" rows={3} placeholder="Características, aplicación o medidas del repuesto"/></label>
    <SubgrupoSelect value={subgrupo} onChange={setSubgrupo}/>
    <label className="admin-checkbox"><input name="active" type="checkbox" defaultChecked/>SKU publicado</label>
    {error && <div className="notice error" role="alert">{error}</div>}
    <button className="button primary full" disabled={busy}>{busy && <LoaderCircle size={16} className="spin"/>}{busy ? 'Guardando…' : 'Crear SKU'}</button>
  </form>;
}
