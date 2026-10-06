import { NextRequest } from 'next/server';
import { django, json, unavailable, validOrigin } from '@/lib/server-api';
export async function POST(request: NextRequest) {
  if (!validOrigin(request)) return json({ detail: 'El origen de la solicitud no es válido.' }, 403);
  let data;
  try { data = await request.json(); } catch { return json({ detail: 'La solicitud no es válida.' }, 400); }
  try {
    const result = await django('auth/signup/', { method: 'POST', body: JSON.stringify(data) });
    return json(result.data, result.status);
  } catch { return unavailable(); }
}
