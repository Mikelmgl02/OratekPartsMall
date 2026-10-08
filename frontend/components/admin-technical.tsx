'use client';

import { FormEvent, useEffect, useMemo, useState } from 'react';
import type { ColDef } from 'ag-grid-community';
import type { CustomCellRendererProps } from 'ag-grid-react';
import AdminServerGrid from './admin-server-grid';
import { ArrowDown, ArrowUp, Plus, SlidersHorizontal, Trash2 } from 'lucide-react';
import { request } from '@/lib/types';
import { PartType, TechnicalField, TechnicalTemplate, VehicleApplication, applicationLabel, kindLabels, units } from '@/lib/technical-types';
import Modal from './modal';
import { UppercaseInput } from './uppercase-field';

const base = '/api/management';
const errorMessage = (e: unknown) => e instanceof Error ? e.message : 'No se pudo guardar. Inténtalo de nuevo.';
export function TechnicalError({ error }: { error: string }) { return error ? <div className="notice error" role="alert">{error}</div> : null; }

type TechnicalActions = { edit: (row: PartType | VehicleApplication) => void; template: (row: PartType) => void };
type Cell<T> = CustomCellRendererProps<T, unknown, TechnicalActions>;
const count = (value: number | null | undefined) => (value ?? 0).toLocaleString('es-PA');
function TypeActions({ data, context }: Cell<PartType>) {
  return data ? <div className="admin-grid-actions"><button className="button soft small" aria-label={`Editar subgrupo ${data.category} / ${data.name}`} onClick={() => context.edit(data)}>Editar</button><button className="button primary small" aria-label={`Configurar plantilla de ${data.name}`} onClick={() => context.template(data)}><SlidersHorizontal size={13} aria-hidden="true"/>Configurar plantilla</button></div> : null;
}
function ApplicationActions({ data, context }: Cell<VehicleApplication>) {
  return data ? <div className="admin-grid-actions"><button className="button soft small" aria-label={`Editar aplicación ${applicationLabel(data)}`} onClick={() => context.edit(data)}>Editar</button></div> : null;
}
const typeOrdering: Record<string, string> = { category: 'category', name: 'name', parts: 'part_count', fields: 'field_count' };
function typeColumns(wide: boolean): ColDef<PartType>[] {
  return [
    { colId: 'category', headerName: 'Grupo', field: 'category', width: 200, cellClass: 'admin-grid-text muted' },
    { colId: 'name', headerName: 'Subgrupo / tipo de repuesto', field: 'name', flex: 1, minWidth: 220, cellClass: 'admin-grid-text strong' },
    { colId: 'parts', headerName: 'SKU', field: 'part_count', valueFormatter: ({ value }) => count(value), width: 110, type: 'rightAligned', cellClass: ['ag-right-aligned-cell', 'admin-grid-number'] },
    { colId: 'fields', headerName: 'Campos', field: 'field_count', valueFormatter: ({ value }) => count(value), width: 110, type: 'rightAligned' },
    { colId: 'actions', headerName: 'Acciones', width: 270, pinned: wide ? 'right' : undefined, cellRenderer: TypeActions, suppressHeaderMenuButton: true, resizable: false },
  ];
}
const applicationOrdering: Record<string, string> = { make: 'make', model: 'model', generation: 'generation', years: 'year_from,year_to', engine: 'engine', trim: 'trim', transmission: 'transmission', market: 'market' };
function applicationColumns(wide: boolean): ColDef<VehicleApplication>[] {
  const text = (colId: keyof VehicleApplication, headerName: string, width: number, strong = false): ColDef<VehicleApplication> => ({ colId, headerName, field: colId, width, cellClass: `admin-grid-text${strong ? ' strong' : ''}`, valueFormatter: ({ value }) => value || '—' });
  return [
    { ...text('make', 'Marca', 150, true), pinned: wide ? 'left' : undefined },
    { ...text('model', 'Modelo', 170, true), flex: 1, minWidth: 150 },
    text('generation', 'Generación / chasis', 170),
    { colId: 'years', headerName: 'Años', valueGetter: ({ data }) => data ? `${data.year_from}–${data.year_to}` : '', width: 120, cellClass: 'admin-grid-text' },
    text('engine', 'Motor', 140), text('trim', 'Versión', 130), text('transmission', 'Transmisión', 130), text('market', 'Mercado', 120),
    { colId: 'actions', headerName: 'Acciones', width: 120, pinned: wide ? 'right' : undefined, cellRenderer: ApplicationActions, suppressHeaderMenuButton: true, resizable: false },
  ];
}

export default function AdminTechnical({ applications = false }: { applications?: boolean }) {
  const [search, setSearch] = useState(''); const [query, setQuery] = useState(''); const [revision, setRevision] = useState(0);
  const [creating, setCreating] = useState(false); const [editing, setEditing] = useState<PartType | VehicleApplication | null>(null);
  const [template, setTemplate] = useState<PartType | null>(null); const [notice, setNotice] = useState('');
  const route = applications ? 'applications' : 'part-types';
  const [wide] = useState(() => typeof window === 'undefined' || window.innerWidth >= 900);
  useEffect(() => { const timer = setTimeout(() => setQuery(search.trim()), 250); return () => clearTimeout(timer); }, [search]);
  const params = useMemo(() => ({ search: query }), [query]);
  const actions = useMemo<TechnicalActions>(() => ({ edit: row => setEditing(row), template: row => setTemplate(row) }), []);
  const typeGrid = useMemo(() => typeColumns(wide), [wide]);
  const applicationGrid = useMemo(() => applicationColumns(wide), [wide]);
  function saved() { setCreating(false); setEditing(null); setRevision(v => v + 1); setNotice('Cambios guardados.'); }
  return <section className="inventory-panel admin-panel technical-panel">
    <div className="admin-toolbar"><div><h2>{applications ? 'Aplicaciones vehiculares' : 'Plantillas técnicas'}</h2><p>{applications ? 'Configuraciones reutilizables. Vincúlalas a los SKU desde su ficha técnica.' : 'Cada subgrupo es un tipo de repuesto y tiene su propia plantilla.'}</p></div><button className="button primary" onClick={() => setCreating(true)}><Plus size={17}/>{applications ? 'Crear aplicación' : 'Crear subgrupo'}</button></div>
    {notice && <div className="notice success" role="status">{notice}</div>}
    <label className="technical-search">{applications ? 'Buscar aplicaciones' : 'Buscar grupo o subgrupo'}<UppercaseInput value={search} onChange={e => setSearch(e.target.value)} placeholder={applications ? 'MARCA, MODELO O MOTOR' : 'EJ.: FRENOS'}/></label>
    {applications
      ? <AdminServerGrid<VehicleApplication> key="applications" storageKey="admin-applications" label="Aplicaciones vehiculares" path={`${base}/applications`} params={params} columns={applicationGrid} rowId={row => row.id}
          ordering={applicationOrdering} revision={revision} rowHeight={52} context={actions} empty={<div className="empty-state"><SlidersHorizontal/><h3>No hay resultados.</h3><p>Agrega una configuración de vehículo para reutilizarla en tus repuestos.</p></div>}/>
      : <AdminServerGrid<PartType> key="part-types" storageKey="admin-part-types" label="Plantillas técnicas" path={`${base}/part-types`} params={params} columns={typeGrid} rowId={row => row.id}
          ordering={typeOrdering} revision={revision} rowHeight={56} context={actions} empty={<div className="empty-state"><SlidersHorizontal/><h3>No hay resultados.</h3><p>Crea un subgrupo y configura los campos de su ficha técnica.</p></div>}/>}
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
