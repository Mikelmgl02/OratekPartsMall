import { NextRequest } from 'next/server';
import { django, json, sessionToken, unavailable, validOrigin } from '@/lib/server-api';

type Context = { params: Promise<{ partId: string; action?: string[] }> };
async function handle(request: NextRequest, context: Context) {
  const token = await sessionToken();
  if (!token) return json({ detail: 'Inicia sesión para continuar.' }, 401);
  if (request.method !== 'GET' && !validOrigin(request)) return json({ detail: 'El origen de la solicitud no es válido.' }, 403);
  const { partId, action = [] } = await context.params;
  const uuid = /^[a-f0-9-]{36}$/;
  const upload = request.method === 'POST' && action.length === 0;
  const valid = uuid.test(partId) && (action.length === 0 && ['GET', 'POST'].includes(request.method) ||
    action.length === 1 && (action[0] === 'order' && request.method === 'POST' || uuid.test(action[0]) && ['PATCH', 'DELETE'].includes(request.method)));
  if (!valid) return json({ detail: 'La ruta solicitada no existe.' }, 404);
  try {
    const profile = await django('auth/me/', {}, token);
    if (profile.status !== 200) return json(profile.data, profile.status);
    if (!profile.data.is_superuser) return json({ detail: 'Esta sección está disponible únicamente para superusuarios.' }, 403);
    let body: FormData | string | undefined;
    if (upload) {
      if (Number(request.headers.get('content-length')) > 11 * 1024 * 1024) return json({ detail: 'Cada imagen puede pesar hasta 10 MB.' }, 413);
      try { body = await request.formData(); } catch { return json({ detail: 'Selecciona una imagen válida.' }, 400); }
      const file = body.get('file');
      if (!(file instanceof File) || file.size > 10 * 1024 * 1024) return json({ detail: 'Selecciona una imagen de hasta 10 MB.' }, 400);
    } else if (['POST', 'PATCH'].includes(request.method)) {
      try { body = JSON.stringify(await request.json()); } catch { return json({ detail: 'La solicitud no es válida.' }, 400); }
    }
    const path = `management/catalog/${partId}/images/${action.length ? `${action[0]}/` : ''}`;
    const result = await django(path, { method: request.method, body }, token, upload ? 120000 : 15000);
    return json(result.data, result.status);
  } catch { return unavailable(); }
}
export const GET = handle;
export const POST = handle;
export const PATCH = handle;
export const DELETE = handle;
