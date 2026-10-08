import { expect, Page, test } from '@playwright/test';
import type { CatalogSuffix, CatalogSuffixDetail, CatalogSuffixPage, CompanySuffixes } from '../lib/suffix-types';
import type { ManagedPart } from '../lib/types';

const stamp = '2026-10-06T12:00:00Z';
const empty = { count: 0, next: null, previous: null, results: [], pending_count: 0 };
const prefix = '/api/management/catalog/suffixes/';

function suffix(token: string, patch: Partial<CatalogSuffix> = {}): CatalogSuffix {
  return { token, match: 'exact', alias_of: null, aliases: [], cls: 'UNKNOWN', tag_kind: '', variant_kind: '', attribution: '', creates_company_reference: false,
    scope_overrides: [], confidence: 'low', status: 'needs_owner_label', owner_confirmed: false, confirmed_by: null, confirmed_at: null, auto_eligible: false,
    proposed_label: '', legacy_company_registry: false, notes: '', stats: { count_all: 0, count_in_stock: 0, examples: [] }, updated_at: stamp, version: 1, ...patch };
}

// Mirrors the server: list filters, one-click labels, version checks (409) and an audit entry per edit.
async function fixture(page: Page, { parts = [] as ManagedPart[], company = { suffixes: { FEB: 'FEBEST', FEBEST: 'FEBEST' }, registry: 'table' } as CompanySuffixes } = {}) {
  test.skip(!process.env.E2E_ADMIN_TOKEN, 'A disposable superuser token is required for server-rendered administration.');
  await page.context().addCookies([{ name: 'partsmall_session', value: process.env.E2E_ADMIN_TOKEN!, url: 'http://localhost:8080', httpOnly: true, sameSite: 'Strict' }]);
  const rows: Record<string, CatalogSuffix> = {
    U: suffix('U', { proposed_label: 'TAG brand UNION (Union Sangyo filters); all seen on FILTRO rows',
      stats: { count_all: 56, count_in_stock: 41, examples: ['8259-23-802-U :: FILTRO ACEITE KIA R2', 'LF01-14-302-U :: FILTRO ACEITE MAZ 3'],
        run: { chain_rows_all: 47, unknown_unlock_if_labeled_TAG: { skus_blocked_all: 47, unlock_alone_all: 24 } } } }),
    'SPK/WAP': suffix('SPK/WAP', { cls: 'TAG', tag_kind: 'brand', attribution: 'brand: SPK or WAP', creates_company_reference: true, confidence: 'medium',
      status: 'owner_confirmed', owner_confirmed: true, confirmed_by: 'ADMIN', auto_eligible: true, version: 7,
      stats: { count_all: 300, count_in_stock: 269, examples: ['54500-07160-SPK/WAP :: TIJERA INF HYU'], run: { owner_confirmation_effect: { auto_if_all_listed_confirmed_all: 12 } } } }),
    FEB: suffix('FEB', { cls: 'TAG', tag_kind: 'company_code', attribution: 'brand: FEBEST', creates_company_reference: true, confidence: 'high',
      status: 'seed_confirmed', auto_eligible: true, legacy_company_registry: true, stats: { count_all: 233, count_in_stock: 173, examples: [] } }),
  };
  const unexpected: string[] = [], lists: URLSearchParams[] = [], paths: string[] = [], patches: { token: string; body: Record<string, unknown> }[] = [];
  let version = 20;
  await page.route('**/api/**', route => { unexpected.push(`${route.request().method()} ${new URL(route.request().url()).pathname}`); return route.fulfill({ status: 404, json: { detail: 'Ruta no prevista' } }); });
  await page.route('**/api/market/analytics/events', route => route.fulfill({ status: 202, json: { recorded: true } }));
  await page.route(/\/api\/management\/(roles|catalog\/import\/issues|part-types)(?:\?.*)?$/, route => route.fulfill({ json: route.request().url().includes('/roles') ? [] : empty }));
  await page.route(/\/api\/management\/catalog(?:\?.*)?$/, route => route.fulfill({ json: { ...empty, count: parts.length, results: parts } }));
  await page.route('**/api/management/catalog/company-suffixes', route => route.fulfill({ json: company }));
  await page.route(/\/api\/management\/catalog\/[0-9a-f-]{36}\/oem-equivalents$/, route => route.fulfill({ json: { numbers: [], cross_references: [] } }));
  await page.route(/\/api\/management\/catalog\/suffixes(?:\?.*)?$/, route => {
    const params = new URL(route.request().url()).searchParams;
    lists.push(params);
    let results = Object.values(rows);
    if (params.get('class')) results = results.filter(row => row.cls === params.get('class'));
    if (params.get('has_unlock') === 'true') results = results.filter(row => row.stats.run?.unknown_unlock_if_labeled_TAG?.unlock_alone_all);
    const body: CatalogSuffixPage = { count: results.length, next: null, previous: null, results, version,
      counts: { cls: { TAG: 2, UNKNOWN: 1 }, status: { needs_owner_label: 1, owner_confirmed: 1, seed_confirmed: 1 }, has_unlock: 1 }, company_suffixes: { FEB: 'FEBEST', FEBEST: 'FEBEST' } };
    return route.fulfill({ json: body });
  });
  await page.route(/\/api\/management\/catalog\/suffixes\/.+$/, route => {
    const path = new URL(route.request().url()).pathname, token = decodeURIComponent(path.slice(prefix.length));
    paths.push(path);
    const row = rows[token];
    if (!row) return route.fulfill({ status: 404, json: { detail: 'Ese sufijo no está en la tabla.' } });
    if (route.request().method() === 'PATCH') {
      const body = route.request().postDataJSON() as Record<string, unknown>;
      patches.push({ token, body });
      if (body.version !== row.version) return route.fulfill({ status: 409, json: { detail: 'El sufijo cambió. Actualiza la tabla antes de continuar.' } });
      const next: CatalogSuffix = { ...row, version: ++version };
      if (body.action === 'confirm_tag') Object.assign(next, { cls: 'TAG', tag_kind: body.tag_kind || 'brand', variant_kind: '', attribution: body.attribution ?? `brand: ${token}`, status: 'owner_confirmed', owner_confirmed: true, auto_eligible: true });
      if (body.action === 'set_variant') Object.assign(next, { cls: 'VARIANT', tag_kind: '', variant_kind: body.variant_kind || 'spec', status: 'owner_confirmed', owner_confirmed: true, auto_eligible: false });
      if (body.action === 'add_alias') next.aliases = [...row.aliases, String(body.alias)];
      if (body.action === 'add_scope_override') next.scope_overrides = [...row.scope_overrides, { heads: body.heads as string[], cls: body.cls as CatalogSuffix['cls'], kind: String(body.kind), attribution: String(body.attribution), confidence: 'high' }];
      rows[token] = next;
    }
    const detail: CatalogSuffixDetail = { ...rows[token], changes: patches.filter(item => item.token === token).reverse().map(item => ({ action: String(item.body.action), actor: 'ADMIN', before: {}, after: {}, note: '', version, created_at: stamp })) };
    return route.fulfill({ json: detail });
  });
  return { rows, unexpected, lists, paths, patches };
}

async function openSuffixes(page: Page, width: number) {
  await page.setViewportSize({ width, height: 1000 });
  await page.goto('/administracion?seccion=inventario');
  await page.getByRole('button', { name: 'Sufijos', exact: true }).click();
  return page.getByRole('dialog', { name: 'Sufijos de códigos' });
}

for (const width of [1440, 390]) {
  test(`suffix table filters, shows impact and labels a token in one click at ${width}px`, async ({ page }) => {
    const mock = await fixture(page);
    const dialog = await openSuffixes(page, width);
    await expect(dialog.getByText('BLOQUEA 47 / DESBLOQUEA 24', { exact: true })).toBeVisible();
    await expect(dialog.getByText('AUTO SI CONFIRMA 12', { exact: true })).toBeVisible();
    await expect(dialog.getByText('Crean referencias de empresa: -FEB → FEBEST · -FEBEST → FEBEST.', { exact: true })).toBeVisible();
    await expect(dialog.getByText('8259-23-802-U :: FILTRO ACEITE KIA R2', { exact: true })).toBeVisible();
    await expect(dialog.getByRole('button', { name: 'Confirmar TAG SPK/WAP', exact: true })).toBeDisabled();
    await expect(dialog.getByRole('button', { name: 'Desconocido U', exact: true })).toBeDisabled();
    expect(await dialog.evaluate(el => el.scrollWidth > el.clientWidth)).toBe(false);
    // Desktop keeps a table; at phone width each suffix is a card that fits without sideways scrolling.
    expect(await dialog.locator('.suffix-table-wrap').evaluate(el => el.scrollWidth > el.clientWidth)).toBe(false);
    await page.screenshot({ path: `/tmp/motionpartes-suffixes-${width}.png` });
    await dialog.getByLabel('Clase', { exact: true }).selectOption('UNKNOWN');
    await expect.poll(() => mock.lists.at(-1)?.get('class')).toBe('UNKNOWN');
    await expect(dialog.getByText('SPK/WAP', { exact: true })).toHaveCount(0);
    await dialog.getByRole('checkbox', { name: 'SOLO LOS QUE DESBLOQUEAN SKU (1)', exact: true }).check();
    await expect.poll(() => mock.lists.at(-1)?.get('has_unlock')).toBe('true');
    await dialog.getByRole('button', { name: 'Confirmar TAG U', exact: true }).click();
    await expect(dialog.getByRole('status')).toHaveText('U confirmado como etiqueta. La próxima revisión OEM usará este cambio.');
    expect(mock.patches).toEqual([{ token: 'U', body: { action: 'confirm_tag', version: 1 } }]);
    await expect(dialog.getByText('No hay sufijos con estos filtros.', { exact: true })).toBeVisible();
    await dialog.getByLabel('Clase', { exact: true }).selectOption('');
    await dialog.getByRole('checkbox', { name: 'SOLO LOS QUE DESBLOQUEAN SKU (1)', exact: true }).uncheck();
    await dialog.getByRole('button', { name: 'Es VARIANTE FEB', exact: true }).click();
    await expect(dialog.getByRole('status')).toHaveText('FEB marcado como variante. La próxima revisión OEM usará este cambio.');
    expect(mock.patches.at(-1)).toEqual({ token: 'FEB', body: { action: 'set_variant', version: 1 } });
    expect(mock.unexpected).toEqual([]);
  });

  test(`suffix editor adds an alias, a scope override and keeps the history at ${width}px`, async ({ page }) => {
    const mock = await fixture(page);
    const dialog = await openSuffixes(page, width);
    await dialog.getByRole('button', { name: 'Editar sufijo SPK/WAP', exact: true }).click();
    await expect(dialog.getByRole('heading', { name: 'SPK/WAP', exact: true })).toBeVisible();
    // The token travels encoded in one path segment (a dev server may request it twice under StrictMode).
    expect([...new Set(mock.paths)]).toEqual([`${prefix}SPK%2FWAP`]);
    await dialog.getByLabel('Nuevo alias', { exact: true }).fill('spkwap');
    await dialog.getByRole('button', { name: 'Agregar alias', exact: true }).click();
    await expect(dialog.getByText('Otras escrituras: SPKWAP.', { exact: true })).toBeVisible();
    await dialog.getByLabel('Sustantivos (separados por comas)', { exact: true }).fill('tijera, brazo');
    await dialog.getByRole('combobox', { name: 'Clase en el alcance', exact: true }).selectOption('VARIANT');
    await dialog.getByRole('combobox', { name: 'Tipo en el alcance', exact: true }).selectOption('position');
    await dialog.getByRole('button', { name: 'Agregar alcance', exact: true }).click();
    await expect(dialog.getByText('TIJERA, BRAZO → VARIANTE · POSICIÓN', { exact: true })).toBeVisible();
    await dialog.getByLabel('Atribución', { exact: true }).fill('brand: SPK');
    await dialog.getByRole('button', { name: 'Guardar clasificación', exact: true }).click();
    await expect(dialog.getByRole('status')).toHaveText('Clasificación guardada.');
    expect(mock.patches.map(item => item.body)).toEqual([
      { action: 'add_alias', alias: 'SPKWAP', version: 7 },
      { action: 'add_scope_override', heads: ['TIJERA', 'BRAZO'], cls: 'VARIANT', kind: 'position', attribution: '', version: 21 },
      { action: 'confirm_tag', attribution: 'brand: SPK', tag_kind: 'brand', creates_company_reference: true, version: 22 }]);
    await expect(dialog.getByText('Alias agregado', { exact: true })).toBeVisible();
    await expect(dialog.getByText('Alcance agregado', { exact: true })).toBeVisible();
    expect(await dialog.evaluate(el => el.scrollWidth > el.clientWidth)).toBe(false);
    await page.screenshot({ path: `/tmp/motionpartes-suffix-editor-${width}.png` });
    await dialog.getByRole('button', { name: 'Volver a la tabla', exact: true }).click();
    await expect(dialog.getByRole('button', { name: 'Editar sufijo SPK/WAP', exact: true })).toBeVisible();
    expect(mock.unexpected).toEqual([]);
  });
}

test('a stale version is reported instead of overwriting the newer label', async ({ page }) => {
  const mock = await fixture(page);
  const dialog = await openSuffixes(page, 1440);
  mock.rows.U = { ...mock.rows.U, version: 30 };
  await dialog.getByRole('button', { name: 'Es VARIANTE U', exact: true }).click();
  await expect(dialog.getByRole('alert')).toHaveText('El sufijo cambió. Actualiza la tabla antes de continuar.');
  await expect(dialog.getByRole('button', { name: 'Es VARIANTE U', exact: true })).toBeEnabled();
  expect(mock.unexpected).toEqual([]);
});

test('the OEM lookup reads company suffixes from the suffix table', async ({ page }) => {
  const part: ManagedPart = { id: '05000000-0000-4000-8000-000000000001', sku: 'ABC-123456-ACME', name: '', description: 'BOMBA AGUA', active: true,
    stock_record_count: 0, codes: [], identity: { ref_type: 'company', status: 'needs_oem', brand: 'ACME CORP' } };
  const mock = await fixture(page, { parts: [part], company: { suffixes: { ACME: 'acme corp' }, registry: 'table' } });
  await page.goto('/administracion?seccion=inventario');
  await page.getByRole('button', { name: `Editar SKU ${part.sku}`, exact: true }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByText('Buscar referencias OEM con IA', { exact: true }).click();
  await expect(dialog.getByLabel('Empresa del código', { exact: true })).toHaveValue('ACME CORP');
  await expect(dialog.getByLabel('Código de empresa', { exact: true })).toHaveValue('ABC-123456');
  expect(mock.unexpected).toEqual([]);
});
