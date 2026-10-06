'use client';

import { FormEvent, useEffect, useState } from 'react';
import { ArrowDown, ArrowUp, LoaderCircle, Plus, SlidersHorizontal, Trash2 } from 'lucide-react';
import { Page, request } from '@/lib/types';
import { PartType, TechnicalField, TechnicalTemplate, VehicleApplication, applicationLabel, kindLabels, units } from '@/lib/technical-types';
import Modal from './modal';
import { UppercaseInput } from './uppercase-field';

const base = '/api/management';
const errorMessage = (e: unknown) => e instanceof Error ? e.message : 'No se pudo guardar. Inténtalo de nuevo.';
export function TechnicalError({ error }: { error: string }) { return error ? <div className="notice error" role="alert">{error}</div> : null; }

export default function AdminTechnical({ applications = false }: { applications?: boolean }) {
  const [search, setSearch] = useState(''); const [page, setPage] = useState(1); const [revision, setRevision] = useState(0);
  const [data, setData] = useState<Page<PartType | VehicleApplication> | null>(null); const [error, setError] = useState('');
  const [creating, setCreating] = useState(false); const [editing, setEditing] = useState<PartType | VehicleApplication | null>(null);
  const [template, setTemplate] = useState<PartType | null>(null); const [notice, setNotice] = useState('');
  const route = applications ? 'applications' : 'part-types';
  useEffect(() => {
    let live = true; setData(null); setError('');
    const timer = setTimeout(() => request<Page<PartType | VehicleApplication>>(`${base}/${route}?search=${encodeURIComponent(search)}&page=${page}`).then(r => { if (live) setData(r); }).catch(e => { if (live) setError(errorMessage(e)); }), 250);
    return () => { live = false; clearTimeout(timer); };
  }, [route, search, page, revision]);
  function saved() { setCreating(false); setEditing(null); setRevision(v => v + 1); setNotice('Cambios guardados.'); }
  return <section className="inventory-panel admin-panel technical-panel">
    <div className="admin-toolbar"><div><h2>{applications ? 'Aplicaciones vehiculares' : 'Plantillas técnicas'}</h2><p>{applications ? 'Configuraciones reutilizables. Vincúlalas a los SKU desde su ficha técnica.' : 'Cada subgrupo es un tipo de repuesto y tiene su propia plantilla.'}</p></div><button className="button primary" onClick={() => setCreating(true)}><Plus size={17}/>{applications ? 'Crear aplicación' : 'Crear subgrupo'}</button></div>
    {notice && <div className="notice success" role="status">{notice}</div>}
    <label className="technical-search">{applications ? 'Buscar aplicaciones' : 'Buscar grupo o subgrupo'}<UppercaseInput value={search} onChange={e => { setSearch(e.target.value); setPage(1); }} placeholder={applications ? 'MARCA, MODELO O MOTOR' : 'EJ.: FRENOS'}/></label>
    <TechnicalError error={error}/>{error && <button className="button soft small" onClick={() => setRevision(v => v + 1)}>Reintentar</button>}
    {!data && !error && <div className="loading"><LoaderCircle className="spin"/>Cargando…</div>}
    {data && !data.results.length && <div className="empty-state"><SlidersHorizontal/><h3>No hay resultados.</h3><p>{applications ? 'Agrega una configuración de vehículo para reutilizarla en tus repuestos.' : 'Crea un subgrupo y configura los campos de su ficha técnica.'}</p></div>}
    {!!data?.results.length && <div className="table-scroll"><table><thead><tr><th>{applications ? 'Vehículo / aplicación' : 'Grupo / subgrupo'}</th>{!applications && <th>SKU</th>}<th>Acciones</th></tr></thead><tbody>{data?.results.map(row => <tr key={row.id}><td>{applications ? <strong>{applicationLabel(row as VehicleApplication)}</strong> : <><strong>{(row as PartType).name}</strong><span>{(row as PartType).category}</span></>}</td>{!applications && <td>{(row as PartType).part_count}</td>}<td><div className="admin-row-actions"><button className="button soft small" onClick={() => setEditing(row)}>Editar</button>{!applications && <button className="button soft small" onClick={() => setTemplate(row as PartType)}>Configurar plantilla</button>}</div></td></tr>)}</tbody></table></div>}
    {data && <div className="pagination"><span>{data.count} resultados</span><div><button className="button soft small" disabled={!data.previous} onClick={() => setPage(v => v - 1)}>Anterior</button><span>Página {page}</span><button className="button soft small" disabled={!data.next} onClick={() => setPage(v => v + 1)}>Siguiente</button></div></div>}
    {(creating || editing) && (applications ? <ApplicationEditor initial={editing as VehicleApplication | null} onClose={() => { setCreating(false); setEditing(null); }} onSaved={saved}/> : <PartTypeEditor initial={editing as PartType | null} onClose={() => { setCreating(false); setEditing(null); }} onSaved={saved}/>)}
    {template && <TemplateEditor type={template} onClose={() => setTemplate(null)}/>}
  </section>;
}

function PartTypeEditor({ initial, onClose, onSaved }: { initial: PartType | null; onClose: () => void; onSaved: () => void }) {
  const [busy, setBusy] = useState(false); const [error, setError] = useState('');
  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault(); const form = new FormData(e.currentTarget); setBusy(true); setError('');
    try { await request(`${base}/part-types${initial ? `/${initial.id}` : ''}`, { method: initial ? 'PATCH' : 'POST', body: JSON.stringify({ category: form.get('group'), name: form.get('subgroup') }) }); onSaved(); }
    catch (e) { setError(errorMessage(e)); } finally { setBusy(false); }
  }
  return <Modal title={initial ? 'Editar subgrupo' : 'Crear subgrupo'} onClose={onClose}><form className="stack-form" autoComplete="off" onSubmit={submit}><label>Grupo<UppercaseInput name="group" required maxLength={120} defaultValue={initial?.category} placeholder="FRENOS"/></label><label>Subgrupo / tipo de repuesto<UppercaseInput name="subgroup" required maxLength={120} defaultValue={initial?.name} placeholder="DISCOS DE FRENO"/></label><p className="form-footnote">Al renombrarlo, sus SKU y su plantilla conservan el vínculo.</p><TechnicalError error={error}/><button className="button primary" disabled={busy}>{busy ? 'Guardando…' : 'Guardar subgrupo'}</button></form></Modal>;
}

export function ApplicationEditor({ initial, onClose, onSaved }: { initial: VehicleApplication | null; onClose: () => void; onSaved: () => void }) {
  const [busy, setBusy] = useState(false); const [error, setError] = useState('');
  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault(); const form = new FormData(e.currentTarget); setBusy(true); setError('');
    const data = Object.fromEntries(form.entries());
    try { await request(`${base}/applications${initial ? `/${initial.id}` : ''}`, { method: initial ? 'PATCH' : 'POST', body: JSON.stringify(data) }); onSaved(); }
    catch (e) { setError(errorMessage(e)); } finally { setBusy(false); }
  }
  const fields = { make: 'Marca del vehículo', model: 'Modelo', generation: 'Generación / chasis', engine: 'Motor', trim: 'Versión', transmission: 'Transmisión', market: 'Mercado' } as const;
  return <Modal title={initial ? 'Editar aplicación' : 'Crear aplicación'} onClose={onClose} wide><form className="stack-form" autoComplete="off" onSubmit={submit}><div className="technical-form-grid">{Object.entries(fields).map(([key, label]) => <label key={key}>{label}<UppercaseInput name={key} maxLength={120} required={key === 'make' || key === 'model'} defaultValue={initial?.[key as keyof typeof fields]}/></label>)}<label>Año desde<input name="year_from" type="number" min={1886} max={2200} required defaultValue={initial?.year_from}/></label><label>Año hasta<input name="year_to" type="number" min={1886} max={2200} required defaultValue={initial?.year_to}/></label></div><p className="form-footnote">Deja sin especificar los datos desconocidos. Crear una aplicación no confirma por sí solo que un repuesto sea compatible.</p><TechnicalError error={error}/><button className="button primary" disabled={busy}>{busy ? 'Guardando…' : 'Guardar aplicación'}</button></form></Modal>;
}

function TemplateEditor({ type, onClose }: { type: PartType; onClose: () => void }) {
  const [template, setTemplate] = useState<TechnicalTemplate | null>(null); const [error, setError] = useState(''); const [busy, setBusy] = useState(false); const [saved, setSaved] = useState(false); const [retry, setRetry] = useState(0);
  useEffect(() => { let live = true; setError(''); request<TechnicalTemplate>(`${base}/part-types/${type.id}/template`).then(t => { if (live) setTemplate(t); }).catch(e => { if (live) setError(errorMessage(e)); }); return () => { live = false; }; }, [type.id, retry]);
  function change(index: number, patch: Partial<TechnicalField>) { setSaved(false); setTemplate(t => t && ({ ...t, fields: t.fields.map((f, i) => i === index ? { ...f, ...patch } : f) })); }
  function move(index: number, by: number) { setSaved(false); setTemplate(t => { if (!t) return t; const fields = [...t.fields]; [fields[index], fields[index + by]] = [fields[index + by], fields[index]]; return { ...t, fields }; }); }
  async function save(e: FormEvent) { e.preventDefault(); if (!template) return; setBusy(true); setError(''); setSaved(false); try { setTemplate(await request<TechnicalTemplate>(`${base}/part-types/${type.id}/template`, { method: 'PATCH', body: JSON.stringify(template) })); setSaved(true); } catch (e) { setError(errorMessage(e)); } finally { setBusy(false); } }
  return <Modal title={`Plantilla · ${type.name}`} wide onClose={onClose}><form className="stack-form technical-configurator" autoComplete="off" onSubmit={save}>
    <p>{type.category} / {type.name}. Estos campos aparecerán en la ficha técnica de sus SKU.</p>
    <TechnicalError error={error}/>{error && <button type="button" className="button soft small" onClick={() => { setTemplate(null); setRetry(v => v + 1); }}>Recargar plantilla</button>}
    {!template && !error && <div className="loading">Cargando plantilla…</div>}
    {template && <><fieldset disabled={busy} className="technical-fields"><legend className="sr-only">Campos de la plantilla</legend>
      {template.fields.map((field, index) => <div className="technical-field-card" key={index}>
        <div className="technical-field-heading"><strong>Campo {index + 1}</strong><div><button className="icon-button" type="button" disabled={index === 0} aria-label={`Subir campo ${index + 1}`} onClick={() => move(index, -1)}><ArrowUp size={16}/></button><button className="icon-button" type="button" disabled={index === template.fields.length - 1} aria-label={`Bajar campo ${index + 1}`} onClick={() => move(index, 1)}><ArrowDown size={16}/></button><button className="icon-button danger-text" type="button" aria-label={`Quitar campo ${index + 1}`} onClick={() => { setSaved(false); setTemplate(t => t && ({ ...t, fields: t.fields.filter((_, i) => i !== index) })); }}><Trash2 size={16}/></button></div></div>
        <div className="technical-form-grid"><label>Nombre del campo<UppercaseInput value={field.label} required maxLength={120} onChange={e => change(index, { label: e.target.value })}/></label><label>Identificador estable<input autoComplete="off" value={field.key} required pattern="[a-z][a-z0-9_]{0,59}" maxLength={60} placeholder="diametro_exterior" onChange={e => change(index, { key: e.target.value.toLowerCase() })}/></label><label>Sección<UppercaseInput value={field.section} required maxLength={120} onChange={e => change(index, { section: e.target.value })}/></label><label>Tipo de dato<select value={field.kind} onChange={e => change(index, { kind: e.target.value as TechnicalField['kind'], unit: '', options: [] })}>{Object.entries(kindLabels).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label>{['number', 'integer'].includes(field.kind) && <label>Unidad<select value={field.unit} onChange={e => change(index, { unit: e.target.value })}>{units.map(u => <option key={u} value={u}>{u || 'SIN UNIDAD'}</option>)}</select></label>}{field.kind === 'choice' && <label>Opciones (separadas por punto y coma)<UppercaseInput required value={field.options.join(';')} onChange={e => change(index, { options: e.target.value.split(';') })} placeholder="VENTILADO;SÓLIDO"/></label>}</div>
        <label className="admin-checkbox"><input type="checkbox" checked={field.required} onChange={e => change(index, { required: e.target.checked })}/>Necesario para completar la ficha</label>
      </div>)}
      <button className="button soft" type="button" disabled={template.fields.length >= 100} onClick={() => { setSaved(false); setTemplate(t => t && ({ ...t, fields: [...t.fields, { key: '', label: '', section: 'CARACTERÍSTICAS', kind: 'number', unit: '', options: [], required: false, position: t.fields.length }] })); }}><Plus size={16}/>Agregar campo</button>
    </fieldset><p className="form-footnote">Los campos necesarios señalan fichas incompletas; puedes guardar avances. Si un campo tiene valores guardados, su identificador, tipo y unidad deben conservarse.</p>
    {saved && <div className="notice success" role="status">Plantilla guardada.</div>}<button className="button primary" disabled={busy}>{busy ? 'Guardando…' : 'Guardar plantilla'}</button></>}
  </form></Modal>;
}
