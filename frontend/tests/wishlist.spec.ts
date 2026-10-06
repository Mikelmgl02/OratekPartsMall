import { expect, Page, test } from '@playwright/test';
import type { Part } from '../lib/types';

const parts: Part[] = [
  { id:'11111111-1111-4111-8111-111111111111', sku:'58411-1R000', name:'', description:'TAMBOR HYU ACCENT 11-16', category:'FRENOS', subcategory:'TAMBORES', codes:[{brand:'', code:'58411-1R000-G'}], availability:{status:'low', supplier_count:1, updated_at:null} },
  { id:'22222222-2222-4222-8222-222222222222', sku:'FILTER-002', name:'', description:'FILTRO DE ACEITE', category:'FILTROS', subcategory:'ACEITE', codes:[], availability:{status:'medium', supplier_count:2, updated_at:null} },
  { id:'33333333-3333-4333-8333-333333333333', sku:'BELT-003', name:'', description:'CORREA', codes:[], availability:{status:'sold_out', supplier_count:0, updated_at:null} },
];
const account = {id:'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', name:'TALLER UNO', roles:['client_business'], capabilities:['client']};

async function fixture(page: Page, { initial = [] as Part[], authenticated = true, catalogParts = parts, inactiveIds = [] as string[] } = {}) {
  let userId: number | undefined = authenticated ? 101 : undefined;
  const saved = new Map<number, Set<string>>([[101, new Set(initial.map(part => part.id))], [102, new Set()]]);
  let failDelete = false;
  let holdSave: (() => Promise<void>) | undefined;
  const writes: string[] = [];
  await page.route('**/api/session', async route => {
    if (route.request().method() === 'DELETE') userId = undefined;
    if (route.request().method() === 'POST') userId = 102;
    return route.fulfill({json: userId ? {authenticated:true, user:{id:userId, username:`USER-${userId}`, is_superuser:false}, accounts:{count:2, next:null, previous:null, results:[account, {...account, id:'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb', name:'TALLER DOS'}]}} : {authenticated:false}});
  });
  await page.route('**/api/market/analytics/events', route => route.fulfill({status:202, json:{recorded:true}}));
  await page.route(/\/api\/market\/catalog(?:\?.*)?$/, route => route.fulfill({json:{count:catalogParts.length, next:null, previous:null, results:catalogParts}}));
  await page.route('**/api/market/wishlist/state', route => route.fulfill({json:{part_ids:[...(saved.get(userId || 0) || [])], count:saved.get(userId || 0)?.size || 0}}));
  await page.route(/\/api\/market\/wishlist(?:\?.*)?$/, route => {
    const query = new URL(route.request().url()).searchParams;
    const term = (query.get('search') || '').toUpperCase();
    const rows = catalogParts.filter(part => saved.get(userId || 0)?.has(part.id) && `${part.sku} ${part.description} ${part.codes.map(code => code.code).join(' ')}`.includes(term));
    const number = Number(query.get('page') || 1);
    if (number > 1 && rows.length <= (number - 1) * 50) return route.fulfill({status:404, json:{detail:'Página inválida.'}});
    return route.fulfill({json:{count:rows.length, next:rows.length > number * 50 ? '/next' : null, previous:number > 1 ? '/previous' : null, results:rows.slice((number - 1) * 50, number * 50).map(part => ({part, saved_at:'2026-10-02T20:00:00Z', available:!inactiveIds.includes(part.id)}))}});
  });
  await page.route(/\/api\/market\/wishlist\/[a-f0-9-]{36}$/, async route => {
    const id = new URL(route.request().url()).pathname.split('/').at(-1)!;
    const method = route.request().method(); writes.push(method);
    if (method === 'DELETE' && failDelete) return route.fulfill({status:503, json:{detail:'No se pudo quitar el favorito. Inténtalo de nuevo.'}});
    if (holdSave) await holdSave();
    const ids = saved.get(userId || 0)!;
    if (method === 'DELETE') { ids.delete(id); return route.fulfill({status:204}); }
    ids.add(id); return route.fulfill({json:{part_id:id, saved:true}});
  });
  await page.route('**/api/market/catalog/*/suppliers', route => route.fulfill({json:[{supplier_id:'supplier', supplier_name:'PROVEEDOR', in_stock:true, items:[{id:'item', codigo:parts[0].sku, brand:'', description:parts[0].description}]}]}));
  await page.route('**/request-state', route => route.fulfill({json:{part_id:parts[0].id, totals:{sent_quantity:0, pending_quantity:0, reviewed_quantity:0}, items:[]}}));
  return { saved, writes, failDelete: () => { failDelete = true; }, recover: () => { failDelete = false; }, hold: (wait: () => Promise<void>) => { holdSave = wait; } };
}

test('saved hearts persist after reopening, reload and account switching; wishlist opens supplier options', async ({page}) => {
  const state = await fixture(page);
  await page.goto('/');
  await page.getByRole('button', {name:'Guardar 58411-1R000 en favoritos', exact:true}).click();
  const savedHeart = page.getByRole('button', {name:'Quitar 58411-1R000 de favoritos', exact:true});
  await expect(savedHeart).toHaveAttribute('aria-pressed','true');
  await page.reload();
  await expect(savedHeart).toHaveAttribute('aria-pressed','true');
  await page.getByLabel('Cuenta activa', {exact:true}).selectOption('bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb');
  await expect(savedHeart).toHaveAttribute('aria-pressed','true');
  await page.getByRole('button', {name:'Mis favoritos', exact:true}).click();
  await expect(page).toHaveURL(/vista=favoritos/);
  await expect(page.locator('.wishlist-page .part-card')).toHaveCount(1);
  await page.getByRole('button', {name:'Ver 58411-1R000', exact:true}).click();
  const detail = page.getByRole('dialog', {name:'Detalles del repuesto', exact:true});
  await expect(detail.getByRole('button', {name:'Quitar 58411-1R000 de favoritos', exact:true})).toHaveAttribute('aria-pressed','true');
  await detail.getByRole('button', {name:'Agregar 58411-1R000 de PROVEEDOR a la cesta', exact:true}).click();
  await expect(detail.getByRole('button', {name:'58411-1R000 agregado, 1 en cesta', exact:true})).toBeVisible();
  await detail.getByRole('button', {name:'Cerrar ventana', exact:true}).click();
  await page.reload();
  await expect(page.locator('.wishlist-page .part-card')).toHaveCount(1);
  expect(state.saved.get(101)?.size).toBe(1);
});

test('search uses alternate codes and removal failures keep the saved item for retry', async ({page}) => {
  const state = await fixture(page, {initial:parts});
  await page.goto('/?vista=favoritos');
  await expect(page.locator('.wishlist-page .part-card')).toHaveCount(3);
  await expect(page.getByRole('article', {name:'Repuesto FILTER-002', exact:true})).toContainText('EXISTENCIAS MEDIAS');
  await page.getByLabel('Buscar en mis favoritos').fill('58411-1r000-g');
  await expect(page.locator('.wishlist-page .part-card')).toHaveCount(1);
  state.failDelete();
  await page.getByRole('button', {name:'Quitar 58411-1R000 de favoritos', exact:true}).click();
  await expect(page.getByRole('status')).toContainText('No se pudo quitar');
  await expect(page.locator('.wishlist-page .part-card')).toHaveCount(1);
  state.recover();
  await page.getByRole('button', {name:'Quitar 58411-1R000 de favoritos', exact:true}).click();
  await expect(page.getByRole('heading', {name:'No encontramos ese repuesto en tus favoritos.'})).toBeVisible();
  await page.getByRole('button', {name:'Limpiar búsqueda de favoritos'}).click();
  await expect(page.locator('.wishlist-page .part-card')).toHaveCount(2);
});

test('a slow save disables the heart and records a single write', async ({page}) => {
  const state = await fixture(page);
  let release: () => void = () => {};
  const waiting = new Promise<void>(resolve => {release = resolve;});
  state.hold(() => waiting);
  await page.goto('/');
  const heart = page.getByRole('button', {name:'Guardar 58411-1R000 en favoritos', exact:true});
  await heart.click();
  await expect(heart).toBeDisabled();
  expect(state.writes).toEqual(['PUT']);
  release();
  await expect(page.getByRole('button', {name:'Quitar 58411-1R000 de favoritos', exact:true})).toBeEnabled();
});

test('guests get sign-in and a different user does not inherit favorites', async ({page}) => {
  await fixture(page, {initial:[parts[0]]});
  await page.goto('/?vista=favoritos');
  await expect(page.locator('.wishlist-page .part-card')).toHaveCount(1);
  await page.getByRole('button', {name:'Cuenta y listas', exact:true}).click();
  await page.getByRole('button', {name:'Cerrar sesión', exact:true}).click();
  await page.getByRole('button', {name:'Mis favoritos', exact:true}).click();
  await expect(page.getByRole('heading', {name:'Tus próximos repuestos, en un solo lugar.'})).toBeVisible();
  await page.locator('.wishlist-page').getByRole('button', {name:'Iniciar sesión'}).click();
  const login = page.getByRole('dialog');
  await login.getByRole('textbox', {name:'Usuario', exact:true}).fill('OTHER');
  await login.getByLabel('Contraseña', {exact:true}).fill('TestPassword!123');
  await login.locator('form').getByRole('button', {name:'Iniciar sesión', exact:true}).click();
  await expect(page.getByRole('heading', {name:'Aún no tienes repuestos guardados.'})).toBeVisible();
});

for (const width of [390, 320]) {
  test(`mobile ${width}: favorites remain reachable and remove without horizontal overflow`, async ({page}) => {
    await page.setViewportSize({width, height:844});
    await fixture(page, {initial:[parts[0]]});
    await page.goto('/');
    await page.getByRole('button', {name:'Mis favoritos', exact:true}).click();
    await expect(page.getByRole('heading', {name:'Mis favoritos', exact:true})).toBeVisible();
    await expect(page.locator('.wishlist-nav-count')).toBeVisible();
    await page.getByRole('button', {name:'Quitar 58411-1R000 de favoritos', exact:true}).click();
    await expect(page.getByRole('heading', {name:'Aún no tienes repuestos guardados.'})).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)).toBe(false);
  });
}


test('removing the last item from page two in the detail view returns to page one', async ({page}) => {
  const pool = Array.from({length:51}, (_, index) => ({...parts[0], id:`00000000-0000-4000-8000-${String(index).padStart(12,'0')}`, sku:`SAVED-${String(index).padStart(3,'0')}`, codes:[]}));
  await fixture(page, {initial:pool, catalogParts:pool});
  await page.goto('/?vista=favoritos');
  await page.getByRole('button', {name:'Siguiente', exact:true}).click();
  await expect(page.locator('.pagination')).toContainText('Página 2');
  await page.getByRole('button', {name:'Ver SAVED-050', exact:true}).click();
  const detail = page.getByRole('dialog', {name:'Detalles del repuesto', exact:true});
  await detail.getByRole('button', {name:'Quitar SAVED-050 de favoritos', exact:true}).click();
  await detail.getByRole('button', {name:'Cerrar ventana', exact:true}).click();
  await expect(page.locator('.pagination')).toContainText('Página 1');
  await expect(page.locator('.wishlist-page .part-card')).toHaveCount(50);
  await expect(page.locator('.wishlist-page').getByRole('alert')).toHaveCount(0);
});

test('an inactive saved SKU remains removable but cannot open supplier options', async ({page}) => {
  await fixture(page, {initial:[parts[0]], inactiveIds:[parts[0].id]});
  await page.goto('/?vista=favoritos');
  await expect(page.getByText('FUERA DEL CATÁLOGO', {exact:true})).toBeVisible();
  await expect(page.getByRole('button', {name:'Ver 58411-1R000', exact:true})).toBeDisabled();
  await page.getByRole('button', {name:'Quitar 58411-1R000 de favoritos', exact:true}).click();
  await expect(page.getByRole('heading', {name:'Aún no tienes repuestos guardados.'})).toBeVisible();
});
