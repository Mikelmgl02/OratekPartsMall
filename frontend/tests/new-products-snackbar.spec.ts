import { expect, test, type Page } from '@playwright/test';

// An administrator on the storefront is reminded of the Productos nuevos waiting for review, with a link to the list; nobody else is,
// and nobody else asks the server.
const summaryPath = '/api/management/catalog/proposals/summary';
const account = { id: '33333333-3333-4333-8333-333333333333', name: 'CLIENTE', roles: ['client_business'], capabilities: ['client'] };
type Answer = { pending: number; at: string | null } | 'error';
const at = (hour: number) => `2026-10-12T${String(hour).padStart(2, '0')}:00:00.123456Z`;

async function open(page: Page, { superuser = true, signedIn = true, answers = [{ pending: 3, at: at(9) }] as Answer[] } = {}) {
  const summaries: string[] = [];
  let call = 0;
  await page.route('**/api/session', route => route.fulfill({ json: signedIn ? { authenticated: true,
    user: { id: 41, username: 'MIGUEL', first_name: 'Miguel', last_name: '', is_superuser: superuser },
    accounts: { count: 1, next: null, previous: null, results: [account] } } : { authenticated: false } }));
  await page.route(/\/api\/market\/catalog(?:\?.*)?$/, route => route.fulfill({ json: { count: 0, next: null, previous: null, results: [] } }));
  await page.route(`**${summaryPath}`, route => {
    summaries.push(route.request().method());
    const answer = answers[Math.min(call++, answers.length - 1)];
    return answer === 'error' ? route.fulfill({ status: 500, json: { detail: 'Error interno del resumen.' } })
      : route.fulfill({ json: { pending: answer.pending, approved: 4, linked: 1, rejected: 2, resolved: 0, pending_changed_at: answer.at } });
  });
  await page.goto('/');
  return summaries;
}

const snackbar = (page: Page) => page.locator('.admin-snackbar');
// The region marks when the count (or its error) has arrived, so a hidden reminder is checked after the answer, not before it.
const answered = (page: Page) => expect(page.locator('.admin-snackbar-region[data-loaded]')).toHaveCount(1);

test('an administrator sees how many new products wait and opens the list; closed, it stays closed until a new one arrives', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  const summaries = await open(page, { answers: [{ pending: 40, at: at(9) }, { pending: 40, at: at(9) }, { pending: 0, at: null }, { pending: 25, at: at(8) }, { pending: 25, at: at(11) }] });
  await expect(snackbar(page)).toHaveText(/Hay 40 productos nuevos por revisar\.\s*Revisar/);
  await expect(page.locator('.admin-snackbar-region')).toHaveAttribute('role', 'status');
  await expect(snackbar(page).getByRole('link', { name: 'Revisar' })).toHaveAttribute('href', '/administracion?seccion=inventario&vista=nuevos');
  const box = await snackbar(page).boundingBox();
  expect(box && box.x < 100 && box.y + box.height > 900).toBe(true);  // bottom left, clear of the toasts on the right and centre
  await snackbar(page).getByRole('button', { name: 'Cerrar aviso de productos nuevos' }).click();
  await expect(snackbar(page)).toHaveCount(0);
  for (const expected of [2, 3, 4]) {
    // Same proposals, then all reviewed, then an older one pending again from before the close: still closed for this tab.
    await page.reload();
    await answered(page);
    expect(summaries.length).toBe(expected);
    await expect(snackbar(page)).toHaveCount(0);
  }
  await page.reload();
  await expect(snackbar(page)).toHaveText(/Hay 25 productos nuevos por revisar\./);  // a proposal arrived after the close
  expect(summaries.every(method => method === 'GET')).toBe(true);
});

test('one new product reads in the singular, and the count is read again when the tab comes back', async ({ page }) => {
  const summaries = await open(page, { answers: [{ pending: 0, at: null }, { pending: 1, at: at(10) }] });
  await answered(page);
  await expect(snackbar(page)).toHaveCount(0);
  await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
  await expect(snackbar(page)).toHaveText(/Hay 1 producto nuevo por revisar\./);
  expect(summaries.length).toBe(2);
});

test('nothing pending or a failed count show no reminder and no error', async ({ page }) => {
  await open(page, { answers: [{ pending: 0, at: null }] });
  await answered(page);
  await expect(snackbar(page)).toHaveCount(0);
  await page.unrouteAll({ behavior: 'ignoreErrors' });
  const summaries = await open(page, { answers: ['error'] });
  await answered(page);
  expect(summaries).toEqual(['GET']);
  await expect(snackbar(page)).toHaveCount(0);
  await expect(page.getByText('Error interno del resumen.')).toHaveCount(0);
});

for (const [name, options] of [['a non-administrator', { superuser: false }], ['a guest', { signedIn: false }]] as const) {
  test(`${name} never asks for the count and sees no reminder`, async ({ page }) => {
    const summaries = await open(page, options);
    // The session is in once the menu greets the person; the reminder would mount in that same render.
    await expect(page.locator('.account-menu-trigger-copy')).toContainText('signedIn' in options ? 'Hola, inicia sesión' : 'CLIENTE');
    await expect(page.locator('.admin-snackbar-region')).toHaveCount(0);  // not even mounted
    expect(summaries).toEqual([]);
  });
}

test('on a phone the reminder spans the bottom without widening the page', async ({ page }) => {
  await page.setViewportSize({ width: 360, height: 760 });
  await open(page, { answers: [{ pending: 12345, at: at(9) }] });
  await expect(snackbar(page)).toHaveText(/Hay 12,345 productos nuevos por revisar\./);
  const box = await snackbar(page).boundingBox();
  expect(box && Math.round(box.x) === 12 && Math.round(box.width) === 336 && Math.round(box.y + box.height) === 748).toBe(true);
  expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)).toBe(false);
});
