import { NextRequest } from 'next/server';
import { django, json, sessionToken, unavailable, validOrigin } from '@/lib/server-api';

type Context = { params: Promise<{ path: string[] }> };
async function handle(request: NextRequest, context: Context) {
  const token = await sessionToken();
  if (!token) return json({ detail: 'Inicia sesión para continuar.' }, 401);
  if (request.method !== 'GET' && !validOrigin(request)) return json({ detail: 'El origen de la solicitud no es válido.' }, 403);
  const segments = (await context.params).path;
  // Suffix tokens may hold '/', spaces or '<>' (SPK/WAP, NEW ERA, _<ID>_DELETED): read them decoded, forward them encoded.
  const suffixToken = segments[0] === 'catalog' && segments[1] === 'suffixes' && segments.length > 2 ? decodeToken(segments.slice(2).join('/')) : null;
  const route = suffixToken === null ? segments.join('/') : `catalog/suffixes/${suffixToken}`;
  const uuid = '[a-f0-9-]{36}';
  const suffix = '[A-Z0-9_][A-Z0-9 ./_:()<>-]{0,59}';
  const routes: Record<string, RegExp> = {
    GET: new RegExp(`^(oem-references|oem-references/${uuid}|oem-review|oem-review/summary|oem-review/bulk|oem-finder/pending|oem-finder/runs|oem-finder/runs/[0-9]+|oem-finder/runs/[0-9]+/spot-check|catalog/suffixes|catalog/suffixes/${suffix}|catalog/company-suffixes|part-types|part-types/${uuid}|part-types/${uuid}/template|applications|applications/${uuid}|catalog/${uuid}/technical|matching|analytics|catalog|catalog/${uuid}|catalog/assistant|catalog/assistant/${uuid}|catalog/grouping|catalog/import/issues|catalog/import/issues/${uuid}|inventory|inventory/${uuid}|alternates|alternates/[0-9]+|accounts|accounts/${uuid}|users|users/[0-9]+|roles|invitations)$`),
    POST: new RegExp(`^(oem-references|oem-review/[0-9]+|oem-review/bulk|oem-review/bulk/[0-9]+|oem-finder/pending/(preview|apply|send-to-review)|oem-finder/runs/[0-9]+/revert|oem-finder/halt|catalog/${uuid}/oem-lookup|part-types|applications|matching|matching/${uuid}|catalog|catalog/assistant|catalog/assistant/${uuid}|catalog/grouping/(merge|classify)|catalog/import/issues/${uuid}|alternates|accounts|memberships|invitations)$`),
    PATCH: new RegExp(`^(oem-references/${uuid}|catalog/suffixes|catalog/suffixes/${suffix}|part-types/${uuid}|part-types/${uuid}/template|applications/${uuid}|catalog/${uuid}/technical|catalog/${uuid}|inventory/${uuid}|alternates/[0-9]+|accounts/${uuid}|users/[0-9]+|memberships/[0-9]+|invitations/[0-9]+/revoke)$`),
    DELETE: new RegExp(`^(applications/${uuid}|alternates/[0-9]+|memberships/[0-9]+)$`),
  };
  if (!routes[request.method]?.test(route)) return json({ detail: 'La ruta solicitada no existe.' }, 404);
  const query = new URLSearchParams();
  for (const key of ['search', 'page', 'status', 'job', 'scope', 'offset', 'days', 'is_OEM', 'class', 'kind', 'has_unlock', 'mode', 'stage', 'tier', 'blocker', 'chain', 'make', 'system', 'in_stock', 'apply_tier', 'ordering', 'q', 'manufacturer', 'source_kind', 'linked']) {
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
    const target = suffixToken === null ? route : `catalog/suffixes/${encodeURIComponent(suffixToken)}`;
    const result = await django(`management/${target}/${query.size ? `?${query}` : ''}`, { method: request.method, body }, token, (route.startsWith('catalog/assistant') || route.startsWith('oem-review') || route.startsWith('oem-finder/pending/') || route.endsWith('/oem-lookup') || route.endsWith('/revert')) ? 120000 : 15000);
    return json(result.data, result.status);
  } catch { return unavailable(); }
}
function decodeToken(value: string) {
  try { return value.includes('%') ? decodeURIComponent(value) : value; } catch { return value; }
}
export const GET = handle;
export const POST = handle;
export const PATCH = handle;
export const DELETE = handle;
