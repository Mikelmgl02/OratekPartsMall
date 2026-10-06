import { expect, test } from '@playwright/test';

const account = '11111111-1111-4111-8111-111111111111';
const settings = `/api/market/accounts/${account}/pricing/settings`;
const foreign = { Origin: 'https://unrelated.example', Cookie: 'partsmall_session=invalid-test-token' };

test('pricing settings proxy requires a session and same-origin writes', async ({ request }) => {
  for (const response of [await request.get(settings), await request.post(settings, { data: { expected_version: 1 } })]) {
    expect(response.status()).toBe(401);
    expect(await response.json()).toEqual({ detail: 'Inicia sesión para continuar.' });
  }
  const denied = await request.post(settings, { headers: foreign, data: { expected_version: 1, over_stock_policy: 'block' } });
  expect(denied.status()).toBe(403);
  expect(await denied.json()).toEqual({ detail: 'El origen de la solicitud no es válido.' });
  // Only the settings route is allowlisted; its neighbours and other methods stay unknown before any origin check.
  for (const route of [`accounts/${account}/pricing`, `accounts/${account}/pricing/settings/extra`, `accounts/not-a-uuid/pricing/settings`]) {
    expect((await request.post(`/api/market/${route}`, { headers: foreign, data: {} })).status()).toBe(404);
  }
  expect((await request.put(settings, { headers: foreign, data: {} })).status()).toBe(404);
  expect((await request.delete(settings, { headers: foreign })).status()).toBe(404);
});

test('quotation draft proxy routes require a session and same-origin writes', async ({ request }) => {
  const draft = `/api/market/accounts/${account}/requests/${account}/draft`;
  for (const response of [await request.get(draft), await request.post(draft, { data: {} }), await request.post(`${draft}/discard`, { data: {} })]) {
    expect(response.status()).toBe(401);
  }
  for (const path of [draft, `${draft}/discard`]) {
    const denied = await request.post(path, { headers: foreign, data: { save_id: account, expected_draft_version: 0 } });
    expect(denied.status()).toBe(403);
    expect(await denied.json()).toEqual({ detail: 'El origen de la solicitud no es válido.' });
  }
  // Later slices add reprice and the assistant; until then these neighbours, GET on discard and other methods stay unknown.
  for (const route of [`${draft}/reprice`, `${draft}/assistant`, `${draft}/discard/extra`]) {
    expect((await request.post(route, { headers: foreign, data: {} })).status()).toBe(404);
  }
  expect((await request.get(`${draft}/discard`, { headers: foreign })).status()).toBe(404);
  expect((await request.put(draft, { headers: foreign, data: {} })).status()).toBe(404);
  expect((await request.delete(draft, { headers: foreign })).status()).toBe(404);
});
