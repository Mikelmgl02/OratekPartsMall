'use client';

import { FormEvent, useEffect, useRef, useState } from 'react';
import { LoaderCircle, Plus, Trash2 } from 'lucide-react';
import Modal from './modal';
import PartTypePicker from './part-type-picker';
import OEMReferenceLookup from './oem-reference-lookup';
import { UppercaseInput, UppercaseTextarea } from './uppercase-field';
import { PartReference, ManagedAlternate, ManagedPart, ManagedStockItem, Page, request } from '@/lib/types';

const base = '/api/management';
const message = (error: unknown) => error instanceof Error ? error.message : 'No se pudo guardar el cambio. Inténtalo de nuevo.';
type Choice = { id: string; sku: string; name: string; active?: boolean };

function FormError({ error }: { error: string }) {
  return error ? <div className="notice error" role="alert">{error}</div> : null;
}
function SaveButton({ busy, disabled, children }: { busy: boolean; disabled?: boolean; children: React.ReactNode }) {
  return <button className="button primary full" disabled={busy || disabled}>{busy && <LoaderCircle size={16} className="spin"/>}{busy ? 'Guardando…' : children}</button>;
}

export function PartEditor({ part, onClose, onSaved }: { part: ManagedPart | null; onClose: () => void; onSaved: (part: ManagedPart) => void }) {
  const [isOEM, setIsOEM] = useState(part?.is_OEM ?? false);
  const [category, setCategory] = useState(part?.category || '');
  const [subcategory, setSubcategory] = useState(part?.subcategory || '');
  const [codes, setCodes] = useState(() => (part?.codes.length ? part.codes : [{ brand: '', code: '' }] as PartReference[]).map((code, key) => ({ ...code, ref_type: code.ref_type || (code.kind === 'oem' ? 'oem' : code.kind === 'manufacturer' ? 'company' : 'unknown'), reference_source: code.reference_source || '', key })));
  const nextKey = useRef(codes.length);
  const [mainReference, setMainReference] = useState('auto');
  const [error, setError] = useState(''); const [busy, setBusy] = useState(false);
  function changeCode(key: number, field: 'brand' | 'code' | 'ref_type' | 'reference_source', value: string) { setCodes(rows => rows.map(row => row.key === key ? { ...row, [field]: value } : row)); }
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setError(''); setBusy(true);
    const form = new FormData(event.currentTarget);
    const payload = { sku: form.get('internal_sku'), is_OEM: isOEM, name: form.get('part_label'), description: form.get('part_details'), category: form.get('part_category'), subcategory: form.get('part_subcategory'), active: form.has('active'), codes: codes.filter(row => row.code.trim() || row.brand.trim()).map(row => ({ brand: row.brand.trim(), code: row.code, ref_type: row.ref_type, reference_source: row.reference_source })), ...(mainReference !== 'auto' && codes.some(row => String(row.key) === mainReference) ? { main_reference: (() => { const row = codes.find(row => String(row.key) === mainReference)!; return { code: row.code, brand: row.brand }; })() } : {}) };
    try { onSaved(await request<ManagedPart>(`${base}/catalog${part ? `/${part.id}` : ''}`, { method: part ? 'PATCH' : 'POST', body: JSON.stringify(payload) })); }
    catch (error) { setError(message(error)); } finally { setBusy(false); }
  }
  return <Modal title={part ? `Editar SKU · ${part.sku}` : 'Crear SKU'} onClose={onClose} wide><form autoComplete="off" className="stack-form catalog-editor" onSubmit={submit}>
    <div className="form-row"><label>SKU principal (MAIN)<UppercaseInput name="internal_sku" defaultValue={part?.sku} onChange={() => setIsOEM(false)} required maxLength={200} placeholder="Ej.: 58411-1R000"/></label><label>Nombre del repuesto (opcional)<UppercaseInput name="part_label" defaultValue={part?.name} maxLength={200} placeholder="Ej.: Disco de freno delantero"/></label></div>
    <label className="admin-checkbox"><input type="checkbox" name="is_OEM" checked={isOEM} onChange={event => setIsOEM(event.target.checked)}/>EL SKU PRINCIPAL ES OEM</label>
    <p className="form-footnote">Marca esta opción cuando hayas identificado el código principal como OEM. Sin marcar significa interno, de empresa o aún sin verificar. Cada alterno conserva su propio tipo de referencia.</p>
    <label>Descripción<UppercaseTextarea aria-label="Descripción" name="part_details" defaultValue={part?.description} rows={3} placeholder="Características, aplicación o medidas del repuesto"/></label>
    <div className="form-row"><label>Grupo (opcional)<UppercaseInput name="part_category" value={category} onChange={e => setCategory(e.target.value)} maxLength={120} placeholder="EJ.: FRENOS"/></label><label>Subgrupo / tipo de repuesto (opcional)<UppercaseInput name="part_subcategory" value={subcategory} onChange={e => setSubcategory(e.target.value)} maxLength={120} placeholder="EJ.: DISCOS DE FRENO"/></label></div>
    <PartTypePicker onSelect={type => { setCategory(type.category); setSubcategory(type.name); }}/>
    <p className="form-footnote">El subgrupo define los campos de la ficha técnica. Guarda el SKU y abre Ficha técnica para completar sus medidas y aplicaciones.</p>
    <section className="notice oem-preference"><strong>MAIN basado en una referencia OEM</strong><p>Usamos el OEM verificado como código principal y conservamos los códigos de empresa como alternos. El ID del repuesto y las existencias de los proveedores se mantienen.</p>
      <label>OEM para el MAIN<select aria-label="OEM para el MAIN" value={mainReference} onChange={event => setMainReference(event.target.value)}><option value="auto">AUTOMÁTICO · CONSERVAR OEM ACTUAL O USAR EL ÚNICO VERIFICADO</option>{codes.filter(row => row.ref_type === 'oem' && row.code.trim() && row.brand.trim() && row.reference_source.trim()).map(row => <option key={row.key} value={row.key}>{row.code} · {row.brand}</option>)}</select></label>
      <p className="form-footnote">Al promover una referencia OEM verificada, el MAIN se marca como OEM automáticamente. Si hay varios OEM, elige el principal. Una referencia a un conjunto completo no demuestra que la pieza individual sea equivalente.</p>
    </section>
    {part && <OEMReferenceLookup part={part} onAdd={references => setCodes(existing => { const pairs = new Set(existing.map(row => `${row.brand}:${row.code}`)); return [...existing, ...references.filter(row => !pairs.has(`${row.brand}:${row.code}`)).map(row => ({...row, ref_type: row.ref_type || 'oem', reference_source: row.reference_source || '', key: nextKey.current++}))]; })}/>}
    <fieldset className="catalog-codes"><legend>Biblioteca de equivalencias del SKU maestro</legend><p>Agrega referencias OEM y de fabricantes que identifiquen la misma pieza, aunque sus códigos sean distintos. El fabricante pertenece a la referencia; los artículos y las existencias de cada proveedor se gestionan por separado.</p>
      {codes.map((row, index) => <div className="catalog-reference-entry" key={row.key}>
        <div className="catalog-code-row"><label>{row.ref_type === 'company' ? 'Empresa / fabricante' : 'Fabricante del código'} {index + 1}{row.ref_type === 'company' ? '' : ' (opcional)'}<UppercaseInput aria-label={`Marca del código ${index + 1}`} name={`part_code_maker_${row.key}`} value={row.brand} onChange={event => changeCode(row.key, 'brand', event.target.value)} required={row.ref_type === 'company'} maxLength={120} placeholder="EJ.: TOYOTA, FEBEST, NIBK"/></label><label>Código {index + 1}<UppercaseInput name={`part_code_value_${row.key}`} value={row.code} onChange={event => changeCode(row.key, 'code', event.target.value)} required={!!row.brand.trim()} maxLength={120} placeholder="EJ.: 51712-1R000"/></label><button type="button" className="icon-button danger-text" aria-label={`Quitar código ${index + 1}`} onClick={() => { setCodes(rows => rows.filter(code => code.key !== row.key)); if (mainReference === String(row.key)) setMainReference('auto'); }}><Trash2 size={17}/></button></div>
        <div className="form-row"><label>Tipo de referencia {index + 1}<select aria-label={`Tipo de referencia ${index + 1}`} value={row.ref_type} onChange={event => { changeCode(row.key, 'ref_type', event.target.value); if (mainReference === String(row.key)) setMainReference('auto'); }}><option value="unknown">SIN CLASIFICAR</option><option value="oem">OEM</option><option value="company">EMPRESA / FABRICANTE</option></select></label><label>Fuente de la equivalencia {index + 1}<input autoComplete="off" maxLength={500} value={row.reference_source} onChange={event => changeCode(row.key, 'reference_source', event.target.value)} placeholder="Catálogo, ficha técnica o enlace"/></label></div>
      </div>)}
      <button type="button" className="button soft small" onClick={() => setCodes(rows => [...rows, { key: nextKey.current++, brand: '', code: '', ref_type: 'unknown', reference_source: '' }])}><Plus size={15}/>Agregar código</button>
      <p>Registra equivalencias verificadas. Una aplicación parecida, una variante o un kit no bastan para considerarlas la misma pieza.</p>
    </fieldset>
    <label className="admin-checkbox"><input name="active" type="checkbox" defaultChecked={part?.active ?? true}/>SKU activo</label><p className="form-footnote">El SKU agrupa repuestos idénticos de distintas marcas y proveedores. Las existencias se conservan por artículo del proveedor.</p>
    <FormError error={error}/><SaveButton busy={busy}>Guardar SKU</SaveButton>
  </form></Modal>;
}

function PartPicker({ label, value, initial, onChange, exclude }: { label: string; value: string; initial?: Choice; onChange: (value: string) => void; exclude?: string }) {
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

export function MatchEditor({ item, onClose, onSaved }: { item: ManagedStockItem; onClose: () => void; onSaved: () => void }) {
  const [part, setPart] = useState(item.part || ''); const [status, setStatus] = useState(item.matching_status);
  const [initial, setInitial] = useState<Choice>(); const [ready, setReady] = useState(!item.part); const [loadError, setLoadError] = useState(''); const [revision, setRevision] = useState(0);
  const [busy, setBusy] = useState(false); const [error, setError] = useState('');
  useEffect(() => {
    if (!item.part) return;
    let cancelled = false; setLoadError('');
    request<ManagedPart>(`${base}/catalog/${item.part}`).then(part => { if (!cancelled) { setInitial(part); setReady(true); } }).catch(error => { if (!cancelled) setLoadError(message(error)); });
    return () => { cancelled = true; };
  }, [item.part, revision]);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); setError('');
    try { await request(`${base}/inventory/${item.id}`, { method: 'PATCH', body: JSON.stringify({ part: part || null, matching_status: status }) }); onSaved(); }
    catch (error) { setError(message(error)); } finally { setBusy(false); }
  }
  return <Modal title="Revisar coincidencia" onClose={onClose}><div className="match-summary"><strong>{item.codigo} · {item.brand}</strong><span>{item.supplier_name}</span><p>{item.description || 'Sin descripción del proveedor'}</p>{!!item.references?.length && <p>REFERENCIAS DECLARADAS: {item.references.map(ref => [ref.brand, ref.code].filter(Boolean).join(': ')).join(' · ')}</p>}<small>ID del proveedor: {item.supplier_invent_id}</small></div><form autoComplete="off" className="stack-form catalog-editor" onSubmit={submit}>
    {ready ? <PartPicker label="SKU del catálogo" value={part} initial={initial} onChange={setPart}/> : loadError ? <div className="notice error" role="alert">{loadError}<button type="button" onClick={() => setRevision(value => value + 1)}>Intentar de nuevo</button></div> : <div className="loading"><LoaderCircle size={18} className="spin"/>Cargando repuesto…</div>}
    <label>Estado de coincidencia<select value={status} onChange={event => setStatus(event.target.value as ManagedStockItem['matching_status'])}><option value="pending">Pendiente</option><option value="review">Por revisar</option><option value="matched">Vinculado</option></select></label><p className="form-footnote">Selecciona el repuesto correcto y marca Vinculado para aprobarlo. Las cantidades y el historial de existencias se conservan.</p>
    <FormError error={error}/><SaveButton busy={busy} disabled={!ready || (status === 'matched' && !part)}>Guardar coincidencia</SaveButton>
  </form></Modal>;
}
