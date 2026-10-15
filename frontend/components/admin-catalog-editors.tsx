'use client';

import { FormEvent, useEffect, useState } from 'react';
import { LoaderCircle, Split } from 'lucide-react';
import Modal from './modal';
import { UppercaseInput } from './uppercase-field';
import { ManagedAlternate, ManagedPart, ManagedStockItem, Page, request } from '@/lib/types';
import { lineEstadoLabels } from '@/lib/line-types';
import type { OEMLine } from '@/lib/line-types';

const base = '/api/management';
const message = (error: unknown) => error instanceof Error ? error.message : 'No se pudo guardar el cambio. Inténtalo de nuevo.';
type Choice = { id: string; sku: string; name: string; active?: boolean };

function FormError({ error }: { error: string }) {
  return error ? <div className="notice error" role="alert">{error}</div> : null;
}
function SaveButton({ busy, disabled, children }: { busy: boolean; disabled?: boolean; children: React.ReactNode }) {
  return <button className="button primary full" disabled={busy || disabled}>{busy && <LoaderCircle size={16} className="spin"/>}{busy ? 'Guardando…' : children}</button>;
}

export function PartPicker({ label, value, initial, onChange, exclude }: { label: string; value: string; initial?: Choice; onChange: (value: string) => void; exclude?: string }) {
  const [search, setSearch] = useState(''); const [data, setData] = useState<Page<ManagedPart> | null>(null); const [error, setError] = useState(''); const [revision, setRevision] = useState(0);
  // Keep the selected option even when it is outside the current result page.
  const [selected, setSelected] = useState<Choice | undefined>(initial);
  useEffect(() => {
    let cancelled = false; setData(null); setError('');
    const timer = setTimeout(() => { request<Page<ManagedPart>>(`${base}/catalog?search=${encodeURIComponent(search.trim())}`).then(result => { if (!cancelled) setData(result); }).catch(error => { if (!cancelled) setError(message(error)); }); }, 250);
    return () => { cancelled = true; clearTimeout(timer); };
  }, [search, revision]);
  const choices: Choice[] = data?.results.filter(part => part.id !== exclude) ?? [];
  if (selected && selected.id !== exclude && !choices.some(part => part.id === selected.id)) choices.unshift(selected);
  return <div className="stack-form part-picker"><label>Buscar {label}<UppercaseInput value={search} onChange={event => setSearch(event.target.value)} placeholder="SKU, nombre o código" onKeyDown={event => { if (event.key === 'Enter') event.preventDefault(); }}/></label>
    <label>{label}<select value={value} onChange={event => { setSelected(choices.find(part => part.id === event.target.value)); onChange(event.target.value); }}><option value="">{!data && !error ? 'Cargando SKU…' : 'Selecciona un SKU'}</option>{choices.map(part => <option key={part.id} value={part.id}>{part.sku}{part.name && part.name !== part.sku ? ` · ${part.name}` : ''}{part.active === false ? ' · Inactivo' : ''}</option>)}</select></label>
    {data?.next && <p className="form-footnote">Hay más repuestos. Afina la búsqueda por nombre o código.</p>}
    {data && !data.results.length && <p className="form-footnote">No encontramos repuestos para esta búsqueda.</p>}
    {error && <div className="notice error" role="alert">{error}<button type="button" onClick={() => setRevision(value => value + 1)}>Intentar de nuevo</button></div>}
  </div>;
}

export function AlternateEditor({ alternate, onClose, onSaved }: { alternate: ManagedAlternate | null; onClose: () => void; onSaved: () => void }) {
  const [part, setPart] = useState(alternate?.part || '');
  const [busy, setBusy] = useState(false); const [error, setError] = useState('');
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setError(''); setBusy(true); const form = new FormData(event.currentTarget);
    try { await request(`${base}/alternates${alternate ? `/${alternate.id}` : ''}`, { method: alternate ? 'PATCH' : 'POST', body: JSON.stringify({ part, code: form.get('alternate_code'), brand: form.get('alternate_maker'), ref_type: form.get('reference_type'), reference_source: form.get('reference_source') }) }); onSaved(); }
    catch (error) { setError(message(error)); } finally { setBusy(false); }
  }
  return <Modal title={alternate ? 'Editar alterno' : 'Crear alterno'} onClose={onClose} wide><form autoComplete="off" className="stack-form catalog-editor" onSubmit={submit}>
    <PartPicker label="SKU interno" value={part} initial={alternate ? { id: alternate.part, name: alternate.part_name, sku: alternate.part_sku } : undefined} onChange={setPart}/>
    <div className="form-row"><label>Código alterno<UppercaseInput name="alternate_code" defaultValue={alternate?.code} required maxLength={120} placeholder="Ej.: 58-1R0"/></label><label>Empresa o fabricante del código<UppercaseInput name="alternate_maker" defaultValue={alternate?.brand} maxLength={120} placeholder="Todas las marcas"/></label></div>
    <div className="form-row"><label>Tipo de referencia<select name="reference_type" defaultValue={alternate?.ref_type || (alternate?.kind === 'oem' ? 'oem' : alternate?.kind === 'manufacturer' ? 'company' : 'unknown')}><option value="unknown">SIN CLASIFICAR</option><option value="oem">OEM</option><option value="company">EMPRESA / FABRICANTE</option></select></label><label>Fuente de la equivalencia<input name="reference_source" autoComplete="off" maxLength={500} defaultValue={alternate?.reference_source} placeholder="Catálogo, ficha técnica o enlace"/></label></div>
    <p className="form-footnote">Esta referencia identifica una pieza equivalente del SKU maestro. El fabricante corresponde al código, no al proveedor. Guardarla no crea artículos ni existencias. Si el código ya es otro SKU, unifícalos primero con Agrupar SKU.</p>
    <FormError error={error}/><SaveButton busy={busy} disabled={!part}>Guardar alterno</SaveButton>
  </form></Modal>;
}

export function RemoveAlternate({ alternate, onClose, onSaved }: { alternate: ManagedAlternate; onClose: () => void; onSaved: () => void }) {
  const [busy, setBusy] = useState(false); const [error, setError] = useState('');
  async function remove() {
    setBusy(true); setError('');
    try { await request(`${base}/alternates/${alternate.id}`, { method: 'DELETE' }); onSaved(); }
    catch (error) { setError(message(error)); } finally { setBusy(false); }
  }
  return <Modal title="Retirar alterno" onClose={onClose}><p className="modal-description">Se retirará el código <strong>{alternate.code}</strong> del SKU <strong>{alternate.part_sku}</strong>. El SKU, los artículos de los proveedores y sus existencias se conservarán.</p><FormError error={error}/><button className="button dark full" disabled={busy} onClick={remove}>{busy && <LoaderCircle size={16} className="spin"/>}Confirmar retiro</button></Modal>;
}

// Revisar coincidencia (Existencias por proveedor): the item's OEM line, read through management/lines/<id> in any estado, can be changed
// for another (the item's Reference moves with it), or the item gets a line of its own with Separar referencia (its code becomes an
// interno code of its supplier, the line waits in Productos nuevos).
export function MatchEditor({ item, onClose, onSaved }: { item: ManagedStockItem; onClose: () => void; onSaved: (text?: string) => void }) {
  const [part, setPart] = useState(item.part || ''); const [status, setStatus] = useState(item.matching_status);
  const [initial, setInitial] = useState<Choice>(); const [line, setLine] = useState<OEMLine | null>(null);
  const [ready, setReady] = useState(!item.part); const [loadError, setLoadError] = useState(''); const [revision, setRevision] = useState(0);
  const [busy, setBusy] = useState(''); const [error, setError] = useState(''); const [separating, setSeparating] = useState(false);
  useEffect(() => {
    if (!item.part) return;
    let cancelled = false; setLoadError('');
    request<OEMLine>(`${base}/lines/${item.part}`).then(found => { if (!cancelled) { setLine(found); setInitial({ id: found.id, sku: found.sku, name: found.name, active: found.estado === 'publicada' }); setReady(true); } })
      .catch(error => { if (!cancelled) setLoadError(message(error)); });
    return () => { cancelled = true; };
  }, [item.part, revision]);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy('save'); setError('');
    try { await request(`${base}/inventory/${item.id}`, { method: 'PATCH', body: JSON.stringify({ part: part || null, matching_status: status }) }); onSaved(); }
    catch (error) { setError(message(error)); } finally { setBusy(''); }
  }
  async function separate() {
    setBusy('separate'); setError('');
    try {
      const result = await request<{ line: OEMLine }>(`${base}/inventory/${item.id}/separate`, { method: 'POST', body: JSON.stringify({}) });
      onSaved(`Referencia separada: ${item.codigo} queda como código interno de ${item.supplier_name} en la línea OEM ${result.line.sku}, que espera en Productos nuevos.`);
    } catch (error) { setError(message(error)); } finally { setBusy(''); }
  }
  const pending = item.codigo_pendiente || item.brand_pendiente;
  return <Modal title="Revisar coincidencia" onClose={onClose}><div className="match-summary"><strong>{item.codigo} · {item.brand}</strong><span>{item.supplier_name}</span><p>{item.description || 'Sin descripción del proveedor'}</p>{!!item.references?.length && <p>REFERENCIAS DECLARADAS (EN REVISIÓN): {item.references.map(ref => [ref.brand, ref.code].filter(Boolean).join(': ')).join(' · ')}</p>}{pending && <p className="stock-code-pending">CÓDIGO NUEVO EN REVISIÓN: {[item.codigo_pendiente || item.codigo, item.brand_pendiente].filter(Boolean).join(' · ')}. Se decide en Casos de coincidencia.</p>}{line && <p>LÍNEA OEM ACTUAL: {line.sku}{line.estado !== 'publicada' ? ` · ${lineEstadoLabels[line.estado]}` : ''}</p>}<small>ID del proveedor: {item.supplier_invent_id}</small></div><form autoComplete="off" className="stack-form catalog-editor" onSubmit={submit}>
    {ready ? <PartPicker label="SKU del catálogo" value={part} initial={initial} onChange={setPart}/> : loadError ? <div className="notice error" role="alert">{loadError}<button type="button" onClick={() => setRevision(value => value + 1)}>Intentar de nuevo</button></div> : <div className="loading"><LoaderCircle size={18} className="spin"/>Cargando repuesto…</div>}
    <label>Estado de coincidencia<select value={status} onChange={event => setStatus(event.target.value as ManagedStockItem['matching_status'])}><option value="pending">Pendiente</option><option value="review">Por revisar</option><option value="matched">Vinculado</option></select></label><p className="form-footnote">Selecciona el repuesto correcto y marca Vinculado para aprobarlo. Las cantidades y el historial de existencias se conservan.</p>
    <FormError error={error}/><SaveButton busy={busy === 'save'} disabled={!ready || !!busy || (status === 'matched' && !part)}>Guardar coincidencia</SaveButton>
  </form>
  {item.part && <section className="match-separate" aria-label="Separar referencia">{separating ? <div className="admin-toolbar-actions"><span>El artículo deja {line?.sku || 'su línea OEM'} y pasa a una línea propia en Productos nuevos.</span>
      <button type="button" className="button dark small" disabled={!!busy} onClick={() => void separate()}>{busy === 'separate' && <LoaderCircle size={14} className="spin"/>}Confirmar Separar referencia</button>
      <button type="button" className="button soft small" disabled={!!busy} onClick={() => setSeparating(false)}>Cancelar</button></div>
    : <button type="button" className="button text small" disabled={!!busy} onClick={() => setSeparating(true)}><Split size={14} aria-hidden="true"/>Separar referencia</button>}
    <p className="form-footnote">Úsalo cuando el código del proveedor no es esta pieza: su código queda como interno de este proveedor y nunca se vuelve a vincular solo a esta línea OEM.</p></section>}
  </Modal>;
}
