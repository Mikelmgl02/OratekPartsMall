import { test, expect, type Page } from '@playwright/test';
import type { CaseBatchIndexRow, CaseRow, MatchingCase, MergeDigestRow } from '../lib/line-types';
import { noNewProducts } from './new-products-route';

// Casos de coincidencia (plan S8.4): one card per case kind, decided on management/cases/<id> with the case's fingerprint and, where a line
// is chosen, the line's id; kind tabs with their counts; lote batches decided together (each case on its own); and the digest of the
// automatic merges with Separar SKU. The backend is mocked in the browser (the proxy's allowlist is admin-proxy-routes' job).
type Code = { brand: string; code: string };
const uuid = (group: number, n: number) => `0${group}000000-0000-4000-8000-${String(n).padStart(12, '0')}`;
const fp = (n: number) => String(n).repeat(64).slice(0, 64);
const item = (n: number, sku: string, brand: string, description: string, extra: Partial<CaseRow> = {}): CaseRow => ({ id: uuid(1, n), sku, brand, description, supplier_invent_id: `SUP-${n}`, references: [], ...extra });
const line = (n: number, sku: string, description: string, extra: Partial<CaseRow> & { codes?: Code[] } = {}): CaseRow & { estado: string } => ({ id: uuid(2, n), sku, name: sku, description, codes: [], estado: 'publicada', ...extra });
const base = (n: number, kind: string, extra: Partial<MatchingCase>): MatchingCase => ({ id: uuid(3, n), kind, sources: [], candidates: [], target_sku: '', reason: '', status: 'review',
  fingerprint: fp(n % 10), supplier_name: 'REPUESTOS DEL ISTMO', ai: {}, route: 'caso', batch_key: '', ...extra });

const drum = 'TAMBOR HYUNDAI ACCENT 11-16';
const L = {
  accent: line(1, '58411-1R000', drum, { codes: [{ code: '58-1R0', brand: '' }] }), accentNew: line(2, '58411-1R100', 'TAMBOR HYUNDAI ACCENT 17-20'), gmb: line(3, 'D-HYU-1R', drum),
  disc: line(4, '43512-60150', 'DISCO FRENO DEL TOY LAND CRUISER 300MM'), clutchNis: line(5, 'EC-01', 'KIT EMBRAGUE NISSAN FRONTIER D22'),
  clutchHyu: line(6, 'EC01', 'KIT EMBRAGUE HYUNDAI H100', { estado: 'preparacion' }), sealAs: line(7, '04111-22152-AS', 'JUEGO EMPAQUES MOTOR TOY 4AFE'),
  seal: line(8, '04111-22152', 'JUEGO EMPAQUES MOTOR TOY 4AFE'), lampL: line(9, '81560-0K010', 'FARO TRASERO TOY HILUX IZQ'), lampR: line(10, '81550-0K010', 'FARO TRASERO TOY HILUX DER'),
  picked: line(11, 'EC-01-NIS', 'KIT EMBRAGUE NISSAN FRONTIER D22 4X4'),
};
const cases = {
  codeChange: base(1, 'code_change', { sources: [item(1, '58411-1R000', 'HYUNDAI', drum, { codigo_pendiente: '58411-1R100', brand_pendiente: null })], lines: [L.accentNew],
    new_code: '58411-1R100', new_brand: 'HYUNDAI', reference: { id: 501, code: '58411-1R000', brand: 'HYUNDAI', tipo: 'oem', line: L.accent.id, line_sku: '58411-1R000' },
    reason: 'El proveedor cambió el código de este artículo y el código nuevo está en otra línea OEM.' }),
  reference: base(2, 'reference', { sources: [item(2, 'D-HYU-1R', 'GMB', drum, { references: [{ code: '58411-1R000', brand: 'HYUNDAI' }] })], lines: [L.gmb, L.accent],
    new_code: '58411-1R000', new_brand: 'HYUNDAI', reference: { id: 502, code: 'D-HYU-1R', brand: 'GMB', tipo: 'aftermarket', line: L.gmb.id, line_sku: 'D-HYU-1R' },
    reason: 'El proveedor declaró una referencia que no está en la línea OEM del artículo.' }),
  variant: base(3, 'variant', { sources: [item(3, '43512-60150', 'TOYOTA', 'DISCO FRENO DEL TOY LAND CRUISER 280MM')], lines: [L.disc], target_sku: L.disc.sku, guard: 'spec_conflict',
    reason: 'El diámetro del artículo (280 MM) no coincide con el de la línea OEM (300 MM).' }),
  line: base(4, 'line', { sources: [item(4, 'EC-01', '', 'KIT EMBRAGUE NISSAN FRONTIER')], lines: [L.clutchNis, L.clutchHyu], reason: 'El código sin marca está en 2 líneas OEM.',
    ai: { target_sku: 'EC-01', reason: 'La descripción del artículo nombra NISSAN FRONTIER.', confidence: .7 } }),
  merge: base(5, 'merge', { supplier_name: '', lines: [L.sealAs, L.seal], target_sku: L.seal.sku, route: 'lote', batch_key: 'legacy-auto-merge',
    evidence: { numbers: ['04111-22152'], codes: [{ brand: 'AS', code: '04111-22152-AS' }] }, reason: 'Unificación automática anterior que las reglas nuevas no harían.' }),
  // A merge case written before the lines field: its lines come from sources and candidates.
  keep: base(6, 'merge', { supplier_name: '', sources: [L.lampL], candidates: [L.lampR], target_sku: L.lampR.sku, guard: 'side', reason: 'Comparten el número 81550-0K010 en un catálogo.' }),
};
const legacyBatch = [5, 7, 8].map(n => base(n, 'merge', { supplier_name: '', lines: [line(20 + n, `0411${n}-22152-AS`, 'JUEGO EMPAQUES'), line(30 + n, `0411${n}-22152`, 'JUEGO EMPAQUES')],
  target_sku: `0411${n}-22152`, route: 'lote', batch_key: 'legacy-auto-merge', fingerprint: fp(n), reason: 'Unificación automática anterior que las reglas nuevas no harían.' }));
const digest: MergeDigestRow[] = [
  { id: uuid(4, 1), created_at: '2026-10-14T22:30:00Z', reason: 'compact_duplicate', target: { id: L.seal.id, sku: '04111-22152', description: 'JUEGO EMPAQUES MOTOR TOY 4AFE', estado: 'publicada' },
    sources: [{ id: L.sealAs.id, sku: '0411122152', still_merged: true }, { id: uuid(2, 40), sku: '04111 22152', still_merged: false }], split: `catalog/${L.seal.id}/split` },
  { id: uuid(4, 2), created_at: '2026-10-13T10:00:00Z', reason: '', target: { id: L.lampR.id, sku: '81550-0K010', description: 'FARO', estado: 'retirada' },
    sources: [{ id: uuid(2, 41), sku: '81550-0K010-X', still_merged: false }], split: `catalog/${L.lampR.id}/split` },
];

async function mockCases(page: Page, list: MatchingCase[], { refuse = false }: { refuse?: boolean } = {}) {
  test.skip(!process.env.E2E_ADMIN_TOKEN, 'Disposable superuser token required.');
  await page.context().addCookies([{ name: 'partsmall_session', value: process.env.E2E_ADMIN_TOKEN!, url: 'http://localhost:8080', httpOnly: true, sameSite: 'Strict' }]);
  const state = new Map(list.map(row => [row.id, structuredClone(row)]));
  const payloads: Record<string, unknown[]> = {}, queries: string[] = [], batches: unknown[] = [], unexpected: string[] = [], digests: string[] = [];
  await page.route('**/api/**', route => { if (noNewProducts(route)) return; unexpected.push(`${route.request().method()} ${new URL(route.request().url()).pathname}`); return route.fulfill({ status: 404, json: { detail: 'Ruta no prevista' } }); });
  await page.route(/\/api\/management\/(roles|catalog\/import\/issues)(?:\?.*)?$/, route => route.fulfill({ json: route.request().url().includes('/roles') ? [] : { count: 0, results: [] } }));
  await page.route(/\/api\/management\/catalog(?:\?.*)?$/, route => {  // the line search of "Buscar otra línea OEM" (PartPicker)
    const search = (new URL(route.request().url()).searchParams.get('search') || '').toUpperCase();
    const results = [L.picked].filter(row => search && row.sku.includes(search)).map(row => ({ ...row, active: true, is_OEM: false, category: '', subcategory: '', part_type: null, stock_record_count: 0, images: [] }));
    return route.fulfill({ json: { count: results.length, next: null, previous: null, results } });
  });
  await page.route(/\/api\/management\/cases(?:\?.*)?$/, route => {
    const params = new URL(route.request().url()).searchParams, status = params.get('status') || 'review', kind = params.get('kind') || '', lane = params.get('route') || '';
    queries.push(params.toString());
    const rows = [...state.values()].filter(row => row.status === status && (!lane || row.route === lane));
    const kinds: Record<string, number> = {};
    for (const row of rows) kinds[row.kind] = (kinds[row.kind] ?? 0) + 1;
    const results = rows.filter(row => !kind || row.kind === kind);
    return route.fulfill({ json: { count: results.length, next: null, previous: null, kinds, results } });
  });
  await page.route(/\/api\/management\/cases\/[0-9a-f-]{36}$/, route => {
    const id = route.request().url().split('/').pop()!, body = route.request().postDataJSON() as { action: string };
    (payloads[id] ??= []).push(body);
    if (refuse) return route.fulfill({ status: 409, json: { detail: 'Los datos cambiaron. Actualiza el análisis antes de continuar.' } });
    const row = state.get(id)!;
    row.status = ['dismiss', 'keep_apart'].includes(body.action) ? 'dismissed' : 'applied';
    return route.fulfill({ json: row });
  });
  await page.route('**/api/management/cases/batches', route => {
    const rows = new Map<string, CaseBatchIndexRow>();
    for (const row of state.values()) {
      if (!row.batch_key) continue;
      const entry = rows.get(row.batch_key) ?? { batch_key: row.batch_key, counts: {}, kinds: {}, count: 0, created_at: '2026-10-15T01:00:00Z' };
      entry.counts[row.status] = (entry.counts[row.status] ?? 0) + 1;
      if (row.status === 'review') { entry.kinds[row.kind] = (entry.kinds[row.kind] ?? 0) + 1; entry.count += 1; }
      rows.set(row.batch_key, entry);
    }
    return route.fulfill({ json: [...rows.values()] });
  });
  await page.route(/\/api\/management\/cases\/batches\/[^/?]+$/, route => {
    const key = decodeURIComponent(route.request().url().split('/').pop()!);
    const inBatch = [...state.values()].filter(row => row.batch_key === key);
    if (route.request().method() === 'POST') {
      const body = route.request().postDataJSON() as { action: string; cases: { id: string; fingerprint: string }[] };
      batches.push(body);
      const decided: string[] = [], failed: { id: string; detail: string }[] = [];
      for (const sent of body.cases) {
        const row = state.get(sent.id)!;
        if (row.fingerprint !== sent.fingerprint || row.id === legacyBatch[2].id) { failed.push({ id: sent.id, detail: 'Los datos cambiaron. Actualiza el análisis antes de continuar.' }); continue; }
        row.status = body.action === 'keep_apart' ? 'dismissed' : 'applied'; decided.push(sent.id);
      }
      return route.fulfill({ json: { batch_key: key, decided, failed, results: decided.map(id => state.get(id)) } });
    }
    const counts: Record<string, number> = {}, kinds: Record<string, number> = {};
    for (const row of inBatch) { counts[row.status] = (counts[row.status] ?? 0) + 1; if (row.status === 'review') kinds[row.kind] = (kinds[row.kind] ?? 0) + 1; }
    const review = inBatch.filter(row => row.status === 'review');
    return route.fulfill({ json: { batch_key: key, counts, kinds, count: review.length, results: review } });
  });
  await page.route(/\/api\/management\/merges(?:\?.*)?$/, route => {
    const params = new URL(route.request().url()).searchParams;
    digests.push(params.toString());
    const search = (params.get('search') || '').toUpperCase();
    const results = digest.filter(row => !search || row.target.sku.includes(search) || row.sources.some(source => source.sku.includes(search)));
    return route.fulfill({ json: { count: results.length, next: null, previous: null, results } });
  });
  await page.route(/\/api\/management\/catalog\/[0-9a-f-]{36}\/split$/, route => route.fulfill({ json: { target: { id: L.seal.id, sku: '04111-22152', name: '', description: 'JUEGO EMPAQUES', category: '', subcategory: '',
    is_OEM: false, active: true, created_by_merge: false }, children: [], codes: [], items: [], images: [], warnings: [] } }));
  return { payloads, queries, batches, unexpected, digests, state };
}
async function openCases(page: Page, query = '') {
  await page.goto(`/administracion?seccion=casos${query}`);
  return page.getByRole('region', { name: 'Casos de coincidencia' });
}

for (const viewport of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }]) {
  test(`each case kind has its card and decides by line id at ${viewport.width}px`, async ({ page }) => {
    const mock = await mockCases(page, Object.values(cases));
    await page.setViewportSize(viewport);
    const screen = await openCases(page);
    const card = (name: string) => screen.getByRole('article', { name, exact: true });
    await expect(card('CAMBIO DE CÓDIGO 58411-1R000')).toBeVisible();
    if (viewport.width >= 900) await expect(page.getByRole('link', { name: 'Casos de coincidencia' })).toHaveAttribute('aria-current', 'page');
    expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)).toBe(false);
    await page.screenshot({ path: `/tmp/motionpartes-matching-cases-${viewport.width}.png`, fullPage: true });

    // code_change: the code the item sells under and the one waiting, out of stock until decided.
    const change = card('CAMBIO DE CÓDIGO 58411-1R000');
    await expect(change.getByText('CÓDIGO ACTUAL', { exact: true })).toBeVisible();
    await expect(change.getByRole('group', { name: 'Cambio de código' }).getByText('58411-1R100', { exact: true })).toBeVisible();
    await expect(change.getByText('EL CÓDIGO NUEVO YA ESTÁ EN: 58411-1R100')).toBeVisible();
    await expect(change.getByText('LÍNEA OEM DEL ARTÍCULO: 58411-1R000')).toBeVisible();
    await expect(change.getByText('Mientras se decide, este artículo no cuenta en las existencias.')).toBeVisible();
    await change.getByRole('button', { name: 'Aprobar', exact: true }).click();
    await expect(screen.getByRole('status').filter({ hasText: 'Cambio de código aprobado.' })).toBeVisible();
    expect(mock.payloads[cases.codeChange.id]).toEqual([{ action: 'approve', fingerprint: cases.codeChange.fingerprint }]);
    await expect(change).toHaveCount(0);  // decided: it leaves POR REVISAR

    // reference: Elegir línea shows the lines involved; the declared code is approved on the one chosen.
    const reference = card('REFERENCIA DECLARADA D-HYU-1R');
    await expect(reference.locator('p.matching-code')).toHaveText(/^REFERENCIA DECLARADA\s*58411-1R000\s*HYUNDAI$/);
    await reference.getByRole('button', { name: 'Elegir línea', exact: true }).click();
    await expect(reference.getByRole('button', { name: 'Aprobar en la línea elegida', exact: true })).toBeDisabled();
    await reference.getByRole('radio', { name: /^58411-1R000/ }).check();
    await reference.getByRole('button', { name: 'Aprobar en la línea elegida', exact: true }).click();
    await expect(screen.getByRole('status').filter({ hasText: 'Referencia aprobada en la línea OEM.' })).toBeVisible();
    expect(mock.payloads[cases.reference.id]).toEqual([{ action: 'approve', fingerprint: cases.reference.fingerprint, line: L.accent.id }]);

    // variant: the line it attached to is proposed; Separar referencia keeps its code as the supplier's own.
    const variant = card('VARIANTE EN REVISIÓN 43512-60150');
    await expect(variant.getByText('Las especificaciones no coinciden con las de la línea OEM.')).toBeVisible();
    await expect(variant.getByRole('radio', { name: /^43512-60150/ })).toBeChecked();
    await variant.getByRole('button', { name: 'Separar referencia', exact: true }).click();
    await expect(screen.getByRole('status').filter({ hasText: 'Referencia separada' })).toBeVisible();
    expect(mock.payloads[cases.variant.id]).toEqual([{ action: 'separar_referencia', fingerprint: cases.variant.fingerprint }]);

    // line: nothing is chosen for the admin; the AI's line and a line still in preparación are marked.
    const choose = card('ELEGIR LÍNEA OEM EC-01');
    await expect(choose.getByRole('button', { name: 'Vincular', exact: true })).toBeDisabled();
    await expect(choose.getByRole('radio', { name: /^EC-01\s*PROPUESTA IA/ })).not.toBeChecked();
    await expect(choose.getByRole('radio', { name: /^EC01\s*EN PREPARACIÓN/ })).toBeVisible();
    await choose.getByRole('radio', { name: /^EC01/ }).check();
    await choose.getByRole('button', { name: 'Vincular', exact: true }).click();
    await expect(screen.getByRole('status').filter({ hasText: 'Artículo vinculado a la línea OEM elegida.' })).toBeVisible();
    expect(mock.payloads[cases.line.id]).toEqual([{ action: 'approve', fingerprint: cases.line.fingerprint, line: L.clutchHyu.id }]);

    // merge (the legacy-auto-merge batch): the proposed survivor is preselected and another may be kept instead.
    const merge = card('UNIFICAR LÍNEAS 04111-22152-AS + 04111-22152');
    await expect(merge.getByText('LOTE · UNIFICACIONES AUTOMÁTICAS ANTERIORES', { exact: true })).toBeVisible();
    await expect(merge.getByText('NÚMEROS OEM: 04111-22152', { exact: true })).toBeVisible();
    await expect(merge.getByText('CÓDIGOS: AS: 04111-22152-AS', { exact: true })).toBeVisible();
    await expect(merge.getByRole('radio', { name: /^04111-22152\s*JUEGO/ })).toBeChecked();
    await merge.getByRole('radio', { name: /^04111-22152-AS/ }).check();
    await merge.getByRole('button', { name: 'Unificar', exact: true }).click();
    await expect(screen.getByRole('status').filter({ hasText: 'Líneas unificadas.' })).toBeVisible();
    expect(mock.payloads[cases.merge.id]).toEqual([{ action: 'merge', fingerprint: cases.merge.fingerprint, line: L.sealAs.id }]);

    // merge, Mantener separadas (its lines read from sources and candidates).
    const keep = card('UNIFICAR LÍNEAS 81560-0K010 + 81550-0K010');
    await expect(keep.getByText('El lado no coincide (izquierdo / derecho, delantero / trasero).')).toBeVisible();
    await keep.getByRole('button', { name: 'Mantener separadas', exact: true }).click();
    await expect(screen.getByRole('status').filter({ hasText: 'Las líneas se mantendrán separadas' })).toBeVisible();
    expect(mock.payloads[cases.keep.id]).toEqual([{ action: 'keep_apart', fingerprint: cases.keep.fingerprint }]);

    await expect(screen.getByText('No hay casos por revisar.', { exact: true })).toBeVisible();
    await screen.getByRole('button', { name: 'APLICADOS', exact: true }).click();
    await expect(screen.getByRole('button', { name: 'TODOS · 5', exact: true })).toBeVisible();
    await expect(card('CAMBIO DE CÓDIGO 58411-1R000').getByRole('button')).toHaveCount(0);  // decided cards are read only
    expect(mock.unexpected).toEqual([]);
  });
}

test('the kind tabs show their counts and ask the backend for one kind and one route', async ({ page }) => {
  const mock = await mockCases(page, Object.values(cases));
  await page.setViewportSize({ width: 1440, height: 1000 });
  const screen = await openCases(page);
  const kinds = screen.getByRole('group', { name: 'Tipo de caso' });
  await expect(kinds.getByRole('button', { name: 'TODOS · 6', exact: true })).toBeVisible();
  await expect(kinds.getByRole('button', { name: 'UNIFICAR LÍNEAS · 2', exact: true })).toBeVisible();
  await kinds.getByRole('button', { name: 'UNIFICAR LÍNEAS · 2', exact: true }).click();
  await expect(screen.getByRole('article')).toHaveCount(2);
  expect(mock.queries.at(-1)).toBe('status=review&page=1&kind=merge');
  await screen.getByLabel('Ruta de los casos').selectOption('lote');
  await expect(screen.getByRole('article')).toHaveCount(1);
  expect(mock.queries.at(-1)).toBe('status=review&page=1&kind=merge&route=lote');
});

test('a stale case says so and stays waiting; Buscar otra línea OEM sends the line found by search', async ({ page }) => {
  const mock = await mockCases(page, [cases.line], { refuse: true });
  await page.setViewportSize({ width: 1440, height: 1000 });
  const screen = await openCases(page);
  const card = screen.getByRole('article', { name: 'ELEGIR LÍNEA OEM EC-01' });
  await card.getByRole('button', { name: 'Buscar otra línea OEM', exact: true }).click();
  await card.getByLabel('Buscar línea OEM').fill('EC-01-NIS');
  await expect(card.getByRole('option', { name: /^EC-01-NIS/ })).toBeAttached();
  await card.getByRole('combobox', { name: 'línea OEM', exact: true }).selectOption(L.picked.id);
  await card.getByRole('button', { name: 'Vincular', exact: true }).click();
  expect(mock.payloads[cases.line.id]).toEqual([{ action: 'approve', fingerprint: cases.line.fingerprint, line: L.picked.id }]);
  await expect(screen.getByRole('alert').filter({ hasText: 'Los datos cambiaron. Actualiza el análisis antes de continuar.' })).toBeVisible();
  await expect(card).toBeVisible();  // refused: still waiting
});

test('a case from before S6 keeps its own card: a SKU is confirmed or kept apart', async ({ page }) => {
  const legacy = base(8, 'supplier', { sources: [item(8, 'D-HYU-1R', 'GMB', drum, { references: [{ code: '58411-1R000', brand: 'HYUNDAI' }] })], candidates: [L.accent],
    target_sku: L.accent.sku, reason: 'Coincidencia semántica: requiere confirmar.', ai: { target_sku: L.accent.sku, reason: 'Revisar aplicación antes de agrupar.', confidence: .8 } });
  const mock = await mockCases(page, [legacy]);
  await page.setViewportSize({ width: 1440, height: 1000 });
  const screen = await openCases(page);
  const card = screen.getByRole('article', { name: 'ARTÍCULO DE PROVEEDOR D-HYU-1R', exact: true });
  await expect(card.getByText('REFERENCIAS DECLARADAS: HYUNDAI: 58411-1R000')).toBeVisible();
  await expect(card.getByLabel('Destino para D-HYU-1R')).toHaveValue('58411-1R000');
  await card.getByRole('button', { name: 'Confirmar coincidencia', exact: true }).click();
  await expect(screen.getByRole('status').filter({ hasText: 'Coincidencia aprobada' })).toBeVisible();
  expect(mock.payloads[legacy.id]).toEqual([{ action: 'approve', target_sku: '58411-1R000', fingerprint: legacy.fingerprint }]);
});

test('a lote batch is decided together, each case on its own: the one that changed is named', async ({ page }) => {
  const mock = await mockCases(page, [...legacyBatch, cases.keep]);
  await page.setViewportSize({ width: 1440, height: 1000 });
  const screen = await openCases(page, '&vista=lotes');
  await expect(screen.getByRole('button', { name: 'Lotes', exact: true })).toHaveAttribute('aria-pressed', 'true');
  const list = screen.getByRole('list', { name: 'Lotes de casos' });
  await expect(list).toContainText('UNIFICACIONES AUTOMÁTICAS ANTERIORES');
  await expect(list).toContainText('3 por revisar · 0 aplicados · 0 descartados');
  await list.getByRole('button', { name: 'Abrir el lote legacy-auto-merge' }).click();
  const batch = screen.getByRole('region', { name: 'Lote legacy-auto-merge' });
  await expect(batch.getByRole('checkbox', { name: /^UNIFICAR LÍNEAS/ })).toHaveCount(3);
  await expect(batch.getByRole('button', { name: 'Unificar seleccionados (3)' })).toBeEnabled();
  await batch.getByRole('checkbox', { name: 'UNIFICAR LÍNEAS 04115-22152-AS + 04115-22152' }).uncheck();
  await batch.getByRole('button', { name: 'Mantener separadas (2)' }).click();
  await expect(batch.getByRole('alert')).toContainText('UNIFICAR LÍNEAS 04118-22152-AS + 04118-22152: Los datos cambiaron.');
  await expect(screen.getByRole('status')).toContainText('UNIFICACIONES AUTOMÁTICAS ANTERIORES: 1 caso decidido, 1 sin decidir.');
  expect(mock.batches).toEqual([{ action: 'keep_apart', cases: [legacyBatch[1], legacyBatch[2]].map(row => ({ id: row.id, fingerprint: row.fingerprint })) }]);
  await expect(batch.getByRole('checkbox', { name: /^UNIFICAR LÍNEAS/ })).toHaveCount(2);  // read again: the decided case left
  await batch.getByRole('button', { name: '← Todos los lotes' }).click();
  await expect(list).toContainText('2 por revisar · 0 aplicados · 1 descartados');
  expect(mock.unexpected).toEqual([]);
});

test('the automatic merges list each line and the ones it joined, and Separar SKU opens on the line', async ({ page }) => {
  const mock = await mockCases(page, []);
  await page.setViewportSize({ width: 1440, height: 1000 });
  const screen = await openCases(page, '&vista=unificaciones');
  const rows = screen.getByRole('list', { name: 'Unificaciones automáticas' });
  const first = rows.getByRole('listitem', { name: 'Unificación en 04111-22152' });
  await expect(first).toContainText('0411122152');
  await expect(first).toContainText('04111 22152 · SEPARADA');
  await expect(first).toContainText('Mismo código con otra grafía');
  await expect(rows.getByRole('listitem', { name: 'Unificación en 81550-0K010' })).toContainText('Ya se separaron todas.');
  await expect(rows.getByRole('listitem', { name: 'Unificación en 81550-0K010' })).toContainText('RETIRADA');
  await screen.getByLabel('Unificaciones desde').fill('2026-10-14');
  await expect.poll(() => mock.digests.at(-1)).toBe('page=1&since=2026-10-14');
  await first.getByRole('button', { name: 'Separar SKU de 04111-22152' }).click();
  await expect(page.getByRole('dialog', { name: 'Separar SKU' })).toBeVisible();
  expect(mock.unexpected).toEqual([]);
});
