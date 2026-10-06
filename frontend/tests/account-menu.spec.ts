import { expect, Page, test } from '@playwright/test';

const client = {id:'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', name:'TALLER UNO', roles:['client_business'], capabilities:['client']};
const supplier = {id:'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb', name:'REPUESTOS DOS', roles:['supplier_wholesale'], capabilities:['supplier']};
const emptyPage = {count:0, next:null, previous:null, results:[]};

async function fixture(page: Page, {guest = false, superuser = false} = {}) {
  let signedIn = !guest;
  let deletes = 0;
  let waitForLogout: Promise<void> | undefined;
  await page.route('**/api/session', async route => {
    if (route.request().method() === 'DELETE') { deletes++; await waitForLogout; signedIn = false; }
    if (route.request().method() === 'POST') signedIn = true;
    return route.fulfill({json: signedIn ? {authenticated:true, user:{id:701, username:'MIGUEL', first_name:'Miguel', last_name:'Tang', is_superuser:superuser}, accounts:{...emptyPage, count:2, results:[client, supplier]}} : {authenticated:false}});
  });
  await page.route('**/api/market/analytics/events', route => route.fulfill({status:202, json:{recorded:true}}));
  await page.route(/\/api\/market\/catalog(?:\?.*)?$/, route => route.fulfill({json:emptyPage}));
  await page.route('**/api/market/wishlist/state', route => route.fulfill({json:{part_ids:['part-1','part-2'], count:2}}));
  await page.route(/\/api\/market\/wishlist(?:\?.*)?$/, route => route.fulfill({json:emptyPage}));
  await page.route(/\/api\/market\/accounts\/[^/]+\/(sent-requests|inventory)(?:\?.*)?$/, route => route.fulfill({json:emptyPage}));
  return {deletes: () => deletes, holdLogout: (promise: Promise<void>) => { waitForLogout = promise; }};
}

const trigger = (page: Page) => page.getByRole('button', {name:'Cuenta y listas', exact:true});
const panel = (page: Page) => page.getByRole('region', {name:'Cuenta y listas', exact:true});

test('desktop hover crosses into the menu and closes when the pointer leaves', async ({page}) => {
  await fixture(page);
  await page.goto('/');
  await expect(trigger(page)).toBeEnabled();
  await expect(panel(page)).toHaveCount(0);
  await trigger(page).hover();
  await expect(trigger(page)).toHaveAttribute('aria-expanded','true');
  await expect(panel(page)).toContainText('Miguel Tang');
  await expect(panel(page).getByRole('button', {name:'Mis favoritos 2', exact:true})).toBeVisible();
  await panel(page).getByRole('button', {name:'Mis solicitudes', exact:true}).hover();
  await expect(panel(page)).toBeVisible();
  const heading = await page.locator('#catalog h2').boundingBox();
  await page.mouse.move(heading!.x + 20, heading!.y + 15);
  await expect(panel(page)).toHaveCount(0);
  await expect(trigger(page)).toHaveAttribute('aria-expanded','false');
  await expect(page.locator('.account-menu-backdrop')).toHaveCount(0);
  // Mouse focus on the trigger must not make later hover menus stick open.
  await trigger(page).click();
  await expect(panel(page)).toBeVisible();
  await page.mouse.move(heading!.x + 20, heading!.y + 15);
  await expect(panel(page)).toHaveCount(0);
});

test('keyboard disclosure focuses links, escapes back to the trigger and closes on tabbing out', async ({page}) => {
  await fixture(page);
  await page.goto('/');
  await trigger(page).focus();
  await page.keyboard.press('ArrowDown');
  await expect(panel(page).getByRole('button', {name:'Mis favoritos 2', exact:true})).toBeFocused();
  await page.keyboard.press('Tab');
  await expect(panel(page).getByRole('button', {name:'Mis solicitudes', exact:true})).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(panel(page)).toHaveCount(0);
  await expect(trigger(page)).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(panel(page)).toBeVisible();
  await page.keyboard.press('Tab');
  await expect(panel(page).getByRole('button', {name:'Mis favoritos 2', exact:true})).toBeFocused();
  await page.keyboard.press('Shift+Tab');
  await page.keyboard.press('Shift+Tab');
  await expect(panel(page)).toHaveCount(0);
});

test('list destinations close the menu and navigate to favorites, requests and the basket', async ({page}) => {
  await fixture(page);
  await page.goto('/');
  for (const [name, destination, heading] of [
    ['Mis favoritos 2','favoritos','Mis favoritos'],
    ['Mis solicitudes','solicitudes','Órdenes enviadas'],
    ['Historial de compras','compras','Historial de compras'],
    ['Mi cesta','cesta','Tu cesta'],
  ]) {
    await trigger(page).click();
    await panel(page).getByRole('button', {name, exact:true}).click();
    await expect(page).toHaveURL(new RegExp(`vista=${destination}`));
    await expect(panel(page)).toHaveCount(0);
    if (destination !== 'cesta') await expect(page.getByRole('heading', {name:heading, exact:true})).toBeVisible();
    else await expect(page.getByRole('main', {name:'Cesta de solicitudes', exact:true})).toBeVisible();
  }
});

test('account switching updates allowed links and opens the supplier workspace', async ({page}) => {
  await fixture(page);
  await page.goto('/');
  await trigger(page).click();
  await expect(panel(page).getByRole('link', {name:'Administración', exact:true})).toHaveCount(0);
  await expect(panel(page).getByRole('button', {name:'Panel de proveedor', exact:true})).toHaveCount(0);
  await expect(panel(page).getByRole('button', {name:'Historial de compras', exact:true})).toBeVisible();
  await panel(page).getByLabel('Cambiar cuenta desde el menú').selectOption(supplier.id);
  await expect(panel(page)).toHaveCount(0);
  await expect(page.getByLabel('Cuenta activa', {exact:true})).toHaveValue(supplier.id);
  await trigger(page).click();
  await expect(panel(page)).toContainText('REPUESTOS DOS');
  await expect(panel(page).getByRole('button', {name:'Mis solicitudes', exact:true})).toHaveCount(0);
  await expect(panel(page).getByRole('button', {name:'Historial de compras', exact:true})).toHaveCount(0);
  await panel(page).getByRole('button', {name:'Panel de proveedor', exact:true}).click();
  await expect(page.getByRole('heading', {name:'Tu inventario, en un solo lugar.', exact:true})).toBeVisible();
  await expect(panel(page)).toHaveCount(0);
  await page.reload();
  await expect(page.getByLabel('Cuenta activa', {exact:true})).toHaveValue(supplier.id);
});

test('superusers get administration; sign-out submits once and returns to guest actions', async ({page}) => {
  const state = await fixture(page, {superuser:true});
  let release: () => void = () => {};
  state.holdLogout(new Promise<void>(resolve => {release = resolve;}));
  await page.goto('/');
  await trigger(page).click();
  await expect(panel(page).getByRole('link', {name:'Administración', exact:true})).toHaveAttribute('href','/administracion');
  await panel(page).getByRole('button', {name:'Cerrar sesión', exact:true}).click();
  await expect(panel(page).getByRole('button', {name:'Cerrando sesión…', exact:true})).toBeDisabled();
  expect(state.deletes()).toBe(1);
  release();
  await expect(panel(page)).toHaveCount(0);
  await trigger(page).click();
  await expect(panel(page).getByRole('button', {name:'Iniciar sesión', exact:true})).toBeVisible();
  await expect(panel(page).getByRole('button', {name:'Cerrar sesión', exact:true})).toHaveCount(0);
  await expect(panel(page).getByRole('link', {name:'Administración', exact:true})).toHaveCount(0);
});

test('purchase history survives reload and browser back, and keeps quotations in sent requests', async ({page}) => {
  await fixture(page);
  await page.goto('/?vista=compras');
  await expect(page.getByRole('heading', {name:'Historial de compras', exact:true})).toBeVisible();
  await expect(page.getByRole('heading', {name:'Aún no tienes acuerdos confirmados.', exact:true})).toBeVisible();
  await expect(page.getByRole('main', {name:'Historial de compras', exact:true})).toContainText('cotizaciones que confirmaste');
  await page.reload();
  await expect(page.getByRole('heading', {name:'Historial de compras', exact:true})).toBeVisible();
  await page.getByRole('button', {name:'Ver mis solicitudes', exact:true}).click();
  await expect(page).toHaveURL(/vista=solicitudes/);
  await expect(page.getByRole('heading', {name:'Órdenes enviadas', exact:true})).toBeVisible();
  await page.goBack();
  await expect(page.getByRole('heading', {name:'Historial de compras', exact:true})).toBeVisible();
  await page.getByLabel('Cuenta activa', {exact:true}).selectOption(supplier.id);
  await expect(page.getByRole('main', {name:'Historial de compras', exact:true})).toContainText('Selecciona una cuenta con rol de cliente');
  await expect(page.getByRole('button', {name:'Ver mis solicitudes', exact:true})).toHaveCount(0);
});

test('guest invitation opens signup directly, then sign-in opens the login form', async ({page}) => {
  await fixture(page, {guest:true});
  await page.goto('/');
  await trigger(page).click();
  await panel(page).getByRole('button', {name:'Actívala aquí', exact:true}).click();
  await expect(panel(page)).toHaveCount(0);
  await expect(page.getByRole('dialog', {name:'Acepta tu invitación', exact:true})).toBeVisible();
  await expect(page.getByLabel('Código de invitación', {exact:true})).toBeVisible();
  await page.keyboard.press('Escape');
  await trigger(page).click();
  await panel(page).getByRole('button', {name:'Iniciar sesión', exact:true}).click();
  const login = page.getByRole('dialog', {name:'Bienvenido a MotionPartes', exact:true});
  await expect(login.getByLabel('Código de invitación', {exact:true})).toHaveCount(0);
  await login.getByRole('textbox', {name:'Usuario', exact:true}).fill('MIGUEL');
  await login.getByLabel('Contraseña', {exact:true}).fill('TestPassword!123');
  await login.locator('form').getByRole('button', {name:'Iniciar sesión', exact:true}).click();
  await expect(login).toHaveCount(0);
  await trigger(page).click();
  await expect(panel(page)).toContainText('Miguel Tang');
});

for (const width of [390,320]) {
  test.describe(`touch viewport ${width}`, () => {
    test.use({viewport:{width, height:844}, isMobile:true, hasTouch:true});
    test('tap toggles the menu; outside tap dismisses it without overflow', async ({page}) => {
      await fixture(page);
      await page.goto('/');
      await trigger(page).tap();
      await expect(panel(page)).toBeVisible();
      const box = await panel(page).boundingBox();
      expect(box!.x).toBeGreaterThanOrEqual(0);
      expect(box!.x + box!.width).toBeLessThanOrEqual(width);
      expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)).toBe(false);
      await trigger(page).tap();
      await expect(panel(page)).toHaveCount(0);
      await trigger(page).tap();
      await page.getByRole('button', {name:'Inicio de MotionPartes', exact:true}).tap();
      await expect(panel(page)).toHaveCount(0);
      await trigger(page).tap();
      await panel(page).getByRole('button', {name:'Mis favoritos 2', exact:true}).tap();
      await expect(page.getByRole('heading', {name:'Mis favoritos', exact:true})).toBeVisible();
      await expect(panel(page)).toHaveCount(0);
    });
  });
}
