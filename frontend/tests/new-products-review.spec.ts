import { expect, test, type Page } from '@playwright/test';
import { signedOutInbox } from './inbox-route';
import type { ActionableRow, Proposal, ReviewReport, ReviewTab, ReviewVerdictRow } from '../lib/new-product-types';

// Productos nuevos → Revisión IA: the AI reviews the pending proposals batch by batch, the report separates the good from the bad ones, and the
// administrator approves or rejects them in batch. Server-rendered administration needs a session token; every API call is mocked.
const runId = '99999999-9999-4999-8999-999999999999';
const stamp = '2026-10-13T12:00:00Z';
const emptyPage = { count: 0, next: null, previous: null, results: [] };
const id = (n: number) => `0000000${n}-0000-4000-8000-00000000000${n}`;

function proposal(n: number, sku: string, verdict: 'good' | 'bad' | 'unsure', reasons: string[]): Proposal {
  return { id: id(n), status: 'pending', status_label: 'Por revisar', version: 2, sku, description: `PIEZA ${sku}`, brand: 'KAIYI', oem: null, part_type: null, references: [],
    field_sources: { sku: { source: 'supplier' }, description: { source: 'supplier' } }, internal_code: false,
    ai_review: { verdict, confidence: verdict === 'good' ? 0.9 : 0.6, reasons, reason: verdict === 'bad' ? 'bad_data' : '', suggestions: {}, stale: false, run: runId, link_part: null, created_at: stamp },
    item_count: 1, supplier_count: 1, stock: 3, part: null, decided_by: null, decided_at: null, reject_reason: '', reject_reason_label: '', reject_note: '', created_at: stamp,
    updated_at: stamp, earlier: null, items: [] };
}

const rows = [{ ...proposal(1, 'F02-4121010-01AA', 'good', ['Faro de la marca con datos completos.']), oem: { code: 'F02-4121010-01AA', brand: 'KAIYI' } }, proposal(2, 'KYJP000484', 'good', ['Mercancía de la marca: no es un número OEM.']),
  proposal(3, 'FX11-1234567-01', 'bad', ['Sin descripción ni marca.']), proposal(4, 'LOW-1', 'unsure', ['La descripción no basta para decidir.'])];

function verdictRow(row: Proposal): ReviewVerdictRow {
  const review = row.ai_review!;
  return { id: `v-${row.id}`, proposal: row.id, state: 'fresh', method: row.sku === 'FX11-1234567-01' ? 'rule' : 'ai', verdict: review.verdict, reason: review.reason,
    reason_label: review.reason ? 'Datos del proveedor incorrectos' : '', confidence: review.confidence, reasons: review.reasons,
    suggestions: row.sku === 'LOW-1' ? { part_type: { category: 'CARROCERÍA', name: 'FAROS', confidence: 0.4 } } : {},
    applied: row.sku === 'KYJP000484' ? { written: ['internal_code', 'oem'], kept: [] } : row.sku === 'F02-4121010-01AA' ? { written: ['part_type'], kept: [] } : {},
    reviewed_version: 2, link_part: null, supplier_description: row.description, stock: 3, signals: row.sku === 'FX11-1234567-01' ? ['descripcion_de_relleno', 'sin_marca'] : [],
    current: { version: 2, status: 'pending', status_label: 'Por revisar', sku: row.sku, description: row.description, brand: row.brand, internal_code: row.sku === 'KYJP000484', oem: row.oem,
      part_type: row.sku === 'F02-4121010-01AA' ? { id: 'pt', category: 'CARROCERÍA', name: 'FAROS' } : null, field_sources: row.field_sources, part: null } };
}

async function mock(page: Page) {
  const posts: { path: string; body: Record<string, unknown> }[] = [], reads: string[] = [];
  let completed = 0, failNext = true;
  let holdBulk: Promise<void> | null = null, releaseBulk = () => {}, failBulk = false;
  const hold = () => { holdBulk = new Promise(resolve => { releaseBulk = resolve; }); };
  const report = (verdict: ReviewTab, offset = 0): ReviewReport => {
    const done = rows.slice(0, completed * 2);
    const shown = done.filter(row => row.ai_review!.verdict === verdict);
    return { id: runId, search: '', only_unreviewed: true, created_by: 'root', created_at: stamp, finished_at: completed >= 2 ? stamp : null, model: 'gemini',
      metrics: { rule_items: completed >= 2 ? 1 : 0, ai_items: done.length, cost_micro_usd: 42_000, good: 2, bad: 1, unsure: 1 },
      total_items: 4, batch_size: 2, completed_batches: completed, total_batches: 2, status: completed >= 2 ? 'completed' : 'ready',
      counts: { good: done.filter(row => row.ai_review!.verdict === 'good').length, bad: done.filter(row => row.ai_review!.verdict === 'bad').length,
        unsure: done.filter(row => row.ai_review!.verdict === 'unsure').length, stale: 0, decided: 0, link: 0, reject: done.filter(row => row.ai_review!.verdict === 'bad').length },
      verdict, offset, count: shown.length, next_offset: null, results: shown.map(verdictRow) };
  };
  await page.route('**/api/**', route => { if (signedOutInbox(route)) return; return route.fulfill({ json: emptyPage }); });
  await page.route(/\/api\/management\/catalog\/proposals(?:\?.*)?$/, route => route.fulfill({ json: { ...emptyPage, count: rows.length, results: rows } }));
  await page.route(/\/api\/management\/catalog\/proposals\/summary$/, route => route.fulfill({ json: { pending: 4, approved: 0, linked: 0, rejected: 0, resolved: 0, pending_changed_at: stamp } }));
  await page.route(/\/api\/management\/catalog\/proposals\/review(?:\?.*)?$/, route => {
    if (route.request().method() === 'GET') return route.fulfill({ json: { configured: true, cap_micro_usd: 10_000_000, month_spend_micro_usd: 300_000, eligible_count: 4, unreviewed_count: 4, runs: [] } });
    posts.push({ path: 'create', body: route.request().postDataJSON() });
    return route.fulfill({ status: 201, json: report('good') });
  });
  await page.route(/\/api\/management\/catalog\/proposals\/review\/[0-9a-f-]{36}(?:\?.*)?$/, route => {
    const url = new URL(route.request().url());
    const verdict = (url.searchParams.get('verdict') || 'good') as ReviewTab;
    if (route.request().method() === 'POST') {
      const body = route.request().postDataJSON();
      posts.push({ path: 'review', body });
      if (body.batch_index === 1 && failNext) { failNext = false; return route.fulfill({ status: 502, json: { detail: 'La IA tardó demasiado en responder. Puedes reanudar este lote sin duplicar propuestas.' } }); }
      completed = Math.max(completed, body.batch_index + 1);
      return route.fulfill({ json: report(verdict) });
    }
    reads.push(url.searchParams.get('mode') || 'report');
    if (url.searchParams.get('mode') === 'actionable') {
      const wanted = url.searchParams.get('verdict');
      const chosen: ActionableRow[] = rows.filter(row => row.ai_review!.verdict === (wanted === 'good' ? 'good' : 'bad')).map(row => ({ id: row.id, version: 2, sku: row.sku,
        description: row.description, reason: row.ai_review!.reason, note: `Revisión IA: ${row.ai_review!.reasons.join(' ')}`, part: null,
        internal_code: row.sku === 'KYJP000484', oem: row.oem }));
      return route.fulfill({ json: { verdict: wanted, count: chosen.length, results: chosen } });
    }
    return route.fulfill({ json: report(verdict) });
  });
  await page.route(/\/api\/management\/catalog\/proposals\/bulk$/, async route => {
    const body = route.request().postDataJSON();
    posts.push({ path: 'bulk', body });
    if (holdBulk) await holdBulk;
    if (failBulk) { failBulk = false; return route.fulfill({ status: 502, json: { detail: 'El servidor no respondió a tiempo.' } }); }
    return route.fulfill({ json: { action: body.action, done: body.proposals.length, approved: body.action === 'approve' ? body.proposals.length : 0,
      results: body.proposals.map((entry: { id: string }) => ({ id: entry.id, ok: true })) } });
  });
  return { posts, reads, hold, release: () => { releaseBulk(); holdBulk = null; }, failOnce: () => { failBulk = true; } };
}

for (const width of [1440, 390]) {
  test(`the AI reviews the new products, the report separates them, and they are approved and rejected in batch at ${width}px`, async ({ page, baseURL }) => {
    test.skip(!process.env.E2E_ADMIN_TOKEN, 'A disposable superuser token is required for server-rendered administration.');
    await page.setViewportSize({ width, height: 900 });
    await page.context().addCookies([{ name: 'partsmall_session', value: process.env.E2E_ADMIN_TOKEN!, url: baseURL || 'http://localhost:8080', httpOnly: true, sameSite: 'Strict' }]);
    const { posts, reads, hold, release, failOnce } = await mock(page);
    await page.goto('/administracion?seccion=inventario&vista=nuevos');
    const good = page.locator(`.ag-row[row-id="${id(1)}"]`).first();
    await expect(good.getByText('BUENA · 90 %')).toBeVisible();
    await page.getByRole('button', { name: 'Revisión IA', exact: true }).click();
    const modal = page.getByRole('dialog', { name: 'Revisión IA de productos nuevos' });
    await expect(modal).toContainText('4 productos por revisar');
    await expect(modal).toContainText('Gasto de este mes: US$ 0.30 de US$ 10.00');
    await modal.getByRole('button', { name: 'Revisar 4 con IA' }).click();
    // The second batch fails once: the saved progress is recovered and the review resumes from it.
    await expect(modal.getByRole('alert')).toContainText('La IA tardó demasiado en responder');
    await expect(modal).toContainText('1 de 2 lotes revisados');
    expect(reads.at(-1)).toBe('report');                     // the saved progress was read back after the failure
    await modal.getByRole('button', { name: 'Continuar revisión' }).click();
    await expect(modal.getByRole('status').filter({ hasText: 'Revisión terminada' })).toBeVisible();
    expect(posts.filter(post => post.path === 'review').map(post => post.body.batch_index)).toEqual([0, 1, 1]);
    expect(posts[0]).toEqual({ path: 'create', body: { id: expect.any(String), search: '', only_unreviewed: true } });

    const tiles = modal.getByLabel('Resumen de la revisión');
    await expect(tiles).toContainText('2Buenas');
    await expect(tiles).toContainText('1Dudosas');
    await expect(tiles).toContainText('1Malas');
    const lamp = modal.getByRole('article', { name: 'Revisión de F02-4121010-01AA' });
    await expect(lamp).toContainText('BUENA · 90 %');
    await expect(lamp).toContainText('CARROCERÍA / FAROS · IA');
    await expect(lamp).toContainText('OEM KAIYI: F02-4121010-01AA');
    await expect(modal.getByRole('article', { name: 'Revisión de KYJP000484' })).toContainText('CÓDIGO INTERNO · NO OEM');
    await modal.getByRole('button', { name: /^Dudosas/ }).click();
    await expect(modal.getByRole('article', { name: 'Revisión de LOW-1' })).toContainText('¿CARROCERÍA / FAROS? 40 %');
    await modal.getByRole('button', { name: /^Malas/ }).click();
    const bad = modal.getByRole('article', { name: 'Revisión de FX11-1234567-01' });
    await expect(bad).toContainText('MOTIVO: DATOS DEL PROVEEDOR INCORRECTOS');
    await expect(bad).toContainText('DESCRIPCIÓN DE RELLENO');

    await modal.getByRole('button', { name: 'Aprobar 2 buenas…' }).click();
    const approving = modal.getByRole('region', { name: 'Aprobar las buenas' });
    await expect(approving).toContainText('2 productos');
    await expect(approving.getByRole('button', { name: 'Aprobar 2' })).toBeDisabled();
    await expect(approving).toContainText('1 se creará con su código como número OEM de la marca');
    await expect(approving).toContainText('F02-4121010-01AA PIEZA F02-4121010-01AA · OEM KAIYI');
    await expect(approving).toContainText('KYJP000484 PIEZA KYJP000484 · CÓDIGO INTERNO');
    await approving.getByLabel('Revisé el informe').check();
    hold();
    await approving.getByRole('button', { name: 'Aprobar 2' }).click();
    await expect(approving.getByRole('button', { name: 'Detener después de este lote' })).toBeVisible();
    await page.keyboard.press('Escape');                     // a batch in progress keeps the modal open
    await expect(modal).toBeVisible();
    await expect(modal.getByRole('button', { name: 'Volver' })).toBeDisabled();
    release();
    await expect(modal.getByRole('status').filter({ hasText: '2 SKU creados.' })).toBeVisible();
    expect(posts.filter(post => post.path === 'bulk').at(-1)!.body).toEqual({ action: 'approve', proposals: [{ id: id(1), version: 2 }, { id: id(2), version: 2 }] });
    await modal.getByRole('button', { name: 'Rechazar 1 malas…' }).click();
    const rejecting = modal.getByRole('region', { name: 'Rechazar las malas' });
    await expect(rejecting).toContainText('FX11-1234567-01');
    await rejecting.getByLabel('Revisé el informe').check();
    // A lost answer: the report and the list are read again (the server may have decided some), and the retry starts from the fresh list.
    failOnce();
    const before = { reports: reads.filter(read => read === 'report').length, lists: reads.filter(read => read === 'actionable').length };
    await rejecting.getByRole('button', { name: 'Rechazar 1' }).click();
    await expect(rejecting.getByRole('alert')).toContainText('El servidor no respondió a tiempo.');
    await expect.poll(() => reads.filter(read => read === 'report').length).toBeGreaterThan(before.reports);
    await expect.poll(() => reads.filter(read => read === 'actionable').length).toBeGreaterThan(before.lists);
    await rejecting.getByLabel('Revisé el informe').check();
    await rejecting.getByRole('button', { name: 'Rechazar 1' }).click();
    await expect(modal.getByRole('status').filter({ hasText: '1 propuesta rechazada.' })).toBeVisible();
    expect(posts.filter(post => post.path === 'bulk').at(-1)!.body).toEqual({ action: 'reject', proposals: [
      { id: id(3), version: 2, reason: 'bad_data', note: 'Revisión IA: Sin descripción ni marca.' }] });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  });
}
