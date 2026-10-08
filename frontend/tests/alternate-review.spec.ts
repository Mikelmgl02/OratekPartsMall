import { expect, Page, test } from '@playwright/test';
import type { AlternateReviewDetail, ReviewAlterno } from '../lib/oem-reference-types';

const id = '0a000000-0000-4000-8000-000000000001';
const catalog = 'GMB Water Pump Catalog 2016';
const empty = { count: 0, next: null, previous: null, results: [], pending_count: 0 };
const ref = (code: string) => ({ id: `0e000000-0000-4000-8000-00000000${code.slice(-4)}`, manufacturer: 'TOYOTA', code, status: 'declared' as const });
const alterno = (pk: number, brand: string, code: string, filed: string[] = []): ReviewAlterno => ({ id: pk, brand, code, reference_source: `${catalog} · pág. 216`,
  filed_under: filed.map(number => ({ reference: ref(number), description: 'BOMBA DE AGUA', applications: 'TOYOTA 1VZFE 2000 CAMRY VCV10' })), filed_count: filed.length });

// Mirrors mall.alternate_review: keep confirms an undecided code, remove deletes one, remove_copies deletes the copies.
async function fixture(page: Page) {
  test.skip(!process.env.E2E_ADMIN_TOKEN, 'A disposable superuser token is required for server-rendered administration.');
  await page.context().addCookies([{ name: 'partsmall_session', value: process.env.E2E_ADMIN_TOKEN!, url: 'http://localhost:8080', httpOnly: true, sameSite: 'Strict' }]);
  const review: AlternateReviewDetail = {
    part: { id, sku: '16100-69205-RAZ', name: '', description: 'BOMBA AGUA TOY HIACE 2RZ', category: 'REFRIGERACIÓN', subcategory: 'BOMBAS DE AGUA', active: true, is_OEM: false },
    numbers: [{ reference: ref('1610069205'), description: 'BOMBA DE AGUA', applications: 'TOYOTA 2RZ HIACE', via: ['sku_name'], printed_for: [{ catalog, brand_codes: ['GWT-71A', 'GWT-80A'] }] }],
    undecided: [alterno(1, 'ASAHI', 'A1773'), alterno(2, 'GMB', 'GWT-71A', ['1610059305'])],
    copies: [alterno(3, 'AISIN', 'WPT-080')],
  };
  const unexpected: string[] = [], posts: Record<string, unknown>[] = [];
  await page.route('**/api/**', route => { unexpected.push(`${route.request().method()} ${new URL(route.request().url()).pathname}`); return route.fulfill({ status: 404, json: { detail: 'Ruta no prevista' } }); });
  await page.route('**/api/market/analytics/events', route => route.fulfill({ status: 202, json: { recorded: true } }));
  await page.route(/\/api\/management\/roles(?:\?.*)?$/, route => route.fulfill({ json: [] }));
  await page.route(/\/api\/management\/alternates(?:\?.*)?$/, route => route.fulfill({ json: empty }));
  await page.route(/\/api\/management\/alternate-review$/, route => route.fulfill({ json: review.undecided.length
    ? { count: 1, results: [{ part: review.part, undecided: review.undecided.length, copies: review.copies.length }] } : { count: 0, results: [] } }));
  await page.route(`**/api/management/alternate-review/${id}`, route => {
    if (route.request().method() === 'POST') {
      const body = route.request().postDataJSON() as { action: string; alternate?: number };
      posts.push(body);
      if (body.action === 'remove_copies') review.copies = [];
      else review.undecided = review.undecided.filter(code => code.id !== body.alternate);
    }
    return route.fulfill({ json: review });
  });
  return { unexpected, posts };
}

for (const width of [1440, 390]) {
  test(`a superuser keeps or removes each catalog alterno no OEM number carries, then the copies, at ${width}px`, async ({ page }) => {
    const mock = await fixture(page);
    await page.setViewportSize({ width, height: 1000 });
    await page.goto('/administracion?seccion=alternos&vista=revisar');
    await expect(page.getByRole('button', { name: /^Por revisar/ })).toHaveAttribute('aria-pressed', 'true');
    await expect(page.getByLabel('1 SKU por revisar', { exact: true })).toBeVisible();
    const list = page.getByRole('region', { name: 'SKU por revisar' });
    await expect(list.getByText('16100-69205-RAZ', { exact: true })).toBeVisible();
    await list.getByRole('button', { name: 'Revisar alternos de 16100-69205-RAZ', exact: true }).click();
    const numbers = page.getByRole('region', { name: 'Números OEM del SKU' });
    await expect(numbers).toContainText(`${catalog} lo imprime para GWT-71A, GWT-80A · VARIAS PIEZAS`);
    const undecided = page.getByRole('region', { name: 'Códigos por decidir' });
    await expect(undecided).toContainText('TOYOTA 1610059305');
    await expect(undecided).toContainText('Ningún número OEM lleva este código.');
    expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)).toBe(false);
    await page.screenshot({ path: `/tmp/motionpartes-alternate-review-${width}.png`, fullPage: true });
    await undecided.getByRole('button', { name: 'Conservar ASAHI A1773', exact: true }).click();
    await expect(page.getByRole('status')).toHaveText('ASAHI A1773 confirmado como alterno del SKU.');
    await undecided.getByRole('button', { name: 'Retirar GMB GWT-71A', exact: true }).click();
    await undecided.getByRole('button', { name: 'Confirmar retiro', exact: true }).click();
    await expect(page.getByRole('status')).toHaveText('GMB GWT-71A retirado del SKU.');
    await expect(undecided).toContainText('No quedan códigos por decidir.');
    const copies = page.getByRole('region', { name: 'Copias' });
    await copies.getByRole('button', { name: 'Retirar la copia', exact: true }).click();
    await copies.getByRole('button', { name: 'Confirmar retiro de 1 copia', exact: true }).click();
    await expect(page.getByText('Revisión completa: este SKU ya no tiene alternos de catálogo por revisar.')).toBeVisible();
    await page.getByRole('button', { name: 'Volver a la lista', exact: true }).click();
    await expect(page.getByText('No hay alternos de catálogo por revisar.')).toBeVisible();
    expect(mock.posts).toEqual([{ action: 'keep', alternate: 1 }, { action: 'remove', alternate: 2 }, { action: 'remove_copies' }]);
    expect(mock.unexpected).toEqual([]);
  });
}
