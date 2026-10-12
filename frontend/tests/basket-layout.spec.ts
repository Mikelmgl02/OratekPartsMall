import { expect, Page, test } from '@playwright/test';
import { expectAccount, expectSignedOut, switchAccount } from './header';
import type { BasketLine, Offer, Part } from '../lib/types';
import { fakeServerBasket } from './server-basket';

const accounts = [
  { id: 'basket-client-a', name: 'EMPRESA A', roles: ['client_business'], capabilities: ['client'] },
  { id: 'basket-client-b', name: 'EMPRESA B', roles: ['client_business'], capabilities: ['client'] },
];
const supplierA = { supplier_id: 'basket-supplier-a', supplier_name: 'REPUESTOS CENTRAL', in_stock: true };
const supplierB = { supplier_id: 'basket-supplier-b', supplier_name: 'DISTRIBUIDORA MOTOR', in_stock: true };
const sharedPart: Part = { id: 'basket-part-shared', sku: '58411-1R000', name: 'DISCO DE FRENO', description: 'DISCO DE FRENO DELANTERO', codes: [{ code: 'A-100', brand: '' }, { code: 'B-100', brand: '' }] };

function line(part: Part, supplier: typeof supplierA, itemId: string, code: string, quantity = 1): BasketLine {
  const selected_item = { id: itemId, codigo: code, brand: 'MARCA DE PRUEBA', description: part.description };
  return { key: `${part.id}:${supplier.supplier_id}:${itemId}`, part, offer: { ...supplier, selected_item }, quantity };
}

async function mockClient(page: Page, parts: Part[], offers: Record<string, Offer[]> = {}) {
  const unexpectedWrites: string[] = [];
  await page.route('**/api/**', async route => {
    if (route.request().method() !== 'GET') unexpectedWrites.push(`${route.request().method()} ${new URL(route.request().url()).pathname}`);
    await route.fulfill({ status: 404, json: { detail: 'Solicitud no esperada en esta prueba.' } });
  });
  await page.route('**/api/market/analytics/events', route => route.fulfill({ status: 202, json: { recorded: true } }));
  await page.route('**/api/session', route => route.fulfill({ json: { authenticated: true, user: { id: 42, username: 'CLIENTE CESTA', first_name: '', last_name: '', is_superuser: false }, accounts: { count: accounts.length, next: null, previous: null, results: accounts } } }));
  await page.route(/\/api\/market\/catalog(?:\?.*)?$/, route => {
    const query = (new URL(route.request().url()).searchParams.get('search') || '').toUpperCase();
    const results = parts.filter(part => `${part.sku} ${part.name} ${part.description} ${part.codes.map(code => code.code).join(' ')}`.toUpperCase().includes(query));
    return route.fulfill({ json: { count: results.length, next: null, previous: null, results } });
  });
  await page.route(/\/api\/market\/catalog\/[^/]+\/suppliers$/, route => {
    const partId = new URL(route.request().url()).pathname.split('/').at(-2)!;
    return route.fulfill({ json: offers[partId] || [] });
  });
  return unexpectedWrites;
}

async function seedDrafts(page: Page, drafts: Record<string, BasketLine[]>) {
  await page.addInitScript(drafts => {
    if (sessionStorage.getItem('basket-layout-seeded')) return;
    for (const [accountId, lines] of Object.entries(drafts)) localStorage.setItem(`partsmall:basket:${accountId}`, JSON.stringify(lines));
    sessionStorage.setItem('basket-layout-seeded', '1');
  }, drafts);
}

function basket(page: Page) { return page.getByRole('main', { name: 'Cesta de solicitudes', exact: true }); }
function item(page: Page, code: string, supplier: string) { return basket(page).getByRole('article', { name: `Artículo ${code} de ${supplier}`, exact: true }); }
async function expectSummary(page: Page, suppliers: number, articles: number, units: number) {
  const summary = page.getByRole('complementary', { name: 'Resumen de solicitud', exact: true });
  for (const [label, value] of [['Proveedores', suppliers], ['Artículos', articles], ['Unidades solicitadas', units]] as const) {
    await expect(summary.getByText(label, { exact: true }).locator('..').locator('dd')).toHaveText(String(value));
  }
}
// What this browser still keeps: a client account's cesta lives on the server and moves in from here once.
async function savedDraft(page: Page, accountId = accounts[0].id): Promise<BasketLine[]> {
  return page.evaluate(accountId => JSON.parse(localStorage.getItem(`partsmall:basket:${accountId}`) || '[]'), accountId);
}

test('full-page basket keeps equivalent supplier items separate, edits quantities and survives navigation and reload', async ({ page }) => {
  const offers: Offer[] = [
    { ...supplierA, items: [{ id: 'supplier-item-a', codigo: 'A-100', brand: 'MARCA A', description: 'DISCO EQUIVALENTE A' }, { id: 'supplier-item-b', codigo: 'B-100', brand: 'MARCA B', description: 'DISCO EQUIVALENTE B' }] },
    { ...supplierB, items: [{ id: 'supplier-item-c', codigo: 'A-100', brand: 'MARCA C', description: 'MISMO CÓDIGO, OTRO PROVEEDOR' }] },
  ];
  const writes = await mockClient(page, [sharedPart], { [sharedPart.id]: offers });
  const cesta = await fakeServerBasket(page, { parts: [sharedPart], offers: { [sharedPart.id]: offers } });
  await page.goto('/?campana=cesta-prueba#inicio');
  const search = page.getByLabel('Buscar repuestos, marcas o códigos', { exact: true });
  await search.fill('58411'); await search.press('Enter');
  await expect(page.locator('.results-heading>p')).toContainText('para “58411”');
  await page.getByRole('button', { name: 'Ver DISCO DE FRENO', exact: true }).click();
  const detail = page.getByRole('dialog');
  await detail.getByLabel('Cantidad solicitada', { exact: true }).fill('1.5');
  await expect(detail.getByLabel('Cantidad solicitada', { exact: true })).toHaveValue('1');
  for (const [code, supplier] of [['A-100', supplierA.supplier_name], ['B-100', supplierA.supplier_name], ['A-100', supplierB.supplier_name]]) {
    await detail.getByRole('button', { name: `Agregar ${code} de ${supplier} a la cesta`, exact: true }).click();
  }
  await detail.getByRole('button', { name: 'Cerrar ventana', exact: true }).click();
  await page.getByRole('button', { name: 'Abrir cesta, 3 artículos', exact: true }).click();
  await expect(basket(page)).toBeVisible();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(basket(page).getByRole('heading', { level: 1, name: 'Tu cesta de solicitudes', exact: true })).toBeFocused();
  expect(new URL(page.url()).searchParams.get('vista')).toBe('cesta');
  expect(new URL(page.url()).searchParams.get('campana')).toBe('cesta-prueba');
  expect(new URL(page.url()).hash).toBe('#inicio');
  await expect(basket(page).getByRole('article')).toHaveCount(3);
  await expect(basket(page).getByRole('region', { name: `Solicitud para ${supplierA.supplier_name}`, exact: true }).getByRole('article')).toHaveCount(2);
  await expect(basket(page).getByRole('region', { name: `Solicitud para ${supplierB.supplier_name}`, exact: true }).getByRole('article')).toHaveCount(1);
  await expectSummary(page, 2, 3, 3);
  await expect(basket(page).getByRole('button', { name: 'Enviar solicitud', exact: true })).toBeEnabled();
  await expect(basket(page).getByText('Precios en cotizaciones privadas', { exact: true })).toBeVisible();
  expect(await basket(page).innerText()).not.toMatch(/[$€]|\b(?:PAB|USD)\b/);

  await item(page, 'A-100', supplierA.supplier_name).getByLabel('Cantidad de A-100', { exact: true }).fill('3');
  await expectSummary(page, 2, 3, 5);
  await item(page, 'B-100', supplierA.supplier_name).getByRole('button', { name: 'Eliminar B-100', exact: true }).click();
  await item(page, 'A-100', supplierB.supplier_name).getByRole('button', { name: 'Aumentar cantidad de A-100', exact: true }).click();
  await expectSummary(page, 2, 2, 5);
  await expect.poll(() => (cesta.lines[accounts[0].id] || []).map(line => [line.offer.selected_item!.id, line.quantity])).toEqual([['supplier-item-a', 3], ['supplier-item-c', 2]]);  // on the server

  await page.goBack();
  await expect(basket(page)).toHaveCount(0);
  await expect(search).toHaveValue('58411');
  expect(new URL(page.url()).searchParams.has('vista')).toBe(false);
  await page.goForward();
  await expectSummary(page, 2, 2, 5);
  await page.reload();
  await expectSummary(page, 2, 2, 5);
  await expect(item(page, 'A-100', supplierA.supplier_name).getByLabel('Cantidad de A-100', { exact: true })).toHaveValue('3');
  await expect(item(page, 'A-100', supplierB.supplier_name).getByLabel('Cantidad de A-100', { exact: true })).toHaveValue('2');
  await basket(page).getByRole('button', { name: 'Volver al catálogo', exact: true }).click();
  await expect(basket(page)).toHaveCount(0);
  await expect(search).toBeFocused();
  expect(new URL(page.url()).searchParams.has('vista')).toBe(false);
  await expect(page.getByRole('button', { name: 'Abrir cesta, 2 artículos', exact: true })).toBeVisible();
  expect(writes).toEqual([]);
});

test('desktop basket has wide supplier groups and a sticky summary, while mobile stacks without overflow', async ({ page }) => {
  const lines = Array.from({ length: 10 }, (_, index) => {
    const part: Part = { id: `basket-layout-part-${index}`, sku: `SKU-${index + 1}`, name: index % 2 ? 'FILTRO DE ACEITE' : 'DISCO DE FRENO', description: 'REPUESTO DE PRUEBA PARA UNA SOLICITUD CON VARIOS ARTÍCULOS', codes: [] };
    return line(part, index < 5 ? supplierA : supplierB, `basket-layout-item-${index}`, `CODIGO-${index + 1}`);
  });
  const writes = await mockClient(page, lines.map(line => line.part));
  await fakeServerBasket(page, { known: lines });  // the browser's cesta moves in
  await seedDrafts(page, { [accounts[0].id]: lines });
  await page.goto('/?vista=cesta');
  await expectSummary(page, 2, 10, 10);
  const firstGroup = basket(page).getByRole('region', { name: `Solicitud para ${supplierA.supplier_name}`, exact: true });
  const summary = page.getByRole('complementary', { name: 'Resumen de solicitud', exact: true });
  const left = (await firstGroup.boundingBox())!;
  const right = (await summary.boundingBox())!;
  expect(left.width).toBeGreaterThan(right.width * 1.5);
  expect(right.x).toBeGreaterThanOrEqual(left.x + left.width + 12);
  expect(Math.abs(left.y - right.y)).toBeLessThanOrEqual(2);
  await page.screenshot({ path: '/tmp/motionpartes-basket-desktop.png', fullPage: false });
  await page.getByTestId('page-viewport').evaluate(viewport => viewport.scrollTo({ top: 600, behavior: 'instant' }));
  const pinned = (await summary.boundingBox())!;
  await page.getByTestId('page-viewport').evaluate(viewport => viewport.scrollTo({ top: 950, behavior: 'instant' }));
  const later = (await summary.boundingBox())!;
  expect(Math.abs(later.y - pinned.y)).toBeLessThanOrEqual(2);
  expect(later.y).toBeGreaterThanOrEqual((await page.getByTestId('page-viewport').boundingBox())!.y);
  expect((await page.locator('.app-toolbar').boundingBox())!.y).toBe(0);
  expect(later.y + later.height).toBeLessThan(page.viewportSize()!.height);
  expect((await firstGroup.getByRole('article').first().boundingBox())!.y).toBeLessThan(0);
  await page.screenshot({ path: '/tmp/motionpartes-basket-sticky.png', fullPage: false });

  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByTestId('page-viewport').evaluate(viewport => viewport.scrollTo({ top: 0, behavior: 'instant' }));
  await expect(basket(page).getByRole('article')).toHaveCount(10);
  expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)).toBe(false);
  const lastItem = (await basket(page).getByRole('article').last().boundingBox())!;
  const mobileSummary = (await summary.boundingBox())!;
  expect(mobileSummary.y).toBeGreaterThanOrEqual(lastItem.y + lastItem.height);
  for (const article of await basket(page).getByRole('article').all()) {
    const box = (await article.boundingBox())!;
    expect(box.x).toBeGreaterThanOrEqual(0);
    expect(box.x + box.width).toBeLessThanOrEqual(390);
  }
  const firstItem = firstGroup.getByRole('article').first();
  await firstItem.getByRole('button', { name: 'Aumentar cantidad de CODIGO-1', exact: true }).click();
  await expect(firstItem.getByLabel('Cantidad de CODIGO-1', { exact: true })).toHaveValue('2');
  await expectSummary(page, 2, 10, 11);
  await page.getByTestId('page-viewport').evaluate(viewport => viewport.scrollTo({ top: 0, behavior: 'instant' }));
  await page.screenshot({ path: '/tmp/motionpartes-basket-mobile.png', fullPage: false });
  expect(writes).toEqual([]);
});

test('account drafts stay isolated through reload, quantity correction and an empty basket', async ({ page }) => {
  const draftA = [line(sharedPart, supplierA, 'account-a-item', 'CUENTA-A', 2)];
  const draftB = [line(sharedPart, supplierB, 'account-b-item', 'CUENTA-B', 3)];
  const writes = await mockClient(page, [sharedPart]);
  const cesta = await fakeServerBasket(page, { known: [...draftA, ...draftB] });
  await seedDrafts(page, { [accounts[0].id]: draftA, [accounts[1].id]: draftB });
  await page.goto('/?vista=cesta');
  await expectSummary(page, 1, 1, 2);
  await switchAccount(page, accounts[1].id);
  await expect(item(page, 'CUENTA-B', supplierB.supplier_name)).toBeVisible();
  await expect(item(page, 'CUENTA-A', supplierA.supplier_name)).toHaveCount(0);
  await expectSummary(page, 1, 1, 3);
  const quantityB = item(page, 'CUENTA-B', supplierB.supplier_name).getByLabel('Cantidad de CUENTA-B', { exact: true });
  await expect(quantityB).toHaveAttribute('autocomplete', 'off');
  await quantityB.fill('4');
  await expectSummary(page, 1, 1, 4);
  await quantityB.fill('');
  await quantityB.blur();
  await expect(quantityB).toHaveValue('1');
  await expectSummary(page, 1, 1, 1);
  await quantityB.fill('10000');
  await quantityB.blur();
  await expect(quantityB).toHaveValue('9999');
  await expect(item(page, 'CUENTA-B', supplierB.supplier_name).getByRole('button', { name: 'Aumentar cantidad de CUENTA-B', exact: true })).toBeDisabled();
  await quantityB.fill('4');
  await expect.poll(() => cesta.lines[accounts[1].id]?.[0]?.quantity).toBe(4);  // saved on the server
  await page.reload();
  await expectAccount(page, accounts[1].id);
  await expectSummary(page, 1, 1, 4);
  await switchAccount(page, accounts[0].id);
  await expectSummary(page, 1, 1, 2);
  await expect(item(page, 'CUENTA-A', supplierA.supplier_name).getByLabel('Cantidad de CUENTA-A', { exact: true })).toHaveValue('2');
  await item(page, 'CUENTA-A', supplierA.supplier_name).getByRole('button', { name: 'Eliminar CUENTA-A', exact: true }).click();
  await expectSummary(page, 0, 0, 0);
  await expect(basket(page).getByRole('article')).toHaveCount(0);
  await expect(basket(page).getByRole('region', { name: 'Cesta vacía', exact: true })).toBeVisible();
  await expect.poll(() => cesta.lines[accounts[0].id]).toEqual([]);
  expect(cesta.lines[accounts[1].id][0].quantity).toBe(4);
  expect(await savedDraft(page, accounts[0].id)).toEqual([]);  // it moved in: the browser keeps no copy
  expect(await savedDraft(page, accounts[1].id)).toEqual([]);
  await basket(page).getByRole('button', { name: 'Explorar repuestos', exact: true }).click();
  await expect(basket(page)).toHaveCount(0);
  expect(new URL(page.url()).searchParams.has('vista')).toBe(false);
  await expect(page.getByRole('button', { name: 'Abrir cesta, 0 artículos', exact: true })).toBeVisible();
  expect(writes).toEqual([]);
});

test('paginated membership loading keeps drafts hidden until the saved active account is validated', async ({ page }) => {
  const unassigned = [line(sharedPart, supplierA, 'unassigned-item', 'SIN-CUENTA', 19)];
  const ownDraft = [line(sharedPart, supplierB, 'validated-account-item', 'CUENTA-VALIDADA', 7)];
  const writes = await mockClient(page, [sharedPart]);
  const cesta = await fakeServerBasket(page, { known: [...unassigned, ...ownDraft] });
  await seedDrafts(page, { unassigned, [accounts[1].id]: ownDraft });
  await page.addInitScript(accountId => localStorage.setItem('motionpartes:active-account:42', accountId), accounts[1].id);
  await page.route('**/api/session', route => route.fulfill({ json: { authenticated: true, user: { id: 42, username: 'CLIENTE CESTA', first_name: '', last_name: '', is_superuser: false }, accounts: { count: 2, next: '/api/market/accounts?page=2', previous: null, results: [accounts[0]] } } }));
  let release!: () => void;
  let requested!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  const pageTwoRequested = new Promise<void>(resolve => { requested = resolve; });
  await page.route('**/api/market/accounts?page=2', async route => {
    requested();
    await held;
    await route.fulfill({ json: { count: 2, next: null, previous: '/api/market/accounts?page=1', results: [accounts[1]] } });
  });
  try {
    await page.goto('/?vista=cesta');
    await pageTwoRequested;
    await expect(page.getByText('Cargando tu cesta…', { exact: true })).toBeVisible();
    await expectSignedOut(page);
    await expect(page.getByLabel('Cuenta activa', { exact: true })).toHaveCount(0);
    await expect(basket(page)).toHaveCount(0);
    await expect(page.getByLabel('Cantidad de SIN-CUENTA', { exact: true })).toHaveCount(0);
    release();
    await expectAccount(page, accounts[1].id);
    await expectSummary(page, 1, 1, 7);
    await expect(item(page, 'CUENTA-VALIDADA', supplierB.supplier_name)).toBeVisible();
    await expect(page.getByText('SIN-CUENTA', { exact: true })).toHaveCount(0);
    expect(await savedDraft(page, 'unassigned')).toEqual(unassigned);  // never an account's: it stays where it was
    expect(cesta.lines[accounts[1].id].map(line => [line.offer.selected_item!.id, line.quantity])).toEqual([['validated-account-item', 7]]);
    expect(await savedDraft(page, accounts[1].id)).toEqual([]);
    expect(Object.keys(cesta.lines)).not.toContain('unassigned');
    expect(writes).toEqual([]);
  } finally { release(); }
});

test('signed in, the cesta lives on the server: what WhatsApp or another device adds shows when the page returns, and a changed article blocks sending', async ({ page }) => {
  const fromWhatsApp = { ...line(sharedPart, supplierA, 'server-item-a', 'POR-WHATSAPP', 2), source: 'whatsapp' as const, available: true, changed: false };
  const writes = await mockClient(page, [sharedPart]);
  const cesta = await fakeServerBasket(page, { server: { [accounts[0].id]: [fromWhatsApp] } });
  await page.goto('/?vista=cesta');
  const whatsapp = item(page, 'POR-WHATSAPP', supplierA.supplier_name);
  await expect(whatsapp).toContainText('Agregado por WhatsApp');
  await expect(basket(page)).toContainText('Tu cesta se guarda en tu cuenta: la ves igual en todos tus dispositivos.');
  cesta.lines[accounts[0].id].push({ ...line(sharedPart, supplierB, 'server-item-b', 'CAMBIADO', 1), available: true, changed: true });  // elsewhere
  await page.evaluate(() => window.dispatchEvent(new Event('focus')));  // the page comes back to the front
  const changed = item(page, 'CAMBIADO', supplierB.supplier_name);
  await expect(changed).toContainText('Cambió desde que lo agregaste. Quítalo y vuelve a elegirlo en el catálogo.');
  await expectSummary(page, 2, 2, 3);
  await expect(basket(page).getByRole('button', { name: 'Enviar solicitud', exact: true })).toBeDisabled();
  await expect(basket(page)).toContainText('Quita o vuelve a elegir los artículos marcados para enviar la solicitud.');
  await changed.getByRole('button', { name: 'Eliminar CAMBIADO', exact: true }).click();
  await expect(basket(page).getByRole('button', { name: 'Enviar solicitud', exact: true })).toBeEnabled();
  await expect.poll(() => cesta.lines[accounts[0].id].map(line => line.offer.selected_item!.id)).toEqual(['server-item-a']);
  expect(cesta.changes.at(-1)).toEqual({ account: accounts[0].id, changes: [{ supplier_item_id: 'server-item-b', quantity: 0, mode: 'set' }] });
  expect(writes).toEqual([]);
});
