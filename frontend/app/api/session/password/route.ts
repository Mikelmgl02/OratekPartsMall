import { cookies } from 'next/headers';
import { NextRequest } from 'next/server';
import { django, json, SESSION_COOKIE, sessionToken, unavailable, validOrigin } from '@/lib/server-api';

// A new password: the server replaces the person's token (their other devices are signed out), and this browser keeps its session
// with the new one, which never reaches the page.
export async function POST(request: NextRequest) {
  if (!validOrigin(request)) return json({ detail: 'El origen de la solicitud no es válido.' }, 403);
  const token = await sessionToken();
  if (!token) return json({ detail: 'Inicia sesión para continuar.' }, 401);
  let data;
  try { data = await request.json(); } catch { return json({ detail: 'La solicitud no es válida.' }, 400); }
  if (typeof data?.current_password !== 'string' || typeof data?.new_password !== 'string') return json({ detail: 'Escribe tu contraseña actual y la nueva.' }, 400);
  try {
    const result = await django('auth/password/', { method: 'POST', body: JSON.stringify({ current_password: data.current_password, new_password: data.new_password }) }, token);
    if (result.status !== 200) return json(result.data, result.status);
    (await cookies()).set(SESSION_COOKIE, result.data.token, { httpOnly: true, sameSite: 'strict', secure: process.env.COOKIE_SECURE === '1', path: '/', maxAge: 86400 });
    return json({ changed: true });
  } catch { return unavailable(); }
}
