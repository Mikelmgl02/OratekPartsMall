import { test, type Page, type Route } from '@playwright/test';
import type { Grupo, LineEstado, OEMLine, Reference, ReferenceEstado, ReferenceTipo } from '../lib/line-types';
import type { ManagedPart } from '../lib/types';
import { noNewProducts } from './new-products-route';

// The OEM lines' management API as S8's screens read it (mall/line_views.py, brand_views.py), mocked in the browser: lines/ filtered by
// estado (publicada,retirada by default), tipo, search and ids; a line's PATCH and transition checked against the version sent (409 with
// the version it has); its References listed, added, made main (the codigo follows), edited with their own version and retired (never
// deleted); grupos/. Every request is recorded; anything else answers 404 and is listed in unexpected.
export const empty = { count: 0, next: null, previous: null, results: [], pending_count: 0 };
export const uid = (group: number, n: number) => `0${group}000000-0000-4000-8000-${String(n).padStart(12, '0')}`;

export function makeLine(n: number, sku: string, description: string, extra: Partial<OEMLine> = {}): OEMLine {
  return { id: uid(5, n), sku, name: sku, description, estado: 'publicada', origen: 'catalogo', tipo: 'UNKNOWN', version: 1, brand: '',
    main: { id: n * 100, code: sku, brand: '', tipo: 'otro', estado: 'declarado' }, grupo: null, subgrupo: null, category: '', subcategory: '',
    aplicaciones_texto: '', merged_into: null, decision: { reason: '', note: '', decided_by: null, decided_at: null }, reference_count: 1, variant_count: 0,
    merged_count: 0, codes: [], images: [], field_sources: {}, technical_revision: 0, created_at: null, updated_at: null, changed_at: null, ...extra };
}
export function makeReference(id: number, line: OEMLine, code: string, extra: Partial<Reference> = {}): Reference {
  return { id, uid: `ref-${id}`, line: line.id, code, clave: code.replace(/[^A-Z0-9]/gi, '').toUpperCase(), brand: '', tipo: 'otro', estado: 'declarado', is_main: false,
    supplier: null, printed_forms: [], dispute_note: '', notes: '', detail: {}, reemplazado_por: null, version: 1, variant_count: 0, sources: [], created_at: null,
    updated_at: null, ...extra };
}
export const grupos: Grupo[] = [
  { id: uid(6, 1), nombre: 'FRENOS', line_count: 1, subgrupos: [{ id: uid(7, 1), name: 'DISCOS DE FRENO', line_count: 1 }, { id: uid(7, 2), name: 'PASTILLAS DE FRENO', line_count: 0 }] },
  { id: uid(6, 2), nombre: 'REFRIGERACIÓN', line_count: 0, subgrupos: [{ id: uid(7, 3), name: 'BOMBAS DE AGUA', line_count: 0 }] },
];

const TIPO_OF: Record<ReferenceTipo, OEMLine['tipo']> = { oem: 'OEM', aftermarket: 'AFTERMARKET', interno: 'UNKNOWN', otro: 'UNKNOWN' };

export async function mockLines(page: Page, initial: { lines: OEMLine[]; references?: Reference[]; refuse?: (method: string, path: string, body: Record<string, unknown>) => { status: number; json: unknown } | null }) {
  test.skip(!process.env.E2E_ADMIN_TOKEN, 'A disposable superuser token is required for server-rendered administration.');
  await page.context().addCookies([{ name: 'partsmall_session', value: process.env.E2E_ADMIN_TOKEN!, url: 'http://localhost:8080', httpOnly: true, sameSite: 'Strict' }]);
  const lines = new Map(initial.lines.map(line => [line.id, structuredClone(line)]));
  const refs = new Map<number, Reference>();
  for (const line of lines.values()) {  // every line holds its main as a Reference
    if (line.main) refs.set(line.main.id, makeReference(line.main.id, line, line.main.code, { brand: line.main.brand, tipo: line.main.tipo, estado: line.main.estado, is_main: true }));
  }
  for (const ref of initial.references ?? []) refs.set(ref.id, structuredClone(ref));
  let nextRef = 9000;
  const calls: string[] = [], unexpected: string[] = [], lists: URLSearchParams[] = [];
  const of = (lineId: string) => [...refs.values()].filter(ref => ref.line === lineId).sort((a, b) => Number(b.is_main) - Number(a.is_main) || a.id - b.id);
  function sync(line: OEMLine) {  // the row as the serializer derives it from its References
    const rows = of(line.id), main = rows.find(ref => ref.is_main && ref.estado !== 'retirada'), live = rows.filter(ref => ref.estado !== 'retirada');
    if (main) Object.assign(line, { sku: main.code, brand: main.brand, tipo: TIPO_OF[main.tipo], main: { id: main.id, code: main.code, brand: main.brand, tipo: main.tipo, estado: main.estado } });
    line.reference_count = live.length;
    line.codes = live.filter(ref => !ref.is_main).map(ref => ({ id: ref.id, code: ref.code, brand: ref.brand, tipo: ref.tipo, estado: ref.estado }));
    return line;
  }
  for (const line of lines.values()) sync(line);
  const subgrupoOf = (id: string | null) => {
    for (const grupo of grupos) for (const sub of grupo.subgrupos) if (sub.id === id) return { grupo: { id: grupo.id, nombre: grupo.nombre }, subgrupo: { id: sub.id, name: sub.name } };
    return null;
  };
  const conflict = (version: number) => ({ status: 409, json: { detail: 'La línea cambió mientras la editabas: vuelve a cargarla.', version } });
  await page.route('**/api/**', route => { if (noNewProducts(route)) return; unexpected.push(`${route.request().method()} ${new URL(route.request().url()).pathname}`); return route.fulfill({ status: 404, json: { detail: 'Ruta no prevista' } }); });
  await page.route('**/api/market/analytics/events', route => route.fulfill({ status: 202, json: { recorded: true } }));
  await page.route(/\/api\/management\/(roles|catalog\/import\/issues)(?:\?.*)?$/, route => route.fulfill({ json: route.request().url().includes('/roles') ? [] : empty }));
  await page.route('**/api/management/grupos', route => route.fulfill({ json: grupos }));
  await page.route(/\/api\/management\/brands(?:\?.*)?$/, route => route.fulfill({ json: { count: 1, next: null, previous: null,
    results: [{ id: 1, nombre: 'TOYOTA', tipo: 'vehiculo', familia: null, aliases: [{ id: 1, alias: 'TOY' }], miembros: [], notas: '', reference_count: 3, created_at: '', updated_at: '' }] } }));
  const handle = (route: Route) => {
    const request = route.request(), url = new URL(request.url()), path = url.pathname.replace('/api/management/', ''), method = request.method();
    const body = (method === 'GET' || method === 'DELETE' ? {} : request.postDataJSON() ?? {}) as Record<string, unknown>;
    calls.push(`${method} ${path}${method === 'GET' ? url.search : ''}${method === 'GET' || method === 'DELETE' ? '' : ` ${JSON.stringify(body)}`}`);
    const refused = initial.refuse?.(method, path, body);
    if (refused) return route.fulfill(refused);
    const parts = path.split('/');
    if (parts[0] === 'lines' && parts.length === 1) {
      lists.push(url.searchParams);
      const estados = (url.searchParams.get('estado') || 'publicada,retirada').split(','), ids = url.searchParams.get('ids')?.split(',');
      const tipo = url.searchParams.get('tipo'), search = (url.searchParams.get('search') || '').toUpperCase();
      const results = [...lines.values()].filter(line => estados.includes(line.estado) && (!ids || ids.includes(line.id)) && (!tipo || line.tipo === tipo)
        && (!search || `${line.sku} ${line.description} ${(line.codes ?? []).map(code => code.code).join(' ')}`.includes(search)));
      return route.fulfill({ json: { count: results.length, next: null, previous: null, results } });
    }
    if (parts[0] === 'lines') {
      const line = lines.get(parts[1]);
      if (!line) return route.fulfill({ status: 404, json: { detail: 'No existe esa línea.' } });
      if (parts.length === 2 && method === 'GET') return route.fulfill({ json: line });
      if (parts.length === 2 && method === 'PATCH') {
        if (body.expected_version !== line.version) return route.fulfill(conflict(line.version));
        for (const key of ['description', 'name', 'aplicaciones_texto'] as const) if (typeof body[key] === 'string') line[key] = (body[key] as string).toUpperCase();
        if ('subgrupo' in body) { const choice = subgrupoOf(body.subgrupo as string | null); Object.assign(line, { grupo: choice?.grupo ?? null, subgrupo: choice?.subgrupo ?? null, category: choice?.grupo.nombre ?? '', subcategory: choice?.subgrupo.name ?? '' }); }
        line.version += 1;
        return route.fulfill({ json: line });
      }
      if (parts[2] === 'transition') {
        if (body.expected_version !== line.version) return route.fulfill(conflict(line.version));
        line.estado = body.estado as LineEstado; line.version += 1;
        if (line.estado === 'rechazada') line.decision = { reason: body.reason as OEMLine['decision']['reason'], note: String(body.note ?? ''), decided_by: 'e2e-admin', decided_at: '2026-10-15T08:00:00Z' };
        return route.fulfill({ json: line });
      }
      if (parts[2] === 'references' && method === 'GET') return route.fulfill({ json: of(line.id) });
      if (parts[2] === 'references') {
        const code = String(body.code).toUpperCase(), brand = String(body.brand ?? '').toUpperCase();
        const found = of(line.id).find(ref => ref.code === code && ref.brand === brand && ref.estado !== 'retirada');
        if (found) return route.fulfill({ status: 200, json: { added: false, reference: found } });
        const ref = makeReference(++nextRef, line, code, { brand, tipo: (body.tipo as ReferenceTipo) ?? 'otro', estado: (body.estado as ReferenceEstado) ?? 'declarado',
          sources: body.source ? [{ kind: 'admin', citation: String(body.source), marks: [], retirada: false, created_at: '2026-10-15T08:00:00Z' }] : [] });
        refs.set(ref.id, ref); sync(line);
        return route.fulfill({ status: 201, json: { added: true, reference: ref } });
      }
      if (parts[2] === 'main') {
        if (body.expected_version !== undefined && body.expected_version !== line.version) return route.fulfill(conflict(line.version));
        for (const ref of of(line.id)) ref.is_main = ref.id === body.reference;
        line.version += 1; sync(line);
        return route.fulfill({ json: line });
      }
    }
    if (parts[0] === 'references') {
      const ref = refs.get(Number(parts[1]));
      if (!ref) return route.fulfill({ status: 404, json: { detail: 'No existe esa referencia.' } });
      const line = lines.get(ref.line)!;
      if (method === 'DELETE') {
        if (ref.is_main) return route.fulfill({ status: 400, json: { detail: 'Es la referencia principal: elige otra principal antes de retirarla.' } });
        ref.estado = 'retirada'; ref.version += 1; sync(line);
        return route.fulfill({ json: ref });
      }
      if (body.expected_version !== ref.version) return route.fulfill({ status: 409, json: { detail: 'La referencia cambió mientras la editabas: vuelve a cargarla.', version: ref.version } });
      for (const key of ['code', 'brand', 'tipo', 'estado', 'dispute_note', 'notes'] as const) if (key in body) (ref as Record<string, unknown>)[key] = body[key];
      ref.version += 1; sync(line);
      return route.fulfill({ json: ref });
    }
    unexpected.push(`${method} ${path}`);
    return route.fulfill({ status: 404, json: { detail: 'Ruta no prevista' } });
  };
  await page.route(/\/api\/management\/(lines|references)(\/.*)?(\?.*)?$/, handle);
  // A line the page creates meanwhile (Crear SKU), with its main Reference.
  function addLine(line: OEMLine) {
    lines.set(line.id, structuredClone(line));
    if (line.main) refs.set(line.main.id, makeReference(line.main.id, line, line.main.code, { brand: line.main.brand, tipo: line.main.tipo, estado: line.main.estado, is_main: true }));
    sync(lines.get(line.id)!);
  }
  // The lines/ lists the grid asked for (re-reads of rows by ids left out).
  const queries = () => lists.filter(params => !params.get('ids'));
  return { lines, refs, calls, unexpected, lists, queries, addLine };
}

// A catalog/ row as the Inventario grid now reads it from lines/, for the specs whose fixtures still hold catalog rows (Agrupar con…,
// Separar SKU, Sufijos, Biblioteca OEM): the line's tipo from is_OEM, its estado from active, its codes with their tipo.
export function lineFromPart(part: ManagedPart): OEMLine {
  const tipo = part.is_OEM ? 'OEM' : part.identity?.ref_type === 'company' ? 'AFTERMARKET' : 'UNKNOWN';
  return makeLine(0, part.sku, part.description, { id: part.id, name: part.name, tipo, brand: part.identity?.brand ?? '', estado: part.active === false ? 'retirada' : 'publicada',
    category: part.category ?? '', subcategory: part.subcategory ?? '', variant_count: part.stock_record_count ?? 0, merged_count: part.merged_count ?? 0, images: part.images ?? [],
    main: { id: 1, code: part.sku, brand: part.identity?.brand ?? '', tipo: part.is_OEM ? 'oem' : 'otro', estado: 'declarado' },
    codes: (part.codes ?? []).map((code, index) => ({ id: code.id ?? index + 2, code: code.code, brand: code.brand, tipo: code.ref_type === 'oem' ? 'oem' : code.ref_type === 'company' ? 'aftermarket' : 'otro', estado: 'declarado' })),
    reference_count: 1 + (part.codes?.length ?? 0) });
}
export async function routeLinesFrom(page: Page, rows: () => ManagedPart[]) {
  await page.route('**/api/management/grupos', route => route.fulfill({ json: grupos }));  // the line panel's subgrupo picker
  await page.route(/\/api\/management\/lines(\/[a-f0-9-]{36}(\/references)?)?(\?.*)?$/, route => {
    const url = new URL(route.request().url()), parts = url.pathname.replace('/api/management/', '').split('/');
    const all = rows().map(lineFromPart);
    if (parts.length === 1) {
      const estados = (url.searchParams.get('estado') || 'publicada,retirada').split(','), ids = url.searchParams.get('ids')?.split(','), search = (url.searchParams.get('search') || '').toUpperCase();
      const results = all.filter(line => estados.includes(line.estado) && (!ids || ids.includes(line.id)) && (!search || `${line.sku} ${line.description}`.includes(search)));
      return route.fulfill({ json: { count: results.length, next: null, previous: null, results } });
    }
    const line = all.find(row => row.id === parts[1]);
    if (!line) return route.fulfill({ status: 404, json: { detail: 'No existe esa línea.' } });
    if (parts[2] === 'references') return route.fulfill({ json: [makeReference(1, line, line.sku, { is_main: true, brand: line.brand, tipo: line.main?.tipo ?? 'otro' }),
      ...(line.codes ?? []).map(code => makeReference(code.id, line, code.code, { brand: code.brand, tipo: code.tipo }))] });
    return route.fulfill({ json: line });
  });
}
