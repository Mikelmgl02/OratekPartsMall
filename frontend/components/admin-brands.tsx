'use client';

import { FormEvent, useEffect, useState } from 'react';
import { Check, LoaderCircle, Plus, Search, Tags, X } from 'lucide-react';
import { Page, request } from '@/lib/types';
import { brandTipoLabels } from '@/lib/line-types';
import type { Brand, BrandAliasAdded, BrandChanged, BrandRekey, BrandTipo } from '@/lib/line-types';
import Modal from './modal';
import { UppercaseInput } from './uppercase-field';

const base = '/api/management';
const message = (error: unknown) => error instanceof Error ? error.message : 'No se pudo guardar la marca. Inténtalo de nuevo.';
const number = (value: number) => value.toLocaleString('es-PA');

// Marcas (plan S8.5): one spelling per brand (its nombre, which its codes carry), its aliases (TOY → TOYOTA: a code typed with one keeps
// the nombre, and the storefront search reads them), its tipo (vehicle or parts maker) and its familia (KAIYI under CHERY: one level).
// management/brands, brands/<id> (rename, merge_into, tipo, familia, notas) and brands/<id>/aliases.
export default function AdminBrands() {
  const [search, setSearch] = useState(''); const [query, setQuery] = useState('');
  const [tipo, setTipo] = useState('');
  const [page, setPage] = useState(1);
  const [data, setData] = useState<Page<Brand> | null>(null);
  const [error, setError] = useState(''); const [notice, setNotice] = useState('');
  const [revision, setRevision] = useState(0);
  const [editing, setEditing] = useState<Brand | null | undefined>();
  useEffect(() => { const timer = setTimeout(() => { setQuery(search.trim()); setPage(1); }, 300); return () => clearTimeout(timer); }, [search]);
  useEffect(() => {
    let live = true; setError('');
    const params = new URLSearchParams({ page: String(page) });
    if (query) params.set('search', query);
    if (tipo) params.set('tipo', tipo);
    request<Page<Brand>>(`${base}/brands?${params}`).then(result => { if (live) setData(result); }).catch(caught => { if (live) setError(message(caught)); });
    return () => { live = false; };
  }, [query, tipo, page, revision]);
  function saved(text: string, keepOpen?: Brand) { setNotice(text); setRevision(value => value + 1); setEditing(keepOpen); }
  return <section className="inventory-panel admin-panel brands-admin" aria-label="Marcas">
    <div className="admin-toolbar"><div><h2>Marcas</h2><p>Cada marca con su nombre, sus alias, su tipo y su familia. Los códigos guardan el nombre de la marca aunque se escriban con un alias.</p></div>
      <div className="admin-toolbar-actions"><button type="button" className="button primary" onClick={() => setEditing(null)}><Plus size={17}/>Crear marca</button></div></div>
    {notice && <div className="notice success admin-notice" role="status"><Check size={16}/>{notice}</div>}
    <div className="inventory-filters">
      <div className="catalog-search admin-search"><Search size={19}/><label className="sr-only" htmlFor="brands-search">Buscar marcas</label><UppercaseInput id="brands-search" value={search} onChange={event => setSearch(event.target.value)} placeholder="Nombre o alias…"/></div>
      <label className="inventory-filter-select"><span>Tipo</span><select aria-label="Tipo de marca" value={tipo} onChange={event => { setTipo(event.target.value); setPage(1); }}>
        <option value="">TODAS</option><option value="vehiculo">{brandTipoLabels.vehiculo}</option><option value="repuesto">{brandTipoLabels.repuesto}</option><option value="none">SIN TIPO</option></select></label>
      <span className="inventory-count" aria-live="polite">{data ? `${number(data.count)} ${data.count === 1 ? 'marca' : 'marcas'}` : <><LoaderCircle className="spin" size={13}/>Cargando…</>}</span>
    </div>
    {error && <div className="notice error" role="alert">{error}<button type="button" onClick={() => setRevision(value => value + 1)}>Intentar de nuevo</button></div>}
    {data && !data.results.length && <div className="empty-state"><Tags size={30}/><h3>{query || tipo ? 'No encontramos marcas.' : 'Todavía no hay marcas.'}</h3><p>{query || tipo ? 'Prueba con otra búsqueda o filtro.' : 'Selecciona Crear marca para agregar la primera.'}</p></div>}
    {!!data?.results.length && <div className="table-scroll"><table className="brands-table">
      <thead><tr><th>Marca</th><th>Tipo</th><th>Alias</th><th>Familia</th><th>Referencias</th><th>Acciones</th></tr></thead>
      <tbody>{data.results.map(brand => <tr key={brand.id}>
        <td><strong>{brand.nombre}</strong>{brand.notas && <span>{brand.notas}</span>}</td>
        <td>{brand.tipo ? brandTipoLabels[brand.tipo] : <span className="admin-grid-muted">Sin tipo</span>}</td>
        <td><div className="brand-aliases">{brand.aliases.length ? brand.aliases.map(alias => <span key={alias.id} className="pill">{alias.alias}</span>) : <span className="admin-grid-muted">—</span>}</div></td>
        <td>{brand.familia ? <span>De la familia {brand.familia.nombre}</span> : brand.miembros.length ? <span>Encabeza: {brand.miembros.map(member => member.nombre).join(', ')}</span> : <span className="admin-grid-muted">—</span>}</td>
        <td>{number(brand.reference_count)}</td>
        <td><button type="button" className="button soft small" aria-label={`Editar marca ${brand.nombre}`} onClick={() => setEditing(brand)}>Editar</button></td>
      </tr>)}</tbody></table></div>}
    {data && <div className="pagination"><span>{number(data.count)} {data.count === 1 ? 'marca' : 'marcas'}</span><div><button type="button" className="button soft small" disabled={!data.previous} onClick={() => setPage(page - 1)}>Anterior</button><span>Página {page}</span><button type="button" className="button soft small" disabled={!data.next} onClick={() => setPage(page + 1)}>Siguiente</button></div></div>}
    <p className="admin-footnote">Renombrar una marca cambia el nombre en todos sus códigos y deja el anterior como alias. Unir dos marcas pasa sus códigos y alias a la que queda. Una familia junta marcas que comparten números (KAIYI con CHERY) sin mezclar sus códigos.</p>
    {editing !== undefined && <BrandEditor key={editing?.id ?? 'new'} brand={editing} onClose={changed => { setEditing(undefined); if (changed) setRevision(value => value + 1); }} onSaved={saved}/>}
  </section>;
}

// A brand picked by its name or alias (the familia and the brand a merge keeps).
// The codes a rename, merge or alias moved onto a nombre that already had them (mall.brands.rekey), said after the main notice.
function rekeyNotice(result: Partial<BrandRekey>) {
  const kept = result.kept ?? [], cases = result.cases ?? [];
  const retired = kept.filter(row => row.estado === 'retirada').length;
  const disputed = kept.filter(row => row.estado === 'disputa' && !row.case);
  const parts: string[] = [];
  if (cases.length) parts.push(`${number(cases.length)} ${cases.length === 1 ? 'caso de unificación abierto' : 'casos de unificación abiertos'} por códigos repetidos.`);
  if (retired) parts.push(`${number(retired)} ${retired === 1 ? 'código repetido quedó retirado, reemplazado' : 'códigos repetidos quedaron retirados, reemplazados'} por el de la otra línea.`);
  if (disputed.length) parts.push(`${disputed.length === 1 ? 'Queda en disputa' : `Quedan ${number(disputed.length)} en disputa`} (${disputed.map(row => row.code).join(', ')}): decide en el editor de su línea qué código lleva.`);
  return parts.length ? ` ${parts.join(' ')}` : '';
}

function BrandChooser({ label, value, onChange, exclude }: { label: string; value: { id: number; nombre: string } | null; onChange: (brand: { id: number; nombre: string } | null) => void; exclude?: number }) {
  const [text, setText] = useState(''); const [results, setResults] = useState<Brand[]>([]);
  useEffect(() => {
    const term = text.trim();
    if (!term) { setResults([]); return; }
    let live = true;
    const timer = setTimeout(() => request<Page<Brand>>(`${base}/brands?search=${encodeURIComponent(term)}&page=1`).then(page => { if (live) setResults(page.results.filter(row => row.id !== exclude).slice(0, 8)); }).catch(() => { if (live) setResults([]); }), 250);
    return () => { live = false; clearTimeout(timer); };
  }, [text, exclude]);
  return <div className="brand-chooser"><span className="brand-chooser-label">{label}</span>
    {value ? <div className="brand-chooser-value"><strong>{value.nombre}</strong><button type="button" className="icon-button" aria-label={`Quitar ${label.toLowerCase()}`} onClick={() => onChange(null)}><X size={14}/></button></div>
      : <><UppercaseInput aria-label={`Buscar ${label.toLowerCase()}`} value={text} onChange={event => setText(event.target.value)} placeholder="NOMBRE O ALIAS"/>
        {!!results.length && <ul className="brand-chooser-results">{results.map(row => <li key={row.id}><button type="button" className="button text small" onClick={() => { onChange({ id: row.id, nombre: row.nombre }); setText(''); }}>{row.nombre}{row.aliases.length ? ` · ${row.aliases.map(alias => alias.alias).join(', ')}` : ''}</button></li>)}</ul>}</>}
  </div>;
}

// onClose(changed): an alias added or removed is saved at once, so closing after one re-reads the list.
function BrandEditor({ brand, onClose, onSaved }: { brand: Brand | null; onClose: (changed: boolean) => void; onSaved: (text: string, keepOpen?: Brand) => void }) {
  const [current, setCurrent] = useState<Brand | null>(brand);
  const [nombre, setNombre] = useState(brand?.nombre ?? '');
  const [tipo, setTipo] = useState<BrandTipo>(brand?.tipo ?? '');
  const [familia, setFamilia] = useState<{ id: number; nombre: string } | null>(brand?.familia ?? null);
  const [notas, setNotas] = useState(brand?.notas ?? '');
  const [alias, setAlias] = useState('');
  const [mergeInto, setMergeInto] = useState<{ id: number; nombre: string } | null>(null);
  const [confirming, setConfirming] = useState(false);   // a merge removes the brand: it asks first, as retiring a reference does
  const [aliasChanged, setAliasChanged] = useState(false);
  const [busy, setBusy] = useState(''); const [error, setError] = useState(''); const [notice, setNotice] = useState('');
  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy('save'); setError('');
    try {
      if (!current) {
        const created = await request<Brand>(`${base}/brands`, { method: 'POST', body: JSON.stringify({ nombre, tipo, familia: familia?.id ?? null, notas }) });
        onSaved(`Marca ${created.nombre} creada.`);
        return;
      }
      const changes: Record<string, unknown> = {};
      if (nombre.trim().toUpperCase() !== current.nombre) changes.nombre = nombre;
      if (tipo !== current.tipo) changes.tipo = tipo;
      if ((familia?.id ?? null) !== (current.familia?.id ?? null)) changes.familia = familia?.id ?? null;
      if (notas !== current.notas) changes.notas = notas;
      if (!Object.keys(changes).length) { onClose(aliasChanged); return; }
      const saved = await request<BrandChanged>(`${base}/brands/${current.id}`, { method: 'PATCH', body: JSON.stringify(changes) });
      onSaved(changes.nombre ? `Marca renombrada a ${saved.nombre}: sus códigos la siguen y ${current.nombre} queda como alias.${rekeyNotice(saved)}` : `Marca ${saved.nombre} guardada.`);
    } catch (caught) { setError(message(caught)); } finally { setBusy(''); }
  }
  async function addAlias() {
    if (!current || !alias.trim()) return;
    setBusy('alias'); setError(''); setNotice('');
    try {
      const result = await request<BrandAliasAdded>(`${base}/brands/${current.id}/aliases`, { method: 'POST', body: JSON.stringify({ alias }) });
      setCurrent(result.brand); setAlias(''); setAliasChanged(true);
      setNotice(`Alias ${result.alias.alias} agregado.${result.moved ? ` ${number(result.moved)} ${result.moved === 1 ? 'código pasó' : 'códigos pasaron'} a ${result.brand.nombre}.` : ''}${rekeyNotice(result)}`);
    } catch (caught) { setError(message(caught)); } finally { setBusy(''); }
  }
  async function removeAlias(id: number, text: string) {
    if (!current) return;
    setBusy(`alias:${id}`); setError(''); setNotice('');
    try {
      await request(`${base}/brands/${current.id}/aliases/${id}`, { method: 'DELETE' });
      setCurrent({ ...current, aliases: current.aliases.filter(row => row.id !== id) }); setAliasChanged(true);
      setNotice(`Alias ${text} quitado.`);
    } catch (caught) { setError(message(caught)); } finally { setBusy(''); }
  }
  async function merge() {
    if (!current || !mergeInto) return;
    setBusy('merge'); setError(''); setConfirming(false);
    try {
      const kept = await request<BrandChanged>(`${base}/brands/${current.id}`, { method: 'PATCH', body: JSON.stringify({ merge_into: mergeInto.id }) });
      onSaved(`${current.nombre} se unió a ${kept.nombre}: sus códigos y alias pasaron a ${kept.nombre}.${rekeyNotice(kept)}`);
    } catch (caught) { setError(message(caught)); } finally { setBusy(''); }
  }
  return <Modal title={current ? `Marca · ${current.nombre}` : 'Crear marca'} onClose={() => { if (!busy) onClose(aliasChanged); }} wide>
    <form className="stack-form catalog-editor" autoComplete="off" onSubmit={save} aria-label="Datos de la marca">
      <div className="form-row"><label>Nombre<UppercaseInput aria-label="Nombre de la marca" value={nombre} required maxLength={120} onChange={event => setNombre(event.target.value)} placeholder="EJ.: TOYOTA"/></label>
        <label>Tipo<select aria-label="Tipo" value={tipo} onChange={event => setTipo(event.target.value as BrandTipo)}><option value="">SIN TIPO</option><option value="vehiculo">{brandTipoLabels.vehiculo}</option><option value="repuesto">{brandTipoLabels.repuesto}</option></select></label></div>
      <BrandChooser label="Familia" value={familia} onChange={setFamilia} exclude={current?.id}/>
      <p className="form-footnote">La familia es la marca que encabeza a otras que comparten números (KAIYI en la familia CHERY). Un solo nivel: una marca que encabeza una familia no pertenece a otra.</p>
      <label>Notas<input aria-label="Notas de la marca" value={notas} onChange={event => setNotas(event.target.value)}/></label>
      {error && <div className="notice error" role="alert">{error}</div>}
      <button className="button primary full" disabled={!!busy || !nombre.trim()}>{busy === 'save' && <LoaderCircle size={16} className="spin"/>}{current ? 'Guardar marca' : 'Crear marca'}</button>
    </form>
    {current && <>
      <section className="brand-alias-editor" aria-label="Alias">
        <strong>Alias</strong>
        {notice && <div className="notice success" role="status">{notice}</div>}
        <ul className="brand-aliases">{current.aliases.map(row => <li key={row.id} className="pill">{row.alias}<button type="button" className="icon-button" aria-label={`Quitar alias ${row.alias}`} disabled={!!busy} onClick={() => void removeAlias(row.id, row.alias)}><X size={12}/></button></li>)}
          {!current.aliases.length && <li className="admin-grid-muted">Sin alias.</li>}</ul>
        <div className="brand-alias-add"><UppercaseInput aria-label="Nuevo alias" value={alias} maxLength={120} onChange={event => setAlias(event.target.value)} onKeyDown={event => { if (event.key === 'Enter') { event.preventDefault(); void addAlias(); } }} placeholder="EJ.: TOY"/>
          <button type="button" className="button soft small" disabled={!!busy || !alias.trim()} onClick={() => void addAlias()}>{busy === 'alias' ? <LoaderCircle size={14} className="spin"/> : <Plus size={14}/>}Agregar alias</button></div>
        <p className="form-footnote">Los códigos que ya llevan ese alias como marca pasan al nombre de la marca; si eso repite un código en dos líneas OEM, se abre un caso de unificación.</p>
      </section>
      <section className="brand-merge" aria-label="Unir con otra marca">
        <strong>Unir con otra marca</strong>
        <BrandChooser label="Marca que queda" value={mergeInto} onChange={value => { setMergeInto(value); setConfirming(false); }} exclude={current.id}/>
        {confirming && mergeInto
          ? <div className="brand-merge-confirm" role="group" aria-label="Confirmar unión"><span className="reference-confirm">¿Unir {current.nombre} con {mergeInto.nombre}? {current.nombre} desaparece y sus {number(current.reference_count)} referencias pasan a {mergeInto.nombre}. No se puede deshacer desde aquí.</span>
              <button type="button" className="button dark small" disabled={!!busy} onClick={() => void merge()}>Confirmar unión</button>
              <button type="button" className="button soft small" disabled={!!busy} onClick={() => setConfirming(false)}>Cancelar</button></div>
          : <button type="button" className="button dark small" disabled={!!busy || !mergeInto} onClick={() => setConfirming(true)}>{busy === 'merge' && <LoaderCircle size={14} className="spin"/>}Unir {current.nombre}{mergeInto ? ` con ${mergeInto.nombre}` : ''}</button>}
        <p className="form-footnote">{current.nombre} desaparece: su nombre y sus alias quedan como alias de la marca que queda, y sus {number(current.reference_count)} referencias pasan a ella.</p>
      </section>
    </>}
  </Modal>;
}
