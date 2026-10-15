'use client';

import { FormEvent, useEffect, useState } from 'react';
import { Check, FolderTree, LoaderCircle, Plus } from 'lucide-react';
import { request } from '@/lib/types';
import type { Grupo, Subgrupo } from '@/lib/line-types';
import type { PartType } from '@/lib/technical-types';
import { forgetGrupos, loadGrupos } from '@/lib/use-grupos';
import Modal from './modal';
import { UppercaseInput } from './uppercase-field';

const base = '/api/management';
const message = (error: unknown) => error instanceof Error ? error.message : 'No se pudo guardar. Inténtalo de nuevo.';
const number = (value: number) => value.toLocaleString('es-PA');
type Editing = { kind: 'grupo'; grupo: Grupo | null } | { kind: 'subgrupo'; grupo: Grupo; subgrupo: Subgrupo | null };

// Grupos y subgrupos (plan S8.6, split out of Plantillas técnicas): each grupo with its subgrupos and how many catalog lines each holds.
// A grupo is created and renamed through management/grupos (its subgrupos' and lines' text follow); a subgrupo, the row the technical
// sheet keys on, is added to a grupo, renamed or moved to another through management/part-types. Its template stays in Plantillas técnicas.
export default function AdminGrupos() {
  const [grupos, setGrupos] = useState<Grupo[] | null>(null);
  const [error, setError] = useState(''); const [notice, setNotice] = useState('');
  const [revision, setRevision] = useState(0);
  const [editing, setEditing] = useState<Editing | null>(null);
  useEffect(() => {
    let live = true; setError('');
    loadGrupos(revision > 0).then(rows => { if (live) setGrupos(rows); }).catch(caught => { if (live) setError(message(caught)); });
    return () => { live = false; };
  }, [revision]);
  function saved(text: string) { forgetGrupos(); setEditing(null); setNotice(text); setRevision(value => value + 1); }
  const total = grupos?.reduce((sum, grupo) => sum + grupo.subgrupos.length, 0) ?? 0;
  return <section className="inventory-panel admin-panel grupos-admin" aria-label="Grupos y subgrupos">
    <div className="admin-toolbar"><div><h2>Grupos y subgrupos</h2><p>La clasificación del catálogo: cada línea OEM pertenece a un subgrupo, y el subgrupo define los campos de su ficha técnica.</p></div>
      <div className="admin-toolbar-actions"><button type="button" className="button primary" onClick={() => setEditing({ kind: 'grupo', grupo: null })}><Plus size={17}/>Crear grupo</button></div></div>
    {notice && <div className="notice success admin-notice" role="status"><Check size={16}/>{notice}</div>}
    {error && <div className="notice error" role="alert">{error}<button type="button" onClick={() => setRevision(value => value + 1)}>Intentar de nuevo</button></div>}
    {!grupos && !error && <div className="loading"><LoaderCircle className="spin" size={18}/>Cargando grupos…</div>}
    {grupos && !grupos.length && <div className="empty-state"><FolderTree size={30}/><h3>Todavía no hay grupos.</h3><p>Selecciona Crear grupo para agregar el primero.</p></div>}
    {grupos && !!grupos.length && <p className="inventory-count">{number(grupos.length)} grupos · {number(total)} subgrupos</p>}
    <div className="grupos-list">{grupos?.map(grupo => <article key={grupo.id} className="grupo-card" aria-label={`Grupo ${grupo.nombre}`}>
      <header><div><strong>{grupo.nombre}</strong><small>{number(grupo.subgrupos.length)} {grupo.subgrupos.length === 1 ? 'subgrupo' : 'subgrupos'} · {number(grupo.line_count)} {grupo.line_count === 1 ? 'línea OEM' : 'líneas OEM'}</small></div>
        <div className="admin-toolbar-actions"><button type="button" className="button soft small" aria-label={`Renombrar grupo ${grupo.nombre}`} onClick={() => setEditing({ kind: 'grupo', grupo })}>Renombrar</button>
          <button type="button" className="button text small" aria-label={`Agregar subgrupo a ${grupo.nombre}`} onClick={() => setEditing({ kind: 'subgrupo', grupo, subgrupo: null })}><Plus size={14}/>Subgrupo</button></div></header>
      {grupo.subgrupos.length ? <ul className="subgrupo-list">{grupo.subgrupos.map(sub => <li key={sub.id}><span>{sub.name}</span><small>{number(sub.line_count)}</small>
        <button type="button" className="button text small" aria-label={`Editar subgrupo ${grupo.nombre} / ${sub.name}`} onClick={() => setEditing({ kind: 'subgrupo', grupo, subgrupo: sub })}>Editar</button></li>)}</ul>
        : <p className="admin-grid-muted">Sin subgrupos.</p>}
    </article>)}</div>
    <p className="admin-footnote">Renombrar un grupo o un subgrupo actualiza el texto de sus líneas OEM y los filtros de la tienda. La plantilla de la ficha técnica de cada subgrupo se edita en Plantillas técnicas.</p>
    {editing && <GrupoEditor editing={editing} grupos={grupos ?? []} onClose={() => setEditing(null)} onSaved={saved}/>}
  </section>;
}

function GrupoEditor({ editing, grupos, onClose, onSaved }: { editing: Editing; grupos: Grupo[]; onClose: () => void; onSaved: (text: string) => void }) {
  const [name, setName] = useState(editing.kind === 'grupo' ? editing.grupo?.nombre ?? '' : editing.subgrupo?.name ?? '');
  const [grupoId, setGrupoId] = useState(editing.kind === 'subgrupo' ? editing.grupo.id : '');
  const [busy, setBusy] = useState(false); const [error, setError] = useState('');
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); setError('');
    try {
      if (editing.kind === 'grupo') {
        const saved = await request<Grupo>(`${base}/grupos${editing.grupo ? `/${editing.grupo.id}` : ''}`, { method: editing.grupo ? 'PATCH' : 'POST', body: JSON.stringify({ nombre: name }) });
        onSaved(editing.grupo ? `Grupo renombrado a ${saved.nombre}.` : `Grupo ${saved.nombre} creado.`);
      } else {
        const category = grupos.find(grupo => grupo.id === grupoId)?.nombre ?? editing.grupo.nombre;
        const saved = await request<PartType>(`${base}/part-types${editing.subgrupo ? `/${editing.subgrupo.id}` : ''}`, { method: editing.subgrupo ? 'PATCH' : 'POST', body: JSON.stringify({ category, name }) });
        onSaved(editing.subgrupo ? `Subgrupo ${saved.category} / ${saved.name} guardado.` : `Subgrupo ${saved.category} / ${saved.name} creado.`);
      }
    } catch (caught) { setError(message(caught)); } finally { setBusy(false); }
  }
  const title = editing.kind === 'grupo' ? (editing.grupo ? `Renombrar grupo · ${editing.grupo.nombre}` : 'Crear grupo') : (editing.subgrupo ? `Subgrupo · ${editing.subgrupo.name}` : `Nuevo subgrupo en ${editing.grupo.nombre}`);
  return <Modal title={title} onClose={() => { if (!busy) onClose(); }}>
    <form className="stack-form catalog-editor" autoComplete="off" onSubmit={submit}>
      <label>{editing.kind === 'grupo' ? 'Nombre del grupo' : 'Nombre del subgrupo'}<UppercaseInput aria-label={editing.kind === 'grupo' ? 'Nombre del grupo' : 'Nombre del subgrupo'} value={name} required maxLength={120} onChange={event => setName(event.target.value)} placeholder={editing.kind === 'grupo' ? 'EJ.: FRENOS' : 'EJ.: DISCOS DE FRENO'}/></label>
      {editing.kind === 'subgrupo' && editing.subgrupo && <label>Grupo<select aria-label="Grupo del subgrupo" value={grupoId} onChange={event => setGrupoId(event.target.value)}>{grupos.map(grupo => <option key={grupo.id} value={grupo.id}>{grupo.nombre}</option>)}</select></label>}
      {editing.kind === 'subgrupo' && editing.subgrupo && <p className="form-footnote">Cambiar el grupo mueve el subgrupo con sus {number(editing.subgrupo.line_count)} líneas OEM y su plantilla.</p>}
      {error && <div className="notice error" role="alert">{error}</div>}
      <button className="button primary full" disabled={busy || !name.trim()}>{busy && <LoaderCircle size={16} className="spin"/>}Guardar</button>
    </form>
  </Modal>;
}
