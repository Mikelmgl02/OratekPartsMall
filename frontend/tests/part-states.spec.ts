import { expect, Page, test } from '@playwright/test';

const part = {id:'state-part', sku:'58411-1R000', name:'', description:'TAMBOR HYU ACCENT', category:'FRENOS', subcategory:'TAMBORES', codes:[]};
const accounts = [
  {id:'state-client-a', name:'EMPRESA A', roles:['client_business'], capabilities:['client']},
  {id:'state-client-b', name:'EMPRESA B', roles:['client_business'], capabilities:['client']},
];
const offers = [
  {supplier_id:'supplier-a', supplier_name:'PROVEEDOR A', in_stock:true, items:[
    {id:'item-a', codigo:'58411-1R000-G', brand:'MARCA A', description:'TAMBOR A'},
    {id:'item-b', codigo:'58411-1R000-KSM', brand:'MARCA B', description:'TAMBOR B'},
  ]},
  {supplier_id:'supplier-b', supplier_name:'PROVEEDOR B', in_stock:true, items:[
    {id:'item-c', codigo:'58411-1R000-G', brand:'MARCA A', description:'TAMBOR C'},
  ]},
];
const history = {part_id:part.id, totals:{sent_quantity:12, pending_quantity:7, reviewed_quantity:5}, items:[
  {supplier_item_id:'item-a', supplier_id:'supplier-a', codigo:'58411-1R000-G', brand:'MARCA A', sent_quantity:8, pending_quantity:3, reviewed_quantity:5},
  {supplier_item_id:'item-c', supplier_id:'supplier-b', codigo:'58411-1R000-G', brand:'MARCA A', sent_quantity:4, pending_quantity:4, reviewed_quantity:0},
]};
const emptyHistory = {part_id:part.id, totals:{sent_quantity:0, pending_quantity:0, reviewed_quantity:0}, items:[]};

async function setup(page: Page) {
  await page.route('**/api/session', route => route.fulfill({json:{authenticated:true, user:{id:1, username:'CLIENTE', is_superuser:false}, accounts:{count:2, next:null, previous:null, results:accounts}}}));
  await page.route(/\/api\/market\/catalog(?:\?.*)?$/, route => route.fulfill({json:{count:1, next:null, previous:null, results:[part]}}));
  await page.route(`**/api/market/catalog/${part.id}/suppliers`, route => route.fulfill({json:offers}));
}
async function openDetail(page: Page) {
  await page.getByRole('button', {name:'Ver 58411-1R000', exact:true}).click();
  await expect(page.getByRole('dialog').getByRole('heading', {name:'Elige tu proveedor', exact:true})).toBeVisible();
}
function row(page: Page, supplier='PROVEEDOR A') {
  return page.locator('.sku-supplier-group').filter({has:page.getByRole('heading', {name:supplier, exact:true})})
    .getByRole('group', {name:'Artículo 58411-1R000-G · MARCA A', exact:true});
}

for (const viewport of [{label:'desktop', width:1440, height:1100}, {label:'mobile', width:390, height:844}]) {
  test(`${viewport.label}: added state survives close, reload and quantity changes without mixing supplier items`, async ({page}) => {
    await page.setViewportSize(viewport);
    await setup(page);
    await page.route('**/request-state', route => route.fulfill({json:history}));
    await page.goto('/'); await openDetail(page);
    const summary = page.getByRole('region', {name:'Tus cantidades para este SKU'});
    await expect(summary.locator('.pending strong')).toHaveText('7');
    await expect(summary.locator('.reviewed strong')).toHaveText('5');
    await expect(summary.locator('.part-request-counts>div')).toHaveCount(3);
    await expect(summary).not.toContainText('ENVÍO CONFIRMADO');
    await expect(row(page).locator('.item-request-states>span')).toHaveText(['3 ENVIADAS · POR REVISAR', '5 EN REVISIÓN']);
    await expect(row(page, 'PROVEEDOR B').locator('.item-request-states>span')).toHaveText(['4 ENVIADAS · POR REVISAR']);
    await page.getByLabel('Cantidad solicitada', {exact:true}).fill('3');
    await row(page).getByRole('button', {name:'Agregar 58411-1R000-G de PROVEEDOR A a la cesta', exact:true}).click();
    await expect(row(page)).toContainText('3 EN CESTA');
    await expect(summary.locator('.draft strong')).toHaveText('3');
    await expect(row(page, 'PROVEEDOR B')).not.toContainText('EN CESTA');
    await row(page).getByRole('button', {name:'Agregar más 58411-1R000-G de PROVEEDOR A', exact:true}).click();
    await expect(row(page)).toContainText('6 EN CESTA');
    await page.getByRole('button', {name:'Cerrar ventana', exact:true}).click();
    await openDetail(page);
    await expect(row(page).getByRole('button', {name:'58411-1R000-G agregado, 6 en cesta', exact:true})).toBeVisible();
    await page.getByRole('button', {name:'Cerrar ventana', exact:true}).click();
    await page.reload(); await openDetail(page);
    await expect(row(page)).toContainText('6 EN CESTA');
    await row(page).getByRole('button', {name:'58411-1R000-G agregado, 6 en cesta', exact:true}).click();
    await expect(page.getByRole('main', {name:'Cesta de solicitudes', exact:true})).toBeVisible();
    await page.getByLabel('Cantidad de 58411-1R000-G', {exact:true}).fill('2');
    await page.getByRole('button', {name:'Volver al catálogo', exact:true}).click(); await openDetail(page);
    await expect(row(page)).toContainText('2 EN CESTA');
    expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)).toBe(false);
    await row(page).getByRole('button', {name:'58411-1R000-G agregado, 2 en cesta', exact:true}).click();
    await page.getByRole('button', {name:'Eliminar 58411-1R000-G', exact:true}).click();
    await page.getByRole('button', {name:'Volver al catálogo', exact:true}).click(); await openDetail(page);
    await expect(row(page)).not.toContainText('EN CESTA');
    await expect(row(page).getByRole('button', {name:'Agregar 58411-1R000-G de PROVEEDOR A a la cesta', exact:true})).toBeVisible();
    await expect(row(page).locator('.item-request-states>span')).toHaveText(['3 ENVIADAS · POR REVISAR', '5 EN REVISIÓN']);
    await page.getByRole('button', {name:'Cerrar ventana', exact:true}).click();
    await page.getByLabel('Cuenta activa', {exact:true}).selectOption('state-client-b');
    await page.unroute('**/request-state');
    await page.route('**/request-state', route => route.fulfill({json:emptyHistory}));
    await openDetail(page);
    await expect(summary.locator('.draft strong')).toHaveText('0');
    await expect(summary.locator('.pending strong')).toHaveText('0');
    await expect(summary.locator('.reviewed strong')).toHaveText('0');
    await expect(row(page)).not.toContainText('ENVIADAS');
  });
}

test('history failure preserves basket state and can be retried independently', async ({page}) => {
  await setup(page);
  let fail = true;
  await page.route('**/request-state', route => route.fulfill(fail ? {status:503, json:{detail:'No se pudieron consultar tus envíos.'}} : {json:history}));
  await page.goto('/'); await openDetail(page);
  await expect(page.getByRole('dialog').getByRole('alert')).toContainText('No se pudieron consultar tus envíos.');
  await row(page).getByRole('button', {name:'Agregar 58411-1R000-G de PROVEEDOR A a la cesta', exact:true}).click();
  await expect(row(page)).toContainText('1 EN CESTA');
  fail = false;
  await page.getByRole('button', {name:'Actualizar estados', exact:true}).click();
  await expect(row(page)).toContainText('3 ENVIADAS · POR REVISAR');
  await expect(row(page)).toContainText('5 EN REVISIÓN');
  await expect(row(page)).toContainText('1 EN CESTA');
});
