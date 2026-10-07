import { expect, Page, test } from '@playwright/test';
import type { OEMApplyTier, OEMAutoTier, OEMHalt, OEMPendingPage, OEMPendingPreview, OEMPendingRow, OEMRun, OEMRunPage } from '../lib/oem-finder-types';

const stamp = '2026-10-06T12:00:00Z';
const empty = { count: 0, next: null, previous: null, results: [], pending_count: 0 };
const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

function pendingRow(id: number, sku: string, patch: Partial<OEMPendingRow> = {}): OEMPendingRow {
  return { id, part_id: `00000000-0000-4000-8000-0000000000${String(id).padStart(2, '0')}`, sku, evaluated_sku: sku, description: 'TERM ESTAB HYU ACCENT',
    tier: 'AUTO_FLAG_CURRENT', apply_tier: 'AUTO_FLAG_CURRENT', candidate: sku, written_form: sku, brand: 'HYUNDAI', makes: ['HYUNDAI'], system: 'HMG', grade: 'F3',
    chain: '', chain_tokens: [], owner_tags: [], method: 'flag', in_stock: false, available_quantity: 0,
    evidence: { lexicon: { result: 'agree_strong', share: 1, n_other_keys: 7, top_heads: [['TERM|ESTAB', 7]] }, units: ['lexicon_agree'], attributions: ['BARE'], n_suppliers: 1,
      verified_oem_alterno: false, second_unit: false, genuine_tokens: [], siblings: [], make_source: 'description', head: 'TERM|ESTAB', reasons: ['formato OEM de HMG (F3)'], warnings: [] },
    status: 'pending', reason: '', reason_label: '', stale: '', stale_label: '', note: '', decided_by: null, decided_at: null, updated_at: stamp, run: 12, ...patch };
}

function run(id: number, patch: Partial<OEMRun> = {}): OEMRun {
  return { id, mode: 'apply_auto', status: 'completed', rules_version: 'oem-finder-2', suffix_table_version: 'db:9',
    scope: { tier: 'AUTO_FLAG_CURRENT', limit: null, canary: 50, in_stock_first: true, batch_size: 100, stage: 'command' }, tiers: {},
    applied: { applied: 50, conflicts: 0, skipped: {}, errors: 0, tiers: { AUTO_FLAG_CURRENT: 50 }, batches: [{ index: 1, applied: 50, conflicts: 0, skipped: 0, errors: 0 }],
      stopped: null, excluded: {}, selected: 50, spot_check: [1] }, errors: [], actor: 'motionpartes-oem-finder-service', started_at: stamp, finished_at: stamp, changes: 50, reverted: 0, ...patch };
}

// Mirrors mall.oem_auto: pending rows per tab and apply tier with the canary credit, read-only preview, apply (idempotent per operation id,
// refused while halted), send-to-review (the rows leave the list) and the run history the result links to.
async function fixture(page: Page) {
  test.skip(!process.env.E2E_ADMIN_TOKEN, 'A disposable superuser token is required for server-rendered administration.');
  await page.context().addCookies([{ name: 'partsmall_session', value: process.env.E2E_ADMIN_TOKEN!, url: 'http://localhost:8080', httpOnly: true, sameSite: 'Strict' }]);
  const rows: OEMPendingRow[] = [
    pendingRow(1, '54830-1R000', { in_stock: true, available_quantity: 5 }),
    pendingRow(2, '48654-30030', { description: 'BASE AMORT TOY COROLLA', brand: 'TOYOTA', makes: ['TOYOTA'], system: 'TOYOTA', evidence: { ...pendingRow(0, '').evidence, lexicon: { result: 'agree_strong', share: 0.86, n_other_keys: 7, top_heads: [['BASE|AMORT', 6]] } } }),
    pendingRow(3, '56820-2W000', { description: 'TERM DIR HYU SANTA FE LH' }),
    pendingRow(4, '17100-M79M00-G', { tier: 'AUTO_RENAME_BASE', apply_tier: 'STRONG_PENDING_OWNER_TAGS', candidate: '17100-M79M00', written_form: '17100-M79M00', brand: 'SUZUKI', makes: ['SUZUKI'],
      system: 'SUZUKI', grade: 'F4', chain: 'G', method: 'rename', description: 'BOMBA AGUA SUZ SWIFT', in_stock: true, available_quantity: 2, owner_tags: ['G'],
      chain_tokens: [{ sep: '-', tok: 'G', token: 'G', class: 'TAG', kind: 'genuine', status: 'owner_confirmed', auto_eligible: true }] }),
    pendingRow(5, '54830-2H000-MOBIS', { tier: 'AUTO_RENAME_BASE', apply_tier: 'AUTO_RENAME_BASE', candidate: '54830-2H000', written_form: '54830-2H000', chain: 'MOBIS', method: 'rename', grade: 'F4',
      description: 'TERM ESTAB HYU ELANTRA', chain_tokens: [{ sep: '-', tok: 'MOBIS', token: 'MOBIS', class: 'TAG', kind: 'genuine', status: 'seed_confirmed', auto_eligible: true }] }),
  ];
  const credit: Record<OEMApplyTier, boolean> = { AUTO_FLAG_CURRENT: false, AUTO_RENAME_BASE: false, STRONG_PENDING_OWNER_TAGS: false };
  const runs: OEMRun[] = [run(12)];
  let halt: OEMHalt | null = null, failNext = 0, busyNext = 0, runStatus: OEMRun['status'] = 'completed';
  const unexpected: string[] = [], lists: URLSearchParams[] = [], previews: Record<string, unknown>[] = [], applies: Record<string, string>[] = [], sends: Record<string, unknown>[] = [];
  const operations: Record<string, OEMRun> = {};
  await page.route('**/api/**', route => { unexpected.push(`${route.request().method()} ${new URL(route.request().url()).pathname}`); return route.fulfill({ status: 404, json: { detail: 'Ruta no prevista' } }); });
  await page.route('**/api/market/analytics/events', route => route.fulfill({ status: 202, json: { recorded: true } }));
  await page.route(/\/api\/management\/(roles|catalog\/import\/issues|part-types)(?:\?.*)?$/, route => route.fulfill({ json: route.request().url().includes('/roles') ? [] : empty }));
  await page.route(/\/api\/management\/catalog(?:\?.*)?$/, route => route.fulfill({ json: empty }));
  await page.route(/\/api\/management\/oem-finder\/runs(?:\?.*)?$/, route => {
    const body: OEMRunPage = { count: runs.length, next: null, previous: null, results: [...runs].reverse(), halt };
    return route.fulfill({ json: body });
  });
  await page.route(/\/api\/management\/oem-finder\/pending(?:\?.*)?$/, route => {
    const params = new URL(route.request().url()).searchParams;
    lists.push(params);
    const live = rows.filter(row => row.status === (params.get('status') || 'pending'));
    const tiers: Partial<Record<OEMAutoTier, number>> = {};
    live.forEach(row => { tiers[row.tier] = (tiers[row.tier] ?? 0) + 1; });
    let shown = live.filter(row => row.tier === (params.get('tier') || 'AUTO_FLAG_CURRENT'));
    if (params.get('chain')) shown = shown.filter(row => row.chain === params.get('chain'));
    if (params.get('make')) shown = shown.filter(row => row.brand === params.get('make'));
    if (params.get('in_stock')) shown = shown.filter(row => row.in_stock === (params.get('in_stock') === 'true'));
    if (params.get('search')) shown = shown.filter(row => row.sku.includes(params.get('search')!));
    const state = (tier: OEMApplyTier) => ({ pending: live.filter(row => row.apply_tier === tier).length, in_stock: live.filter(row => row.apply_tier === tier && row.in_stock).length, canary_done: credit[tier] });
    const statuses: Record<string, number> = {};
    rows.forEach(row => { statuses[row.status] = (statuses[row.status] ?? 0) + 1; });
    const inTab = live.filter(row => row.tier === (params.get('tier') || 'AUTO_FLAG_CURRENT'));
    const count = (key: 'chain' | 'brand') => Object.entries(inTab.reduce<Record<string, number>>((acc, row) => { if (row[key]) acc[row[key]] = (acc[row[key]] ?? 0) + 1; return acc; }, {}));
    const body: OEMPendingPage = { count: shown.length, next: null, previous: null, results: shown, tiers, apply_tiers: {}, statuses,
      facets: { chains: count('chain'), makes: count('brand'), systems: [] }, apply: { AUTO_FLAG_CURRENT: state('AUTO_FLAG_CURRENT'), AUTO_RENAME_BASE: state('AUTO_RENAME_BASE'),
        STRONG_PENDING_OWNER_TAGS: state('STRONG_PENDING_OWNER_TAGS') }, sizes: { canary: 50, batch: 100 }, halt, busy: { lock: false, running: null },
      last_refresh: { id: 12, mode: 'dry_run', stage: 'command', finished_at: stamp } };
    return route.fulfill({ json: body });
  });
  await page.route('**/api/management/oem-finder/pending/preview', route => {
    const body = route.request().postDataJSON() as { tier: OEMApplyTier; mode: 'canary' | 'batch' };
    previews.push(body);
    const picked = rows.filter(row => row.status === 'pending' && row.apply_tier === body.tier).sort((a, b) => Number(b.in_stock) - Number(a.in_stock));
    const result: OEMPendingPreview = { tier: body.tier, mode: body.mode, size: body.mode === 'canary' ? 50 : 100, eligible: picked.length, selected: picked.length, would_apply: picked.length - 1,
      conflicts: 1, skipped: {}, excluded: { sent_to_review: 1 }, canary_done: credit[body.tier], halt: null, suffix_table_version: 'db:9', seconds: 6.4,
      rows: picked.map((row, index) => ({ part_id: row.part_id, sku: row.sku, description: row.description, oem: row.candidate, brand: row.brand, method: row.method,
        outcome: index === picked.length - 1 ? 'conflict' : 'applied', detail: index === picked.length - 1 ? 'HYUNDAI 56820-2W000 ya es un alterno de OTRO-1.' : '',
        available_quantity: row.available_quantity, in_stock: row.in_stock })) };
    return route.fulfill({ json: result });
  });
  await page.route('**/api/management/oem-finder/pending/apply', route => {
    const body = route.request().postDataJSON() as { tier: OEMApplyTier; mode: 'canary' | 'batch'; operation_id: string };
    applies.push(body);
    if (halt) return route.fulfill({ status: 409, json: { detail: `La aplicación automática está detenida: ${halt.reason}. Levanta la detención para continuar.`, reason: 'halted' } });
    if (body.mode === 'batch' && !credit[body.tier]) return route.fulfill({ status: 409, json: { detail: 'Primero aplica un canario.', reason: 'needs_canary' } });
    if (busyNext) { busyNext -= 1; return route.fulfill({ status: 409, json: { detail: 'Hay un análisis de coincidencias o una búsqueda OEM en curso; vuelve a intentarlo cuando termine.' } }); }
    let created = operations[body.operation_id];
    const replayed = !!created;
    if (!created) {
      const taken = rows.filter(row => row.status === 'pending' && row.apply_tier === body.tier);
      taken.forEach(row => { row.status = 'applied'; });
      credit[body.tier] = true;
      created = run(56 + runs.length, { status: runStatus, scope: { tier: body.tier, canary: body.mode === 'canary' ? 50 : null, limit: body.mode === 'batch' ? 100 : null, in_stock_first: true, batch_size: 100, stage: 'admin', operation_id: body.operation_id },
        applied: { applied: taken.length, conflicts: 0, skipped: {}, errors: 0, tiers: { [body.tier]: taken.length }, batches: [{ index: 1, applied: taken.length, conflicts: 0, skipped: 0, errors: 0 }],
          stopped: null, excluded: {}, selected: taken.length, spot_check: [1] }, actor: 'ADMIN', changes: taken.length });
      operations[body.operation_id] = created;
      runs.push(created);
    }
    if (failNext) { failNext -= 1; return route.fulfill({ status: 502, json: { detail: 'No se pudo conectar con MotionPartes. Inténtalo de nuevo en unos momentos.' } }); }
    return route.fulfill({ status: replayed ? 200 : 201, json: { run: created, replayed } });
  });
  await page.route('**/api/management/oem-finder/pending/send-to-review', route => {
    const body = route.request().postDataJSON() as { parts: string[] };
    sends.push(body);
    const sent = rows.filter(row => body.parts.includes(row.part_id) && row.status === 'pending');
    sent.forEach(row => { row.status = 'sent_to_review'; });
    return route.fulfill({ json: { sent: sent.map(row => ({ part_id: row.part_id, sku: row.sku, case: row.id + 100 })), already: [], skipped: [] } });
  });
  return { rows, credit, unexpected, lists, previews, applies, sends, setHalt: (value: OEMHalt | null) => { halt = value; },
    failApply: (status: OEMRun['status'] = 'completed') => { failNext = 1; runStatus = status; }, busyApply: () => { busyNext = 1; } };
}

async function openRuns(page: Page, width: number) {
  await page.setViewportSize({ width, height: 1000 });
  await page.goto('/administracion?seccion=inventario');
  await page.getByRole('button', { name: 'Aplicación OEM', exact: true }).click();
  return page.getByRole('dialog', { name: 'Aplicación OEM automática' });
}

for (const width of [1440, 390]) {
  test(`OEM pending auto rows: preview, canary, batch, send to review and halt at ${width}px`, async ({ page }) => {
    const mock = await fixture(page);
    const dialog = await openRuns(page, width);
    const pending = dialog.getByRole('region', { name: 'Pendientes de aplicación automática' });
    await expect(pending.getByRole('button', { name: 'SKU YA ES EL OEM (3)' })).toHaveAttribute('aria-pressed', 'true');
    await expect(pending.getByRole('button', { name: 'RENOMBRAR A BASE OEM (2)' })).toBeVisible();
    await expect(pending.getByText('54830-1R000', { exact: true })).toBeVisible();
    await expect(pending.getByText('EL SKU YA ES EL OEM: SE MARCA COMO OEM').first()).toBeVisible();
    await expect(dialog.getByRole('heading', { name: 'Historial de ejecuciones' })).toBeVisible();  // the run history stays below
    await expect(dialog.getByText('#12', { exact: true })).toBeVisible();
    expect(mock.lists[0].get('status')).toBe('pending');
    expect(mock.lists[0].get('tier')).toBe('AUTO_FLAG_CURRENT');

    const flag = pending.getByRole('group', { name: 'Aplicación de SKU YA ES EL OEM' });
    await expect(flag).toContainText('3 PENDIENTES · 1 CON EXISTENCIAS');
    await expect(flag).toContainText('FALTA EL CANARIO');
    const batch = flag.getByRole('button', { name: 'Aplicar lote (100) de SKU YA ES EL OEM' });
    await expect(batch).toBeDisabled();
    await expect(flag.getByText('Aplica primero un canario de 50: los lotes de 100 se habilitan cuando termina.')).toBeVisible();

    await flag.getByRole('button', { name: 'Vista previa de SKU YA ES EL OEM' }).click();
    const preview = pending.getByRole('region', { name: 'Vista previa de la aplicación' });
    await expect(preview).toContainText('Se tomarían 3 de 3 SKU: 2 se aplicarían, 1 pasarían a revisión por conflicto y 0 se omitirían. Solo lectura: no se escribió nada.');
    await expect(preview).toContainText('1 excluidos: se enviaron a la revisión OEM.');
    await expect(preview.getByText('PASARÍA A CONFLICTO')).toBeVisible();
    expect(mock.previews).toEqual([{ tier: 'AUTO_FLAG_CURRENT', mode: 'canary' }]);
    expect(mock.applies).toEqual([]);

    await flag.getByRole('button', { name: 'Aplicar canario (50) de SKU YA ES EL OEM' }).click();
    const confirm = pending.getByRole('alertdialog', { name: 'Aplicar canario · SKU YA ES EL OEM' });
    await expect(confirm).toContainText('Se aplicarán hasta 3 SKU de 3 pendientes de este nivel');
    expect(mock.applies).toEqual([]);
    await confirm.getByRole('button', { name: 'Cancelar' }).click();
    await expect(confirm).toBeHidden();
    await flag.getByRole('button', { name: 'Aplicar canario (50) de SKU YA ES EL OEM' }).click();
    mock.failApply();  // the first response is lost: the retry reuses the operation id and the server replays the run
    await pending.getByRole('button', { name: 'Aplicar 3 SKU' }).click();
    await expect(pending.getByRole('alert')).toContainText('No se pudo confirmar el resultado');
    await pending.getByRole('button', { name: 'Aplicar 3 SKU' }).click();
    await expect(pending.getByRole('status')).toContainText('Ejecución #57 (canario de 50): 3 aplicados');
    await expect(pending.getByRole('status')).toContainText('Esta operación ya se había ejecutado: no se aplicó nada nuevo.');
    expect(mock.applies).toHaveLength(2);
    expect(mock.applies[0].operation_id).toMatch(uuidPattern);
    expect(mock.applies[1]).toEqual(mock.applies[0]);
    expect(mock.applies[0]).toMatchObject({ tier: 'AUTO_FLAG_CURRENT', mode: 'canary' });
    await expect(pending.getByRole('button', { name: 'SKU YA ES EL OEM (0)' })).toBeVisible();
    await expect(flag).toContainText('CANARIO HECHO: LOTES HABILITADOS');
    await expect(flag.getByRole('button', { name: 'Aplicar canario (50) de SKU YA ES EL OEM' })).toHaveCount(0);
    await pending.getByRole('button', { name: 'Ver ejecución #57 en el historial' }).click();
    await expect(dialog.locator('#oem-run-57')).toHaveClass(/oem-run-focus/);
    await expect(dialog.locator('#oem-run-57')).toContainText('DESDE PENDIENTES · ADMIN');

    await pending.getByRole('button', { name: 'RENOMBRAR A BASE OEM (2)' }).click();
    const confirmed = pending.getByRole('group', { name: 'Aplicación de RENOMBRAR (ETIQUETAS CONFIRMADAS)' });
    await expect(confirmed).toContainText('1 PENDIENTES · 1 CON EXISTENCIAS');
    await expect(pending.getByRole('group', { name: 'Aplicación de RENOMBRAR A LA BASE OEM' })).toBeVisible();
    await expect(pending.getByText('17100-M79M00-G → 17100-M79M00')).toBeVisible();
    await expect(pending.getByText('-G · ETIQUETA · CONFIRMADA')).toBeVisible();
    await expect(pending.getByText('-MOBIS · ETIQUETA · AUTOMÁTICA')).toBeVisible();
    await pending.getByLabel('Sufijo').selectOption('G');
    await expect(pending.getByText('54830-2H000-MOBIS → 54830-2H000')).toBeHidden();
    expect(mock.lists[mock.lists.length - 1].get('chain')).toBe('G');
    expect(mock.lists[mock.lists.length - 1].get('tier')).toBe('AUTO_RENAME_BASE');
    await pending.getByRole('button', { name: 'Quitar filtros' }).click();
    await pending.getByRole('checkbox', { name: 'Seleccionar 17100-M79M00-G' }).check();
    await expect(pending.getByRole('group', { name: 'Seleccionados' })).toContainText('1 SKU seleccionado');
    await pending.getByRole('button', { name: 'Enviar seleccionados a revisión' }).click();
    await expect(pending.getByRole('status')).toContainText('1 SKU enviado a la Revisión OEM: la aplicación automática ya no los tomará.');
    expect(mock.sends).toEqual([{ parts: [mock.rows[3].part_id] }]);
    await expect(pending.getByText('17100-M79M00-G → 17100-M79M00')).toBeHidden();
    await pending.getByRole('button', { name: 'Enviar 54830-2H000-MOBIS a revisión' }).click();
    await expect(pending.getByRole('status')).toContainText('1 SKU enviado');
    expect(mock.sends[1]).toEqual({ parts: [mock.rows[4].part_id] });
    await expect(pending.getByText('No hay SKU pendientes con estos filtros.')).toBeVisible();

    mock.rows.push(pendingRow(6, '43212-KK010', { description: 'MUNON TOY HILUX LH', brand: 'TOYOTA', system: 'TOYOTA' }));
    mock.setHalt({ reason: '2 FILAS INCORRECTAS', run: 57, created_by: 'ADMIN', created_at: stamp });
    await pending.getByRole('button', { name: 'SKU YA ES EL OEM (0)' }).click();  // the tab reloads: the new row and the halt arrive
    await expect(pending.getByRole('button', { name: 'SKU YA ES EL OEM (1)' })).toHaveAttribute('aria-pressed', 'true');
    await expect(flag.getByText('Aplicación detenida: 2 FILAS INCORRECTAS. Levanta la detención (arriba) para continuar.')).toBeVisible();
    await expect(flag.getByRole('button', { name: 'Aplicar lote (100) de SKU YA ES EL OEM' })).toBeDisabled();
    await expect(flag.getByRole('button', { name: 'Vista previa de SKU YA ES EL OEM' })).toBeEnabled();  // a preview never writes
    expect(mock.applies).toHaveLength(2);
    expect(mock.unexpected).toEqual([]);
  });
}

test('a refused apply closes the confirmation and shows the reason', async ({ page }) => {
  const mock = await fixture(page);
  const dialog = await openRuns(page, 1440);
  const pending = dialog.getByRole('region', { name: 'Pendientes de aplicación automática' });
  await pending.getByRole('button', { name: 'Aplicar canario (50) de SKU YA ES EL OEM' }).click();
  mock.setHalt({ reason: 'MUESTRA CON ERRORES', run: null, created_by: 'ADMIN', created_at: stamp });  // halted by someone else meanwhile
  await pending.getByRole('button', { name: 'Aplicar 3 SKU' }).click();
  await expect(pending.getByRole('alert')).toContainText('La aplicación automática está detenida: MUESTRA CON ERRORES.');
  await expect(pending.getByRole('alertdialog')).toHaveCount(0);
  expect(mock.rows.filter(row => row.status === 'applied')).toEqual([]);
  expect(mock.unexpected).toEqual([]);
});

test('a busy apply keeps the confirmation and its operation id; filters never narrow the apply', async ({ page }) => {
  const mock = await fixture(page);
  const dialog = await openRuns(page, 1440);
  const pending = dialog.getByRole('region', { name: 'Pendientes de aplicación automática' });
  await pending.getByLabel('Existencias').selectOption('true');
  await expect(pending.getByText('48654-30030', { exact: true })).toBeHidden();
  await pending.getByRole('button', { name: 'Aplicar canario (50) de SKU YA ES EL OEM' }).click();
  const confirm = pending.getByRole('alertdialog', { name: 'Aplicar canario · SKU YA ES EL OEM' });
  await expect(confirm).toContainText('Se aplicarán hasta 3 SKU de 3 pendientes de este nivel');
  await expect(confirm).toContainText('Los filtros y la selección solo afectan la lista: la aplicación toma los primeros SKU pendientes del nivel completo');
  await expect(confirm.getByRole('button', { name: 'Cancelar' })).toBeFocused();
  mock.busyApply();  // a matching pass (or this operation's own lost first request) holds the lock: nothing started
  await confirm.getByRole('button', { name: 'Aplicar 3 SKU' }).click();
  await expect(pending.getByRole('alert')).toContainText('La confirmación sigue abierta con la misma operación');
  await expect(confirm).toBeVisible();
  mock.failApply('failed');  // the run fails and its 500 is lost on the way: the retry replays it and says so
  await confirm.getByRole('button', { name: 'Aplicar 3 SKU' }).click();
  await expect(pending.getByRole('alert')).toContainText('No se pudo confirmar el resultado');
  await confirm.getByRole('button', { name: 'Aplicar 3 SKU' }).click();
  await expect(pending.getByRole('status')).toContainText('Ejecución #57 falló: 3 cambios escritos. Revísala en el historial antes de continuar');
  await expect(pending.getByRole('status')).toContainText('Esta operación ya se había ejecutado: no se aplicó nada nuevo.');
  expect(mock.applies).toHaveLength(3);
  expect(new Set(mock.applies.map(body => body.operation_id)).size).toBe(1);
  expect(mock.unexpected).toEqual([]);
});

test('before migration 0038 the pending section explains it and the run history keeps working', async ({ page }) => {
  const mock = await fixture(page);
  await page.route(/\/api\/management\/oem-finder\/pending(?:\?.*)?$/, route => route.fulfill({ status: 503, json: { detail: 'Los pendientes de aplicación automática aún no están disponibles: faltan migraciones por aplicar.' } }));
  const dialog = await openRuns(page, 390);
  const pending = dialog.getByRole('region', { name: 'Pendientes de aplicación automática' });
  await expect(pending.getByRole('alert')).toContainText('faltan migraciones por aplicar');
  await expect(pending.getByRole('button', { name: 'Intentar de nuevo' })).toBeVisible();
  await expect(dialog.getByText('#12', { exact: true })).toBeVisible();
  await expect(dialog.getByRole('button', { name: 'Muestra de control de la ejecución 12' })).toBeEnabled();
  expect(mock.unexpected).toEqual([]);
});

test('the proxy only forwards the pending routes it allows', async ({ page }) => {
  test.skip(!process.env.E2E_ADMIN_TOKEN, 'A disposable superuser token is required for server-rendered administration.');
  await page.context().addCookies([{ name: 'partsmall_session', value: process.env.E2E_ADMIN_TOKEN!, url: 'http://localhost:8080', httpOnly: true, sameSite: 'Strict' }]);
  await page.goto('/');
  const statuses = await page.evaluate(async () => Promise.all([
    fetch('/api/management/oem-finder/pending/preview').then(r => r.status),
    fetch('/api/management/oem-finder/pending/delete', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }).then(r => r.status),
  ]));
  expect(statuses).toEqual([404, 404]);
});
