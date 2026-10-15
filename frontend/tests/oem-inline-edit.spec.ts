import { expect, Page, test } from '@playwright/test';
import type { OEMCrossReference, OEMReferenceDetail, OEMReferencePage } from '../lib/oem-reference-types';

const empty = { count: 0, next: null, previous: null, results: [], pending_count: 0 };
const stamp = '2026-10-08T12:00:00Z';
const ids = { pump: '0e100000-0000-4000-8000-000000000001', disc: '0e100000-0000-4000-8000-000000000002' };
const reference = (id: string, code: string, patch: Partial<OEMReferenceDetail>): OEMReferenceDetail => ({ id, manufacturer: 'TOYOTA', code, printed_forms: [], system: 'TOYOTA',
  family: code.slice(0, 5), part_type: '', description: '', applications: '', status: 'declared', status_label: 'Declarado', dispute_note: '', superseded_by: null, notes: '',
  source_count: 1, source_kinds: { aftermarket_catalog: 1 }, linked_skus: [], linked_count: 2, created_by: null, updated_by: null, created_at: stamp, updated_at: stamp,
  version: 3, sources: [], supersedes: [], cross_reference_count: 0, cross_reference_preview: [], cross_references: [], ...patch });

// Mirrors the library API: the batch saves every row under its version or none (409 with the rows as they are now), alternos are
// added and retired one by one, a dispute needs the version too, and ?ids= re-reads rows.
async function fixture(page: Page) {
  test.skip(!process.env.E2E_ADMIN_TOKEN, 'A disposable superuser token is required for server-rendered administration.');
  await page.context().addCookies([{ name: 'partsmall_session', value: process.env.E2E_ADMIN_TOKEN!, url: 'http://localhost:8080', httpOnly: true, sameSite: 'Strict' }]);
  const rows: Record<string, OEMReferenceDetail> = {
    [ids.pump]: reference(ids.pump, '1610029155', { part_type: 'BOMBAS DE AGUA', description: 'BOMBA AGUA',
      cross_references: [{ id: 41, brand: 'GMB', code: 'GWT-41A', citations: ['CATÁLOGO GMB 2016 · pág. 4'] }] }),
    [ids.disc]: reference(ids.disc, '4351260150', { part_type: 'DISCOS DE FRENO', description: 'DISCO FRENO DEL', version: 5 }),
  };
  const summarize = (row: OEMReferenceDetail) => ({ ...row, cross_reference_count: row.cross_references.length, cross_reference_preview: row.cross_references.slice(0, 3) });
  let next = 100;
  const unexpected: string[] = [], batches: unknown[] = [], alternos: string[] = [], patches: unknown[] = [], rereads: string[] = [];
  await page.route('**/api/**', route => { unexpected.push(`${route.request().method()} ${new URL(route.request().url()).pathname}`); return route.fulfill({ status: 404, json: { detail: 'Ruta no prevista' } }); });
  await page.route('**/api/market/analytics/events', route => route.fulfill({ status: 202, json: { recorded: true } }));
  await page.route(/\/api\/management\/(roles|catalog\/import\/issues)(?:\?.*)?$/, route => route.fulfill({ json: route.request().url().includes('/roles') ? [] : empty }));
  await page.route(/\/api\/management\/oem-references(?:\?.*)?$/, route => {
    const wanted = new URL(route.request().url()).searchParams.get('ids');
    if (wanted) rereads.push(wanted);
    const results = (wanted ? wanted.split(',').map(id => rows[id]).filter(Boolean) : Object.values(rows)).map(summarize);
    const body: OEMReferencePage = { count: results.length, next: null, previous: null, results, counts: { status: { declared: 2 }, manufacturer: { TOYOTA: 2 }, total: 2 } };
    return route.fulfill({ json: body });
  });
  await page.route('**/api/management/oem-references/part-types', route => route.fulfill({ json: { results: ['BOMBAS DE AGUA', 'DISCOS DE FRENO'] } }));
  await page.route('**/api/management/oem-references/bulk-edit', route => {
    const batch = (route.request().postDataJSON() as { rows: (Record<string, unknown> & { id: string; expected_version: number })[] }).rows;
    batches.push(batch);
    const stale = batch.filter(row => rows[row.id].version !== row.expected_version);
    if (stale.length) return route.fulfill({ status: 409, json: { detail: 'No se guardó ningún cambio: otra persona cambió TOYOTA 4351260150 mientras editabas. Revisa sus valores actuales.', rows: stale.map(row => summarize(rows[row.id])) } });
    for (const row of batch) {
      const { id, expected_version: _, ...fields } = row;
      rows[id] = { ...rows[id], ...Object.fromEntries(Object.entries(fields).map(([key, value]) => [key, String(value).toUpperCase()])), version: rows[id].version + 1 };
    }
    return route.fulfill({ json: { updated: batch.map(row => summarize(rows[row.id])) } });
  });
  await page.route(/\/api\/management\/oem-references\/[a-f0-9-]{36}$/, route => {
    const id = new URL(route.request().url()).pathname.split('/').pop()!;
    if (route.request().method() === 'PATCH') {
      const body = route.request().postDataJSON() as { action: string; expected_version: number; note?: string };
      patches.push(body);
      if (body.action === 'dispute') rows[id] = { ...rows[id], status: 'disputed', status_label: 'En disputa', dispute_note: String(body.note).toUpperCase(), version: rows[id].version + 1 };
      return route.fulfill({ json: summarize(rows[id]) });
    }
    return route.fulfill({ json: summarize(rows[id]) });
  });
  await page.route(/\/api\/management\/oem-references\/[a-f0-9-]{36}\/cross-references(?:\/\d+)?$/, route => {
    const parts = new URL(route.request().url()).pathname.split('/'), id = parts[4];
    if (route.request().method() === 'POST') {
      const body = route.request().postDataJSON() as { brand: string; code: string };
      alternos.push(`POST ${body.brand} ${body.code}`);
      const saved: OEMCrossReference = { id: ++next, brand: body.brand.toUpperCase(), code: body.code.toUpperCase(), citations: ['Registro manual · ADMIN'] };
      rows[id] = { ...rows[id], cross_references: [...rows[id].cross_references, saved] };
      return route.fulfill({ status: 201, json: saved });
    }
    const code = Number(parts.at(-1));
    alternos.push(`DELETE ${code}`);
    rows[id] = { ...rows[id], cross_references: rows[id].cross_references.filter(row => row.id !== code) };
    return route.fulfill({ status: 204, body: '' });
  });
  return { rows, unexpected, batches, alternos, patches, rereads };
}

const cell = (page: Page, id: string, column: string) => page.locator(`.ag-center-cols-container .ag-row[row-id="${id}"] .ag-cell[col-id="${column}"]`);
async function editMode(page: Page) {
  await page.getByRole('button', { name: 'Editar celdas', exact: true }).click();
  await expect(page.getByText(/Tipo de pieza y descripción se guardan juntos/)).toBeVisible();
}

test('Editar celdas saves a pasted batch under each row\'s version and flashes what the server kept', async ({ page, context }) => {
  await context.grantPermissions(['clipboard-read', 'clipboard-write']);
  const mock = await fixture(page);
  await page.setViewportSize({ width: 1600, height: 1000 });
  await page.goto('/administracion?seccion=oem');
  await expect(cell(page, ids.pump, 'description')).toHaveText('BOMBA AGUA');
  await cell(page, ids.pump, 'description').dblclick();
  await expect(page.locator('.ag-cell-inline-editing')).toHaveCount(0);  // read-only until Editar celdas
  await editMode(page);
  await expect(cell(page, ids.pump, 'description')).toHaveClass(/editable-cell-active/);
  await page.evaluate(() => navigator.clipboard.writeText('bomba agua toy 22r\ndisco freno del toy'));
  await cell(page, ids.pump, 'description').click();
  await page.keyboard.press('ControlOrMeta+V');
  await expect(page.getByRole('status').filter({ hasText: '2 números guardados.' })).toBeVisible();
  await expect(cell(page, ids.disc, 'description')).toHaveText('DISCO FRENO DEL TOY');
  expect(mock.batches).toEqual([[{ id: ids.pump, expected_version: 3, description: 'bomba agua toy 22r' }, { id: ids.disc, expected_version: 5, description: 'disco freno del toy' }]]);

  await cell(page, ids.disc, 'family').click();
  await page.keyboard.press('Enter');
  await page.getByRole('option', { name: 'BOMBAS DE AGUA' }).click();
  await expect(cell(page, ids.disc, 'family')).toHaveClass(/ag-cell-data-changed/);
  expect(mock.batches[1]).toEqual([{ id: ids.disc, expected_version: 6, part_type: 'BOMBAS DE AGUA' }]);  // the version the save returned
  expect(mock.unexpected).toEqual([]);
});

test('a batch another person overtook saves nothing, puts the cells back and shows what changed', async ({ page }) => {
  const mock = await fixture(page);
  await page.setViewportSize({ width: 1600, height: 1000 });
  await page.goto('/administracion?seccion=oem');
  await editMode(page);
  mock.rows[ids.disc] = { ...mock.rows[ids.disc], description: 'DISCO FRENO DEL (OTRA PERSONA)', version: 6 };  // saved elsewhere meanwhile
  await cell(page, ids.disc, 'description').dblclick();
  await page.keyboard.press('ControlOrMeta+A');
  await page.keyboard.type('disco mio');
  await page.keyboard.press('Enter');
  await expect(page.getByRole('alert').filter({ hasText: 'otra persona cambió' })).toBeVisible();
  await expect(cell(page, ids.disc, 'description')).toHaveText('DISCO FRENO DEL (OTRA PERSONA)');  // re-read after the refusal
  expect(mock.batches).toEqual([[{ id: ids.disc, expected_version: 5, description: 'disco mio' }]]);
  expect(mock.unexpected).toEqual([]);
});

test('the Alternos popup reads and copies without Editar celdas, adds and retires codes with it', async ({ page, context }) => {
  await context.grantPermissions(['clipboard-read', 'clipboard-write']);
  const mock = await fixture(page);
  await page.setViewportSize({ width: 1600, height: 1000 });
  await page.goto('/administracion?seccion=oem');
  await cell(page, ids.pump, 'cross').dblclick();
  const popup = page.getByRole('dialog', { name: 'Alternos de TOYOTA 1610029155' });
  await expect(popup).toContainText('Solo lectura');
  await popup.getByRole('button', { name: /^GWT-41A/ }).click();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe('GWT-41A');
  await expect(popup.getByLabel('Código del alterno')).toHaveCount(0);
  await page.keyboard.press('Escape');

  await editMode(page);
  await cell(page, ids.pump, 'cross').dblclick();
  await popup.getByLabel('Marca del alterno').fill('aisin');
  await page.keyboard.press('Tab');
  await page.keyboard.type('wpt-111');
  await page.keyboard.press('Enter');
  await expect(popup.getByRole('button', { name: /^WPT-111/ })).toBeVisible();
  await popup.getByRole('button', { name: 'Retirar alterno GMB GWT-41A' }).click();
  await expect(popup.getByRole('alert')).toContainText('Lo dejan de ver las 2 líneas OEM vinculadas');
  await popup.getByRole('button', { name: 'Retirar', exact: true }).click();
  await expect(popup.getByRole('button', { name: /^GWT-41A/ })).toHaveCount(0);
  await popup.getByRole('button', { name: 'Cerrar alternos' }).click();
  await expect(cell(page, ids.pump, 'cross')).toContainText('AISIN WPT-111');  // re-read when the popup closed
  await expect(cell(page, ids.pump, 'cross')).toHaveClass(/ag-cell-data-changed/);
  expect(mock.alternos).toEqual(['POST AISIN WPT-111', 'DELETE 41']);
  expect(mock.rereads).toContain(ids.pump);
  expect(mock.unexpected).toEqual([]);
});

test('the Estado popup puts a number in dispute with its reason', async ({ page }) => {
  const mock = await fixture(page);
  await page.setViewportSize({ width: 1600, height: 1000 });
  await page.goto('/administracion?seccion=oem');
  await editMode(page);
  await cell(page, ids.disc, 'status').dblclick();
  const popup = page.getByRole('dialog', { name: 'Estado de TOYOTA 4351260150' });
  await expect(popup.getByRole('button', { name: 'Marcar en disputa' })).toBeDisabled();
  await popup.getByLabel('Motivo de la disputa').fill('el catálogo lo cita para otro modelo');
  await popup.getByRole('button', { name: 'Marcar en disputa' }).click();
  await expect(popup).toHaveCount(0);
  await expect(cell(page, ids.disc, 'status')).toContainText('EN DISPUTA');
  expect(mock.patches).toEqual([{ expected_version: 5, action: 'dispute', note: 'EL CATÁLOGO LO CITA PARA OTRO MODELO' }]);
  expect(mock.unexpected).toEqual([]);
});

test('the OEM edit toggle and popups fit a phone screen', async ({ page }) => {
  await fixture(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/administracion?seccion=oem');
  await editMode(page);
  expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)).toBe(false);
  await cell(page, ids.pump, 'cross').dblclick();
  const popup = page.getByRole('dialog', { name: 'Alternos de TOYOTA 1610029155' });
  await expect(popup.getByRole('button', { name: 'Agregar alterno' })).toBeVisible();
  const box = (await popup.boundingBox())!;  // popups open on the page: whole on screen, without widening it
  expect(box.x).toBeGreaterThanOrEqual(0);
  expect(box.x + box.width).toBeLessThanOrEqual(390);
  expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)).toBe(false);
});
