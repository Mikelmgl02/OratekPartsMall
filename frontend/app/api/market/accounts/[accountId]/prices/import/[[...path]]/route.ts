import { NextRequest, NextResponse } from 'next/server';
import { django, json, sessionToken, unavailable, validOrigin } from '@/lib/server-api';

type Context = { params: Promise<{ accountId: string; path?: string[] }> };
const uuid = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i;
const spreadsheetType = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet';

// Supplier-only price import: multipart upload, batch commits and the binary template and correction workbooks.
async function proxy(request: NextRequest, context: Context) {
  const { accountId, path = [] } = await context.params;
  const isJob = path.length >= 2 && path[0] === 'jobs' && uuid.test(path[1]);
  const isDownload = request.method === 'GET' && ((path.length === 1 && path[0] === 'template') || (isJob && path.length === 3 && path[2] === 'errors'));
  const permitted = isDownload || (request.method === 'GET' && isJob && path.length === 2) || (request.method === 'POST' && (path.length === 0 || (isJob && path.length === 2)));
  if (!uuid.test(accountId) || !permitted) return json({ detail: 'Esta página no está disponible.' }, 404);
  const token = await sessionToken();
  if (!token) return json({ detail: 'Inicia sesión para continuar.' }, 401);
  if (request.method === 'POST' && !validOrigin(request)) return json({ detail: 'El origen de la solicitud no es válido.' }, 403);
  const route = `accounts/${accountId}/prices/import/${path.length ? `${path.join('/')}/` : ''}`;
  try {
    if (isDownload) {
      const base = process.env.API_INTERNAL_URL || 'http://127.0.0.1:8000';
      const response = await fetch(`${base}/api/v1/${route}`, { headers: { Authorization: `Token ${token}` }, cache: 'no-store', signal: AbortSignal.timeout(60000) });
      if (!response.ok) {
        let data: unknown;
        try { data = await response.json(); } catch { data = { detail: 'No se pudo descargar el archivo. Inténtalo de nuevo.' }; }
        return json(data, response.status);
      }
      return new NextResponse(await response.arrayBuffer(), { headers: {
        'Content-Type': spreadsheetType,
        'Content-Disposition': `attachment; filename="${path[0] === 'template' ? 'plantilla-precios.xlsx' : 'errores-precios.xlsx'}"`,
        'Cache-Control': 'private, no-store',
      } });
    }
    let body: FormData | string | undefined;
    if (request.method === 'POST' && path.length === 0) {
      let form: FormData;
      try { form = await request.formData(); } catch { return json({ detail: 'La solicitud de archivo no es válida.' }, 400); }
      const file = form.get('file');
      if (!(file instanceof File) || !file.name.toLowerCase().endsWith('.xlsx') || file.size > 5 * 1024 * 1024) return json({ detail: 'Selecciona un archivo .xlsx de hasta 5 MB.' }, 400);
      body = new FormData(); body.set('file', file);
    } else if (request.method === 'POST') {
      let data: { batch_index?: unknown; acknowledge_big_changes?: unknown; confirm_new_lists?: unknown };
      try { data = await request.json(); } catch { return json({ detail: 'La solicitud no es válida.' }, 400); }
      if (typeof data.batch_index !== 'number' || !Number.isInteger(data.batch_index) || data.batch_index < 0) return json({ detail: 'El lote de importación no es válido.' }, 400);
      // Only the batch and its two explicit confirmations reach the API.
      body = JSON.stringify({ batch_index: data.batch_index, acknowledge_big_changes: data.acknowledge_big_changes === true, confirm_new_lists: data.confirm_new_lists === true });
    }
    const result = await django(route, { method: request.method, ...(body ? { body } : {}) }, token, path.length === 0 ? 120000 : 60000);
    return json(result.data, result.status);
  } catch { return unavailable(); }
}

export const GET = proxy;
export const POST = proxy;
