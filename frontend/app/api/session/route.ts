import { cookies } from 'next/headers';
import { NextRequest } from 'next/server';
import { django, json, SESSION_COOKIE, sessionToken, unavailable, validOrigin } from '@/lib/server-api';

export async function GET() {
  const token = await sessionToken();
  if (!token) return json({ authenticated: false });
  try {
    const [result, profile] = await Promise.all([django('accounts/', {}, token), django('auth/me/', {}, token)]);
    if (result.status === 401 || profile.status === 401) {
      (await cookies()).delete(SESSION_COOKIE);
      return json({ authenticated: false });
    }
    if (result.status !== 200) return json(result.data, result.status);
    if (profile.status !== 200) return json(profile.data, profile.status);
    return json({ authenticated: true, accounts: result.data, user: profile.data });
  } catch { return unavailable(); }
}
export async function POST(request: NextRequest) {
  if (!validOrigin(request)) return json({ detail: 'El origen de la solicitud no es válido.' }, 403);
  let data;
  try { data = await request.json(); } catch { return json({ detail: 'La solicitud no es válida.' }, 400); }
  if (!data || typeof data.username !== 'string' || typeof data.password !== 'string') return json({ detail: 'Introduce tu usuario y contraseña.' }, 400);
  try {
    const result = await django('auth/login/', { method: 'POST', body: JSON.stringify({ username: data.username, password: data.password }) });
    if (result.status !== 200) return json(result.data, result.status);
    (await cookies()).set(SESSION_COOKIE, result.data.token, { httpOnly: true, sameSite: 'strict', secure: process.env.COOKIE_SECURE === '1', path: '/', maxAge: 86400 });
    return json({ authenticated: true });
  } catch { return unavailable(); }
}
export async function DELETE(request: NextRequest) {
  if (!validOrigin(request)) return json({ detail: 'El origen de la solicitud no es válido.' }, 403);
  const token = await sessionToken();
  try {
    if (token) {
      const result = await django('auth/logout/', { method: 'POST' }, token);
      if (![204, 401].includes(result.status)) return json(result.data, result.status);
    }
    (await cookies()).delete(SESSION_COOKIE);
    return json({ authenticated: false });
  } catch { return unavailable(); }
}
