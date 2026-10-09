'use client';

import { useEffect, useState } from 'react';
import { LoaderCircle, Plus, Search, X } from 'lucide-react';
import { ManagedPart, Page, request } from '@/lib/types';
import Modal from './modal';
import { UppercaseInput } from './uppercase-field';
import { identityText, MERGE_LIMIT, subgroupLabel } from './admin-catalog-grids';

type MergeResult = { merged_skus: string[]; already_merged_skus: string[] };
const words = (value: string) => value.trim().toUpperCase().replace(/\s+/g, ' ');
const plural = (count: number, one: string, many: string) => `${count} ${count === 1 ? one : many}`;

// Agrupar con…, from the grid's right click: the SKUs picked in the grid (or one, and those added here) become one. The SKU chosen as
// principal keeps its code; the others retire into it with their alternos, stock and photos (catalog/grouping/merge, which records
// what each brought, so Separar SKU can undo it exactly).
export default function CatalogMergeWith({ parts, onClose, onSaved }: { parts: ManagedPart[]; onClose: () => void; onSaved: (text: string) => void }) {
  const [members, setMembers] = useState<ManagedPart[]>(parts);
  // An OEM SKU stays as principal by default (as the matching service keeps it); otherwise the one right-clicked.
  const [mainId, setMainId] = useState(() => (parts.find(part => part.is_OEM) ?? parts[0]).id);
  const [search, setSearch] = useState(''); const [query, setQuery] = useState('');
  const [results, setResults] = useState<ManagedPart[] | null>(null); const [searchError, setSearchError] = useState('');
  const [reviewed, setReviewed] = useState(false); const [busy, setBusy] = useState(false); const [error, setError] = useState('');
  useEffect(() => { const timer = setTimeout(() => setQuery(search.trim()), 300); return () => clearTimeout(timer); }, [search]);
  useEffect(() => {
    if (query.length < 2) { setResults(null); return; }
    let live = true; setSearchError('');
    request<Page<ManagedPart>>(`/api/management/catalog?search=${encodeURIComponent(query)}&page=1`).then(page => { if (live) setResults(page.results); })
      .catch(reason => { if (live) { setResults([]); setSearchError((reason as Error).message); } });
    return () => { live = false; };
  }, [query]);
  const main = members.find(part => part.id === mainId) ?? members[0];
  const others = members.filter(part => part.id !== main.id);
  const found = (results ?? []).filter(part => !members.some(member => member.id === part.id)).slice(0, 8);
  const warnings = [
    new Set(members.map(part => words(part.description))).size > 1 && 'Las descripciones no son iguales: confirma que son el mismo repuesto y no variantes (lado, posición, medida o año).',
    new Set(members.map(part => subgroupLabel(part)).filter(Boolean)).size > 1 && 'Los grupos / subgrupos no coinciden.',
    !main.is_OEM && others.some(part => part.is_OEM) && `${others.filter(part => part.is_OEM).map(part => part.sku).join(', ')} es OEM: normalmente el SKU OEM queda como principal.`,
    members.length > MERGE_LIMIT && `Una agrupación toma hasta ${MERGE_LIMIT} SKU.`,
  ].filter(Boolean) as string[];

  function add(part: ManagedPart) { setMembers(current => [...current, part]); setSearch(''); setQuery(''); setResults(null); setReviewed(false); setError(''); }
  function remove(id: string) {
    const rest = members.filter(part => part.id !== id);
    setMembers(rest);
    if (id === main.id && rest.length) setMainId((rest.find(part => part.is_OEM) ?? rest[0]).id);
    setReviewed(false); setError('');
  }
  function choose(id: string) { setMainId(id); setReviewed(false); setError(''); }
  async function merge() {
    if (busy || !others.length || !reviewed || members.length > MERGE_LIMIT) return;
    setBusy(true); setError('');
    try {
      const result = await request<MergeResult>('/api/management/catalog/grouping/merge', { method: 'POST', body: JSON.stringify({ target_sku: main.sku, source_skus: others.map(part => part.sku) }) });
      const merged = [...result.merged_skus, ...result.already_merged_skus];
      onSaved(`${plural(merged.length, 'SKU agrupado', 'SKU agrupados')} en ${main.sku}: ${merged.join(', ')}. Si fue un error, Separar SKU lo deshace.`);
    } catch (reason) { setError((reason as Error).message); }
    finally { setBusy(false); }
  }

  return <Modal title="Agrupar SKU" wide onClose={() => { if (!busy) onClose(); }}>
    <p className="modal-description">Une SKU que son el mismo repuesto. El SKU principal conserva su código y recibe los alternos, existencias e imágenes de los demás, que dejan de aparecer separados. Separar SKU lo deshace.</p>
    <section className="merge-editor" aria-label="SKU por agrupar">
      <h3>Elige el SKU principal</h3>
      <div className="grouping-source-list">{members.map(part => <article key={part.id} className={`grouping-source merge-member ${part.id === main.id ? 'selected' : ''}`}>
        <div className="merge-member-head">
          <label className="grouping-select"><input type="radio" name="merge-main" aria-label={`${part.sku} como SKU principal`} checked={part.id === main.id} disabled={busy} onChange={() => choose(part.id)}/>
            <span>{part.id === main.id ? 'SKU principal: conserva su código' : 'Se agrupa en el principal'}</span></label>
          {members.length > 1 && <button type="button" className="icon-button" aria-label={`Quitar ${part.sku}`} disabled={busy} onClick={() => remove(part.id)}><X size={15}/></button>}
        </div>
        <div className="grouping-part-context"><strong>{part.sku}</strong><p>{part.description || 'Sin descripción'}</p>
          <small>{[subgroupLabel(part), identityText(part)].filter(Boolean).join(' · ')}</small>
          <small>{plural(part.stock_record_count, 'existencia', 'existencias')} · {plural(part.codes.length, 'alterno', 'alternos')}{part.merged_count ? ` · ${plural(part.merged_count, 'SKU agrupado', 'SKU agrupados')}` : ''}</small></div>
      </article>)}</div>
      <div className="merge-add">
        <div className="catalog-search admin-search"><Search size={18}/><UppercaseInput aria-label="Buscar SKU para agrupar" value={search} placeholder="Agrega otro SKU: código, nombre o alterno…" disabled={busy} onChange={event => setSearch(event.target.value)}/></div>
        {searchError && <p className="notice error" role="alert">{searchError}</p>}
        {results !== null && <div className="merge-results">{found.length ? found.map(part => <div className="merge-result" key={part.id}>
          <div className="split-row-main"><strong>{part.sku}</strong><small>{part.description || 'Sin descripción'}{part.active ? '' : ' · INACTIVO'}</small></div>
          <button type="button" className="button soft small" aria-label={`Agregar ${part.sku}`} disabled={busy || !part.active} title={part.active ? undefined : 'Un SKU inactivo no se agrupa: actívalo primero.'} onClick={() => add(part)}><Plus size={14}/>Agregar</button>
        </div>) : <p className="merge-empty">No encontramos otros SKU con «{query}».</p>}</div>}
      </div>
      {!!warnings.length && <div className="notice split-warnings" role="status">{warnings.map(text => <p key={text}>{text}</p>)}</div>}
      {others.length > 0 && <div className="grouping-confirmation">
        <p>{others.length === 1 ? `${others[0].sku} deja` : `${others.map(part => part.sku).join(', ')} dejan`} de aparecer separado{others.length === 1 ? '' : 's'}: sus alternos, existencias e imágenes pasan a {main.sku}.</p>
        <label className="admin-checkbox"><input type="checkbox" checked={reviewed} disabled={busy} onChange={event => setReviewed(event.target.checked)}/><span>Revisé que son el mismo repuesto.</span></label>
      </div>}
      {error && <p className="notice error" role="alert">{error}</p>}
      <button type="button" className="button primary full" disabled={busy || !others.length || !reviewed || members.length > MERGE_LIMIT} onClick={() => void merge()}>
        {busy ? <><LoaderCircle size={16} className="spin"/>Agrupando…</> : others.length ? `Agrupar ${others.length} SKU en ${main.sku}` : 'Agrega al menos otro SKU'}</button>
    </section>
  </Modal>;
}
