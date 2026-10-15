'use client';

import { useEffect, useState } from 'react';
import { ArrowLeft, Bot, Check, LoaderCircle, Search, Ungroup } from 'lucide-react';
import { Page, request } from '@/lib/types';
import type { MergedSku, SplitChild, SplitPlan, SplitResult } from '@/lib/catalog-split-types';
import { referenceLabel } from '@/lib/reference-label';
import Modal from './modal';
import { UppercaseInput } from './uppercase-field';

type Target = { id: string; sku: string };
type ListState = { search: string; origin: string; page: number };
const day = (value: string | null) => value ? new Date(value).toLocaleDateString('es-PA', { timeZone: 'America/Panama', dateStyle: 'medium' }) : '';
const subgroup = (part: { category: string; subcategory: string }) => [part.category, part.subcategory].filter(Boolean).join(' / ');
const plural = (count: number, one: string, many: string) => `${count} ${count === 1 ? one : many}`;

// Separar SKU: undo a grouping that put different parts under one SKU. Opened from the tool, it lists the SKUs holding grouped SKUs
// (latest grouping first) and comes back to the list after each split, since bad groupings come in batches; opened from a grid row's
// badge, it goes straight to that SKU and closes when done.
export default function CatalogSplit({ part = null, onClose, onChanged }: { part?: Target | null; onClose: () => void; onChanged: (text: string) => void }) {
  const [target, setTarget] = useState<Target | null>(part);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState('');
  // The list keeps its search, filter and page while a split is reviewed, so the next one is where it was.
  const [list, setList] = useState<ListState>({ search: '', origin: '', page: 1 });
  function saved(text: string) {
    onChanged(text);
    if (part) onClose();
    else { setNotice(text); setTarget(null); }
  }
  return <Modal title="Separar SKU" wide onClose={() => { if (!busy) onClose(); }}>
    <p className="modal-description">Devuelve los SKU que se agruparon por error: vuelven a estar activos con los alternos, existencias e imágenes que les asignes, y el servicio de coincidencias no los vuelve a agrupar.</p>
    {notice && !target && <p className="notice success" role="status"><Check size={16}/>{notice}</p>}
    {target ? <SplitEditor key={target.id} target={target} onBack={part ? undefined : () => setTarget(null)} onBusyChange={setBusy} onSaved={saved}/>
      : <MergedList state={list} onState={setList} onPick={next => { setNotice(''); setTarget(next); }}/>}
  </Modal>;
}

function MergedList({ state, onState, onPick }: { state: ListState; onState: (state: ListState) => void; onPick: (target: Target) => void }) {
  const { origin, page } = state;
  const [search, setSearch] = useState(state.search); const [query, setQuery] = useState(state.search.trim());
  const [data, setData] = useState<Page<MergedSku> | null>(null);
  const [error, setError] = useState(''); const [revision, setRevision] = useState(0);
  const setPage = (next: (value: number) => number) => onState({ ...state, page: next(page) });
  useEffect(() => {
    if (search.trim() === query) return;
    const timer = setTimeout(() => { setQuery(search.trim()); onState({ ...state, search, page: 1 }); }, 300);
    return () => clearTimeout(timer);
  }, [search, query, state, onState]);
  useEffect(() => {
    let live = true; setData(null); setError('');
    const params = new URLSearchParams({ page: String(page) });
    if (query) params.set('search', query);
    if (origin) params.set('origin', origin);
    request<Page<MergedSku>>(`/api/management/catalog/merged?${params}`).then(result => { if (live) setData(result); })
      .catch(reason => { if (live) setError((reason as Error).message); });
    return () => { live = false; };
  }, [query, origin, page, revision]);
  return <>
    <div className="split-filters">
      <div className="catalog-search admin-search"><Search size={18}/><UppercaseInput aria-label="Buscar SKU agrupados" value={search} placeholder="SKU principal, descripción o código agrupado…" onChange={event => setSearch(event.target.value)}/></div>
      <label className="inventory-filter-select"><span>Agrupado por</span><select aria-label="Agrupado por" value={origin} onChange={event => onState({ ...state, origin: event.target.value, page: 1 })}>
        <option value="">TODOS</option><option value="automatic">SERVICIO AUTOMÁTICO</option><option value="manual">UNA PERSONA</option></select></label>
    </div>
    {error && <p className="notice error" role="alert">{error}<button type="button" onClick={() => setRevision(value => value + 1)}>Intentar de nuevo</button></p>}
    {!data && !error && <div className="loading"><LoaderCircle className="spin" size={20}/>Buscando SKU agrupados…</div>}
    {data && !data.results.length && <div className="empty-state"><Ungroup size={30}/><h3>No encontramos SKU agrupados.</h3><p>{query || origin ? 'Prueba otra búsqueda o filtro.' : 'Los SKU que se agrupen aparecerán aquí.'}</p></div>}
    {!!data?.results.length && <div className="grouping-families">{data.results.map(row => <article className="grouping-family" key={row.id}>
      <div><h3>{row.sku}{!row.active && <span className="split-inactive"> · INACTIVO</span>}</h3><p>{row.description || 'Sin descripción'}</p>
        <div className="admin-role-list">{row.merged.map(item => <span key={item.sku} title={item.description}>{item.sku}</span>)}{row.merged_count > row.merged.length && <span>+{row.merged_count - row.merged.length}</span>}</div>
        <small>{plural(row.merged_count, 'SKU agrupado', 'SKU agrupados')}{row.last_merged_at ? ` · ${day(row.last_merged_at)}` : ''} · {row.automatic ? 'Agrupación automática' : 'Agrupado por una persona'}</small></div>
      <button type="button" className="button soft small" aria-label={`Revisar ${row.sku}`} onClick={() => onPick({ id: row.id, sku: row.sku })}>Revisar</button>
    </article>)}</div>}
    {data && <div className="pagination grouping-pagination"><span>{plural(data.count, 'SKU con agrupados', 'SKU con agrupados')}</span><div>
      <button type="button" className="button soft small" disabled={!data.previous} onClick={() => setPage(value => value - 1)}>Anterior</button><span>Página {page}</span>
      <button type="button" className="button soft small" disabled={!data.next} onClick={() => setPage(value => value + 1)}>Siguiente</button></div></div>}
  </>;
}

function Evidence({ child }: { child: SplitChild }) {
  return <div className="grouping-evidence">
    <strong>{child.automatic ? <><Bot size={12} aria-hidden="true"/> Agrupado automáticamente</> : `Agrupado por ${child.merged_by}`}{child.merged_at ? ` el ${day(child.merged_at)}` : ''}</strong>
    <p>{child.method}</p>
    <small>{child.codes === null ? 'No quedó registro de sus alternos al agruparse.' : child.codes.length ? `Alternos al agruparse: ${child.codes.map(code => code.code).join(', ')}` : 'No tenía alternos al agruparse.'}</small>
  </div>;
}

function SplitEditor({ target, onBack, onBusyChange, onSaved }: { target: Target; onBack?: () => void; onBusyChange: (busy: boolean) => void; onSaved: (text: string) => void }) {
  const [plan, setPlan] = useState<SplitPlan | null>(null);
  const [error, setError] = useState(''); const [revision, setRevision] = useState(0);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  // An explicit destination per row ('code:7' -> grouped SKU id, '' = stays); rows without one follow the plan's suggestion.
  const [choices, setChoices] = useState<Record<string, string>>({});
  const [reviewed, setReviewed] = useState(false);
  const [saving, setSaving] = useState(false); const [saveError, setSaveError] = useState('');
  useEffect(() => {
    let live = true; setPlan(null); setError('');
    request<SplitPlan>(`/api/management/catalog/${target.id}/split`).then(result => { if (live) setPlan(result); }).catch(reason => { if (live) setError((reason as Error).message); });
    return () => { live = false; };
  }, [target.id, revision]);
  if (error) return <p className="notice error" role="alert">{error}<button type="button" onClick={() => setRevision(value => value + 1)}>Intentar de nuevo</button></p>;
  if (!plan) return <div className="loading"><LoaderCircle className="spin" size={20}/>Cargando {target.sku}…</div>;

  const { children, target: main } = plan;
  const chosen = children.filter(child => selected.has(child.id));
  const sku = (id: string) => id ? children.find(child => child.id === id)?.sku ?? '' : main.sku;
  const owner = (key: string, suggested: string) => {
    const choice = choices[key];
    if (choice !== undefined) return choice && !selected.has(choice) ? '' : choice;
    return selected.has(suggested) ? suggested : '';
  };
  const options: [string, string][] = [['', `Se queda en ${main.sku}`], ...chosen.map(child => [child.id, `Pasa a ${child.sku}`] as [string, string])];
  const movable = plan.codes.filter(code => !code.alias_of);
  const restore = chosen.map(child => ({ id: child.id,
    codes: movable.filter(code => owner(`code:${code.id}`, code.suggested) === child.id).map(code => code.id),
    items: plan.items.filter(item => owner(`item:${item.id}`, item.suggested) === child.id).map(item => item.id),
    images: plan.images.filter(image => owner(`image:${image.id}`, image.suggested) === child.id).map(image => image.id) }));
  const moving = restore.reduce((sum, entry) => ({ codes: sum.codes + entry.codes.length, items: sum.items + entry.items.length, images: sum.images + entry.images.length }), { codes: 0, items: 0, images: 0 });
  const empties = main.created_by_merge && chosen.length === children.length && moving.codes === movable.length && moving.items === plan.items.length && moving.images === plan.images.length;

  function toggle(id: string, checked: boolean) {
    setSelected(current => { const next = new Set(current); if (checked) next.add(id); else next.delete(id); return next; });
    setReviewed(false); setSaveError('');
  }
  function choose(key: string, value: string) { setChoices(current => ({ ...current, [key]: value })); setReviewed(false); }
  function destination(key: string, suggested: string, label: string) {
    const value = owner(key, suggested);
    return <select aria-label={label} className={value ? 'moves' : ''} value={value} disabled={saving} onChange={event => choose(key, event.target.value)}>
      {options.map(([id, text]) => <option key={id || 'main'} value={id}>{text}</option>)}</select>;
  }
  const hint = (suggested: string) => suggested ? <small className="split-hint">Venía de {sku(suggested)}</small> : null;
  async function save() {
    if (saving || !chosen.length || !reviewed) return;
    setSaving(true); onBusyChange(true); setSaveError('');
    try {
      const result = await request<SplitResult>(`/api/management/catalog/${target.id}/split`, { method: 'POST', body: JSON.stringify({ restore }) });
      onSaved(`${plural(result.summary.restored_skus, 'SKU separado', 'SKU separados')} de ${main.sku}: ${result.restored.map(part => part.sku).join(', ')}.${result.summary.target_retired ? ` ${main.sku} quedó inactivo porque no le quedó nada.` : ''}`);
    } catch (reason) { setSaveError((reason as Error).message); }
    finally { setSaving(false); onBusyChange(false); }
  }

  return <section className="split-editor" aria-label={`Separación de ${main.sku}`}>
    {onBack && <div className="grouping-heading"><button type="button" className="button text small" disabled={saving} onClick={onBack}><ArrowLeft size={15}/>Volver a la lista</button></div>}
    <div className="grouping-target"><span className="eyebrow">SKU principal</span><div className="grouping-part-context"><strong>{main.sku}</strong><p>{main.description || 'Sin descripción'}</p>{subgroup(main) && <small>{subgroup(main)}</small>}</div></div>
    {!!plan.warnings.length && <div className="notice split-warnings" role="status">{plan.warnings.map(text => <p key={text}>{text}</p>)}</div>}
    {!children.length ? <div className="empty-state"><Ungroup size={30}/><h3>{main.sku} no tiene SKU agrupados.</h3><p>Solo se separan los SKU que una agrupación retiró.</p></div> : <>
      <h3>Elige los SKU que vuelven a estar separados</h3>
      <div className="grouping-source-list">{children.map(child => <article key={child.id} className={`grouping-source ${selected.has(child.id) ? 'selected' : ''}`}>
        <label className="grouping-select"><input type="checkbox" aria-label={`Separar ${child.sku}`} checked={selected.has(child.id)} disabled={saving} onChange={event => toggle(child.id, event.target.checked)}/><span>Separar este SKU</span></label>
        <div className="grouping-part-context"><strong>{child.sku}</strong><p>{child.description || 'Sin descripción'}</p>{subgroup(child) && <small>{subgroup(child)}</small>}</div>
        <Evidence child={child}/>
      </article>)}</div>
      {!!chosen.length && <>
        <div className="split-section"><h3>Alternos</h3>
          {plan.codes.length ? <div className="split-rows">{plan.codes.map(code => <div className="split-row" key={code.id}>
            <div className="split-row-main"><strong>{code.code}</strong><small>{referenceLabel(code)}</small>{!code.alias_of && hint(code.suggested)}</div>
            {code.alias_of ? <span className="split-locked">{selected.has(code.alias_of) ? `Es el código de ${sku(code.alias_of)}: ${code.alias_returns ? 'vuelve con él' : 'se retira, el SKU ya lo identifica'}.` : `Es el código de ${sku(code.alias_of)}, que sigue agrupado: se queda.`}</span>
              : destination(`code:${code.id}`, code.suggested, `Destino del alterno ${code.code}`)}
          </div>)}</div> : <p>{main.sku} no tiene alternos.</p>}
        </div>
        <div className="split-section"><h3>Existencias de proveedores</h3>
          {plan.items.length ? <div className="split-rows">{plan.items.map(item => <div className="split-row" key={item.id}>
            <div className="split-row-main"><strong>{item.supplier_name} · {item.codigo}{item.brand ? ` · ${item.brand}` : ''}</strong><small>{item.description || 'Sin descripción'} · {item.available_quantity.toLocaleString('es-PA')} disponibles</small>{hint(item.suggested)}</div>
            {/* A variant hangs from a Reference and goes with it (plan S8.9): its destination is read, never chosen. */}
            <span className="split-locked" role="status" aria-label={`Destino de ${item.supplier_name} ${item.codigo}`}>{owner(`item:${item.id}`, item.suggested) ? `Pasa a ${sku(owner(`item:${item.id}`, item.suggested))}` : `Se queda en ${main.sku}`}</span>
          </div>)}</div> : <p>{main.sku} no tiene existencias de proveedores.</p>}
          {!!plan.items.length && <p className="form-footnote">Cada existencia sigue a la referencia de la que cuelga: va con su código cuando ese código pasa a otro SKU.</p>}
        </div>
        {!!plan.images.length && <div className="split-section"><h3>Imágenes</h3><div className="split-images">{plan.images.map((image, index) => <figure className="split-image" key={image.id}>
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src={image.thumbnail_url} alt={image.alt_text || `Imagen ${index + 1} de ${main.sku}`}/>
          {destination(`image:${image.id}`, image.suggested, `Destino de la imagen ${index + 1}`)}
        </figure>)}</div></div>}
        <div className="grouping-confirmation">
          <p>{chosen.length === 1 ? `${chosen[0].sku} vuelve a estar activo` : `${chosen.map(child => child.sku).join(', ')} vuelven a estar activos`} con {plural(moving.codes, 'alterno', 'alternos')}, {plural(moving.items, 'existencia', 'existencias')} y {plural(moving.images, 'imagen', 'imágenes')}; lo demás se queda en {main.sku}.{empties ? ` ${main.sku} quedará inactivo porque no le queda nada.` : ''}</p>
          <label className="admin-checkbox"><input type="checkbox" checked={reviewed} disabled={saving} onChange={event => setReviewed(event.target.checked)}/><span>Revisé a qué SKU va cada alterno, existencia e imagen.</span></label>
        </div>
      </>}
      {saveError && <p className="notice error" role="alert">{saveError}</p>}
      <button type="button" className="button primary full" disabled={saving || !chosen.length || !reviewed} onClick={() => void save()}>{saving ? <><LoaderCircle size={16} className="spin"/>Separando…</> : chosen.length ? `Separar ${chosen.length} SKU` : 'Elige al menos un SKU'}</button>
    </>}
  </section>;
}
