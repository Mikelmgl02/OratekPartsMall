import { test, expect, type Page } from '@playwright/test';
import type { Grupo } from '../lib/line-types';
import { noNewProducts } from './new-products-route';

// Grupos y subgrupos (plan S8.6): every grupo with its subgrupos and line counts; a grupo created and renamed (management/grupos), a
// subgrupo added to a grupo and moved to another (management/part-types). Mocked in the browser.
const uid = (group: number, n: number) => `0${group}000000-0000-4000-8000-${String(n).padStart(12, '0')}`;

async function mockGrupos(page: Page) {
  test.skip(!process.env.E2E_ADMIN_TOKEN, 'Disposable superuser token required.');
  await page.context().addCookies([{ name: 'partsmall_session', value: process.env.E2E_ADMIN_TOKEN!, url: 'http://localhost:8080', httpOnly: true, sameSite: 'Strict' }]);
  const grupos: Grupo[] = [
    { id: uid(6, 1), nombre: 'FRENOS', line_count: 412, subgrupos: [{ id: uid(7, 1), name: 'DISCOS DE FRENO', line_count: 300 }, { id: uid(7, 2), name: 'PASTILLAS DE FRENO', line_count: 112 }] },
    { id: uid(6, 2), nombre: 'REFRIGERACIÓN', line_count: 0, subgrupos: [] },
  ];
  const writes: string[] = [], unexpected: string[] = [];
  let next = 50;
  await page.route('**/api/**', route => { if (noNewProducts(route)) return; unexpected.push(`${route.request().method()} ${new URL(route.request().url()).pathname}`); return route.fulfill({ status: 404, json: { detail: 'Ruta no prevista' } }); });
  await page.route(/\/api\/management\/roles$/, route => route.fulfill({ json: [] }));
  await page.route(/\/api\/management\/grupos(\/[0-9a-f-]{36})?$/, route => {
    const request = route.request(), id = request.url().match(/grupos\/([0-9a-f-]{36})$/)?.[1];
    if (request.method() === 'GET') return route.fulfill({ json: grupos });
    const body = request.postDataJSON() as { nombre: string };
    writes.push(`${request.method()} grupos${id ? `/${id}` : ''} ${JSON.stringify(body)}`);
    const nombre = body.nombre.toUpperCase();
    if (grupos.some(grupo => grupo.nombre === nombre && grupo.id !== id)) return route.fulfill({ status: 400, json: { nombre: [`Ya existe el grupo ${nombre}.`] } });
    if (id) { const grupo = grupos.find(row => row.id === id)!; grupo.nombre = nombre; return route.fulfill({ json: grupo }); }
    const grupo = { id: uid(6, ++next), nombre, line_count: 0, subgrupos: [] };
    grupos.push(grupo);
    return route.fulfill({ status: 201, json: grupo });
  });
  await page.route(/\/api\/management\/part-types(\/[0-9a-f-]{36})?$/, route => {
    const request = route.request(), id = request.url().match(/part-types\/([0-9a-f-]{36})$/)?.[1];
    const body = request.postDataJSON() as { category: string; name: string };
    writes.push(`${request.method()} part-types${id ? `/${id}` : ''} ${JSON.stringify(body)}`);
    const target = grupos.find(grupo => grupo.nombre === body.category.toUpperCase())!;
    let sub = id ? grupos.flatMap(grupo => grupo.subgrupos).find(row => row.id === id)! : { id: uid(7, ++next), name: '', line_count: 0 };
    for (const grupo of grupos) grupo.subgrupos = grupo.subgrupos.filter(row => row.id !== sub.id);
    sub = { ...sub, name: body.name.toUpperCase() };
    target.subgrupos.push(sub);
    return route.fulfill({ status: id ? 200 : 201, json: { id: sub.id, category: target.nombre, name: sub.name, part_count: sub.line_count, field_count: 0 } });
  });
  return { grupos, writes, unexpected };
}

for (const width of [1440, 390]) {
  test(`each grupo lists its subgrupos and their OEM lines at ${width}px`, async ({ page }) => {
    const mock = await mockGrupos(page);
    await page.setViewportSize({ width, height: 1000 });
    await page.goto('/administracion?seccion=grupos');
    const screen = page.getByRole('region', { name: 'Grupos y subgrupos' });
    await expect(screen.getByText('2 grupos · 2 subgrupos')).toBeVisible();
    const frenos = screen.getByRole('article', { name: 'Grupo FRENOS' });
    await expect(frenos).toContainText('2 subgrupos · 412 líneas OEM');
    await expect(frenos.getByRole('listitem').first()).toHaveText(/DISCOS DE FRENO\s*300/);
    await expect(screen.getByRole('article', { name: 'Grupo REFRIGERACIÓN' })).toContainText('Sin subgrupos.');
    expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)).toBe(false);
    await page.screenshot({ path: `/tmp/motionpartes-grupos-${width}.png` });
    expect(mock.unexpected).toEqual([]);
  });
}

test('a grupo is created and renamed, and a subgrupo added to it and moved to another grupo', async ({ page }) => {
  const mock = await mockGrupos(page);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto('/administracion?seccion=grupos');
  const screen = page.getByRole('region', { name: 'Grupos y subgrupos' });
  await screen.getByRole('button', { name: 'Crear grupo' }).click();
  let dialog = page.getByRole('dialog', { name: 'Crear grupo' });
  await dialog.getByLabel('Nombre del grupo').fill('frenos');
  await dialog.getByRole('button', { name: 'Guardar' }).click();
  await expect(dialog.getByRole('alert')).toHaveText('Nombre: Ya existe el grupo FRENOS.');
  await dialog.getByLabel('Nombre del grupo').fill('suspension');
  await dialog.getByRole('button', { name: 'Guardar' }).click();
  await expect(screen.getByRole('status')).toHaveText('Grupo SUSPENSION creado.');

  await screen.getByRole('button', { name: 'Renombrar grupo SUSPENSION' }).click();
  dialog = page.getByRole('dialog', { name: 'Renombrar grupo · SUSPENSION' });
  await dialog.getByLabel('Nombre del grupo').fill('suspensión');
  await dialog.getByRole('button', { name: 'Guardar' }).click();
  await expect(screen.getByRole('status')).toHaveText('Grupo renombrado a SUSPENSIÓN.');

  await screen.getByRole('button', { name: 'Agregar subgrupo a SUSPENSIÓN' }).click();
  dialog = page.getByRole('dialog', { name: 'Nuevo subgrupo en SUSPENSIÓN' });
  await dialog.getByLabel('Nombre del subgrupo').fill('amortiguadores');
  await dialog.getByRole('button', { name: 'Guardar' }).click();
  await expect(screen.getByRole('status')).toHaveText('Subgrupo SUSPENSIÓN / AMORTIGUADORES creado.');
  await expect(screen.getByRole('article', { name: 'Grupo SUSPENSIÓN' })).toContainText('AMORTIGUADORES');

  await screen.getByRole('button', { name: 'Editar subgrupo FRENOS / PASTILLAS DE FRENO' }).click();
  dialog = page.getByRole('dialog', { name: 'Subgrupo · PASTILLAS DE FRENO' });
  await expect(dialog).toContainText('Cambiar el grupo mueve el subgrupo con sus 112 líneas OEM y su plantilla.');
  await dialog.getByLabel('Grupo del subgrupo').selectOption({ label: 'SUSPENSIÓN' });
  await dialog.getByRole('button', { name: 'Guardar' }).click();
  await expect(screen.getByRole('article', { name: 'Grupo SUSPENSIÓN' })).toContainText('PASTILLAS DE FRENO');
  expect(mock.writes).toEqual(['POST grupos {"nombre":"FRENOS"}', 'POST grupos {"nombre":"SUSPENSION"}', `PATCH grupos/${uid(6, 51)} {"nombre":"SUSPENSIÓN"}`,
    'POST part-types {"category":"SUSPENSIÓN","name":"AMORTIGUADORES"}', `PATCH part-types/${uid(7, 2)} {"category":"SUSPENSIÓN","name":"PASTILLAS DE FRENO"}`]);
  expect(mock.unexpected).toEqual([]);
});
