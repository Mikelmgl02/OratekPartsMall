import { NextRequest } from 'next/server';
import { django, json, sessionToken, unavailable, validOrigin } from '@/lib/server-api';

export async function POST(request: NextRequest) {
  const token = await sessionToken();
  if (!token) return json({ detail: 'Inicia sesión para continuar.' }, 401);
  if (!validOrigin(request)) return json({ detail: 'El origen de la solicitud no es válido.' }, 403);
  try {
    const profile = await django('auth/me/', {}, token);
    if (profile.status !== 200) return json(profile.data, profile.status);
    if (!profile.data.is_superuser) return json({ detail: 'Esta sección está disponible únicamente para superusuarios.' }, 403);
    let form: FormData;
    try { form = await request.formData(); } catch { return json({ detail: 'La solicitud de archivo no es válida.' }, 400); }
    const file = form.get('file');
    if (!(file instanceof File) || file.size > 5 * 1024 * 1024) return json({ detail: 'Selecciona un archivo .xlsx de hasta 5 MB.' }, 400);
    const result = await django('management/catalog/import/', { method: 'POST', body: form }, token, 120000);
    return json(result.data, result.status);
  } catch { return unavailable(); }
}
