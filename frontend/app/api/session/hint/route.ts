import { NextRequest } from 'next/server';
import { django, json, unavailable } from '@/lib/server-api';

// Who a notice link was sent to (mall.login_hints): its signed hint gives back only the username, so a signed-out visitor signs in with
// it filled in. No session needed; anything that is not a hint is refused before asking the server.
export async function GET(request: NextRequest) {
  const hint = request.nextUrl.searchParams.get('hint') || '';
  if (!/^[A-Za-z0-9_:.-]{10,400}$/.test(hint)) return json({ detail: 'El enlace ya no es válido.' }, 404);
  try {
    const result = await django(`auth/login-hint/?hint=${encodeURIComponent(hint)}`);
    return json(result.data, result.status);
  } catch { return unavailable(); }
}
