import { NextRequest } from 'next/server';
import { django, json, sessionToken, unavailable, validOrigin } from '@/lib/server-api';

type Context = { params: Promise<{ path: string[] }> };
async function handle(request: NextRequest, context: Context) {
  const token = await sessionToken();
  if (!token) return json({ detail: 'Inicia sesión para continuar.' }, 401);
  const { path } = await context.params;
  const route = path.join('/');
  const uuid = '[a-f0-9-]{36}';
  const readable = new RegExp(`^(accounts|catalog|wishlist|wishlist/state|catalog/${uuid}/technical|catalog/${uuid}/suppliers|accounts/${uuid}/inventory|accounts/${uuid}/inventory/${uuid}/ledger|accounts/${uuid}/deals/${uuid}/messages|accounts/${uuid}/requests|accounts/${uuid}/requests/${uuid}|accounts/${uuid}/sent-requests|accounts/${uuid}/sent-requests/${uuid}|accounts/${uuid}/catalog/${uuid}/request-state|accounts/${uuid}/pricing/settings|accounts/${uuid}/requests/${uuid}/draft|accounts/${uuid}/requests/${uuid}/quotations/${uuid}/trace|accounts/${uuid}/(pricing/history|price-lists|prices|prices/${uuid}/history|clients|clients/${uuid}/profile|pricing-rules))$`);
  const writable = new RegExp(`^(analytics/events|accounts/${uuid}/(inventory/ingest|requests|requests/${uuid}/review|deals/${uuid}/actions|deals/${uuid}/messages|pricing/settings|requests/${uuid}/draft|requests/${uuid}/draft/discard|requests/${uuid}/draft/reprice|price-lists|price-lists/${uuid}|prices|clients/${uuid}/profile|pricing-rules|pricing-rules/${uuid}|pricing-rules/${uuid}/archive|pricing/simulate))$`);
  const wishlistWritable = new RegExp(`^wishlist/${uuid}$`);
  const allowed = request.method === 'GET' ? readable.test(route) : request.method === 'POST' ? writable.test(route) : ['PUT', 'DELETE'].includes(request.method) && wishlistWritable.test(route);
  if (!allowed) return json({ detail: 'La ruta solicitada no existe.' }, 404);
  if (request.method !== 'GET' && !validOrigin(request)) return json({ detail: 'El origen de la solicitud no es válido.' }, 403);
  const query = new URLSearchParams();
  for (const key of ['search', 'page', 'status', 'after', 'before']) {
    const value = request.nextUrl.searchParams.get(key);
    if (value) query.set(key, value);
  }
  // The price grid filters by list; the key reaches only the supplier's private pricing routes.
  if (new RegExp(`^accounts/${uuid}/(prices|pricing/history)$`).test(route)) {
    const list = request.nextUrl.searchParams.get('list');
    if (list && new RegExp(`^${uuid}$`).test(list)) query.set('list', list);
  }
  // Rules filter by one client of the pair (a uuid) or by scope.
  if (new RegExp(`^accounts/${uuid}/pricing-rules$`).test(route)) {
    const client = request.nextUrl.searchParams.get('client'), scope = request.nextUrl.searchParams.get('scope');
    if (client && new RegExp(`^${uuid}$`).test(client)) query.set('client', client);
    if (scope === 'all' || scope === 'client') query.set('scope', scope);
  }
  if (route === 'catalog') {
    for (const key of ['category', 'subcategory', 'supplier', 'availability', 'include_facets']) {
      for (const value of request.nextUrl.searchParams.getAll(key)) query.append(key, value);
    }
  }
  let body;
  if (request.method === 'POST') {
    try { body = JSON.stringify(await request.json()); } catch { return json({ detail: 'La solicitud no es válida.' }, 400); }
  }
  try {
    const result = await django(`${route}/${query.size ? `?${query}` : ''}`, { method: request.method, body }, token);
    return json(result.data, result.status);
  } catch { return unavailable(); }
}
export const GET = handle;
export const POST = handle;
export const PUT = handle;
export const DELETE = handle;
