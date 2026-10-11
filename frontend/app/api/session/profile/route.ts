import { NextRequest } from 'next/server';
import { django, json, sessionToken, unavailable, validOrigin } from '@/lib/server-api';

// Mi perfil: the signed-in person's names, and their email with their current password (mall.management.ProfileView).
export async function PATCH(request: NextRequest) {
  if (!validOrigin(request)) return json({ detail: 'El origen de la solicitud no es válido.' }, 403);
  const token = await sessionToken();
  if (!token) return json({ detail: 'Inicia sesión para continuar.' }, 401);
  let data;
  try { data = await request.json(); } catch { return json({ detail: 'La solicitud no es válida.' }, 400); }
  const body = Object.fromEntries(['first_name', 'last_name', 'email', 'current_password'].filter(key => typeof data?.[key] === 'string').map(key => [key, data[key]]));
  try {
    const result = await django('auth/me/', { method: 'PATCH', body: JSON.stringify(body) }, token);
    return json(result.data, result.status);
  } catch { return unavailable(); }
}
