import { expect, Page, test } from '@playwright/test';

type Part = { id: string; sku: string; name: string; description: string; codes: { brand: string; code: string }[] };
type CatalogPage = { count: number; next: string | null; previous: string | null; results: Part[] };
type CatalogGate = {
  requested: Promise<void>; finished: Promise<void>; respond: (value: CatalogPage) => void;
  reply: { promise: Promise<CatalogPage>; resolve: (value: CatalogPage) => void };
  signal: () => void; complete: () => void;
};

const accounts = [
  { id: 'client-a', name: 'EMPRESA A', roles: ['client_business'], capabilities: ['client'] },
  { id: 'client-b', name: 'EMPRESA B', roles: ['client_business'], capabilities: ['client'] },
];
const emptyCatalog: CatalogPage = { count: 0, next: null, previous: null, results: [] };
function parts(prefix: string, count: number): Part[] {
  return Array.from({ length: count }, (_, index) => ({ id: `${prefix}-${index}`, sku: `${prefix}-${String(index + 1).padStart(3, '0')}`, name: `${prefix}-${String(index + 1).padStart(3, '0')}`, description: 'REPUESTO DE PRUEBA', codes: [] }));
}
function catalog(results: Part[], count = results.length, next: string | null = null, previous: string | null = null): CatalogPage {
  return { count, next, previous, results };
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => { resolve = done; });
  return { promise, resolve };
}

async function mockClient(page: Page) {
  await page.route('**/api/session', route => route.fulfill({ json: { authenticated: true, user: { id: 1, username: 'CLIENTE', first_name: '', last_name: '', is_superuser: false }, accounts: { count: 2, next: null, previous: null, results: accounts } } }));
}

async function controlledCatalog(page: Page) {
  const all: CatalogGate[] = [];
  const planned = new Map<string, CatalogGate[]>();
  const unexpected: string[] = [];
  function queue(search = '', pageNumber = 1): CatalogGate {
    const requested = deferred<void>();
    const reply = deferred<CatalogPage>();
    const finished = deferred<void>();
    const item = { requested: requested.promise, finished: finished.promise, respond: reply.resolve, reply, signal: requested.resolve, complete: finished.resolve };
    const key = `${search}:${pageNumber}`;
    planned.set(key, [...(planned.get(key) || []), item]); all.push(item);
    return item;
  }
  await page.route(/\/api\/market\/catalog(?:\?.*)?$/, async route => {
    const url = new URL(route.request().url());
    const key = `${url.searchParams.get('search') || ''}:${url.searchParams.get('page') || '1'}`;
    const item = planned.get(key)?.shift();
    if (!item) { unexpected.push(key); await route.fulfill({ json: emptyCatalog }); return; }
    item.signal();
    try {
      await route.fulfill({ json: await item.reply.promise });
    } catch (error) {
      // An obsolete fetch may be aborted while this controlled reply is delayed.
      if (!route.request().failure()) throw error;
    } finally { item.complete(); }
  });
  return { queue, unexpected, release: () => all.forEach(item => item.respond(emptyCatalog)) };
}

async function pinCatalog(page: Page) {
  await page.locator('.part-card').nth(15).scrollIntoViewIfNeeded();
  const viewportTop = Math.round((await page.getByTestId('page-viewport').boundingBox())!.y);
  await expect.poll(async () => Math.round((await page.locator('.catalog-sticky').boundingBox())?.y ?? -999)).toBe(viewportTop);
  expect((await page.locator('.app-toolbar').boundingBox())!.y).toBe(0);
  expect(await page.evaluate(() => window.scrollY)).toBe(0);
}

async function expectPinnedResult(page: Page, result: ReturnType<Page['locator']>) {
  const sticky = await page.locator('.catalog-sticky').boundingBox();
  const box = await result.boundingBox();
  expect(sticky).not.toBeNull(); expect(box).not.toBeNull();
  const viewport = (await page.getByTestId('page-viewport').boundingBox())!;
  expect(Math.abs(sticky!.y - viewport.y)).toBeLessThanOrEqual(1);
  expect(box!.y).toBeGreaterThanOrEqual(sticky!.y + sticky!.height - 1);
  expect(box!.y).toBeLessThan(page.viewportSize()!.height);
}

async function expectInitialSkeletons(page: Page) {
  await expect(page.locator('.part-card.skeleton-card')).toHaveCount(6);
  await expect(page.locator('.catalog-skeleton')).toHaveCount(6);
  await expect(page.locator('.catalog-skeleton').first()).toBeVisible();
  await expect(page.getByText('Cargando el catálogo…', { exact: true })).toBeAttached();
  await expect(page.getByRole('region', { name: 'Resultados del catálogo', exact: true })).toHaveAttribute('aria-busy', 'true');
}

async function expectPendingSkeletons(page: Page, count: number) {
  await expect(page.locator('.part-card')).toHaveCount(count);
  await expect(page.locator('.part-card.is-loading')).toHaveCount(count);
  await expect(page.locator('.catalog-skeleton')).toHaveCount(count);
  await expect(page.locator('.catalog-skeleton').first()).toBeVisible();
  await expect(page.locator('.part-card').first()).toHaveAttribute('aria-hidden', 'true');
  await expect(page.locator('.part-card').first()).toHaveAttribute('inert', '');
  await expect(page.getByRole('region', { name: 'Resultados del catálogo', exact: true })).toHaveAttribute('aria-busy', 'true');
}

for (const viewport of [{ label: 'desktop', width: 1440, height: 1000 }, { label: 'mobile', width: 390, height: 844 }]) {
  test(`${viewport.label}: search skeletons preserve card space and pinned controls through one or zero results`, async ({ page }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await mockClient(page);
    const mock = await controlledCatalog(page);
    const initial = mock.queue();
    const one = mock.queue('58411');
    const zero = mock.queue('SINRESULTADO');
    const returnFromEmpty = mock.queue('REGRESO');
    const input = page.getByLabel('Buscar repuestos, marcas o códigos', { exact: true });
    try {
      await page.goto('/'); await initial.requested;
      await expectInitialSkeletons(page);
      await page.locator('#catalog').evaluate(section => {
        const viewport = document.querySelector<HTMLElement>('.app-viewport')!;
        viewport.scrollTo({ top: section.getBoundingClientRect().top - viewport.getBoundingClientRect().top + viewport.scrollTop + 30, behavior: 'instant' });
      });
      await page.screenshot({ path: `/tmp/motionpartes-skeleton-initial-${viewport.label}.png` });
      initial.respond(catalog(parts('INICIAL', 50)));
      await expect(page.locator('.part-card')).toHaveCount(50);
      await pinCatalog(page);
      const topBefore = (await input.boundingBox())!.y;
      const scrollBefore = await page.getByTestId('page-viewport').evaluate(viewport => viewport.scrollTop);
      const heightBefore = (await page.locator('.parts-grid').boundingBox())!.height;
      await input.fill('58411'); await one.requested;
      await expectPendingSkeletons(page, 50);
      await expect(page.getByRole('heading', { name: 'INICIAL-001', exact: true })).not.toBeVisible();
      expect(Math.abs((await page.locator('.parts-grid').boundingBox())!.height - heightBefore)).toBeLessThanOrEqual(1);
      await expect(page.locator('.results-heading>p')).toContainText('50 repuestos del catálogo');
      await expect(page.locator('.results-heading>p')).not.toContainText('58411');
      await expect(input).toBeFocused();
      expect(Math.abs((await input.boundingBox())!.y - topBefore)).toBeLessThanOrEqual(1);
      expect(Math.abs(await page.getByTestId('page-viewport').evaluate(viewport => viewport.scrollTop) - scrollBefore)).toBeLessThanOrEqual(1);
      await page.screenshot({ path: `/tmp/motionpartes-skeleton-pending-${viewport.label}.png` });
      one.respond(catalog(parts('58411-1R000', 1)));
      await expect(page.locator('.part-card')).toHaveCount(1);
      await expect(page.locator('.catalog-skeleton')).toHaveCount(0);
      await expect(page.locator('.results-heading>p')).toContainText('1 repuesto del catálogo para “58411”');
      await expect(input).toBeFocused();
      expect(Math.abs((await input.boundingBox())!.y - topBefore)).toBeLessThanOrEqual(1);
      await expectPinnedResult(page, page.locator('.part-card').first());
      await page.screenshot({ path: `/tmp/motionpartes-search-stable-${viewport.label}.png` });

      await input.fill('SINRESULTADO'); await zero.requested;
      await expectPendingSkeletons(page, 1);
      await expect(page.locator('.results-heading>p')).toContainText('para “58411”');
      zero.respond(emptyCatalog);
      await expect(page.locator('.part-card')).toHaveCount(0);
      const empty = page.getByRole('heading', { name: 'No encontramos repuestos para esta búsqueda.', exact: true });
      await expect(empty).toBeVisible();
      await expect(page.locator('.results-heading>p')).toContainText('0 repuestos del catálogo para “SINRESULTADO”');
      await expect(input).toBeFocused();
      expect(Math.abs((await input.boundingBox())!.y - topBefore)).toBeLessThanOrEqual(1);
      await expectPinnedResult(page, empty);
      await input.fill('REGRESO'); await returnFromEmpty.requested;
      await expectInitialSkeletons(page);
      await expect(empty).not.toBeVisible();
      await expect(input).toBeFocused();
      expect(Math.abs((await input.boundingBox())!.y - topBefore)).toBeLessThanOrEqual(1);
      returnFromEmpty.respond(catalog(parts('REGRESO', 1)));
      await expect(page.getByRole('heading', { name: 'REGRESO-001', exact: true })).toBeVisible();
      await expect(page.locator('.catalog-skeleton')).toHaveCount(0);
      await expectPinnedResult(page, page.locator('.part-card').first());
      expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)).toBe(false);
      expect(mock.unexpected).toEqual([]);
    } finally { mock.release(); }
  });
}

test('an obsolete delayed search cannot replace newer accepted results', async ({ page }) => {
  await mockClient(page);
  const mock = await controlledCatalog(page);
  const initial = mock.queue();
  const older = mock.queue('ANTERIOR');
  const newer = mock.queue('ACTUAL');
  try {
    await page.goto('/'); await initial.requested; initial.respond(catalog(parts('BASE', 20)));
    await expect(page.locator('.part-card')).toHaveCount(20);
    await pinCatalog(page);
    const input = page.getByLabel('Buscar repuestos, marcas o códigos', { exact: true });
    await input.fill('ANTERIOR'); await older.requested;
    await input.fill('ACTUAL'); await newer.requested;
    newer.respond(catalog(parts('ACTUAL', 1)));
    await expect(page.getByRole('heading', { name: 'ACTUAL-001', exact: true })).toBeVisible();
    older.respond(catalog(parts('OBSOLETO', 3))); await older.finished;
    await page.evaluate(() => new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
    await expect(page.getByRole('heading', { name: 'ACTUAL-001', exact: true })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'OBSOLETO-001', exact: true })).not.toBeVisible();
    await expect(page.locator('.part-card')).toHaveCount(1);
    await expect(page.locator('.results-heading>p')).toContainText('1 repuesto del catálogo para “ACTUAL”');
    await expect(input).toBeFocused();
    expect(mock.unexpected).toEqual([]);
  } finally { mock.release(); }
});

test('switching accounts hides the previous account catalog until the new response arrives', async ({ page }) => {
  await mockClient(page);
  const mock = await controlledCatalog(page);
  const firstAccount = mock.queue();
  const secondAccount = mock.queue();
  try {
    await page.goto('/'); await firstAccount.requested; firstAccount.respond(catalog(parts('CUENTA-A', 2)));
    await expect(page.getByRole('heading', { name: 'CUENTA-A-001', exact: true })).toBeVisible();
    await page.getByLabel('Cuenta activa', { exact: true }).selectOption('client-b'); await secondAccount.requested;
    await expectInitialSkeletons(page);
    await expect(page.getByRole('heading', { name: 'CUENTA-A-001', exact: true })).not.toBeVisible();
    secondAccount.respond(catalog(parts('CUENTA-B', 1)));
    await expect(page.getByRole('heading', { name: 'CUENTA-B-001', exact: true })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'CUENTA-A-001', exact: true })).not.toBeVisible();
    await expect(page.locator('.results-heading>p')).toContainText('1 repuesto del catálogo');
    expect(mock.unexpected).toEqual([]);
  } finally { mock.release(); }
});

test('pagination retains its accepted page while pending and brings the next page into view', async ({ page }) => {
  await mockClient(page);
  const mock = await controlledCatalog(page);
  const firstPage = mock.queue();
  const nextPage = mock.queue('', 2);
  try {
    await page.goto('/'); await firstPage.requested;
    firstPage.respond(catalog(parts('PAGINA-UNO', 50), 100, '/api/market/catalog?page=2'));
    await expect(page.locator('.part-card')).toHaveCount(50);
    await page.getByRole('button', { name: 'Siguiente', exact: true }).click(); await nextPage.requested;
    await expectPendingSkeletons(page, 50);
    await expect(page.getByRole('heading', { name: 'PAGINA-UNO-001', exact: true })).not.toBeVisible();
    await expect(page.getByRole('button', { name: 'Siguiente', exact: true })).toBeDisabled();
    nextPage.respond(catalog(parts('PAGINA-DOS', 50), 100, null, '/api/market/catalog?page=1'));
    await expect(page.getByRole('heading', { name: 'PAGINA-DOS-001', exact: true })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'PAGINA-UNO-001', exact: true })).not.toBeVisible();
    await expectPinnedResult(page, page.locator('.part-card').first());
    await expect(page.locator('.pagination')).toContainText('Página 2');
    expect(mock.unexpected).toEqual([]);
  } finally { mock.release(); }
});

test('mobile list view keeps loading placeholders within the screen under reduced motion', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await mockClient(page);
  const mock = await controlledCatalog(page);
  const initial = mock.queue();
  const search = mock.queue('RODAMIENTO');
  try {
    await page.goto('/'); await initial.requested;
    await expectInitialSkeletons(page);
    await page.getByRole('button', { name: 'Vista de lista', exact: true }).click();
    await expect(page.getByRole('button', { name: 'Vista de lista', exact: true })).toHaveAttribute('aria-pressed', 'true');
    const firstSkeleton = await page.locator('.part-card.skeleton-card').first().boundingBox();
    expect(firstSkeleton!.width).toBeGreaterThan(330);
    expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)).toBe(false);
    initial.respond(catalog(parts('LISTA', 20)));
    await expect(page.locator('.part-card')).toHaveCount(20);
    await pinCatalog(page);
    const heightBefore = (await page.locator('.parts-grid').boundingBox())!.height;
    const input = page.getByLabel('Buscar repuestos, marcas o códigos', { exact: true });
    await input.fill('RODAMIENTO'); await search.requested;
    await expectPendingSkeletons(page, 20);
    expect(Math.abs((await page.locator('.parts-grid').boundingBox())!.height - heightBefore)).toBeLessThanOrEqual(1);
    expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)).toBe(false);
    await expect(input).toBeFocused();
    await page.screenshot({ path: '/tmp/motionpartes-skeleton-list-mobile.png' });
    search.respond(catalog(parts('RODAMIENTO', 1)));
    await expect(page.getByRole('heading', { name: 'RODAMIENTO-001', exact: true })).toBeVisible();
    await expect(page.locator('.catalog-skeleton')).toHaveCount(0);
    await expectPinnedResult(page, page.locator('.part-card').first());
    expect(mock.unexpected).toEqual([]);
  } finally { mock.release(); }
});
