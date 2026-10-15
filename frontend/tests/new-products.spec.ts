import { expect, test } from '@playwright/test';
import { signedOutInbox } from './inbox-route';
import type { Proposal } from '../lib/new-product-types';
import type { Reference } from '../lib/line-types';
import { makeLine, makeReference } from './lines-route';

// Administración → Inventario → Productos nuevos. Server-rendered administration needs a session token; the screen's API calls are mocked.
const kaiyiId = '11111111-1111-4111-8111-111111111111';
const wurthId = '22222222-2222-4222-8222-222222222222';
const stamp = '2026-10-11T12:00:00Z';
const emptyPage = { count: 0, next: null, previous: null, results: [] };

function proposal(id: string, sku: string, brand: string, oem: Proposal['oem'], overrides: Partial<Proposal> = {}): Proposal {
  return { id, status: 'pending', status_label: 'Por revisar', version: 1, sku, description: `PIEZA ${sku}`, brand, oem, part_type: null, references: [],
    field_sources: { sku: { source: 'supplier' }, description: { source: 'supplier' }, ...(oem ? { oem: { source: 'supplier' as const } } : {}) },
    item_count: 1, supplier_count: 1, stock: 4, part: null, decided_by: null, decided_at: null, reject_reason: '', reject_reason_label: '', reject_note: '',
    created_at: stamp, updated_at: stamp, earlier: null, internal_code: false, ai_review: null,
    items: [{ id: `${id.slice(0, 8)}-item`, supplier: 's1', supplier_name: 'Motion Motors', supplier_invent_id: '10', codigo: sku, brand, description: `PIEZA ${sku}`,
      references: [], source: 'apiag', available: 4, matching_status: 'pending' }], ...overrides };
}

test('Productos nuevos lists the proposed SKUs and approves, edits, rejects and approves in bulk', async ({ page, baseURL }) => {
  test.skip(!process.env.E2E_ADMIN_TOKEN, 'A disposable superuser token is required for server-rendered administration.');
  await page.context().addCookies([{ name: 'partsmall_session', value: process.env.E2E_ADMIN_TOKEN!, url: baseURL || 'http://localhost:8080', httpOnly: true, sameSite: 'Strict' }]);
  let rows = [proposal(kaiyiId, '025CHA-1502606', 'KAIYI', { code: '025CHA-1502606', brand: 'KAIYI' }), proposal(wurthId, '0500-900-013', 'WURTH', null)];
  const posts: { path: string; body: unknown }[] = [], patches: unknown[] = [];
  let conflict = false;
  await page.route('**/api/**', route => { if (signedOutInbox(route)) return; return route.fulfill({ json: emptyPage }); });   // the rest of the inventory screen
  // Each product is a line in preparación (S6) with the same id: its References editor reads and writes management/lines/<id>.
  const lineRefs: Record<string, Reference[]> = {};
  const refWrites: unknown[] = [];
  await page.route(/\/api\/management\/lines\/[0-9a-f-]{36}(\/references)?$/, route => {
    const parts = new URL(route.request().url()).pathname.split('/'), id = parts[4], row = rows.find(entry => entry.id === id)!;
    const line = makeLine(1, row.sku, row.description, { id, estado: row.status === 'pending' ? 'preparacion' : 'publicada', origen: 'proveedor' });
    lineRefs[id] ??= [makeReference(1, line, row.sku, { is_main: true, brand: row.brand }), ...row.references.map((ref, index) => makeReference(index + 2, line, ref.code, { brand: ref.brand, tipo: ref.ref_type === 'oem' ? 'oem' : 'otro' }))];
    if (parts[5] !== 'references') return route.fulfill({ json: { ...line, reference_count: lineRefs[id].length } });
    if (route.request().method() === 'GET') return route.fulfill({ json: lineRefs[id] });
    const body = route.request().postDataJSON() as { code: string; brand: string; tipo: Reference['tipo'] };
    refWrites.push(body);
    const added = makeReference(50 + lineRefs[id].length, line, body.code, { brand: body.brand, tipo: body.tipo });
    lineRefs[id].push(added);
    return route.fulfill({ status: 201, json: { added: true, reference: added } });
  });
  await page.route(/\/api\/management\/catalog\/proposals(?:\?.*)?$/, route => {
    const status = new URL(route.request().url()).searchParams.get('status') || 'pending';
    const shown = rows.filter(row => status === 'all' || row.status === status);
    return route.fulfill({ json: { ...emptyPage, count: shown.length, results: shown } });
  });
  await page.route(/\/api\/management\/catalog\/proposals\/summary$/, route => route.fulfill({ json: {
    pending: rows.filter(row => row.status === 'pending').length, approved: rows.filter(row => row.status === 'approved').length, linked: 0,
    rejected: rows.filter(row => row.status === 'rejected').length, resolved: 0 } }));
  await page.route(/\/api\/management\/catalog\/proposals\/bulk$/, route => {
    const body = route.request().postDataJSON();
    posts.push({ path: 'bulk', body });
    for (const entry of body.proposals) rows = rows.map(row => row.id === entry.id ? { ...row, status: 'approved', status_label: 'Aprobado', part: { id: 'p', sku: row.sku } } : row);
    return route.fulfill({ json: { approved: body.proposals.length, results: body.proposals.map((entry: { id: string }) => ({ id: entry.id, ok: true, sku: 'X' })) } });
  });
  await page.route(/\/api\/management\/catalog\/proposals\/[0-9a-f-]{36}$/, route => {
    const id = new URL(route.request().url()).pathname.split('/').pop()!;
    const current = rows.find(row => row.id === id)!;
    if (route.request().method() === 'GET') return route.fulfill({ json: current });
    const body = route.request().postDataJSON();
    if (route.request().method() === 'PATCH') {
      patches.push(body);
      const updated = { ...current, version: current.version + 1, description: body.description ?? current.description, references: body.references ?? current.references,
        oem: 'oem_code' in body ? body.oem_code ? { code: body.oem_code, brand: body.oem_brand } : null : current.oem,
        field_sources: { ...current.field_sources, description: { source: 'admin' as const, by: 'root' } } };
      rows = rows.map(row => row.id === id ? updated : row);
      return route.fulfill({ json: updated });
    }
    posts.push({ path: id, body });
    if (conflict) return route.fulfill({ status: 409, json: { detail: 'La propuesta cambió mientras la revisabas: vuelve a abrirla.', proposal: { ...current, version: current.version + 1 } } });
    const decided = body.action === 'reject' ? { ...current, status: 'rejected' as const, status_label: 'Rechazado', reject_reason: body.reason }
      : body.action === 'link' ? { ...current, status: 'linked' as const, status_label: 'Vinculado', part: { id: body.part, sku: current.earlier?.sku ?? 'X' } }
      : { ...current, status: 'approved' as const, status_label: 'Aprobado', part: { id: 'p', sku: current.oem?.code ?? current.sku } };
    rows = rows.map(row => row.id === id ? decided : row);
    return route.fulfill({ json: decided });
  });

  await page.goto('/administracion?seccion=inventario&vista=nuevos');
  await expect(page.getByRole('button', { name: /Productos nuevos/ })).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByLabel('2 productos nuevos por revisar')).toBeVisible();
  const kaiyi = page.locator(`.ag-row[row-id="${kaiyiId}"]`).first();
  await expect(kaiyi.getByText('OEM KAIYI', { exact: true })).toBeVisible();
  await expect(kaiyi.getByText('POR INVESTIGAR', { exact: true })).toBeVisible();
  await expect(kaiyi.getByText('Motion Motors · 025CHA-1502606')).toBeVisible();
  await expect(page.locator(`.ag-row[row-id="${wurthId}"]`).first().getByText('SIN OEM', { exact: true })).toBeVisible();
  await page.screenshot({ path: '/tmp/motionpartes-new-products.png' });

  // A conflict keeps the proposal and says why; then the approval goes through.
  conflict = true;
  await page.getByRole('button', { name: 'Aprobar 025CHA-1502606' }).click();
  await expect(page.getByRole('alert').filter({ hasText: 'cambió mientras la revisabas' })).toBeVisible();
  conflict = false;
  await page.getByRole('button', { name: 'Aprobar 025CHA-1502606' }).click();
  await expect(page.getByRole('status').filter({ hasText: 'SKU 025CHA-1502606 publicado y vinculado a 1 artículo.' })).toBeVisible();
  await expect.poll(() => posts.at(-1)).toEqual({ path: kaiyiId, body: { action: 'approve', expected_version: 1 } });
  await expect(page.locator(`.ag-row[row-id="${kaiyiId}"]`)).toHaveCount(0);

  // The detail: correct what is missing, then save and approve in one step.
  await page.getByRole('button', { name: 'Ver detalle de 0500-900-013' }).click();
  const drawer = page.getByRole('dialog', { name: 'Producto nuevo · 0500-900-013' });
  await expect(drawer.getByText('del proveedor').first()).toBeVisible();
  await expect(drawer.getByRole('cell', { name: 'Motion Motors' })).toBeVisible();
  await drawer.screenshot({ path: '/tmp/motionpartes-new-product-detail.png' });
  await drawer.getByLabel(/^Descripción/).fill('CLIP DE COVER DE MOTOR');
  await drawer.getByLabel(/^Número OEM/).fill('90467-10188');
  await drawer.getByLabel('Fabricante del OEM').fill('TOYOTA');
  await drawer.getByRole('button', { name: 'Guardar y aprobar' }).click();
  await expect(drawer).toBeHidden();
  expect(patches).toEqual([{ expected_version: 1, description: 'CLIP DE COVER DE MOTOR', oem_code: '90467-10188', oem_brand: 'TOYOTA' }]);   // only what changed
  await expect.poll(() => posts.at(-1)).toEqual({ path: wurthId, body: { action: 'approve', expected_version: 2 } });
  await expect(page.getByRole('status').filter({ hasText: 'SKU 90467-10188 publicado y vinculado.' })).toBeVisible();

  // Rejecting, and approving several at once. A product linked before offers its SKU; its References are added one by one with their tipo.
  rows = [...rows, proposal('33333333-3333-4333-8333-333333333333', 'A-1', 'ACME', null, { catalog_match: [{ id: 'part-9', sku: 'A1-ACME', code: 'A-1', active: true }] }),
    proposal('44444444-4444-4444-8444-444444444444', 'B-1', 'ACME', null),
    proposal('55555555-5555-4555-8555-555555555555', 'GWT-41A', 'GMB', null, { earlier: { id: 'part-1', sku: '16100-39315' },
      references: [{ brand: 'TOYOTA', code: '16100-39315', ref_type: 'oem' }] })];
  await page.getByRole('button', { name: /^En preparación/ }).click();
  await page.getByRole('button', { name: 'Todos' }).click();
  await page.getByRole('button', { name: /^En preparación/ }).click();
  await expect(page.locator('.ag-row[row-id="55555555-5555-4555-8555-555555555555"]').first().getByText('Ya existe como 16100-39315')).toBeVisible();
  await page.getByRole('button', { name: 'Ver detalle de GWT-41A' }).click();
  const gmb = page.getByRole('dialog', { name: 'Producto nuevo · GWT-41A' });
  await expect(gmb.getByRole('note')).toContainText('ya se vinculó antes al SKU 16100-39315');
  const references = gmb.getByRole('region', { name: 'Referencias de la línea OEM' });
  await expect(references.getByRole('listitem', { name: 'Referencia 16100-39315' })).toContainText('OEM · TOYOTA');
  await references.getByLabel('Código de la nueva referencia').fill('wpt-111');
  await references.getByLabel('Marca de la nueva referencia').fill('aisin');
  await references.getByLabel('Tipo de la nueva referencia').selectOption('aftermarket');
  await references.getByRole('button', { name: 'Agregar referencia', exact: true }).click();
  await expect(references.getByRole('listitem', { name: 'Referencia WPT-111' })).toContainText('AFTERMARKET · AISIN');
  expect(refWrites).toEqual([{ code: 'WPT-111', brand: 'AISIN', tipo: 'aftermarket', estado: 'declarado' }]);
  await expect(gmb.getByLabel(/^Alternos/)).toHaveCount(0);  // the References editor replaced the text field
  await gmb.getByRole('button', { name: 'Vincular a 16100-39315' }).click();
  await expect.poll(() => posts.at(-1)).toEqual({ path: '55555555-5555-4555-8555-555555555555', body: { action: 'link', expected_version: 1, part: 'part-1' } });
  await expect(gmb).toBeHidden();
  await page.getByRole('button', { name: 'Ver detalle de A-1' }).click();
  await expect(page.getByRole('dialog').getByRole('note')).toContainText('Este código ya está en el catálogo');   // its items belong to that SKU
  await expect(page.getByRole('dialog').getByRole('button', { name: 'Vincular a A1-ACME' })).toBeVisible();
  await page.getByRole('dialog').getByRole('button', { name: 'Rechazar', exact: true }).click();
  await page.getByRole('dialog').getByLabel('Motivo').selectOption('not_a_part');
  await page.getByRole('dialog').getByLabel('Nota (opcional)').fill('Es un servicio');
  await page.getByRole('dialog').getByRole('button', { name: 'Rechazar propuesta' }).click();
  await expect.poll(() => posts.at(-1)).toEqual({ path: '33333333-3333-4333-8333-333333333333', body: { action: 'reject', expected_version: 1, reason: 'not_a_part', note: 'Es un servicio' } });
  await page.locator('.ag-row[row-id="44444444-4444-4444-8444-444444444444"] .ag-selection-checkbox input').first().check();
  await expect(page.getByRole('group', { name: 'Seleccionadas' })).toContainText('1 propuesta seleccionada');
  await page.getByRole('button', { name: 'Aprobar seleccionadas' }).click();
  await expect.poll(() => posts.at(-1)).toEqual({ path: 'bulk', body: { action: 'approve', proposals: [{ id: '44444444-4444-4444-8444-444444444444', version: 1 }] } });
  await expect(page.getByRole('status').filter({ hasText: '1 SKU publicado.' })).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});
