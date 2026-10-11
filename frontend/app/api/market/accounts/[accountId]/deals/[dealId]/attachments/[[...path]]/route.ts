import { NextRequest, NextResponse } from 'next/server';
import { django, json, sessionToken, unavailable, validOrigin } from '@/lib/server-api';

type Context = { params: Promise<{ accountId: string; dealId: string; path?: string[] }> };
const uuid = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i;
const MAX_BODY = 34 * 1024 * 1024;  // the API takes 32 MB of files per message
// The file's own headers, as the API sets them for privacy (no sniffing, a sandbox) and for Safari's media ranges.
const PASSED = ['content-type', 'content-disposition', 'content-length', 'content-range', 'accept-ranges', 'cache-control', 'x-content-type-options', 'content-security-policy'];

// The deals' chat files, which the JSON catch-all proxy cannot carry: POST sends a message with files (multipart), GET <id> opens one.
async function handle(request: NextRequest, context: Context) {
  const { accountId, dealId, path = [] } = await context.params;
  const download = request.method === 'GET' && path.length === 1 && /^\d{1,18}$/.test(path[0]);
  const upload = request.method === 'POST' && path.length === 0;
  if (!uuid.test(accountId) || !uuid.test(dealId) || !(download || upload)) return json({ detail: 'La ruta solicitada no existe.' }, 404);
  const token = await sessionToken();
  if (!token) return json({ detail: 'Inicia sesión para continuar.' }, 401);
  try {
    if (download) {
      const base = process.env.API_INTERNAL_URL || 'http://127.0.0.1:8000';
      const range = request.headers.get('range');
      const response = await fetch(`${base}/api/v1/accounts/${accountId}/deals/${dealId}/attachments/${path[0]}/`, {
        headers: { Authorization: `Token ${token}`, ...(range ? { Range: range } : {}) }, cache: 'no-store', signal: AbortSignal.timeout(60000) });
      if (!response.ok && response.status !== 416) {
        let data: unknown;
        try { data = await response.json(); } catch { data = { detail: 'Este archivo no está disponible.' }; }
        return json(data, response.status);
      }
      const headers = new Headers();
      for (const name of PASSED) { const value = response.headers.get(name); if (value) headers.set(name, value); }
      return new NextResponse(response.body, { status: response.status, headers });
    }
    if (!validOrigin(request)) return json({ detail: 'El origen de la solicitud no es válido.' }, 403);
    if (Number(request.headers.get('content-length')) > MAX_BODY) return json({ detail: 'Los archivos de un mensaje pueden pesar hasta 32 MB en total.' }, 413);
    let body: FormData;
    try { body = await request.formData(); } catch { return json({ detail: 'Adjunta archivos válidos.' }, 400); }
    const result = await django(`accounts/${accountId}/deals/${dealId}/messages/`, { method: 'POST', body }, token, 120000);
    return json(result.data, result.status);
  } catch { return unavailable(); }
}
export const GET = handle;
export const POST = handle;
