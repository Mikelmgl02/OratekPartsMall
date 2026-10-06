import { NextRequest, NextResponse } from 'next/server';
import { json, sessionToken, unavailable } from '@/lib/server-api';

type Context = { params: Promise<{ accountId: string }> };
const uuid = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i;

// Supplier-only binary download of the private price lists; the JSON catch-all proxy cannot carry it.
export async function GET(request: NextRequest, context: Context) {
  const { accountId } = await context.params;
  const list = request.nextUrl.searchParams.get('list');
  if (!uuid.test(accountId) || (list !== null && !uuid.test(list))) return json({ detail: 'Esta página no está disponible.' }, 404);
  const token = await sessionToken();
  if (!token) return json({ detail: 'Inicia sesión para continuar.' }, 401);
  const base = process.env.API_INTERNAL_URL || 'http://127.0.0.1:8000';
  try {
    const response = await fetch(`${base}/api/v1/accounts/${accountId}/prices/export/${list ? `?list=${list}` : ''}`,
      { headers: { Authorization: `Token ${token}` }, cache: 'no-store', signal: AbortSignal.timeout(60000) });
    if (!response.ok) {
      let data: unknown;
      try { data = await response.json(); } catch { data = { detail: 'No se pudo descargar el archivo. Inténtalo de nuevo.' }; }
      return json(data, response.status);
    }
    return new NextResponse(await response.arrayBuffer(), { headers: {
      'Content-Type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
      'Content-Disposition': 'attachment; filename="precios-proveedor.xlsx"',
      'Cache-Control': 'private, no-store',
    } });
  } catch { return unavailable(); }
}
