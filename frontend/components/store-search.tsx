'use client';
import { KeyboardEvent, RefObject, useEffect, useRef, useState } from 'react';
import { LoaderCircle, Search, X } from 'lucide-react';
import { CatalogPhoto } from './part-image';
import { catalogQuery, matchesFilters } from '@/lib/catalog-filters';
import { matchesSearch } from '@/lib/catalog-search';
import { previewParts } from '@/lib/preview';
import { CatalogFilters, Part, request } from '@/lib/types';

const MIN_CHARS = 2;
const SUGGESTIONS = 6;
const optionId = (index: number) => `catalog-suggestion-${index}`;

type Props = {
  value: string; onChange: (value: string) => void;
  // Enter, the search button or the last row: the results below change only then.
  onSearch: (term: string) => void; onOpen: (part: Part) => void;
  filters: CatalogFilters; departments: { value: string; label: string }[]; onDepartment: (value: string) => void;
  preview: boolean; accountId: string; inputRef: RefObject<HTMLInputElement | null>;
  // scope: which SKUs the chosen Mis repuestos filters select now (marketplace mineKey); cached suggestions are kept per scope.
  scope?: string;
};

// Amazon-style search box: while typing, the first results the search would list drop down under it (with the department and the
// filters applied); picking one opens the part, Enter lists every result.
export default function StoreSearch({ value, onChange, onSearch, onOpen, filters, departments, onDepartment, preview, accountId, inputRef, scope = '' }: Props) {
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(-1);
  const [shown, setShown] = useState<{ key: string; parts: Part[] } | null>(null);
  const [loading, setLoading] = useState(false);
  const cache = useRef(new Map<string, Part[]>());
  const term = value.trim();
  const query = catalogQuery(term, 1, filters, preview ? '' : accountId);
  const key = `${preview ? 'preview' : accountId}:${query}:${scope}`;
  const wanted = open && term.length >= MIN_CHARS && (preview || !!accountId);
  useEffect(() => {
    if (!wanted) return;
    const show = (parts: Part[]) => { setShown({ key, parts }); setActive(-1); };
    if (preview) { show(previewParts.filter(part => matchesSearch(part, term) && matchesFilters(part, filters)).slice(0, SUGGESTIONS)); return; }
    const cached = cache.current.get(key);
    if (cached) { show(cached); return; }
    const controller = new AbortController();
    const timer = setTimeout(() => {
      setLoading(true);
      request<unknown>(`/api/market/catalog/suggest?${query}`, { signal: controller.signal })
        .then(data => { const parts = Array.isArray(data) ? data : []; cache.current.set(key, parts); show(parts); })
        .catch(() => { /* Only a preview: Enter still lists the results. */ })
        .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    }, 150);
    return () => { clearTimeout(timer); controller.abort(); setLoading(false); };
    // key covers the term, the query, the filters and the account.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wanted, key]);
  // The previous suggestions stay up while the next ones load, so the list does not flicker between keystrokes.
  const parts = wanted ? shown?.parts ?? [] : [];
  const settled = shown?.key === key;
  const listed = wanted && (parts.length > 0 || settled);
  const total = parts.length + 1;

  function search() { setOpen(false); setActive(-1); onSearch(value); }
  // The search box lets go of focus first: closing the part's dialog hands focus back to what opened it, which would open the list again.
  function choose(part: Part) { setOpen(false); setActive(-1); inputRef.current?.blur(); onOpen(part); }
  function keyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      if (!listed) { setOpen(true); return; }
      setActive(current => event.key === 'ArrowDown' ? (current + 1) % total : current <= 0 ? total - 1 : current - 1);
    } else if (event.key === 'Escape' && listed) {
      event.preventDefault(); setOpen(false); setActive(-1);
    } else if (event.key === 'Enter' && listed && active >= 0 && active < parts.length) {
      event.preventDefault(); choose(parts[active]);
    }
  }

  return <form className="store-search" role="search" aria-label="Buscar en el catálogo" onSubmit={event => { event.preventDefault(); search(); }}>
    <label className="sr-only" htmlFor="store-department">Departamento</label>
    <select id="store-department" className="store-search-scope" value={filters.category.length === 1 ? filters.category[0] : ''} onChange={event => onDepartment(event.target.value)}><option value="">Todos</option>{departments.map(department => <option key={department.value} value={department.value}>{department.label}</option>)}</select>
    <label className="sr-only" htmlFor="catalog-search">Buscar repuestos, marcas o códigos</label>
    <input ref={inputRef} id="catalog-search" value={value} autoComplete="off" placeholder="Busca por código, OEM, marca o repuesto…"
      role="combobox" aria-autocomplete="list" aria-expanded={listed} aria-controls="catalog-search-suggestions" aria-activedescendant={listed && active >= 0 ? optionId(active) : undefined}
      onChange={event => { onChange(event.target.value); setOpen(true); setActive(-1); }} onFocus={() => setOpen(true)} onBlur={() => setOpen(false)} onKeyDown={keyDown}/>
    {value && <button type="button" className="icon-button store-search-clear" aria-label="Limpiar búsqueda" onClick={() => { setOpen(false); onSearch(''); inputRef.current?.focus(); }}><X size={16}/></button>}
    <button type="submit" className="store-search-go" aria-label="Buscar"><Search size={20}/></button>
    {listed && <ul id="catalog-search-suggestions" className="store-suggestions" role="listbox" aria-label="Sugerencias de búsqueda" onMouseDown={event => event.preventDefault()}>
      {parts.map((part, index) => <li key={part.id} id={optionId(index)} role="option" aria-selected={active === index} className={active === index ? 'active' : ''} onMouseEnter={() => setActive(index)} onClick={() => choose(part)}>
        <span className="store-suggestion-photo" aria-hidden="true"><CatalogPhoto image={part.images?.[0]} alt={`${part.name} ${part.description} ${part.category}`} thumbnail/></span>
        <span className="store-suggestion-text"><strong>{part.sku}</strong><small>{part.name && part.name !== part.sku ? part.name : part.description || 'DESCRIPCIÓN PENDIENTE'}</small></span>
      </li>)}
      {settled && !parts.length && <li className="store-suggestion-empty" role="presentation">No encontramos repuestos para «{term}».</li>}
      <li id={optionId(parts.length)} role="option" aria-selected={active === parts.length} className={`store-suggestion-all${active === parts.length ? ' active' : ''}`} onMouseEnter={() => setActive(parts.length)} onClick={search}>
        <Search size={15} aria-hidden="true"/><span>Ver todos los resultados para <strong>«{term}»</strong></span>{loading && <LoaderCircle size={14} className="spin" aria-label="Buscando sugerencias"/>}
      </li>
    </ul>}
  </form>;
}
