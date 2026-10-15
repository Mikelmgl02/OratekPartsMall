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
  const batch = '[A-Za-z0-9_.:-]{1,120}';  // a case batch's key (legacy-auto-merge)
  // The inventory refactor's S7 routes (mall/line_views.py, case_views.py, brand_views.py), read and written by S8's screens.
  const s7 = {
    GET: `lines|lines/${uuid}|lines/${uuid}/references|lines/${uuid}/main|cases|cases/${uuid}|cases/batches|cases/batches/${batch}|merges|brands|brands/[0-9]+|grupos|grupos/${uuid}`,
    POST: `lines/${uuid}/transition|lines/${uuid}/references|lines/${uuid}/main|cases/${uuid}|cases/batches/${batch}|brands|brands/[0-9]+/aliases|grupos|inventory/${uuid}/separate`,
    PATCH: `lines/${uuid}|references/[0-9]+|brands/[0-9]+|grupos/${uuid}`,
    DELETE: `references/[0-9]+|brands/[0-9]+/aliases/[0-9]+`,
  };
  const routes: Record<string, RegExp> = {
    GET: new RegExp(`^(catalog/proposals|catalog/proposals/summary|catalog/proposals/${uuid}|catalog/proposals/review|catalog/proposals/review/${uuid}|deals|deals/${uuid}|oem-references|oem-references/part-types|oem-references/${uuid}|catalog/suffixes|catalog/suffixes/${suffix}|catalog/company-suffixes|catalog/taxonomy|part-types|part-types/${uuid}|part-types/${uuid}/template|applications|applications/${uuid}|catalog/${uuid}/technical|catalog/${uuid}/oem-equivalents|catalog/${uuid}/split|catalog/merged|matching|analytics|catalog|catalog/${uuid}|catalog/assistant|catalog/assistant/${uuid}|catalog/import/issues|catalog/import/issues/${uuid}|inventory|inventory/${uuid}|alternates|alternates/[0-9]+|accounts|accounts/${uuid}|accounts/${uuid}/ledger|accounts/${uuid}/ledger/runs|accounts/${uuid}/ledger/runs/${uuid}|accounts/${uuid}/ledger/runs/${uuid}/changes|users|users/[0-9]+|roles|invitations|${s7.GET})$`),
    POST: new RegExp(`^(catalog/proposals/bulk|catalog/proposals/${uuid}|catalog/proposals/review|catalog/proposals/review/${uuid}|deals/${uuid}/cancel|oem-references|oem-references/bulk-edit|oem-references/${uuid}/cross-references|catalog/${uuid}/oem-lookup|catalog/${uuid}/split|catalog/bulk-edit|part-types|applications|matching|matching/${uuid}|catalog|catalog/assistant|catalog/assistant/${uuid}|catalog/grouping/merge|catalog/import/issues/${uuid}|alternates|accounts|memberships|invitations|accounts/${uuid}/apiag|accounts/${uuid}/apiag/test|${s7.POST})$`),
    PATCH: new RegExp(`^(catalog/proposals/${uuid}|oem-references/${uuid}|catalog/suffixes|catalog/suffixes/${suffix}|part-types/${uuid}|part-types/${uuid}/template|applications/${uuid}|catalog/${uuid}/technical|catalog/${uuid}|inventory/${uuid}|alternates/[0-9]+|accounts/${uuid}|users/[0-9]+|memberships/[0-9]+|invitations/[0-9]+/revoke|${s7.PATCH})$`),
    DELETE: new RegExp(`^(applications/${uuid}|alternates/[0-9]+|memberships/[0-9]+|oem-references/${uuid}/cross-references/[0-9]+|accounts/${uuid}/apiag|${s7.DELETE})$`),
  };
  if (!routes[request.method]?.test(route)) return json({ detail: 'La ruta solicitada no existe.' }, 404);
  const query = new URLSearchParams();
  for (const key of ['search', 'page', 'status', 'brand', 'has_oem', 'supplier', 'job', 'scope', 'offset', 'days', 'is_OEM', 'class', 'kind', 'has_unlock', 'mode', 'ordering', 'q', 'manufacturer', 'source_kind', 'linked', 'ids', 'origin', 'after', 'before', 'item', 'action', 'verdict', 'ai', 'estado', 'tipo', 'origen', 'grupo', 'subgrupo', 'line', 'reference', 'route', 'batch', 'familia', 'since']) {
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
    const result = await django(`management/${target}/${query.size ? `?${query}` : ''}`, { method: request.method, body }, token, (route.startsWith('catalog/assistant') || route.endsWith('/oem-lookup') || route.endsWith('/apiag/test') || route === 'catalog/proposals/bulk' || route.startsWith('catalog/proposals/review') || route === 'catalog/bulk-edit' || route.startsWith('cases/batches/') || /^lines\/[a-f0-9-]{36}\/transition$/.test(route) || route === 'merges' || (request.method !== 'GET' && /^brands\/[0-9]+(\/aliases(\/[0-9]+)?)?$/.test(route))) ? 120000 : 15000);
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
