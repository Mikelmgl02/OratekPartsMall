'use client';

import { useId, useState } from 'react';
import { ChevronDown, Search, SlidersHorizontal, X } from 'lucide-react';
import type { CatalogFacet, CatalogFacets, CatalogFilters } from '@/lib/types';
import { emptyFilters, filterCount, stockFilterLabels } from '@/lib/catalog-filters';

type Props = { facets: CatalogFacets; filters: CatalogFilters; loading: boolean; onChange: (filters: CatalogFilters) => void };

function FacetSection({ title, options, selected, loading, onToggle }: { title: string; options: CatalogFacet[]; selected: string[]; loading: boolean; onToggle: (value: string) => void }) {
  const [search, setSearch] = useState('');
  const [expanded, setExpanded] = useState(false);
  const shown = [...options];
  for (const value of selected) if (!shown.some(option => option.value === value)) shown.push({ value, label: value || (title === 'Grupo' ? 'Sin clasificar' : 'Sin subgrupo'), count: 0 });
  const filtered = shown.filter(option => option.label.toLocaleLowerCase('es').includes(search.toLocaleLowerCase('es')));
  const visible = search || expanded ? filtered : filtered.filter((option, index) => index < 6 || selected.includes(option.value));
  return <details className="catalog-facet" open>
    <summary>{title}{selected.length > 0 && <span className="facet-selection-count">{selected.length}</span>}<ChevronDown size={16}/></summary>
    {shown.length > 6 && <label className="facet-search"><Search size={14}/><input aria-label={`Buscar en ${title.toLowerCase()}`} value={search} onChange={event => setSearch(event.target.value)} placeholder={`Buscar ${title.toLowerCase()}…`} autoComplete="off"/></label>}
    <div className="facet-options" role="group" aria-label={title}>
      {visible.map(option => <label key={option.value} className="facet-option"><input type="checkbox" aria-label={option.label} checked={selected.includes(option.value)} onChange={() => onToggle(option.value)} disabled={!loading && !option.count && !selected.includes(option.value)}/><span>{option.label}</span><small>{option.count.toLocaleString('es')}</small></label>)}
      {!visible.length && <p className="facet-empty">{loading ? 'Cargando opciones…' : search ? 'Sin coincidencias.' : 'Sin opciones para esta búsqueda.'}</p>}
    </div>
    {!search && filtered.length > 6 && <button className="facet-show-more" onClick={() => setExpanded(!expanded)}>{expanded ? 'Ver menos' : `Ver todos (${filtered.length})`}</button>}
  </details>;
}

export default function CatalogFilterPanel({ facets, filters, loading, onChange }: Props) {
  const radioName = useId();
  function toggle(field: 'category' | 'subcategory' | 'supplier', value: string) {
    const selected = filters[field];
    onChange({ ...filters, [field]: selected.includes(value) ? selected.filter(item => item !== value) : [...selected, value], ...(field === 'category' ? { subcategory: [] } : {}) });
  }
  return <div className="catalog-filter-panel">
    <div className="filter-panel-heading"><h3><SlidersHorizontal size={17}/>Filtros</h3>{filterCount(filters) > 0 && <button onClick={() => onChange(emptyFilters)}>Limpiar todo</button>}</div>
    <FacetSection title="Grupo" options={facets.category} selected={filters.category} loading={loading} onToggle={value => toggle('category', value)}/>
    <FacetSection title="Subgrupo" options={facets.subcategory} selected={filters.subcategory} loading={loading} onToggle={value => toggle('subcategory', value)}/>
    <details className="catalog-facet" open><summary>Existencias<ChevronDown size={16}/></summary><div className="facet-options" role="group" aria-label="Existencias">
      <label className="facet-option"><input type="radio" name={radioName} checked={!filters.availability} onChange={() => onChange({ ...filters, availability: '' })}/><span>Todos los estados</span></label>
      {facets.availability.map(option => <label key={option.value} className="facet-option"><input type="radio" aria-label={option.label} name={radioName} checked={filters.availability === option.value} onChange={() => onChange({ ...filters, availability: option.value })} disabled={!loading && !option.count && filters.availability !== option.value}/><span>{option.label}</span><small>{option.count.toLocaleString('es')}</small></label>)}
    </div></details>
    <FacetSection title="Proveedor" options={facets.supplier} selected={filters.supplier} loading={loading} onToggle={value => toggle('supplier', value)}/>
    <p className="filter-panel-note">Cada resultado reúne los códigos equivalentes de un mismo SKU. Las existencias corresponden al SKU entre todos sus proveedores.</p>
  </div>;
}

export function ActiveCatalogFilters({ facets, filters, onChange }: Omit<Props, 'loading'>) {
  if (!filterCount(filters)) return null;
  return <div className="active-catalog-filters" aria-label="Filtros aplicados">
    {(['category', 'subcategory', 'supplier'] as const).flatMap(field => filters[field].map(value => {
      const label = facets[field].find(option => option.value === value)?.label || value || (field === 'category' ? 'Sin clasificar' : 'Sin subgrupo');
      return <button key={`${field}:${value}`} onClick={() => onChange({ ...filters, [field]: filters[field].filter(item => item !== value), ...(field === 'category' ? { subcategory: [] } : {}) })} aria-label={`Quitar filtro ${label}`}><span>{label}</span><X size={12}/></button>;
    }))}
    {filters.availability && <button onClick={() => onChange({ ...filters, availability: '' })} aria-label={`Quitar filtro ${stockFilterLabels[filters.availability]}`}><span>{stockFilterLabels[filters.availability]}</span><X size={12}/></button>}
    <button className="clear-catalog-filters" onClick={() => onChange(emptyFilters)}>Limpiar todo</button>
  </div>;
}
