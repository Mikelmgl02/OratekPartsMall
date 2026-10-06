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
  const decisions = `${draft}/assistant/${account}/decisions`;
  for (const response of [await request.get(draft), await request.post(draft, { data: {} }), await request.post(`${draft}/discard`, { data: {} }),
    await request.post(`${draft}/reprice`, { data: {} }), await request.get(`${draft}/assistant`), await request.post(`${draft}/assistant`, { data: {} }),
    await request.post(decisions, { data: {} })]) {
    expect(response.status()).toBe(401);
  }
  for (const path of [draft, `${draft}/discard`, `${draft}/reprice`, `${draft}/assistant`, decisions]) {
    const denied = await request.post(path, { headers: foreign, data: { save_id: account, expected_draft_version: 0 } });
    expect(denied.status()).toBe(403);
    expect(await denied.json()).toEqual({ detail: 'El origen de la solicitud no es válido.' });
  }
  // GET on discard, reprice or decisions, neighbouring paths and other methods stay unknown.
  for (const route of [`${draft}/discard/extra`, `${draft}/reprice/extra`, `${draft}/assistant/extra`, `${draft}/assistant/${account}`, `${decisions}/extra`,
    `${draft}/assistant/not-a-uuid/decisions`]) {
    expect((await request.post(route, { headers: foreign, data: {} })).status()).toBe(404);
  }
  for (const route of [`${draft}/discard`, `${draft}/reprice`, decisions]) expect((await request.get(route, { headers: foreign })).status()).toBe(404);
  expect((await request.put(`${draft}/assistant`, { headers: foreign, data: {} })).status()).toBe(404);
  expect((await request.put(draft, { headers: foreign, data: {} })).status()).toBe(404);
  expect((await request.delete(draft, { headers: foreign })).status()).toBe(404);
});

test('the supplier-only quotation trace is a session-bound read and nothing else', async ({ request }) => {
  const quotations = `/api/market/accounts/${account}/requests/${account}/quotations`, trace = `${quotations}/${account}/trace`;
  const anonymous = await request.get(trace);
  expect(anonymous.status()).toBe(401);
  expect(await anonymous.json()).toEqual({ detail: 'Inicia sesión para continuar.' });
  // Writes and neighbouring paths stay unknown before reaching the API.
  expect((await request.post(trace, { headers: foreign, data: {} })).status()).toBe(404);
  for (const route of [quotations, `${quotations}/${account}`, `${trace}/extra`, `${quotations}/not-a-uuid/trace`]) {
    expect((await request.get(route, { headers: foreign })).status()).toBe(404);
  }
  expect((await request.delete(trace, { headers: foreign })).status()).toBe(404);
});

test('price lists, the price grid and their history are session-bound, with same-origin writes only', async ({ request }) => {
  const base = `/api/market/accounts/${account}`;
  for (const path of [`${base}/price-lists`, `${base}/prices`, `${base}/prices/${account}/history`, `${base}/pricing/history`, `${base}/prices/export`]) {
    const anonymous = await request.get(path);
    expect(anonymous.status()).toBe(401);
    expect(await anonymous.json()).toEqual({ detail: 'Inicia sesión para continuar.' });
  }
  for (const path of [`${base}/price-lists`, `${base}/price-lists/${account}`, `${base}/prices`]) {
    expect((await request.post(path, { data: {} })).status()).toBe(401);
    const denied = await request.post(path, { headers: foreign, data: { expected_version: 1 } });
    expect(denied.status()).toBe(403);
    expect(await denied.json()).toEqual({ detail: 'El origen de la solicitud no es válido.' });
  }
  // History and exports are reads; neighbouring paths, writes to reads and other methods stay unknown before reaching the API.
  for (const path of [`${base}/prices/${account}/history`, `${base}/pricing/history`, `${base}/price-lists/${account}/archive`, `${base}/prices/${account}`, `${base}/pricing`]) {
    expect((await request.post(path, { headers: foreign, data: {} })).status()).toBe(404);
  }
  for (const path of [`${base}/price-lists/${account}`, `${base}/prices/not-a-uuid/history`, `${base}/prices/import`]) {
    expect((await request.get(path, { headers: foreign })).status()).toBe(404);
  }
  expect((await request.delete(`${base}/prices`, { headers: foreign })).status()).toBe(404);
  expect((await request.put(`${base}/price-lists`, { headers: foreign, data: {} })).status()).toBe(404);
  // The binary export route validates its account and list before any session check.
  expect((await request.get('/api/market/accounts/not-a-uuid/prices/export')).status()).toBe(404);
  expect((await request.get(`${base}/prices/export?list=1;drop`)).status()).toBe(404);
  expect((await request.post(`${base}/prices/export`, { headers: foreign, data: {} })).status()).toBe(405);
});

test('the price import route is session-bound, same-origin for writes and only forwards its own paths', async ({ request }) => {
  const imports = `/api/market/accounts/${account}/prices/import`, job = `${imports}/jobs/${account}`;
  for (const response of [await request.get(`${imports}/template`), await request.get(job), await request.get(`${job}/errors`),
    await request.post(imports, { multipart: { file: { name: 'precios.xlsx', mimeType: 'application/octet-stream', buffer: Buffer.from('x') } } }),
    await request.post(job, { data: { batch_index: 0 } })]) {
    expect(response.status()).toBe(401);
    expect(await response.json()).toEqual({ detail: 'Inicia sesión para continuar.' });
  }
  for (const path of [imports, job]) {
    const denied = await request.post(path, { headers: foreign, data: { batch_index: 0, confirm_new_lists: true } });
    expect(denied.status()).toBe(403);
    expect(await denied.json()).toEqual({ detail: 'El origen de la solicitud no es válido.' });
  }
  // Reads of the upload path, writes to downloads and unknown neighbours stay unknown before any session or origin check.
  for (const [method, path] of [['GET', imports], ['POST', `${imports}/template`], ['POST', `${job}/errors`], ['GET', `${imports}/jobs/not-a-uuid`],
    ['GET', `${job}/extra`], ['GET', `${imports}/other`], ['GET', `/api/market/accounts/not-a-uuid/prices/import/template`]] as const) {
    const response = method === 'GET' ? await request.get(path, { headers: foreign }) : await request.post(path, { headers: foreign, data: {} });
    expect(response.status(), `${method} ${path}`).toBe(404);
  }
  expect((await request.put(job, { headers: foreign, data: {} })).status()).toBe(405);
  expect((await request.delete(job, { headers: foreign })).status()).toBe(405);
});

test('client profiles, commercial rules and the simulator are session-bound, with same-origin writes only', async ({ request }) => {
  const base = `/api/market/accounts/${account}`;
  for (const path of [`${base}/clients`, `${base}/clients/${account}/profile`, `${base}/pricing-rules`, `${base}/pricing-rules?client=${account}&scope=all`]) {
    const anonymous = await request.get(path);
    expect(anonymous.status()).toBe(401);
    expect(await anonymous.json()).toEqual({ detail: 'Inicia sesión para continuar.' });
  }
  for (const path of [`${base}/clients/${account}/profile`, `${base}/pricing-rules`, `${base}/pricing-rules/${account}`, `${base}/pricing-rules/${account}/archive`, `${base}/pricing/simulate`]) {
    expect((await request.post(path, { data: {} })).status()).toBe(401);
    const denied = await request.post(path, { headers: foreign, data: { expected_version: 1 } });
    expect(denied.status()).toBe(403);
    expect(await denied.json()).toEqual({ detail: 'El origen de la solicitud no es válido.' });
  }
  // The client list and single rules are not writable, the simulator and archive are not readable, and neighbours stay unknown.
  for (const path of [`${base}/clients`, `${base}/clients/${account}`, `${base}/clients/not-a-uuid/profile`, `${base}/pricing-rules/${account}/delete`]) {
    expect((await request.post(path, { headers: foreign, data: {} })).status(), path).toBe(404);
  }
  for (const path of [`${base}/pricing/simulate`, `${base}/pricing-rules/${account}`, `${base}/pricing-rules/${account}/archive`, `${base}/clients/${account}`]) {
    expect((await request.get(path, { headers: foreign })).status(), path).toBe(404);
  }
  expect((await request.delete(`${base}/pricing-rules/${account}`, { headers: foreign })).status()).toBe(404);
  expect((await request.put(`${base}/clients/${account}/profile`, { headers: foreign, data: {} })).status()).toBe(404);
});
