import { NextRequest } from 'next/server';
import { django, json, sessionToken, unavailable, validOrigin } from '@/lib/server-api';

type Context = { params: Promise<{ id: string }> };
async function handle(request: NextRequest, context: Context) {
  const token = await sessionToken();
  if (!token) return json({ detail: 'Inicia sesión para continuar.' }, 401);
  if (request.method === 'POST' && !validOrigin(request)) return json({ detail: 'El origen de la solicitud no es válido.' }, 403);
  const { id } = await context.params;
  if (!/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i.test(id)) return json({ detail: 'La importación solicitada no existe.' }, 404);
  try {
    const profile = await django('auth/me/', {}, token);
    if (profile.status !== 200) return json(profile.data, profile.status);
    if (!profile.data.is_superuser) return json({ detail: 'Esta sección está disponible únicamente para superusuarios.' }, 403);
    let body: string | undefined;
    if (request.method === 'POST') {
      let data: unknown;
      try { data = await request.json(); } catch { return json({ detail: 'La solicitud no es válida.' }, 400); }
      const index = data && typeof data === 'object' && 'batch_index' in data ? data.batch_index : undefined;
      if (typeof index !== 'number' || !Number.isInteger(index) || index < 0) return json({ detail: 'El número de lote no es válido.' }, 400);
      body = JSON.stringify({ batch_index: index });
    }
    const result = await django(`management/catalog/import/jobs/${id}/`, { method: request.method, body }, token, 60000);
    return json(result.data, result.status);
  } catch { return unavailable(); }
}

export const GET = handle;
export const POST = handle;
