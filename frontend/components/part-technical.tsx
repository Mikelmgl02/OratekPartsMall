'use client';

import { FormEvent, useEffect, useState } from 'react';
import { LoaderCircle, Plus, Trash2 } from 'lucide-react';
import { ManagedPart, Page, request } from '@/lib/types';
import { ApplicationLink, SpecificationValue, TechnicalSheet, VehicleApplication, applicationLabel } from '@/lib/technical-types';
import Modal from './modal';
import { UppercaseInput, UppercaseTextarea } from './uppercase-field';
import { TechnicalError } from './admin-technical';

const message = (e: unknown) => e instanceof Error ? e.message : 'No se pudo cargar la ficha técnica.';

export function PartTechnicalEditor({ part, onClose, onSaved }: { part: ManagedPart; onClose: () => void; onSaved: () => void }) {
  const [sheet, setSheet] = useState<TechnicalSheet | null>(null); const [busy, setBusy] = useState(false); const [error, setError] = useState(''); const [retry, setRetry] = useState(0);
  const [values, setValues] = useState<Record<string, SpecificationValue>>({}); const [links, setLinks] = useState<ApplicationLink[]>([]);
  useEffect(() => { let live = true; setError(''); request<TechnicalSheet>(`/api/management/catalog/${part.id}/technical`).then(s => { if (live) { setSheet(s); setValues(s.values); setLinks(s.applications); } }).catch(e => { if (live) setError(message(e)); }); return () => { live = false; }; }, [part.id, retry]);
  function value(key: string, patch: Partial<SpecificationValue>) { setValues(v => ({ ...v, [key]: { ...v[key], value: v[key]?.value ?? null, ...patch } })); }
  function link(index: number, patch: Partial<ApplicationLink>) { setLinks(ls => ls.map((l, i) => i === index ? { ...l, ...patch } : l)); }
  async function submit(e: FormEvent) {
    e.preventDefault(); if (!sheet) return; setError(''); setBusy(true);
    try { await request(`/api/management/catalog/${part.id}/technical`, { method: 'PATCH', body: JSON.stringify({ revision: sheet.revision, part_type: sheet.part_type?.id ?? null, template_revision: sheet.template?.revision ?? null, values, applications: links.map(l => ({ ...l, application: l.application.id })) }) }); onSaved(); }
    catch (e) { setError(message(e)); } finally { setBusy(false); }
  }
  return <Modal title={`Ficha técnica · ${part.sku}`} onClose={onClose} wide><form className="stack-form technical-editor" autoComplete="off" onSubmit={submit}>
    <TechnicalError error={error}/>{error && <button type="button" className="button soft small" onClick={() => { setSheet(null); setRetry(v => v + 1); }}>Recargar ficha</button>}
    {!sheet && !error && <div className="loading"><LoaderCircle className="spin"/>Cargando ficha técnica…</div>}
    {sheet && <><p>{sheet.part_type ? `${sheet.part_type.category} / ${sheet.part_type.name}` : 'Asigna grupo y subgrupo desde Editar SKU para cargar su plantilla.'}</p><fieldset disabled={busy} className="technical-fields"><legend className="sr-only">Datos técnicos y aplicaciones</legend>
      {!sheet.template?.fields.length && <div className="notice">Este subgrupo todavía no tiene campos técnicos configurados. Puedes definirlos en Administración → Plantillas técnicas.</div>}
      {Array.from(new Set(sheet.template?.fields.map(f => f.section))).map(section => <section className="technical-field-card" key={section}><h3>{section}</h3><div className="technical-form-grid">{sheet.template?.fields.filter(f => f.section === section).map(field => <div className="technical-value" key={field.key}><label>{field.label}{field.unit ? ` (${field.unit})` : ''}{field.required ? ' · NECESARIO' : ''}
        {field.kind === 'boolean' ? <select value={values[field.key]?.value === true ? 'true' : values[field.key]?.value === false ? 'false' : ''} onChange={e => value(field.key, { value: e.target.value === '' ? null : e.target.value === 'true' })}><option value="">SIN ESPECIFICAR</option><option value="true">SÍ</option><option value="false">NO</option></select> : field.kind === 'choice' ? <select value={String(values[field.key]?.value ?? '')} onChange={e => value(field.key, { value: e.target.value })}><option value="">SIN ESPECIFICAR</option>{field.options.map(o => <option key={o}>{o}</option>)}</select> : field.kind === 'text' ? <UppercaseInput maxLength={2000} value={String(values[field.key]?.value ?? '')} onChange={e => value(field.key, { value: e.target.value })} placeholder="SIN ESPECIFICAR"/> : <input type="text" inputMode={field.kind === 'integer' ? 'numeric' : 'decimal'} autoComplete="off" value={String(values[field.key]?.value ?? '')} onChange={e => value(field.key, { value: e.target.value })} placeholder="SIN ESPECIFICAR"/>}
      </label><label className="technical-source">Fuente de {field.label}<input autoComplete="off" maxLength={500} value={values[field.key]?.source || ''} onChange={e => value(field.key, { source: e.target.value })} placeholder="CATÁLOGO, FICHA O REFERENCIA"/></label></div>)}</div></section>)}
      <section className="technical-field-card"><h3>APLICACIONES VEHICULARES</h3><p>Vincula configuraciones existentes. Solo las aplicaciones verificadas se muestran a los clientes.</p>
        {links.map((l, index) => <div className="technical-application-link" key={`${l.application.id}-${index}`}><div className="technical-field-heading"><strong>{applicationLabel(l.application)}</strong><button type="button" className="icon-button danger-text" aria-label={`Quitar aplicación ${index + 1}`} onClick={() => setLinks(ls => ls.filter((_, i) => i !== index))}><Trash2 size={16}/></button></div><div className="technical-form-grid"><label>Posición<UppercaseInput value={l.position} maxLength={120} placeholder="DELANTERO / TRASERO" onChange={e => link(index, { position: e.target.value })}/></label><label>Fuente de compatibilidad<input value={l.source || ''} maxLength={500} autoComplete="off" onChange={e => link(index, { source: e.target.value })}/></label></div><label>Condiciones de aplicación<UppercaseTextarea rows={2} maxLength={2000} value={l.notes} placeholder="EJ.: SOLO VEHÍCULOS CON ABS" onChange={e => link(index, { notes: e.target.value })}/></label><label className="admin-checkbox"><input type="checkbox" checked={!!l.verified} onChange={e => link(index, { verified: e.target.checked })}/>Compatibilidad verificada</label></div>)}
        <ApplicationPicker onAdd={a => setLinks(ls => [...ls, { application: a, position: '', notes: '', source: '', verified: false }])} selected={links.filter(l => !l.position).map(l => l.application.id)}/>
      </section>
    </fieldset><p className="form-footnote">Deja vacíos los datos desconocidos. Puedes completar la ficha en varias sesiones. Los datos de este SKU deben ser comunes a sus repuestos equivalentes.</p><button className="button primary" disabled={busy}>{busy ? 'Guardando…' : 'Guardar ficha técnica'}</button></>}
  </form></Modal>;
}

function ApplicationPicker({ selected, onAdd }: { selected: string[]; onAdd: (application: VehicleApplication) => void }) {
  const [search, setSearch] = useState(''); const [page, setPage] = useState(1); const [data, setData] = useState<Page<VehicleApplication> | null>(null); const [error, setError] = useState(''); const [retry, setRetry] = useState(0);
  useEffect(() => { let live = true; setError(''); setData(null); const timer = setTimeout(() => request<Page<VehicleApplication>>(`/api/management/applications?search=${encodeURIComponent(search)}&page=${page}`).then(d => { if (live) setData(d); }).catch(e => { if (live) setError(message(e)); }), 250); return () => { live = false; clearTimeout(timer); }; }, [search, page, retry]);
  return <div className="application-picker"><label>Buscar aplicación para vincular<UppercaseInput value={search} onKeyDown={e => { if (e.key === 'Enter') e.preventDefault(); }} onChange={e => { setSearch(e.target.value); setPage(1); }} placeholder="MARCA, MODELO O MOTOR"/></label><TechnicalError error={error}/>{error && <button type="button" className="button soft small" onClick={() => setRetry(v => v + 1)}>Reintentar búsqueda</button>}
    {!data && !error && <p>Cargando aplicaciones…</p>}{data && !data.results.length && <p>No hay aplicaciones para esta búsqueda. Créalas en Administración → Aplicaciones vehiculares.</p>}
    <div className="application-results">{data?.results.map(a => <div key={a.id}><span>{applicationLabel(a)}</span><button type="button" className="button soft small" disabled={selected.includes(a.id)} onClick={() => onAdd(a)}><Plus size={14}/>{selected.includes(a.id) ? 'Vinculada' : 'Vincular'}</button></div>)}</div>
    {data && (data.next || data.previous) && <div className="pagination"><button type="button" className="button soft small" disabled={!data.previous} onClick={() => setPage(v => v - 1)}>Anterior</button><span>Página {page}</span><button type="button" className="button soft small" disabled={!data.next} onClick={() => setPage(v => v + 1)}>Siguiente</button></div>}
  </div>;
}

export default function PartTechnical({ partId }: { partId: string }) {
  const [sheet, setSheet] = useState<TechnicalSheet | null>(null); const [error, setError] = useState(''); const [retry, setRetry] = useState(0);
  useEffect(() => { let live = true; setSheet(null); setError(''); request<TechnicalSheet>(`/api/market/catalog/${partId}/technical`).then(s => { if (live) setSheet(s); }).catch(e => { if (live) setError(message(e)); }); return () => { live = false; }; }, [partId, retry]);
  return <details className="part-technical"><summary>Ficha técnica y aplicaciones</summary>{error ? <div className="notice error" role="alert">{error}<button onClick={() => setRetry(v => v + 1)}>Reintentar</button></div> : !sheet ? <p>Cargando ficha…</p> : <>
    {!Object.keys(sheet.values).length && <p>No hay especificaciones publicadas para este SKU.</p>}
    {Array.from(new Set(sheet.template?.fields.map(f => f.section))).map(section => { const fields = sheet.template?.fields.filter(f => f.section === section && sheet.values[f.key]); return fields?.length ? <section key={section}><h4>{section}</h4><dl className="technical-spec-list">{fields.map(f => <div key={f.key}><dt>{f.label}</dt><dd>{sheet.values[f.key].value === true ? 'SÍ' : sheet.values[f.key].value === false ? 'NO' : String(sheet.values[f.key].value)}{f.unit && ` ${f.unit}`}</dd></div>)}</dl></section> : null; })}
    <h4>APLICACIONES VERIFICADAS</h4>{sheet.applications.length ? sheet.applications.map((l, index) => <div className="technical-application-public" key={index}><strong>{applicationLabel(l.application)}</strong>{l.position && <span>{l.position}</span>}{l.notes && <p>{l.notes}</p>}</div>) : <p>Todavía no hay aplicaciones verificadas. Consulta al proveedor para confirmar compatibilidad.</p>}
  </>}</details>;
}
