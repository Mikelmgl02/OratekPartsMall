import type { CatalogFacets, CatalogFilters, MineFilter, Part } from './types';
import { previewOffers } from './preview';

export const emptyFilters: CatalogFilters = { category: [], subcategory: [], availability: '', supplier: [], mine: [] };
export const mineFilters: MineFilter[] = ['cesta', 'ordenes', 'favoritos'];
export const mineFilterLabels: Record<MineFilter, string> = { cesta: 'En cesta', ordenes: 'En órdenes', favoritos: 'Favoritos' };
export const mineFilterHelp: Record<MineFilter, string> = {
  cesta: 'Ver solo los repuestos que tienes en tu cesta', ordenes: 'Ver solo los repuestos de tus órdenes enviadas que aún no recibes', favoritos: 'Ver solo tus repuestos favoritos',
};
export const emptyFacets: CatalogFacets = { category: [], subcategory: [], availability: [], supplier: [] };
export const stockFilterLabels: Record<string, string> = {
  in_stock: 'Con existencias', high: 'Altas existencias', medium: 'Existencias medias', low: 'Bajas existencias',
  sold_out: 'Agotado', unknown: 'Sin existencias reportadas',
};
export function filterCount(filters: CatalogFilters) {
  return filters.category.length + filters.subcategory.length + filters.supplier.length + Number(!!filters.availability) + filters.mine.length + (filters.technical ? 1 + filters.technical.rules.length : 0);
}
// account: the active account, so a supplier-only account gets only its own SKUs.
export function catalogQuery(search: string, page: number, filters: CatalogFilters, account = '') {
  const query = new URLSearchParams({ search, page: String(page), include_facets: '1' });
  for (const key of ['category', 'subcategory', 'supplier', 'mine'] as const) for (const value of filters[key]) query.append(key, value);
  if (filters.availability) query.set('availability', filters.availability);
  if (filters.technical) query.set('technical', JSON.stringify({ part_type: filters.technical.part_type,
    rules: filters.technical.rules.map(({ key, operator, value, value_to }) => ({ key, operator, value, ...(operator === 'between' ? { value_to } : {}) })) }));
  if (account) query.set('account', account);
  return query.toString();
}
export function matchesFilters(part: Part, filters: CatalogFilters) {
  const status = part.availability?.status || 'unknown';
  return (!filters.category.length || filters.category.includes(part.category || ''))
    && (!filters.subcategory.length || filters.subcategory.includes(part.subcategory || ''))
    && (!filters.availability || (filters.availability === 'in_stock' ? ['low', 'medium', 'high'].includes(status) : filters.availability === status));
}
export function previewFacets(parts: Part[], filters: CatalogFilters): CatalogFacets {
  const result: CatalogFacets = { ...emptyFacets };
  for (const field of ['category', 'subcategory'] as const) {
    const scope = parts.filter(part => matchesFilters(part, { ...filters, [field]: [], ...(field === 'category' ? { subcategory: [] } : {}) }));
    const counts = new Map<string, number>();
    for (const part of scope) { const value = part[field] || ''; counts.set(value, (counts.get(value) || 0) + 1); }
    result[field] = [...counts].sort(([a], [b]) => a.localeCompare(b)).map(([value, count]) => ({ value, label: value || (field === 'category' ? 'Sin clasificar' : 'Sin subgrupo'), count }));
  }
  const stockScope = parts.filter(part => matchesFilters(part, { ...filters, availability: '' }));
  result.availability = Object.entries(stockFilterLabels).map(([value, label]) => ({ value, label, count: stockScope.filter(part => matchesFilters(part, { ...emptyFilters, availability: value })).length }));
  result.supplier = previewOffers.map(offer => ({ value: offer.supplier_id, label: offer.supplier_name, count: parts.filter(part => matchesFilters(part, filters)).length }));
  return result;
}
