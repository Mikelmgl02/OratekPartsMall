import { expect, test, type Page } from '@playwright/test';

// A supplier-only account browses only its own SKUs (the API enforces it, mall/catalog_scope.py); the storefront sends the active
// account and drops what only makes sense across suppliers.
const supplierAccount = { id: '22222222-2222-4222-8222-222222222222', name: 'PROVEEDOR', roles: ['supplier_retail'], capabilities: ['supplier'] };
const clientAccount = { id: '33333333-3333-4333-8333-333333333333', name: 'CLIENTE', roles: ['client_business'], capabilities: ['client'] };
const part = (id: string, sku: string, status: string, suppliers: number) => ({ id, sku, name: '', description: 'DISCO DE FRENO', category: 'FRENOS',
  subcategory: 'DISCOS', codes: [], equivalents: [], availability: { status, supplier_count: suppliers, updated_at: '2026-10-02T16:30:00Z' } });
const facets = (supplier: { value: string; label: string; count: number }[]) => ({ category: [{ value: 'FRENOS', label: 'FRENOS', count: 2 }],
  subcategory: [], availability: [], supplier });

async function open(page: Page, account: typeof supplierAccount, results: ReturnType<typeof part>[], supplier: { value: string; label: string; count: number }[] = []) {
  const urls: string[] = [];
  await page.route('**/api/session', route => route.fulfill({ json: { authenticated: true, user: { id: 7, username: 'MIEMBRO', is_superuser: false },
    accounts: { count: 1, next: null, previous: null, results: [account] } } }));
  await page.route(/\/api\/market\/catalog(?:\/suggest)?(?:\?.*)?$/, route => {
    urls.push(route.request().url());
    const suggest = route.request().url().includes('/catalog/suggest');
    return route.fulfill({ json: suggest ? results : { count: results.length, next: null, previous: null, results, facets: facets(supplier) } });
  });
  await page.route(/\/api\/market\/catalog\/[^/]+\/technical(?:\?.*)?$/, route => { urls.push(route.request().url()); return route.fulfill({ json: { template: null, values: {}, applications: [] } }); });
  await page.goto('/');
  return urls;
}

test('a supplier-only account sees its own articles, with its own stock and no supplier filter', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  const urls = await open(page, supplierAccount, [part('part-1', 'MINE-1', 'low', 1), part('part-2', 'MINE-2', 'sold_out', 0)]);
  await expect(page.getByRole('article', { name: 'Repuesto MINE-1', exact: true })).toContainText('CON STOCK EN TU INVENTARIO');
  await expect(page.getByRole('article', { name: 'Repuesto MINE-2', exact: true })).toContainText('SIN STOCK EN TU INVENTARIO');
  await expect(page.getByText('Estás viendo tus artículos.')).toBeVisible();
  await expect(page.locator('.results-heading')).toContainText('2 artículos tuyos en el catálogo');
  await expect(page.locator('.results-heading')).toContainText('Tus artículos');
  await expect(page.getByRole('heading', { name: /El repuesto indicado/ })).toHaveCount(0);
  await expect(page.getByRole('group', { name: 'Proveedor', exact: true })).toHaveCount(0);
  await expect(page.getByRole('group', { name: 'Grupo', exact: true })).toBeVisible();
  expect(urls.some(url => new URL(url).searchParams.get('account') === supplierAccount.id)).toBe(true);
  await page.locator('#catalog-search').fill('MINE');
  await expect.poll(() => urls.some(url => url.includes('/catalog/suggest') && new URL(url).searchParams.get('account') === supplierAccount.id)).toBe(true);
  await page.keyboard.press('Escape');
  await page.getByRole('article', { name: 'Repuesto MINE-1', exact: true }).getByRole('button', { name: 'Ver MINE-1' }).click();
  await expect(page.getByRole('dialog')).toContainText('Tu cuenta activa necesita un rol de cliente');
  // The sheet follows the user's scope, like favorites, so a favorite opened under a narrower account still shows it.
  await expect(page.getByRole('dialog')).toContainText('No hay especificaciones publicadas para este SKU.');
});

test('a supplier-only account without articles is pointed to its inventory', async ({ page }) => {
  await open(page, supplierAccount, []);
  await expect(page.getByRole('heading', { name: 'Aún no tienes artículos en el catálogo.' })).toBeVisible();
  await expect(page.getByText('Tus artículos aparecen aquí cuando el catálogo reconoce los códigos de tu inventario.')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Ir a tu inventario' })).toBeVisible();
});

test('a client account keeps the whole storefront and the supplier filter', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  const urls = await open(page, clientAccount, [part('part-1', 'SHARED-1', 'high', 2)], [{ value: supplierAccount.id, label: 'PROVEEDOR', count: 1 }]);
  await expect(page.getByRole('article', { name: 'Repuesto SHARED-1', exact: true })).toContainText('2 PROVEEDORES CON STOCK');
  await expect(page.getByRole('heading', { name: /El repuesto indicado/ })).toBeVisible();
  await expect(page.getByRole('group', { name: 'Proveedor', exact: true })).toBeVisible();
  await expect(page.getByText('Estás viendo tus artículos.')).toHaveCount(0);
  await expect(page.locator('.results-heading')).toContainText('1 repuesto del catálogo');
  expect(urls.some(url => new URL(url).searchParams.get('account') === clientAccount.id)).toBe(true);
});
