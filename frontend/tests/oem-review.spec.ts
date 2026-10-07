import { expect, Page, test } from '@playwright/test';
import type { OEMBulkPreview, OEMBulkProgress, OEMReviewCase, OEMReviewPage } from '../lib/oem-review-types';
import type { OEMRun, OEMRunPage } from '../lib/oem-finder-types';

const stamp = '2026-10-06T12:00:00Z';
const empty = { count: 0, next: null, previous: null, results: [], pending_count: 0 };
const evidence = (patch: Partial<OEMReviewCase['evidence']> = {}): OEMReviewCase['evidence'] => ({
  lexicon: { result: 'agree_strong', share: 0.91, n_other_keys: 11, top_heads: [['BASE|AMORT', 10], ['TOPE|AMORT', 1]], side: '' }, units: ['lexicon_agree'],
  attributions: ['MANDO'], n_suppliers: 1, source_counts: { sku: 1, codigo: 1 }, genuine_tokens: [], flags: [], contradictions: [], verified_oem_alterno: false,
  second_unit: false, siblings: [], conflicts: [], conflict_kind: '', shared_with: [], family_incompatible: [], siblings_to_merge_into_this: [], inconsistent_siblings: [],
  warnings: [], secondary_company_refs: [], pending_owner_tags: [], blocking_unknowns: [], reasons: ['base OEM + sufijo solo de ETIQUETAS, F3 con unidades lexicon_agree'],
  makes: ['TOYOTA'], make_source: 'description', head: 'BASE|AMORT', aftermarket_family: [], apply_conflict: null, ...patch,
});

function oemCase(id: number, sku: string, patch: Partial<OEMReviewCase> = {}): OEMReviewCase {
  return { id, part_id: `00000000-0000-4000-8000-0000000000${String(id).padStart(2, '0')}`, sku, description: 'BASE AMORT TOY HILUX', evaluated_sku: sku,
    evaluated_description: 'BASE AMORT TOY HILUX', tier: 'PROBABLE_BASE', tier_label: 'Base probable', underlying_tier: '', status: 'review', fingerprint: String(id).repeat(64).slice(0, 64),
    candidate: '48654-0K040', written_form: '48654-0K040', brand: 'TOYOTA', system: 'TOYOTA', grade: 'F3', chain: 'MANDO',
    chain_tokens: [{ sep: '-', tok: 'MANDO', token: 'MANDO', class: 'TAG', kind: 'brand', status: 'seed_confirmed', auto_eligible: true }], blockers: ['no_second_unit'],
    in_stock: true, available_quantity: 4, method: 'rename', evidence: evidence(), choices: [], codes: [], supplier_codes: [{ codigo: '48654-0K040-MANDO', brand: 'MANDO', supplier: 'PROVEEDOR UNO', available: 4 }],
    stale: '', stale_label: '', actions: ['approve', 'dismiss'], decision: {}, decided_by: null, decided_at: null, ...patch };
}

function run(id: number, patch: Partial<OEMRun> = {}): OEMRun {
  return { id, mode: 'apply_auto', status: 'completed', rules_version: 'oem-finder-3', suffix_table_version: 'db:9',
    scope: { stage: 'review', action: 'approve_batch', tier: 'PROBABLE_BASE', filters: { tier: 'PROBABLE_BASE' }, in_stock_first: true, batch_size: 50 },
    tiers: { PROBABLE_BASE: [2, 2] }, applied: { applied: 2, conflicts: 0, skipped: {}, errors: 0, tiers: { PROBABLE_BASE: 2 }, batches: [{ index: 1, applied: 2, conflicts: 0, skipped: 0, errors: 0 }],
      stopped: null, excluded: {}, selected: 2, done: 2, total: 2, spot_check: [1, 2] }, errors: [], actor: 'ADMIN', started_at: stamp, finished_at: stamp, changes: 2, reverted: 0, ...patch };
}

// Mirrors the server: filters and counts, one decision per case (fingerprint), the Agrupar SKU and Sufijos deep links, bulk preview/start/steps and run undo.
async function fixture(page: Page, { unavailable = false } = {}) {
  test.skip(!process.env.E2E_ADMIN_TOKEN, 'A disposable superuser token is required for server-rendered administration.');
  await page.context().addCookies([{ name: 'partsmall_session', value: process.env.E2E_ADMIN_TOKEN!, url: 'http://localhost:8080', httpOnly: true, sameSite: 'Strict' }]);
  const cases: OEMReviewCase[] = [
    oemCase(1, '48654-0K040-MANDO'),
    oemCase(2, '11210-30110 11210-0L020', { tier: 'MULTI_OEM', tier_label: 'Varios OEM', candidate: '11210-30110', written_form: '11210-30110', chain: '', chain_tokens: [], blockers: [],
      description: 'TAPA VALV TOY HILUX', grade: 'F2', actions: ['choose_oem', 'dismiss'], evidence: evidence({ lexicon: { result: 'no_data', share: 0, n_other_keys: 0, top_heads: [], side: '' }, units: [] }),
      choices: [{ key: '1121030110', code: '11210-30110', written: '11210-30110', system: 'TOYOTA', grade: 2, brand: 'TOYOTA' }, { key: '112100L020', code: '11210-0L020', written: '11210-0L020', system: 'TOYOTA', grade: 2, brand: 'TOYOTA' }] }),
    oemCase(3, '48654-0K050-MANDO', { tier: 'CONFLICT', tier_label: 'Conflicto (agrupar)', candidate: '48654-0K050', written_form: '48654-0K050', blockers: ['conflict:shared_base_family'],
      actions: ['send_to_merge', 'dismiss'], evidence: evidence({ conflict_kind: 'shared_base_family', shared_with: ['48654-0K050-NAK'] }) }),
    oemCase(4, '48654-60020-MANDO', { tier: 'CONFLICT', tier_label: 'Conflicto (agrupar)', candidate: '48654-60020', blockers: ['conflict:base_is_existing_active_part', 'family_incompatible'],
      actions: ['dismiss'], description: 'BASE AMORT TOY PRADO RH', evidence: evidence({ conflict_kind: 'base_is_existing_active_part', shared_with: ['48654-60020'], family_incompatible: ['48654-60020'] }) }),
    oemCase(5, '43211-29026-JR', { tier: 'UNKNOWN_SUFFIX', tier_label: 'Sufijo desconocido', candidate: '43211-29026', chain: 'JR', blockers: ['unknown:JR'], actions: ['dismiss'], description: 'MUÑEQUILLA TOY HIACE',
      chain_tokens: [{ sep: '-', tok: 'JR', token: 'JR', class: 'UNKNOWN', kind: '', status: 'needs_owner_label', auto_eligible: false }], evidence: evidence({ blocking_unknowns: ['JR'] }) }),
    oemCase(6, '333512', { tier: 'NEEDS_AI', tier_label: 'Requiere IA', candidate: '', written_form: '', brand: '', system: '', grade: '', chain: '', chain_tokens: [], blockers: [], actions: ['dismiss'],
      description: 'AMORT KIA RIO DEL LH', in_stock: false, available_quantity: 0, evidence: evidence({ lexicon: { result: null, share: null, n_other_keys: null, top_heads: null, side: null }, units: [], attributions: [], n_suppliers: 0, source_counts: {} }) }),
    oemCase(7, '48654-12030', { tier: 'CURRENT_REVIEW', tier_label: 'Actual (revisar)', candidate: '48654-12030', written_form: '48654-12030', chain: '', chain_tokens: [], blockers: ['desc_variant:COMPLETO'],
      method: 'flag', stale: 'snapshot_changed', stale_label: 'el SKU cambió desde el análisis', actions: ['dismiss'], description: 'BASE AMORT TOY COROLLA COMPLETO EDITADO' }),
  ];
  const runs: OEMRun[] = [run(31, { scope: { stage: 'review', action: 'approve', sku: '48654-0K040-MANDO', case: 1 }, applied: { applied: 1, done: 1, total: 1, spot_check: [9] }, changes: 1 })];
  const unexpected: string[] = [], lists: URLSearchParams[] = [], decisions: Record<string, unknown>[] = [], bulkStarts: Record<string, unknown>[] = [], steps: string[] = [];
  const reverts: Record<string, unknown>[] = [], groupingSearches: string[] = [], suffixSearches: string[] = [];
  let bulkRun: OEMRun | null = null;
  await page.route('**/api/**', route => { unexpected.push(`${route.request().method()} ${new URL(route.request().url()).pathname}`); return route.fulfill({ status: 404, json: { detail: 'Ruta no prevista' } }); });
  await page.route('**/api/market/analytics/events', route => route.fulfill({ status: 202, json: { recorded: true } }));
  await page.route(/\/api\/management\/(roles|catalog\/import\/issues|part-types)(?:\?.*)?$/, route => route.fulfill({ json: route.request().url().includes('/roles') ? [] : empty }));
  await page.route(/\/api\/management\/catalog(?:\?.*)?$/, route => route.fulfill({ json: empty }));
  await page.route(/\/api\/management\/matching(?:\?.*)?$/, route => route.fulfill({ json: { running: false, queued: false, state: 'completed', stage: 'completed', last_run: null, summary: {}, error: '', counts: { review: 2 }, count: 0, next_offset: null, results: [] } }));
  const gone = { status: 503, json: { detail: 'La revisión OEM aún no está disponible: faltan migraciones por aplicar.' } };
  await page.route('**/api/management/oem-review/summary', route => route.fulfill(unavailable ? gone : { json: { statuses: { review: cases.filter(c => c.status === 'review').length }, review: cases.filter(c => c.status === 'review').length } }));
  await page.route(/\/api\/management\/oem-review(?:\?.*)?$/, route => {
    if (unavailable) return route.fulfill(gone);
    const params = new URL(route.request().url()).searchParams;
    lists.push(params);
    const status = params.get('status') || 'review';
    let rows = cases.filter(c => c.status === status);
    const tiers: Record<string, number> = {};
    rows.forEach(c => { tiers[c.tier] = (tiers[c.tier] ?? 0) + 1; });
    if (params.get('tier')) rows = rows.filter(c => c.tier === params.get('tier'));
    if (params.get('blocker')) rows = rows.filter(c => c.blockers.some(b => b === params.get('blocker') || b.startsWith(`${params.get('blocker')}:`)));
    const body: OEMReviewPage = { count: rows.length, next: null, previous: null, results: rows, tiers, statuses: { review: cases.filter(c => c.status === 'review').length, applied: cases.filter(c => c.status === 'applied').length },
      facets: { blockers: [['no_second_unit', 1], ['conflict:shared_base_family', 1], ['unknown:JR', 1]], chains: [['MANDO', 3], ['JR', 1]], makes: [['TOYOTA', 6]], systems: [['TOYOTA', 6]] },
      last_run: { id: 12, finished_at: stamp, suffix_table_version: 'db:9', tiers: { AUTO_RENAME_BASE: [22, 19] }, scenario: { tokens: ['G', 'NP'], tiers: { AUTO_RENAME_BASE: 428 } }, cases: {} } };
    return route.fulfill({ json: body });
  });
  await page.route(/\/api\/management\/oem-review\/[0-9]+$/, route => {
    const id = Number(new URL(route.request().url()).pathname.split('/').pop());
    const body = route.request().postDataJSON() as Record<string, string>;
    decisions.push({ id, ...body });
    const item = cases.find(c => c.id === id)!;
    if (body.fingerprint !== item.fingerprint) return route.fulfill({ status: 409, json: { detail: 'El caso cambió o ya tiene otra decisión. Actualiza la lista antes de continuar.' } });
    if (body.action === 'approve' || body.action === 'choose_oem') {
      const code = body.code || item.candidate;
      Object.assign(item, { status: 'applied', actions: [], decision: { action: body.action, run: 32, change: 5, code, brand: item.brand, method: item.method, previous_sku: item.sku }, decided_by: 'ADMIN', decided_at: stamp });
      return route.fulfill({ json: { case: item, run: 32, change: 5, family: null } });
    }
    if (body.action === 'send_to_merge') {
      Object.assign(item, { status: 'resolved', actions: ['reopen'], decision: { action: 'send_to_merge', target_sku: '48654-0K050' } });
      const member = (sku: string, idx: number) => ({ id: `00000000-0000-4000-8000-00000000009${idx}`, sku, name: sku, description: 'BASE AMORT TOY HILUX', category: '', subcategory: '', codes: [] });
      return route.fulfill({ json: { case: item, run: null, change: null, family: { target_sku: '48654-0K050', target: { id: '', sku: '48654-0K050', name: '48654-0K050', description: 'BASE AMORT TOY HILUX', category: '', subcategory: '', codes: [], proposed_parent: true },
        sources: [member('48654-0K050-MANDO', 1), member('48654-0K050-NAK', 2)], source_skus: ['48654-0K050-MANDO', '48654-0K050-NAK'], source_count: 2,
        reason: 'Conflicto OEM 48654-0K050: shared_base_family. Revisa si son el mismo repuesto antes de agrupar.', warnings: ['Después de agrupar, el próximo análisis OEM propondrá 48654-0K050 (TOYOTA) como MAIN del SKU resultante.'],
        needs_review: true, oem: { code: '48654-0K050', brand: 'TOYOTA' } } } });
    }
    if (body.action === 'dismiss') {
      if (!body.reason) return route.fulfill({ status: 400, json: { reason: ['Elige un motivo para descartar.'] } });
      Object.assign(item, { status: 'dismissed', actions: ['reopen'], decision: { action: 'dismiss', reason: body.reason, label: 'El número propuesto no es el OEM de esta pieza', note: body.note } });
    }
    return route.fulfill({ json: { case: item, run: null, change: null, family: null } });
  });
  await page.route(/\/api\/management\/oem-review\/bulk(?:\?.*)?$/, route => {
    if (route.request().method() === 'GET') {
      const body: OEMBulkPreview = { count: 2, selected: 2, limit: 500, batch_size: 50, excluded: { stale: 1 }, tiers: { PROBABLE_BASE: 2 }, methods: { rename: 2 },
        sample: [{ case: 1, sku: '48654-0K040-MANDO', description: 'BASE AMORT TOY HILUX', tier: 'PROBABLE_BASE', code: '48654-0K040', brand: 'TOYOTA', method: 'rename', outcome: 'applied', detail: '' },
          { case: 8, sku: '54650-D4000-MANDO', description: 'AMORT KIA OPTIMA DEL LH', tier: 'PROBABLE_BASE', code: '54650-D4000', brand: 'KIA', method: 'rename', outcome: 'conflict', detail: 'El OEM o el SKU anterior ya identifica otro SKU: 54650D4000' }],
        selection: 'f'.repeat(64), halt: null, running: null };
      return route.fulfill({ json: body });
    }
    bulkStarts.push(route.request().postDataJSON() as Record<string, unknown>);
    bulkRun = run(40, { status: 'running', applied: { applied: 0, conflicts: 0, skipped: {}, errors: 0, tiers: {}, batches: [], stopped: null, excluded: { stale: 1 }, selected: 2, done: 0, total: 2, spot_check: [] }, changes: 0 });
    const body: OEMBulkProgress = { run: bulkRun, finished: false, waiting: '' };
    return route.fulfill({ status: 201, json: body });
  });
  await page.route('**/api/management/oem-review/bulk/40', route => {
    steps.push(String((route.request().postDataJSON() as Record<string, string>).action));
    const done = (bulkRun!.applied.done ?? 0) + 1;
    bulkRun = run(40, { status: done >= 2 ? 'completed' : 'running', applied: { applied: done, conflicts: 0, skipped: {}, errors: 0, tiers: { PROBABLE_BASE: done }, batches: Array.from({ length: done }, (_, i) => ({ index: i + 1, applied: 1, conflicts: 0, skipped: 0, errors: 0 })),
      stopped: null, excluded: {}, selected: 2, done, total: 2, spot_check: done >= 2 ? [1, 2] : [] }, changes: done });
    if (done >= 2) runs.unshift(bulkRun);
    return route.fulfill({ json: { run: bulkRun, finished: done >= 2, waiting: '' } });
  });
  await page.route(/\/api\/management\/oem-finder\/runs(?:\?.*)?$/, route => {
    const params = new URL(route.request().url()).searchParams;
    lists.push(params);
    const body: OEMRunPage = { count: runs.length, next: null, previous: null, results: runs, halt: null };
    return route.fulfill({ json: body });
  });
  await page.route('**/api/management/oem-finder/runs/40/spot-check', route => route.fulfill({ json: { run: 40, columns: ['ejecucion', 'correcto_si_no'], rows: [], csv: 'ejecucion,correcto_si_no\n40,\n' } }));
  await page.route('**/api/management/oem-finder/runs/40/revert', route => {
    reverts.push(route.request().postDataJSON() as Record<string, unknown>);
    runs[0] = { ...runs[0], reverted: runs[0].changes };
    return route.fulfill({ json: { run: 40, reverted: 2, already_reverted: 0, skipped: [] } });
  });
  await page.route(/\/api\/management\/catalog\/grouping(?:\?.*)?$/, route => { groupingSearches.push(new URL(route.request().url()).searchParams.get('search') || ''); return route.fulfill({ json: { ...empty, configured: false } }); });
  await page.route(/\/api\/management\/catalog\/suffixes(?:\?.*)?$/, route => {
    suffixSearches.push(new URL(route.request().url()).searchParams.get('search') || '');
    return route.fulfill({ json: { count: 1, next: null, previous: null, version: 9, counts: { cls: { UNKNOWN: 1 }, status: { needs_owner_label: 1 }, has_unlock: 1 }, company_suffixes: {},
      results: [{ token: 'JR', match: 'exact', alias_of: null, aliases: [], cls: 'UNKNOWN', tag_kind: '', variant_kind: '', attribution: '', creates_company_reference: false, scope_overrides: [],
        confidence: 'low', status: 'needs_owner_label', owner_confirmed: false, confirmed_by: null, confirmed_at: null, auto_eligible: false, proposed_label: '', legacy_company_registry: false, notes: '',
        stats: { count_all: 1, count_in_stock: 0, examples: ['43211-29026-JR :: MUÑEQUILLA TOY HIACE'] }, updated_at: stamp, version: 9 }] } });
  });
  return { cases, unexpected, lists, decisions, bulkStarts, steps, reverts, groupingSearches, suffixSearches };
}

async function openReview(page: Page, width: number) {
  await page.setViewportSize({ width, height: 1000 });
  await page.goto('/administracion?seccion=inventario');
  await page.getByRole('button', { name: 'Agrupación inteligente', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Agrupación inteligente' });
  await expect(dialog.getByRole('button', { name: 'POR REVISAR · 2', exact: true })).toBeVisible();
  return dialog;
}

for (const width of [1440, 390]) {
  test(`OEM review tab: evidence, decisions, deep links, bulk approval and undo at ${width}px`, async ({ page }) => {
    const mock = await fixture(page);
    const dialog = await openReview(page, width);
    await dialog.getByRole('button', { name: 'REVISIÓN OEM · 7', exact: true }).click();
    const card = (sku: string) => dialog.getByRole('article', { name: `Caso OEM ${sku}`, exact: true });
    const mando = card('48654-0K040-MANDO');
    await expect(mando.getByText('48654-0K040 · TOYOTA', { exact: true })).toBeVisible();
    await expect(mando.getByText('COINCIDE FUERTE · 11 CLAVES · 91 %')).toBeVisible();
    await expect(mando.getByText('SIN SEGUNDA EVIDENCIA INDEPENDIENTE', { exact: true })).toBeVisible();
    await expect(mando.getByText('-MANDO · ETIQUETA · MARCA · AUTOMÁTICA', { exact: true })).toBeVisible();
    await expect(mando.getByText('APROBAR RENOMBRA 48654-0K040-MANDO → 48654-0K040; 48654-0K040-MANDO QUEDA COMO ALTERNO.', { exact: true })).toBeVisible();
    await expect(dialog.getByText(/SI EL PROPIETARIO CONFIRMA G, NP: \+406 RENOMBRES AUTOMÁTICOS/)).toBeVisible();
    await expect(card('48654-12030').getByText(/EL SKU CAMBIÓ DESDE EL ANÁLISIS/)).toBeVisible();
    await expect(card('48654-12030').getByRole('button', { name: 'Aprobar 48654-12030' })).toHaveCount(0);
    await expect(card('48654-60020-MANDO').getByRole('button', { name: /Agrupar SKU/ })).toHaveCount(0);
    await expect(card('48654-60020-MANDO').getByText(/LA FAMILIA MEZCLA PIEZAS INCOMPATIBLES/)).toBeVisible();
    expect(await dialog.evaluate(el => el.scrollWidth > el.clientWidth)).toBe(false);
    await page.screenshot({ path: `/tmp/motionpartes-oem-review-${width}.png` });

    await mando.getByRole('button', { name: 'Aprobar 48654-0K040-MANDO', exact: true }).click();
    await expect(dialog.getByRole('status').filter({ hasText: 'renombrado a 48654-0K040' })).toBeVisible();
    expect(mock.decisions.at(-1)).toEqual({ id: 1, action: 'approve', fingerprint: mock.cases[0].fingerprint });

    await dialog.getByRole('button', { name: 'VARIOS OEM · 1', exact: true }).click();
    await expect.poll(() => mock.lists.at(-1)?.get('tier')).toBe('MULTI_OEM');
    const multi = card('11210-30110 11210-0L020');
    await multi.getByLabel('11210-0L020 · TOYOTA · F2').check();
    await multi.getByRole('button', { name: 'Elegir OEM para 11210-30110 11210-0L020', exact: true }).click();
    await expect(dialog.getByRole('status').filter({ hasText: 'renombrado a 11210-0L020' })).toBeVisible();
    expect(mock.decisions.at(-1)).toEqual({ id: 2, action: 'choose_oem', fingerprint: mock.cases[1].fingerprint, code: '11210-0L020' });
    await dialog.getByRole('button', { name: /^TODOS · / }).click();

    await card('48654-0K050-MANDO').getByRole('button', { name: 'Enviar 48654-0K050-MANDO a Agrupar SKU', exact: true }).click();
    const grouping = page.getByRole('dialog', { name: 'Agrupar SKU' });
    await expect(grouping.getByRole('region', { name: 'Revisión de familia 48654-0K050' })).toBeVisible();
    await expect(grouping.getByText(/Familia enviada desde la Revisión OEM/)).toBeVisible();
    await expect(grouping.getByLabel('Agrupar 48654-0K050-NAK bajo 48654-0K050')).toBeVisible();
    expect(mock.groupingSearches.at(-1)).toBe('48654-0K050');
    await grouping.getByRole('button', { name: 'Cerrar ventana' }).click();
    await expect(dialog.getByRole('status').filter({ hasText: 'Caso enviado a Agrupar SKU' })).toBeVisible();

    await card('43211-29026-JR').getByRole('button', { name: 'Etiquetar sufijo JR', exact: true }).click();
    const suffixes = page.getByRole('dialog', { name: 'Sufijos de códigos' });
    await expect(suffixes.getByLabel('Buscar sufijo')).toHaveValue('JR');
    await expect(suffixes.getByRole('button', { name: 'Confirmar TAG JR', exact: true })).toBeVisible();
    expect(mock.suffixSearches[0]).toBe('JR');
    await suffixes.getByRole('button', { name: 'Cerrar ventana' }).click();

    const ai = card('333512');
    await ai.getByRole('button', { name: 'Descartar 333512', exact: true }).click();
    await ai.getByLabel('Motivo para descartar 333512').selectOption('not_oem');
    await ai.getByRole('button', { name: 'Confirmar descarte', exact: true }).click();
    await expect(dialog.getByRole('status').filter({ hasText: 'Caso de 333512 descartado.' })).toBeVisible();
    expect(mock.decisions.at(-1)).toEqual({ id: 6, action: 'dismiss', fingerprint: mock.cases[5].fingerprint, reason: 'not_oem', note: '' });
    await expect(dialog.getByRole('button', { name: 'REVISIÓN OEM · 3', exact: true })).toBeVisible();  // approve, choose, send to merge, dismiss

    await dialog.getByLabel('Bloqueo').selectOption('conflict:shared_base_family');
    await expect.poll(() => mock.lists.at(-1)?.get('blocker')).toBe('conflict:shared_base_family');
    await dialog.getByLabel('Bloqueo').selectOption('');
    await dialog.getByLabel('Cadena de sufijos').selectOption('MANDO');
    await expect.poll(() => mock.lists.at(-1)?.get('chain')).toBe('MANDO');
    await dialog.getByRole('button', { name: 'Aprobar en lote…', exact: true }).click();
    const bulk = dialog.getByRole('region', { name: 'Aprobación en lote' });
    await expect(bulk.getByText('Se aprobarán 2 de 2 casos por revisar')).toBeVisible();
    await expect(bulk.getByText('1 cambiaron desde el análisis (el SKU o un sufijo de su cadena).', { exact: true })).toBeVisible();
    await expect(bulk.getByText('PASARÍA A CONFLICTO', { exact: true })).toBeVisible();
    expect(mock.bulkStarts).toEqual([]);
    await bulk.getByRole('button', { name: 'Aprobar 2 casos', exact: true }).click();
    await expect(dialog.getByRole('status').filter({ hasText: 'Aprobación en lote #40: 2 aprobados.' })).toBeVisible();
    expect(mock.bulkStarts).toEqual([{ tier: '', blocker: '', chain: 'MANDO', make: '', system: '', in_stock: '', search: '', selection: 'f'.repeat(64) }]);
    expect(mock.steps).toEqual(['continue', 'continue']);
    await expect(bulk.getByRole('progressbar', { name: 'Progreso de la aprobación en lote' })).toHaveAttribute('aria-valuenow', '2');
    await bulk.getByRole('button', { name: 'Ver ejecución #40', exact: true }).click();

    await expect(dialog.getByRole('cell', { name: /APROBACIÓN EN LOTE · BASE PROBABLE 2 \/ 2 PROCESADOS · 2 LOTES$/ })).toBeVisible();
    await expect.poll(() => mock.lists.at(-1)?.get('stage')).toBe('review');
    const file = page.waitForEvent('download');
    await dialog.getByRole('button', { name: 'Descargar la muestra de la ejecución 40', exact: true }).click();
    expect((await file).suggestedFilename()).toBe('oem-run-40-spot-check.csv');
    await dialog.getByRole('button', { name: 'Deshacer la ejecución 40', exact: true }).click();
    expect(mock.reverts).toEqual([]);
    await dialog.getByRole('button', { name: 'Confirmar deshacer la ejecución 40', exact: true }).click();
    await expect(dialog.getByRole('status').filter({ hasText: 'Ejecución #40: 2 cambios deshechos' })).toBeVisible();
    expect(mock.reverts).toEqual([{}]);
    await expect(dialog.getByRole('button', { name: 'Deshacer la ejecución 40', exact: true })).toBeDisabled();
    await expect(dialog.getByRole('cell', { name: /APROBACIÓN INDIVIDUAL · 48654-0K040-MANDO$/ })).toBeVisible();
    expect(await dialog.evaluate(el => el.scrollWidth > el.clientWidth)).toBe(false);
    expect(mock.unexpected).toEqual([]);
  });
}

test('OEM review tab explains when its tables are not migrated yet', async ({ page }) => {
  const mock = await fixture(page, { unavailable: true });
  const dialog = await openReview(page, 1440);
  await dialog.getByRole('button', { name: 'REVISIÓN OEM', exact: true }).click();
  await expect(dialog.getByRole('alert')).toContainText('La revisión OEM aún no está disponible: faltan migraciones por aplicar.');
  await dialog.getByRole('button', { name: 'POR REVISAR · 2', exact: true }).click();
  await expect(dialog.getByRole('button', { name: 'Analizar catálogo y proveedores', exact: true })).toBeVisible();
  expect(mock.unexpected).toEqual([]);
});

test('the proxy only forwards the OEM review routes it allows', async ({ page }) => {
  test.skip(!process.env.E2E_ADMIN_TOKEN, 'A disposable superuser token is required for server-rendered administration.');
  await page.context().addCookies([{ name: 'partsmall_session', value: process.env.E2E_ADMIN_TOKEN!, url: 'http://localhost:8080', httpOnly: true, sameSite: 'Strict' }]);
  await page.goto('/');
  const statuses = await page.evaluate(async () => Promise.all([
    fetch('/api/management/oem-review/12/apply', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }).then(r => r.status),
    fetch('/api/management/oem-review/bulk/12/delete', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }).then(r => r.status),
    fetch('/api/management/oem-review/12', { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: '{}' }).then(r => r.status),
  ]));
  expect(statuses).toEqual([404, 404, 404]);
});
