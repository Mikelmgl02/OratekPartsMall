import { NextRequest } from 'next/server';
import { django, json, sessionToken, unavailable, validOrigin } from '@/lib/server-api';
import { forwardedQuery } from '@/lib/market-proxy';

type Context = { params: Promise<{ path: string[] }> };
async function handle(request: NextRequest, context: Context) {
  const token = await sessionToken();
  if (!token) return json({ detail: 'Inicia sesión para continuar.' }, 401);
  const { path } = await context.params;
  const route = path.join('/');
  const uuid = '[a-f0-9-]{36}';
  const readable = new RegExp(`^(accounts|catalog|catalog/suggest|inbox|inbox/conversations|inbox/notifications|inbox/push|inbox/email|inbox/whatsapp|wishlist|wishlist/state|catalog/${uuid}/technical|catalog/${uuid}/suppliers|accounts/${uuid}/apiag|accounts/${uuid}/inventory|accounts/${uuid}/inventory/${uuid}/ledger|accounts/${uuid}/ledger|accounts/${uuid}/ledger/runs|accounts/${uuid}/ledger/runs/${uuid}|accounts/${uuid}/ledger/runs/${uuid}/changes|accounts/${uuid}/whatsapp|accounts/${uuid}/basket|accounts/${uuid}/basket/activity|accounts/${uuid}/purchase-activity|accounts/${uuid}/open-units|accounts/${uuid}/open-units/${uuid}|accounts/${uuid}/threads/${uuid}/messages|accounts/${uuid}/threads/${uuid}/orders|accounts/${uuid}/deals/${uuid}/messages|accounts/${uuid}/requests|accounts/${uuid}/requests/${uuid}|accounts/${uuid}/sent-requests|accounts/${uuid}/sent-requests/summary|accounts/${uuid}/sent-requests/${uuid}|accounts/${uuid}/sent-requests/${uuid}/missing|accounts/${uuid}/catalog/${uuid}/request-state|accounts/${uuid}/pricing/settings|accounts/${uuid}/requests/${uuid}/draft|accounts/${uuid}/requests/${uuid}/draft/assistant|accounts/${uuid}/requests/${uuid}/quotations/${uuid}/trace|accounts/${uuid}/(pricing/history|price-lists|prices|prices/${uuid}/history|clients|clients/${uuid}/profile|pricing-rules))$`);
  const writable = new RegExp(`^(analytics/events|inbox/conversations/${uuid}/read|inbox/conversations/${uuid}/place|inbox/groups|inbox/groups/${uuid}|inbox/notifications/seen|inbox/push|inbox/push/remove|inbox/email|inbox/whatsapp|inbox/whatsapp/verify|accounts/${uuid}/(whatsapp|whatsapp/verify|basket|sent-requests/${uuid}/missing|threads|threads/${uuid}/messages|inventory/ingest|apiag/sync|requests|requests/${uuid}/review|deals/${uuid}/actions|deals/${uuid}/messages|pricing/settings|requests/${uuid}/draft|requests/${uuid}/draft/discard|requests/${uuid}/draft/reprice|requests/${uuid}/draft/assistant|requests/${uuid}/draft/assistant/${uuid}/decisions|price-lists|price-lists/${uuid}|prices|clients/${uuid}/profile|pricing-rules|pricing-rules/${uuid}|pricing-rules/${uuid}/archive|pricing/simulate))$`);
  const wishlistWritable = new RegExp(`^wishlist/${uuid}$`), inboxGroup = new RegExp(`^inbox/groups/${uuid}$`);
  const allowed = request.method === 'GET' ? readable.test(route) : request.method === 'POST' ? writable.test(route)
    : (['PUT', 'DELETE'].includes(request.method) && wishlistWritable.test(route)) || (request.method === 'DELETE' && inboxGroup.test(route));
  if (!allowed) return json({ detail: 'La ruta solicitada no existe.' }, 404);
  if (request.method !== 'GET' && !validOrigin(request)) return json({ detail: 'El origen de la solicitud no es válido.' }, 403);
  const query = forwardedQuery(route, request.nextUrl.searchParams);
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
