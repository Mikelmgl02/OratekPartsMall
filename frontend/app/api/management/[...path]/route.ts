import { NextRequest } from 'next/server';
import { django, json, sessionToken, unavailable, validOrigin } from '@/lib/server-api';

type Context = { params: Promise<{ path: string[] }> };
async function handle(request: NextRequest, context: Context) {
  const token = await sessionToken();
  if (!token) return json({ detail: 'Inicia sesión para continuar.' }, 401);
  if (request.method !== 'GET' && !validOrigin(request)) return json({ detail: 'El origen de la solicitud no es válido.' }, 403);
  const route = (await context.params).path.join('/');
  const uuid = '[a-f0-9-]{36}';
  const routes: Record<string, RegExp> = {
    GET: new RegExp(`^(part-types|part-types/${uuid}|part-types/${uuid}/template|applications|applications/${uuid}|catalog/${uuid}/technical|matching|analytics|catalog|catalog/${uuid}|catalog/assistant|catalog/assistant/${uuid}|catalog/grouping|catalog/import/issues|catalog/import/issues/${uuid}|inventory|inventory/${uuid}|alternates|alternates/[0-9]+|accounts|accounts/${uuid}|users|users/[0-9]+|roles|invitations)$`),
    POST: new RegExp(`^(catalog/${uuid}/oem-lookup|part-types|applications|matching|matching/${uuid}|catalog|catalog/assistant|catalog/assistant/${uuid}|catalog/grouping/(merge|classify)|catalog/import/issues/${uuid}|alternates|accounts|memberships|invitations)$`),
    PATCH: new RegExp(`^(part-types/${uuid}|part-types/${uuid}/template|applications/${uuid}|catalog/${uuid}/technical|catalog/${uuid}|inventory/${uuid}|alternates/[0-9]+|accounts/${uuid}|users/[0-9]+|memberships/[0-9]+|invitations/[0-9]+/revoke)$`),
    DELETE: new RegExp(`^(applications/${uuid}|alternates/[0-9]+|memberships/[0-9]+)$`),
  };
  if (!routes[request.method]?.test(route)) return json({ detail: 'La ruta solicitada no existe.' }, 404);
  const query = new URLSearchParams();
  for (const key of ['search', 'page', 'status', 'job', 'scope', 'offset', 'days', 'is_OEM']) {
    const value = request.nextUrl.searchParams.get(key);
    if (value) query.set(key, value);
  }
  let body;
  if (['POST', 'PATCH'].includes(request.method)) {
    try { body = JSON.stringify(await request.json()); } catch { return json({ detail: 'La solicitud no es válida.' }, 400); }
  }
  try {
    const profile = await django('auth/me/', {}, token);
    if (profile.status !== 200) return json(profile.data, profile.status);
    if (!profile.data.is_superuser) return json({ detail: 'Esta sección está disponible únicamente para superusuarios.' }, 403);
    const result = await django(`management/${route}/${query.size ? `?${query}` : ''}`, { method: request.method, body }, token, (route.startsWith('catalog/assistant') || route.endsWith('/oem-lookup')) ? 120000 : 15000);
    return json(result.data, result.status);
  } catch { return unavailable(); }
}
export const GET = handle;
export const POST = handle;
export const PATCH = handle;
export const DELETE = handle;
