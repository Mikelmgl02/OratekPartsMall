import { expect, test } from '@playwright/test';
import { fakeServerBasket } from './server-basket';
import type { Part } from '../lib/types';

// Preguntar al proveedor: from a part's suppliers, the client's conversation with one of them (made the first time), with a question ready.
const account = { id: '11111111-1111-4111-8111-111111111111', name: 'TALLER CENTRAL', roles: ['client_business'], capabilities: ['client'], permission: 'owner' };
const supplier = { id: '22222222-2222-4222-8222-222222222222', name: 'GRUPO RAZOR PARTS' };
const thread = '33333333-3333-4333-8333-333333333333';
const part: Part = { id: '44444444-4444-4444-8444-444444444444', sku: '58411-1R000', name: '', description: 'TAMBOR TRASERO HYU ACCENT', codes: [] };
const offers = [{ supplier_id: supplier.id, supplier_name: supplier.name, in_stock: true, items: [{ id: '55555555-5555-4555-8555-555555555555', codigo: '58411-1R000-G', brand: 'KSM', description: 'TAMBOR' }] }];
const empty = { count: 0, next: null, previous: null, results: [] };

test('a client asks a supplier about a part before ordering: the conversation opens with the question ready, in general', async ({ page }) => {
  const started: unknown[] = [], posted: Record<string, unknown>[] = [];
  await page.route('**/api/**', route => route.fulfill({ json: empty }));
  await page.route('**/api/session', route => route.fulfill({ json: { authenticated: true, user: { id: 9, username: 'TALLER', first_name: '', last_name: '', is_superuser: false },
    accounts: { ...empty, count: 1, results: [account] } } }));
  await page.route('**/api/market/wishlist/state', route => route.fulfill({ json: { part_ids: [], count: 0 } }));
  await page.route(/\/api\/market\/inbox$/, route => route.fulfill({ json: { unread_messages: 0, unread_conversations: 0, unseen_notifications: 0, latest_message: null, latest_notification: null } }));
  await page.route(/\/api\/market\/catalog(?:\?.*)?$/, route => route.fulfill({ json: { ...empty, count: 1, results: [part] } }));
  await page.route(`**/api/market/catalog/${part.id}/suppliers`, route => route.fulfill({ json: offers }));
  await page.route(/\/api\/market\/catalog\/[^/]+\/technical$/, route => route.fulfill({ json: { part: part.id, part_type: null, revision: 0, template: null, values: {}, applications: [], missing_required: [] } }));
  await page.route('**/request-state', route => route.fulfill({ json: { part_id: part.id, totals: { sent_quantity: 0, pending_quantity: 0, reviewed_quantity: 0 }, items: [] } }));
  await fakeServerBasket(page, { parts: [part], offers: { [part.id]: offers } });
  await page.route(`**/api/market/accounts/${account.id}/threads`, route => {
    started.push(route.request().postDataJSON());
    return route.fulfill({ json: { thread_id: thread, account: { id: account.id, name: account.name }, counterpart: supplier } });
  });
  const row = { id: thread, order_id: null, reference: '', status: 'pending', orders: [], order_count: 0, open_orders: 0, side: 'client', account: { id: account.id, name: account.name },
    counterpart: supplier, unread: 0, activity: new Date().toISOString(), last_message: null, pinned: false, group: null };
  await page.route(/\/api\/market\/inbox\/conversations(\?.*)?$/, route => route.fulfill({ json: { ...empty, count: 1, results: [row] } }));
  await page.route(new RegExp(`/threads/${thread}/messages(\\?.*)?$`), route => {
    if (route.request().method() === 'POST') {
      const body = route.request().postDataJSON(); posted.push(body);
      return route.fulfill({ json: { id: 1, message_id: body.message_id, body: body.body, account_id: account.id, account_name: account.name, actor_name: 'Taller',
        created_at: new Date().toISOString(), order_id: null, order_reference: null } });
    }
    return route.fulfill({ json: { results: [], cursor: 0, has_more: false, has_earlier: false, assistant_notice: false, thread_id: thread } });
  });
  await page.goto('/');
  await page.getByRole('button', { name: `Ver ${part.sku}`, exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: `Preguntar a ${supplier.name}`, exact: true }).click();
  expect(started).toEqual([{ supplier_id: supplier.id }]);
  const conversation = page.getByRole('dialog', { name: 'Conversaciones', exact: true }).getByRole('region', { name: `Conversación con ${supplier.name}`, exact: true });
  await expect(conversation).toContainText('Consulta, sin órdenes');
  await expect(conversation.getByRole('button', { name: 'Cotizaciones', exact: true })).toHaveCount(0);  // no order to show yet
  await expect(conversation.getByRole('button', { name: /^Solo esta orden/ })).toHaveCount(0);
  const composer = conversation.getByLabel('Mensaje del acuerdo', { exact: true });
  await expect(composer).toHaveValue('Hola, consulta sobre 58411-1R000-G (KSM) · SKU 58411-1R000: ');
  await expect(conversation.getByRole('status')).toContainText('Escribe tu pregunta y envíala');
  await composer.pressSequentially('¿lo tienen para hoy?');
  await composer.press('Enter');
  await expect.poll(() => posted.length).toBe(1);
  expect(posted[0]).toMatchObject({ body: 'Hola, consulta sobre 58411-1R000-G (KSM) · SKU 58411-1R000: ¿lo tienen para hoy?', general: true });
  await expect(conversation.getByRole('log')).toContainText('¿lo tienen para hoy?');
});
