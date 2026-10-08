import { expect, Page, test } from '@playwright/test';
import type { OEMReferenceDetail, OEMReferencePage, OEMReferenceSource, OEMReferenceStatus, OEMSourceKind } from '../lib/oem-reference-types';

const stamp = '2026-10-07T12:00:00Z';
const empty = { count: 0, next: null, previous: null, results: [], pending_count: 0 };
const base = '/api/management/oem-references';
const ids = { filter: '0e000000-0000-4000-8000-000000000001', mitsubishi: '0e000000-0000-4000-8000-000000000002', honda: '0e000000-0000-4000-8000-000000000003' };
const kindLabels: Record<OEMSourceKind, string> = {
  price_list: 'Lista de precios de distribuidor', manufacturer_catalog: 'Catálogo del fabricante', aftermarket_catalog: 'Catálogo de marca de repuesto',
  ai_lookup: 'Búsqueda OEM con IA', supplier_declaration: 'Declaración de proveedor', catalog_approval: 'Aprobación en el catálogo', oem_finder: 'Buscador OEM',
  manual: 'Registro manual',
};
const statusLabels: Record<OEMReferenceStatus, string> = { verified: 'Verificado', declared: 'Declarado', inferred: 'Inferido', disputed: 'En disputa' };
const compact = (value: string) => value.toUpperCase().replace(/[^0-9A-Z]/g, '');

function source(id: number, kind: OEMSourceKind, citation: string, detail: OEMReferenceSource['detail'] = {}): OEMReferenceSource {
  return { id, kind, kind_label: kindLabels[kind], citation, detail, created_by: kind === 'manual' ? 'ADMIN' : null, created_at: stamp,
    removable: !['oem_finder', 'catalog_approval', 'ai_lookup'].includes(kind) };
}

// Same rule as mall.oem_reference.derived_status.
function derive(sources: OEMReferenceSource[]): OEMReferenceStatus {
  if (sources.some(s => ['price_list', 'manufacturer_catalog'].includes(s.kind) || (s.kind === 'manual' && s.detail.verified) || (s.kind === 'ai_lookup' && s.detail.approved))) return 'verified';
  return sources.some(s => ['aftermarket_catalog', 'supplier_declaration', 'catalog_approval', 'manual'].includes(s.kind)) ? 'declared' : 'inferred';
}

function reference(id: string, manufacturer: string, code: string, patch: Partial<OEMReferenceDetail>): OEMReferenceDetail {
  const row: OEMReferenceDetail = { id, manufacturer, code, printed_forms: [], system: '', family: '', part_type: '', description: '', applications: '', status: 'inferred',
    status_label: 'Inferido', dispute_note: '', superseded_by: null, notes: '', source_count: 0, source_kinds: {}, linked_skus: [], linked_count: 0, created_by: null,
    updated_by: null, created_at: stamp, updated_at: stamp, version: 1, sources: [], supersedes: [], cross_reference_count: 0, cross_reference_preview: [],
    cross_references: [], ...patch };
  row.cross_reference_count = row.cross_references.length;
  row.cross_reference_preview = row.cross_references.slice(0, 3);
  return summarize(row);
}

function summarize(row: OEMReferenceDetail) {
  row.source_count = row.sources.length;
  row.source_kinds = row.sources.reduce<OEMReferenceDetail['source_kinds']>((kinds, s) => ({ ...kinds, [s.kind]: (kinds[s.kind] ?? 0) + 1 }), {});
  if (row.status !== 'disputed') row.status = derive(row.sources);
  row.status_label = statusLabels[row.status];
  return row;
}

// Mirrors the server: list filters and counts, create-or-merge on the compact number, the version check (409) and every action.
async function fixture(page: Page) {
  test.skip(!process.env.E2E_ADMIN_TOKEN, 'A disposable superuser token is required for server-rendered administration.');
  await page.context().addCookies([{ name: 'partsmall_session', value: process.env.E2E_ADMIN_TOKEN!, url: 'http://localhost:8080', httpOnly: true, sameSite: 'Strict' }]);
  let next = 10;
  const rows: Record<string, OEMReferenceDetail> = {
    [ids.filter]: reference(ids.filter, 'TOYOTA', '1780130070', { printed_forms: ['17801-30070'], system: 'TOYOTA', family: '17801', part_type: 'FILTRO AIRE',
      description: 'FILTRO AIRE TOY HILUX 2.7', version: 3, linked_count: 1, sources: [source(1, 'oem_finder', 'algo:oem-finder:v2:current:2',
        { run: 2, rule: 'current', rules_version: 'v2', part_codes: [{ id: 7, part: '05000000-0000-4000-8000-000000000001', code: '17801-30070' }] })],
      linked_skus: [{ id: '05000000-0000-4000-8000-000000000001', sku: '17801-30070', is_OEM: true, active: true, via: 'sku', code: '17801-30070' }] }),
    [ids.mitsubishi]: reference(ids.mitsubishi, 'MITSUBISHI', 'MR968365', { printed_forms: ['MR-968365'], system: 'MITSU_OLD', part_type: 'BASE AMORT',
      sources: [source(2, 'ai_lookup', 'https://parts.example/MR968365', { approved: true })] }),
    [ids.honda]: reference(ids.honda, 'HONDA', '19200PNA003', { printed_forms: ['19200-PNA-003'], system: 'HONDA', family: '19200', part_type: 'BOMBA AGUA',
      sources: [source(3, 'aftermarket_catalog', 'GMB 2025 P. 81')], linked_count: 1,
      linked_skus: [{ id: '05000000-0000-4000-8000-000000000002', sku: '19200-PNA-003-NPW', is_OEM: false, active: true, via: 'sku_name', code: '19200-PNA-003-NPW' }],
      cross_references: [{ id: 1, brand: 'AISIN', code: 'WPH-016', citations: ['GMB 2025 · pág. 81'] }, { id: 2, brand: 'GMB', code: 'GWHO-49A', citations: ['GMB 2025 · pág. 81'] }] }),
  };
  const unexpected: string[] = [], lists: URLSearchParams[] = [], posts: Record<string, unknown>[] = [], patches: { id: string; body: Record<string, unknown> }[] = [];
  await page.route('**/api/**', route => { unexpected.push(`${route.request().method()} ${new URL(route.request().url()).pathname}`); return route.fulfill({ status: 404, json: { detail: 'Ruta no prevista' } }); });
  await page.route('**/api/market/analytics/events', route => route.fulfill({ status: 202, json: { recorded: true } }));
  await page.route(/\/api\/management\/(roles|catalog\/import\/issues|part-types)(?:\?.*)?$/, route => route.fulfill({ json: route.request().url().includes('/roles') ? [] : empty }));
  await page.route(/\/api\/management\/catalog(?:\?.*)?$/, route => route.fulfill({ json: empty }));
  await page.route(/\/api\/management\/oem-references(?:\?.*)?$/, route => {
    if (route.request().method() === 'POST') {
      const body = route.request().postDataJSON() as { manufacturer: string; code: string; part_type?: string; description?: string; source?: { kind: OEMSourceKind; citation: string; verified?: boolean } };
      posts.push(body);
      const existing = Object.values(rows).find(row => row.manufacturer === body.manufacturer && row.code === compact(body.code));
      const row = existing ?? reference(`0e000000-0000-4000-8000-0000000000${++next}`, body.manufacturer, compact(body.code), { part_type: body.part_type || '', description: body.description || '', created_by: 'ADMIN' });
      if (body.code !== row.code && !row.printed_forms.includes(body.code)) row.printed_forms = [...row.printed_forms, body.code];
      const first = body.source ?? { kind: 'manual' as const, citation: 'Registro manual de ADMIN', verified: false };
      if (!row.sources.some(s => s.kind === first.kind && s.citation === first.citation)) row.sources = [...row.sources, source(++next, first.kind, first.citation, first.kind === 'manual' ? { verified: !!first.verified } : {})];
      rows[row.id] = summarize(row);
      return route.fulfill({ status: existing ? 200 : 201, json: row });
    }
    const params = new URL(route.request().url()).searchParams;
    lists.push(params);
    let results = Object.values(rows).sort((a, b) => `${a.manufacturer}${a.code}`.localeCompare(`${b.manufacturer}${b.code}`));
    const q = params.get('q');
    if (q) results = results.filter(row => row.code.startsWith(compact(q)) || row.printed_forms.some(form => form.includes(q)) || row.part_type.includes(q));
    if (params.get('status')) results = results.filter(row => row.status === params.get('status'));
    if (params.get('manufacturer')) results = results.filter(row => row.manufacturer === params.get('manufacturer'));
    if (params.get('linked')) results = results.filter(row => (row.linked_count > 0) === (params.get('linked') === 'true'));
    const all = Object.values(rows), count = (pick: (row: OEMReferenceDetail) => string) => all.reduce<Record<string, number>>((out, row) => ({ ...out, [pick(row)]: (out[pick(row)] ?? 0) + 1 }), {});
    const body: OEMReferencePage = { count: results.length, next: null, previous: null, results, counts: { status: count(row => row.status), manufacturer: count(row => row.manufacturer), total: all.length } };
    return route.fulfill({ json: body });
  });
  await page.route(/\/api\/management\/oem-references\/[a-f0-9-]{36}$/, route => {
    const id = new URL(route.request().url()).pathname.split('/').pop()!, row = rows[id];
    if (!row) return route.fulfill({ status: 404, json: { detail: 'Ese número no está en la biblioteca OEM.' } });
    if (route.request().method() !== 'PATCH') return route.fulfill({ json: row });
    const body = route.request().postDataJSON() as Record<string, unknown> & { action: string; expected_version: number };
    patches.push({ id, body });
    if (body.expected_version !== row.version) return route.fulfill({ status: 409, json: { detail: 'La referencia cambió desde que la revisaste. Revisa sus valores actuales antes de continuar.', reference: row } });
    const src = body.source as { kind: OEMSourceKind; citation: string; verified?: boolean } | undefined;
    if (body.action === 'edit') Object.assign(row, { description: body.description, part_type: body.part_type, applications: body.applications, notes: body.notes });
    if (body.action === 'dispute') Object.assign(row, { status: 'disputed', dispute_note: body.note });
    if (body.action === 'clear_dispute') Object.assign(row, { status: 'inferred', dispute_note: '' });
    if (body.action === 'add_source' && src) row.sources = [...row.sources, source(++next, src.kind, src.citation, src.kind === 'manual' ? { verified: !!src.verified } : {})];
    if (body.action === 'remove_source') row.sources = row.sources.filter(s => s.id !== body.source_id);
    if (body.action === 'set_superseded_by') {
      const code = compact(String(body.code)), maker = String(body.manufacturer || row.manufacturer);
      const target = Object.values(rows).find(item => item.manufacturer === maker && item.code === code)
        ?? reference(`0e000000-0000-4000-8000-0000000000${++next}`, maker, code, { sources: [source(++next, 'manual', `Reemplaza a ${row.manufacturer} ${row.code}`, { verified: false, supersedes: row.id })] });
      rows[target.id] = target;
      target.supersedes = [{ id: row.id, manufacturer: row.manufacturer, code: row.code, status: row.status }];
      row.superseded_by = { id: target.id, manufacturer: target.manufacturer, code: target.code, status: target.status };
    }
    if (body.action === 'clear_superseded_by') row.superseded_by = null;
    row.version += 1;
    row.updated_by = 'ADMIN';
    return route.fulfill({ json: summarize(row) });
  });
  return { rows, unexpected, lists, posts, patches };
}

async function openLibrary(page: Page, width: number) {
  await page.setViewportSize({ width, height: 1000 });
  await page.goto('/administracion?seccion=inventario');
  await page.getByRole('button', { name: 'Biblioteca OEM', exact: true }).click();
  return page.getByRole('dialog', { name: 'Biblioteca OEM' });
}

async function fitsWidth(page: Page) {
  const dialog = page.getByRole('dialog', { name: 'Biblioteca OEM' });
  expect(await dialog.evaluate(el => el.scrollWidth > el.clientWidth)).toBe(false);
}

for (const width of [1440, 390]) {
  test(`the OEM library lists compact numbers with status, sources and linked SKUs, filters them and opens one at ${width}px`, async ({ page }) => {
    const mock = await fixture(page);
    const dialog = await openLibrary(page, width);
    await expect(dialog.getByLabel('Resumen de la biblioteca')).toHaveText('BIBLIOTECA: 3 NÚMEROS · 1 VERIFICADO · 1 DECLARADO · 1 INFERIDO · 0 EN DISPUTA');
    const table = dialog.locator('.oem-library-table');  // the filters list the same labels as options
    await expect(table.getByText('1780130070', { exact: true })).toBeVisible();
    await expect(table.getByText('17801-30070', { exact: true })).toHaveCount(2);  // printed form and linked SKU
    await expect(table.getByText('BUSCADOR OEM', { exact: true })).toBeVisible();
    await expect(table.getByText('INFERIDO', { exact: true })).toBeVisible();
    await expect(table.getByText('CATÁLOGO DE REPUESTO', { exact: true })).toBeVisible();
    await expect(table.getByText('GMB GWHO-49A', { exact: true })).toBeVisible();  // aftermarket codes filed under the number
    await fitsWidth(page);
    expect(await dialog.locator('.suffix-table-wrap').evaluate(el => el.scrollWidth > el.clientWidth)).toBe(false);
    await page.screenshot({ path: `/tmp/motionpartes-oem-library-${width}.png` });
    await dialog.getByLabel('Estado', { exact: true }).selectOption('verified');
    await expect.poll(() => mock.lists.at(-1)?.get('status')).toBe('verified');
    await expect(dialog.getByText('MR968365', { exact: true })).toBeVisible();
    await expect(dialog.getByText('1780130070', { exact: true })).toHaveCount(0);
    await dialog.getByLabel('Estado', { exact: true }).selectOption('');
    await dialog.getByLabel('SKU vinculados', { exact: true }).selectOption('false');
    await expect.poll(() => mock.lists.at(-1)?.get('linked')).toBe('false');
    await dialog.getByLabel('SKU vinculados', { exact: true }).selectOption('');
    await dialog.getByLabel('Buscar número OEM', { exact: true }).fill('17801-3');
    await expect.poll(() => mock.lists.at(-1)?.get('q')).toBe('17801-3');
    await expect(dialog.getByText('MR968365', { exact: true })).toHaveCount(0);
    await dialog.getByRole('button', { name: 'Ver TOYOTA 1780130070', exact: true }).click();
    await expect(dialog.getByRole('heading', { name: '1780130070', exact: true })).toBeVisible();
    const sources = dialog.getByRole('region', { name: 'Fuentes' });
    await expect(sources.getByText('Buscador OEM', { exact: true })).toBeVisible();
    await expect(sources.getByText('algo:oem-finder:v2:current:2', { exact: true })).toBeVisible();
    await expect(sources.getByText('CORRIDA 2 · REGLA current · v2', { exact: true })).toBeVisible();
    await expect(sources.getByRole('button', { name: /^Quitar fuente/ })).toHaveCount(0);  // automatic sources follow the catalog
    await expect(dialog.getByRole('region', { name: 'SKU vinculados' }).getByText('SKU PRINCIPAL · MAIN OEM', { exact: true })).toBeVisible();
    await expect(dialog.getByRole('region', { name: 'Equivalencias de posventa' }).getByText('Ningún catálogo de marca de repuesto imprime equivalencias de este número.', { exact: true })).toBeVisible();
    await fitsWidth(page);
    await page.screenshot({ path: `/tmp/motionpartes-oem-reference-${width}.png` });
    await dialog.getByRole('button', { name: 'Volver a la biblioteca', exact: true }).click();
    await dialog.getByLabel('Buscar número OEM', { exact: true }).fill('');
    await dialog.getByRole('button', { name: 'Ver HONDA 19200PNA003', exact: true }).click();
    const codes = dialog.getByRole('region', { name: 'Equivalencias de posventa' });
    await expect(codes.getByRole('heading', { name: 'Equivalencias de posventa (2)', exact: true })).toBeVisible();
    await expect(codes.getByText('GMB GWHO-49A', { exact: true })).toBeVisible();
    await expect(dialog.getByRole('region', { name: 'SKU vinculados' }).getByText('NOMBRE DEL SKU', { exact: true })).toBeVisible();
    await fitsWidth(page);
    await dialog.getByRole('button', { name: 'Volver a la biblioteca', exact: true }).click();
    await expect(dialog.getByRole('button', { name: 'Ver TOYOTA 1780130070', exact: true })).toBeVisible();
    expect(mock.unexpected).toEqual([]);
  });

  test(`the detail disputes a number, adds and removes a manual source and records a supersession at ${width}px`, async ({ page }) => {
    const mock = await fixture(page);
    const dialog = await openLibrary(page, width);
    await dialog.getByRole('button', { name: 'Ver TOYOTA 1780130070', exact: true }).click();
    await dialog.getByLabel('Motivo de la disputa', { exact: true }).fill('Es filtro de cabina según el EPC.');
    await dialog.getByRole('button', { name: 'Marcar en disputa', exact: true }).click();
    await expect(dialog.getByRole('status')).toHaveText('Número marcado en disputa.');
    await expect(dialog.getByText('Es filtro de cabina según el EPC.', { exact: true })).toBeVisible();
    await expect(dialog.getByText('EN DISPUTA', { exact: true })).toBeVisible();
    await dialog.getByRole('button', { name: 'Quitar disputa', exact: true }).click();
    await expect(dialog.getByRole('status')).toHaveText('Disputa quitada. El estado vuelve a seguir a las fuentes.');
    await dialog.getByLabel('Cita de la fuente', { exact: true }).fill('EPC TOYOTA 2026 P. 12');
    await dialog.getByRole('checkbox', { name: 'Lo comprobé en una fuente primaria', exact: true }).check();
    await dialog.getByRole('button', { name: 'Agregar fuente', exact: true }).click();
    await expect(dialog.getByRole('status')).toHaveText('Fuente agregada.');
    await expect(dialog.getByText('VERIFICADO', { exact: true })).toBeVisible();
    await dialog.getByRole('button', { name: 'Quitar fuente Registro manual EPC TOYOTA 2026 P. 12', exact: true }).click();
    await expect(dialog.getByRole('status')).toHaveText('Fuente quitada.');
    await dialog.getByLabel('Número que lo reemplaza', { exact: true }).fill('17801-30080');
    await dialog.getByRole('button', { name: 'Indicar reemplazo', exact: true }).click();
    await expect(dialog.getByText('REEMPLAZADO POR TOYOTA 1780130080 · DECLARADO', { exact: true })).toBeVisible();
    await fitsWidth(page);
    await dialog.getByRole('button', { name: 'Ver 1780130080', exact: true }).click();
    await expect(dialog.getByText('REEMPLAZA A TOYOTA 1780130070 · INFERIDO', { exact: true })).toBeVisible();
    await dialog.getByRole('button', { name: 'Ver 1780130070', exact: true }).click();
    await dialog.getByRole('button', { name: 'Quitar reemplazo', exact: true }).click();
    await expect(dialog.getByRole('status')).toHaveText('Reemplazo quitado.');
    const sourceId = mock.patches.find(item => item.body.action === 'remove_source')?.body.source_id;
    expect(mock.patches.map(item => item.body)).toEqual([
      { action: 'dispute', note: 'Es filtro de cabina según el EPC.', expected_version: 3 },
      { action: 'clear_dispute', expected_version: 4 },
      { action: 'add_source', source: { kind: 'manual', citation: 'EPC TOYOTA 2026 P. 12', verified: true }, expected_version: 5 },
      { action: 'remove_source', source_id: sourceId, expected_version: 6 },
      { action: 'set_superseded_by', manufacturer: 'TOYOTA', code: '17801-30080', expected_version: 7 },
      { action: 'clear_superseded_by', expected_version: 8 }]);
    expect(mock.unexpected).toEqual([]);
  });
}

test('adding a number stores it compact, or merges it into the number already in the library', async ({ page }) => {
  const mock = await fixture(page);
  const dialog = await openLibrary(page, 1440);
  await dialog.getByRole('button', { name: 'Agregar número', exact: true }).click();
  await dialog.getByLabel('Fabricante', { exact: true }).fill('toyota');
  await dialog.getByLabel('Número OEM', { exact: true }).fill('17801-30080');
  await dialog.getByLabel('Tipo de pieza', { exact: true }).fill('filtro aire');
  await dialog.getByRole('combobox', { name: 'Tipo de fuente', exact: true }).selectOption('price_list');
  await dialog.getByLabel('Cita', { exact: true }).fill('LISTA TOYOTA 2026 P. 4');
  await dialog.getByRole('button', { name: 'Agregar a la biblioteca', exact: true }).click();
  await expect(dialog.getByRole('status')).toHaveText('Número agregado a la biblioteca.');
  await expect(dialog.getByRole('heading', { name: '1780130080', exact: true })).toBeVisible();
  await expect(dialog.getByText('VERIFICADO', { exact: true })).toBeVisible();
  await dialog.getByRole('button', { name: 'Volver a la biblioteca', exact: true }).click();
  await dialog.getByRole('button', { name: 'Agregar número', exact: true }).click();
  await dialog.getByLabel('Fabricante', { exact: true }).fill('TOYOTA');
  await dialog.getByLabel('Número OEM', { exact: true }).fill('17801 30070');
  await dialog.getByRole('button', { name: 'Agregar a la biblioteca', exact: true }).click();
  await expect(dialog.getByRole('status')).toHaveText('El número ya estaba en la biblioteca: se unieron la forma escrita y la fuente.');
  await expect(dialog.getByText('17801-30070 · 17801 30070', { exact: true })).toBeVisible();
  expect(mock.posts).toEqual([
    { manufacturer: 'TOYOTA', code: '17801-30080', description: '', part_type: 'FILTRO AIRE', applications: '', source: { kind: 'price_list', citation: 'LISTA TOYOTA 2026 P. 4' } },
    { manufacturer: 'TOYOTA', code: '17801 30070', description: '', part_type: '', applications: '' }]);
  expect(mock.unexpected).toEqual([]);
});

test('a stale version shows the current reference instead of overwriting it', async ({ page }) => {
  const mock = await fixture(page);
  const dialog = await openLibrary(page, 1440);
  await dialog.getByRole('button', { name: 'Ver TOYOTA 1780130070', exact: true }).click();
  await expect(dialog.getByLabel('Descripción', { exact: true })).toHaveValue('FILTRO AIRE TOY HILUX 2.7');
  Object.assign(mock.rows[ids.filter], { version: 9, description: 'FILTRO AIRE MOTOR 2TR' });
  await dialog.getByLabel('Descripción', { exact: true }).fill('otra descripción');
  await dialog.getByRole('button', { name: 'Guardar datos', exact: true }).click();
  await expect(dialog.getByRole('alert')).toHaveText('La referencia cambió desde que la revisaste. Revisa sus valores actuales antes de continuar. Ya ves la versión actual.');
  await expect(dialog.getByLabel('Descripción', { exact: true })).toHaveValue('FILTRO AIRE MOTOR 2TR');
  expect(mock.patches.at(-1)?.body.expected_version).toBe(3);
  expect(mock.unexpected).toEqual([]);
});
