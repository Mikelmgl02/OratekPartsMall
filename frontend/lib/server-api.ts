import { cookies } from 'next/headers';
import { NextRequest, NextResponse } from 'next/server';

export const SESSION_COOKIE = 'partsmall_session';
const base = process.env.API_INTERNAL_URL || 'http://127.0.0.1:8000';
export async function sessionToken() {
  return (await cookies()).get(SESSION_COOKIE)?.value;
}
export function validOrigin(request: NextRequest) {
  const origin = request.headers.get('origin');
  const host = request.headers.get('host');
  if (!origin || !host) return false;
  try { return new URL(origin).host === host; } catch { return false; }
}
export function json(data: unknown, status = 200) {
  if (status === 204) return new NextResponse(null, { status, headers: { 'Cache-Control': 'no-store' } });
  return NextResponse.json(data, { status, headers: { 'Cache-Control': 'no-store' } });
}
export async function django(path: string, options: RequestInit = {}, token?: string, timeoutMs = 15000) {
  const headers = new Headers(options.headers);
  headers.set('Accept', 'application/json');
  if (options.body && !(options.body instanceof FormData)) headers.set('Content-Type', 'application/json');
  if (token) headers.set('Authorization', `Token ${token}`);
  const response = await fetch(`${base}/api/v1/${path}`, { ...options, headers, cache: 'no-store', signal: AbortSignal.timeout(timeoutMs) });
  const data = response.status === 204 ? null : await response.json();
  return { status: response.status, data };
}
export function unavailable() {
  return json({ detail: 'No se pudo conectar con MotionPartes. Inténtalo de nuevo en unos momentos.' }, 502);
}
