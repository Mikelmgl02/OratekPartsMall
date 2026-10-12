import { NextRequest } from 'next/server';
import { django, json, sessionToken, unavailable, validOrigin } from '@/lib/server-api';

type Context = { params: Promise<{ path: string[] }> };
async function handle(request: NextRequest, context: Context) {
  const token = await sessionToken();
  if (!token) return json({ detail: 'Inicia sesión para continuar.' }, 401);
  const { path } = await context.params;
  const route = path.join('/');
  const uuid = '[a-f0-9-]{36}';
  const readable = new RegExp(`^(accounts|catalog|catalog/suggest|inbox|inbox/conversations|inbox/notifications|inbox/push|inbox/email|inbox/whatsapp|wishlist|wishlist/state|catalog/${uuid}/technical|catalog/${uuid}/suppliers|accounts/${uuid}/apiag|accounts/${uuid}/inventory|accounts/${uuid}/inventory/${uuid}/ledger|accounts/${uuid}/whatsapp|accounts/${uuid}/basket|accounts/${uuid}/basket/activity|accounts/${uuid}/deals/${uuid}/messages|accounts/${uuid}/requests|accounts/${uuid}/requests/${uuid}|accounts/${uuid}/sent-requests|accounts/${uuid}/sent-requests/${uuid}|accounts/${uuid}/catalog/${uuid}/request-state|accounts/${uuid}/pricing/settings|accounts/${uuid}/requests/${uuid}/draft|accounts/${uuid}/requests/${uuid}/draft/assistant|accounts/${uuid}/requests/${uuid}/quotations/${uuid}/trace|accounts/${uuid}/(pricing/history|price-lists|prices|prices/${uuid}/history|clients|clients/${uuid}/profile|pricing-rules))$`);
  const writable = new RegExp(`^(analytics/events|inbox/conversations/${uuid}/read|inbox/conversations/${uuid}/place|inbox/groups|inbox/groups/${uuid}|inbox/notifications/seen|inbox/push|inbox/push/remove|inbox/email|inbox/whatsapp|inbox/whatsapp/verify|accounts/${uuid}/(whatsapp|whatsapp/verify|basket|inventory/ingest|apiag/sync|requests|requests/${uuid}/review|deals/${uuid}/actions|deals/${uuid}/messages|pricing/settings|requests/${uuid}/draft|requests/${uuid}/draft/discard|requests/${uuid}/draft/reprice|requests/${uuid}/draft/assistant|requests/${uuid}/draft/assistant/${uuid}/decisions|price-lists|price-lists/${uuid}|prices|clients/${uuid}/profile|pricing-rules|pricing-rules/${uuid}|pricing-rules/${uuid}/archive|pricing/simulate))$`);
  const wishlistWritable = new RegExp(`^wishlist/${uuid}$`), inboxGroup = new RegExp(`^inbox/groups/${uuid}$`);
  const allowed = request.method === 'GET' ? readable.test(route) : request.method === 'POST' ? writable.test(route)
    : (['PUT', 'DELETE'].includes(request.method) && wishlistWritable.test(route)) || (request.method === 'DELETE' && inboxGroup.test(route));
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
  // The supplier's stock grid filters by matching state and sorts by column (the view accepts only its own ordering fields).
  if (new RegExp(`^accounts/${uuid}/inventory$`).test(route)) {
    const matching = request.nextUrl.searchParams.get('matching_status'), ordering = request.nextUrl.searchParams.get('ordering');
    if (matching && ['matched', 'pending', 'review', 'unmatched'].includes(matching)) query.set('matching_status', matching);
    if (ordering && /^[-a-z_,]{1,120}$/.test(ordering)) query.set('ordering', ordering);
  }
  if (route === 'inbox/conversations') {
    if (request.nextUrl.searchParams.get('unread') === '1') query.set('unread', '1');
    const side = request.nextUrl.searchParams.get('side');
    if (side === 'client' || side === 'supplier') query.set('side', side);
    const order = request.nextUrl.searchParams.get('order');
    if (order && new RegExp(`^${uuid}$`).test(order)) query.set('order', order);
  }
  // Rules filter by one client of the pair (a uuid) or by scope.
  if (new RegExp(`^accounts/${uuid}/pricing-rules$`).test(route)) {
    const client = request.nextUrl.searchParams.get('client'), scope = request.nextUrl.searchParams.get('scope');
    if (client && new RegExp(`^${uuid}$`).test(client)) query.set('client', client);
    if (scope === 'all' || scope === 'client') query.set('scope', scope);
  }
  if (route === 'catalog' || route === 'catalog/suggest') {
    for (const key of ['category', 'subcategory', 'supplier', 'availability', 'include_facets']) {
      for (const value of request.nextUrl.searchParams.getAll(key)) query.append(key, value);
    }
  }
  let body;
  if (request.method === 'POST') {
    try { body = JSON.stringify(await request.json()); } catch { return json({ detail: 'La solicitud no es válida.' }, 400); }
  }
  // Starting an assistant run waits for the AI provider (up to 45 s on the server), so it gets a longer timeout than every other route.
  const timeout = request.method === 'POST' && new RegExp(`^accounts/${uuid}/requests/${uuid}/draft/assistant$`).test(route) ? 60000 : undefined;
  try {
    const result = await django(`${route}/${query.size ? `?${query}` : ''}`, { method: request.method, body }, token, timeout);
    return json(result.data, result.status);
  } catch { return unavailable(); }
}
export const GET = handle;
export const POST = handle;
export const PUT = handle;
export const DELETE = handle;
