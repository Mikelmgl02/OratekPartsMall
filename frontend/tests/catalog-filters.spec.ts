import { expect, Page, test } from '@playwright/test';
import { matchesFilters, stockFilterLabels } from '../lib/catalog-filters';
import type { CatalogFacets, CatalogFilters, Part } from '../lib/types';

const supplierA = '11111111-1111-4111-8111-111111111111';
const supplierB = '22222222-2222-4222-8222-222222222222';
const parts = Array.from({ length: 56 }, (_, index) => ({
  id: `part-${index}`, sku: `TEST-${String(index).padStart(3, '0')}`, name: '', description: 'REPUESTO TEST',
  category: index < 52 ? 'FRENOS' : 'FILTROS', subcategory: index < 50 ? 'DISCOS' : index < 52 ? 'PASTILLAS' : 'ACEITE',
  codes: [], availability: { status: index === 0 ? 'low' : index < 54 ? 'high' : 'sold_out', supplier_count: index < 54 ? 1 : 0, updated_at: '2026-10-02T20:00:00Z' },
  supplier: index % 2 ? supplierB : supplierA,
} satisfies Part & { supplier: string }));

async function mockCatalog(page: Page) {
  const queries: URLSearchParams[] = [];
  const events: { kind: string; term?: string }[] = [];
  await page.route('**/api/session', route => route.fulfill({ json: { authenticated: true, user: { id: 101, username: 'CLIENTE', is_superuser: false },
    accounts: { count: 1, next: null, previous: null, results: [{ id: 'client-a', name: 'CLIENTE', roles: ['client_business'], capabilities: ['client'] }] } } }));
  await page.route('**/analytics/events', route => { events.push(route.request().postDataJSON()); return route.fulfill({ status: 202, json: { recorded: true } }); });
  await page.route(/\/api\/market\/catalog(?:\?.*)?$/, route => {
    const query = new URL(route.request().url()).searchParams;
    queries.push(query);
    const filters: CatalogFilters = { category: query.getAll('category'), subcategory: query.getAll('subcategory'), supplier: query.getAll('supplier'), availability: query.get('availability') || '' };
    const base = parts.filter(part => `${part.sku} ${part.description}`.includes((query.get('search') || '').toUpperCase()));
    const scope = (values: CatalogFilters) => base.filter(part => matchesFilters(part, values) && (!values.supplier.length || values.supplier.includes(part.supplier)));
    const facets: CatalogFacets = { category: [], subcategory: [], availability: [], supplier: [] };
    for (const field of ['category', 'subcategory'] as const) {
      facets[field] = [...new Set(parts.map(part => part[field]))].map(value => ({ value, label: value, count: scope({ ...filters, [field]: [], ...(field === 'category' ? { subcategory: [] } : {}) }).filter(part => part[field] === value).length }));
    }
    facets.availability = Object.entries(stockFilterLabels).map(([value, label]) => ({ value, label, count: scope({ ...filters, availability: value }).length }));
    facets.supplier = [supplierA, supplierB].map((value, index) => ({ value, label: index ? 'PROVEEDOR DOS' : 'PROVEEDOR UNO', count: scope({ ...filters, supplier: [] }).filter(part => part.supplier === value).length }));
    const results = scope(filters);
    const pageNumber = Number(query.get('page') || 1);
    return route.fulfill({ json: { count: results.length, next: results.length > pageNumber * 50 ? '/next' : null, previous: pageNumber > 1 ? '/previous' : null,
      results: results.slice((pageNumber - 1) * 50, pageNumber * 50), facets } });
  });
  return { queries, events };
}

test('desktop: combine groups, subgroups, stock and suppliers with removable filters', async ({ page }) => {
  const { queries } = await mockCatalog(page);
  await page.goto('/');
  const sidebar = page.getByRole('complementary', { name: 'Filtros del catálogo' });
  await expect(sidebar).toBeVisible();
  await expect(sidebar.getByRole('checkbox', { name: 'FRENOS', exact: true })).toBeVisible();
  await sidebar.getByRole('checkbox', { name: 'FRENOS', exact: true }).check();
  await sidebar.getByRole('checkbox', { name: 'FILTROS', exact: true }).check();
  await expect.poll(() => queries.at(-1)?.getAll('category')).toEqual(['FRENOS', 'FILTROS']);
  await sidebar.getByRole('checkbox', { name: 'PASTILLAS', exact: true }).check();
  await expect(page.locator('.results-heading>p')).toContainText('2 repuestos');
  await sidebar.getByRole('radio', { name: 'Con existencias', exact: true }).check();
  await sidebar.getByRole('checkbox', { name: 'PROVEEDOR UNO', exact: true }).check();
  await expect(page.locator('.part-card')).toHaveCount(1);
  expect(queries.at(-1)?.get('supplier')).toBe(supplierA);
  expect(queries.at(-1)?.get('availability')).toBe('in_stock');
  await page.getByRole('button', { name: 'Quitar filtro PASTILLAS', exact: true }).click();
  await expect(page.locator('.results-heading>p')).toContainText('27 repuestos');
  await sidebar.getByRole('button', { name: 'Limpiar todo', exact: true }).click();
  await expect(page.locator('.results-heading>p')).toContainText('56 repuestos');
  await expect(page.getByLabel('Filtros aplicados')).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)).toBe(false);
});

test('filtering resets pagination, retains the search, and does not inflate search statistics', async ({ page }) => {
  const { queries, events } = await mockCatalog(page);
  await page.goto('/');
  const search = page.getByLabel('Buscar repuestos, marcas o códigos', { exact: true });
  await search.fill('TEST');
  await expect.poll(() => events.filter(event => event.kind === 'search').length).toBe(1);
  await page.getByRole('button', { name: 'Siguiente', exact: true }).click();
  await expect(page.locator('.pagination')).toContainText('Página 2');
  await page.getByRole('complementary').getByRole('checkbox', { name: 'FILTROS', exact: true }).check();
  await expect(page.locator('.part-card')).toHaveCount(4);
  expect(queries.at(-1)?.get('page')).toBe('1');
  expect(queries.at(-1)?.get('search')).toBe('TEST');
  await expect(search).toHaveValue('TEST');
  expect(events.filter(event => event.kind === 'search')).toHaveLength(1);
  await page.getByRole('button', { name: 'Limpiar búsqueda', exact: true }).click();
  await expect.poll(() => queries.at(-1)?.get('search')).toBe('');
  expect(queries.at(-1)?.getAll('category')).toEqual(['FILTROS']);
});

for (const width of [390, 320]) {
  test(`mobile ${width}: drawer filters persist when closed and reopened; Escape dismisses it`, async ({ page }) => {
    await page.setViewportSize({ width, height: 844 });
    await mockCatalog(page);
    await page.goto('/');
    const trigger = page.getByRole('button', { name: 'Abrir filtros del catálogo', exact: true });
    await trigger.click();
    const drawer = page.getByRole('dialog', { name: 'Filtrar repuestos', exact: true });
    await expect(drawer).toBeVisible();
    await drawer.getByRole('checkbox', { name: 'FILTROS', exact: true }).check();
    await expect(drawer.getByRole('button', { name: 'Ver 4 repuestos', exact: true })).toBeVisible();
    await drawer.getByRole('button', { name: 'Ver 4 repuestos', exact: true }).click();
    await expect(drawer).toHaveCount(0);
    await expect(page.locator('.part-card')).toHaveCount(4);
    await trigger.click();
    await expect(drawer.getByRole('checkbox', { name: 'FILTROS', exact: true })).toBeChecked();
    await drawer.press('Escape');
    await expect(drawer).toHaveCount(0);
    expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)).toBe(false);
  });
}
