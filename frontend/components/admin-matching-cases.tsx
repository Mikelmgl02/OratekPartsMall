'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { Check, GitMerge, Layers3, ListChecks, LoaderCircle, RefreshCw, Search, Ungroup } from 'lucide-react';
import { Page, request } from '@/lib/types';
import { batchLabels, CASE_KINDS, caseKindLabels, lineEstadoLabels } from '@/lib/line-types';
import type { CaseAction, CaseBatch, CaseBatchIndexRow, CaseBatchResult, CaseDecision, CasePage, MatchingCase, MergeDigestRow } from '@/lib/line-types';
import { CaseArticle, CaseCard, caseLines, caseTitle, guardLabels, isKnownKind, isOpen, LegacyCaseCard } from './matching-case-card';
import CatalogSplit from './admin-catalog-split';
import { UppercaseInput } from './uppercase-field';

const base = '/api/management';
const message = (error: unknown) => error instanceof Error ? error.message : 'No se pudo guardar la decisión. Inténtalo de nuevo.';
const number = (value: number) => value.toLocaleString('es-PA');
const day = (value: string | null) => value ? new Date(value).toLocaleString('es-PA', { timeZone: 'America/Panama', dateStyle: 'medium', timeStyle: 'short' }) : '';
type View = 'cases' | 'batches' | 'merges';
const VIEWS: { key: View; slug: string; label: string; Icon: typeof ListChecks }[] = [
  { key: 'cases', slug: 'casos', label: 'Casos', Icon: ListChecks }, { key: 'batches', slug: 'lotes', label: 'Lotes', Icon: Layers3 },
  { key: 'merges', slug: 'unificaciones', label: 'Unificaciones automáticas', Icon: GitMerge },
];
const STATUSES = [['review', 'POR REVISAR'], ['applied', 'APLICADOS'], ['dismissed', 'DESCARTADOS']] as const;

// The Matching Cases queue (plan S8.4): every case the matching engine could not decide alone, by kind, decided one at a time on its card
// (management/cases/<id> with the case's fingerprint); the lote batches decided together (management/cases/batches/<key>, each case on its
// own: one that changed fails alone); and the digest of the automatic merges, each with Separar SKU. It replaces Agrupar SKU's families
// and Agrupación inteligente's case list; ?vista= deep-links a view.
export default function AdminMatchingCases() {
  const [view, setView] = useState<View>('cases');
  const [split, setSplit] = useState<{ id: string; sku: string } | null>(null);
  const [notice, setNotice] = useState('');
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const wanted = new URL(window.location.href).searchParams.get('vista');
    const match = VIEWS.find(item => item.slug === wanted);
    if (match) setView(match.key);
  }, []);
  function open(next: View) {
    setView(next); setNotice('');
    const url = new URL(window.location.href);
    if (next === 'cases') url.searchParams.delete('vista'); else url.searchParams.set('vista', VIEWS.find(item => item.key === next)!.slug);
    window.history.replaceState(window.history.state, '', url);
  }
  return <section className="inventory-panel admin-panel smart-matching-modal matching-cases" aria-label="Casos de coincidencia">
    <div className="admin-toolbar"><div><h2>Casos de coincidencia</h2><p>Lo que el análisis no pudo decidir solo: cambios de código, referencias declaradas, variantes, artículos sin línea OEM y líneas por unificar.</p></div></div>
    <div className="inventory-view-tabs" role="group" aria-label="Vistas de casos">{VIEWS.map(({ key, label, Icon }) =>
      <button key={key} type="button" className={view === key ? 'selected' : ''} aria-pressed={view === key} onClick={() => open(key)}><Icon size={15} aria-hidden="true"/>{label}</button>)}</div>
    {notice && <div className="notice success admin-notice" role="status"><Check size={16}/>{notice}</div>}
    {view === 'cases' ? <CaseQueue revision={revision}/> : view === 'batches' ? <Batches onDecided={text => { setNotice(text); setRevision(value => value + 1); }}/>
      : <MergeDigest revision={revision} onSplit={setSplit}/>}
    <p className="admin-footnote">Un caso guarda la huella de sus datos: si algo cambió desde que lo leíste, la decisión se rechaza y el caso se vuelve a cargar. Las unificaciones automáticas solo juntan piezas idénticas; Separar SKU devuelve cualquiera de ellas.</p>
    {split && <CatalogSplit part={split} onClose={() => setSplit(null)} onChanged={text => { setNotice(text); setRevision(value => value + 1); }}/>}
  </section>;
}

// ---------------------------------------------------------------- cases, one card each
function CaseQueue({ revision }: { revision: number }) {
  const [status, setStatus] = useState('review');
  const [kind, setKind] = useState('');
  const [route, setRoute] = useState('');
  const [page, setPage] = useState(1);
  const [data, setData] = useState<CasePage | null>(null);
  const [lines, setLines] = useState<Record<string, string>>({});          // the line chosen, by id
  const [targets, setTargets] = useState<Record<string, string>>({});      // legacy cases: the SKU typed or picked
  const [searching, setSearching] = useState<Record<string, boolean>>({});
  const [picking, setPicking] = useState<Record<string, boolean>>({});
  const [busy, setBusy] = useState(''); const [error, setError] = useState(''); const [notice, setNotice] = useState('');
  const requestId = useRef(0);
  const load = useCallback(async () => {
    const id = ++requestId.current;
    const params = new URLSearchParams({ status, page: String(page) });
    if (kind) params.set('kind', kind);
    if (route) params.set('route', route);
    try { const result = await request<CasePage>(`${base}/cases?${params}`); if (id === requestId.current) setData(result); }
    catch (caught) { if (id === requestId.current) setError(message(caught)); }
  }, [status, kind, route, page]);
  useEffect(() => { void load(); }, [load, revision]);
  function reset() { setPage(1); setLines({}); setTargets({}); setSearching({}); setPicking({}); setError(''); setNotice(''); }
  async function decide(item: MatchingCase, decision: CaseDecision, text: string) {
    setBusy(item.id); setError(''); setNotice('');
    try {
      await request(`${base}/cases/${item.id}`, { method: 'POST', body: JSON.stringify({ ...decision, fingerprint: item.fingerprint }) });
      setNotice(text);
    } catch (caught) { setError(message(caught)); }
    finally { setBusy(''); await load(); }
  }
  const kinds = data?.kinds ?? {};
  const total = Object.values(kinds).reduce((sum, value) => sum + value, 0);
  const shownKinds = [...CASE_KINDS.filter(value => kinds[value] || kind === value), ...Object.keys(kinds).filter(value => !(CASE_KINDS as string[]).includes(value) && kinds[value])];
  return <>
    <div className="admin-inventory-tabs" role="group" aria-label="Estado de los casos">{STATUSES.map(([value, label]) =>
      <button key={value} type="button" className={status === value ? 'selected' : ''} aria-pressed={status === value} onClick={() => { setStatus(value); setKind(''); reset(); }}>{label}</button>)}
      <label className="inventory-filter-select matching-route"><span>Ruta</span><select aria-label="Ruta de los casos" value={route} onChange={event => { setRoute(event.target.value); reset(); }}>
        <option value="">TODAS</option><option value="caso">CASO</option><option value="lote">LOTE</option></select></label>
      <button type="button" className="button soft small" onClick={() => void load()}><RefreshCw size={14} aria-hidden="true"/>Actualizar</button></div>
    <div className="admin-inventory-tabs matching-kinds" role="group" aria-label="Tipo de caso"><button type="button" className={!kind ? 'selected' : ''} aria-pressed={!kind} onClick={() => { setKind(''); reset(); }}>TODOS · {number(total)}</button>
      {shownKinds.map(value => <button key={value} type="button" className={kind === value ? 'selected' : ''} aria-pressed={kind === value} onClick={() => { setKind(value); reset(); }}>{caseKindLabels[value] ?? value.toUpperCase()} · {number(kinds[value] ?? 0)}</button>)}</div>
    {error && <div className="notice error" role="alert">{error}</div>}
    {notice && <div className="notice success" role="status">{notice}</div>}
    {!data && !error && <div className="loading"><LoaderCircle className="spin" size={18}/>Cargando casos…</div>}
    {data && !data.results.length && <div className="notice">No hay casos {status === 'review' ? 'por revisar' : status === 'applied' ? 'aplicados' : 'descartados'}{kind ? ` de este tipo` : ''}.</div>}
    <div className="inventory-ai-proposals">{data?.results.map(item => {
      const key = `${item.id}:${item.fingerprint}`;
      return <CaseArticle key={key} item={item}>{isKnownKind(item)
        ? <CaseCard item={item} busy={!!busy} line={lines[key] ?? ''} onLine={value => setLines(current => ({ ...current, [key]: value }))}
            searching={!!searching[key]} onSearch={() => setSearching(current => ({ ...current, [key]: true }))}
            picking={!!picking[key]} onPick={() => setPicking(current => ({ ...current, [key]: !current[key] }))}
            onDecide={(decision, text) => void decide(item, decision, text)}/>
        : <LegacyCaseCard item={item} busy={!!busy} target={targets[key] ?? item.target_sku} onTarget={value => setTargets(current => ({ ...current, [key]: value }))}
            onDecide={(decision, text) => void decide(item, decision, text)}/>}</CaseArticle>;
    })}</div>
    {data && <div className="pagination"><span>{number(data.count)} {data.count === 1 ? 'caso' : 'casos'}</span><div><button type="button" className="button soft small" disabled={!data.previous || !!busy} onClick={() => setPage(page - 1)}>Anterior</button><span>Página {page}</span><button type="button" className="button soft small" disabled={!data.next || !!busy} onClick={() => setPage(page + 1)}>Siguiente</button></div></div>}
  </>;
}

// ---------------------------------------------------------------- lote batches, decided together
const batchAction: Record<CaseAction, string> = { approve: 'Aprobar seleccionados', dismiss: 'Descartar seleccionados', merge: 'Unificar seleccionados', keep_apart: 'Mantener separadas',
  separar_referencia: 'Separar referencia' };
function Batches({ onDecided }: { onDecided: (text: string) => void }) {
  const [rows, setRows] = useState<CaseBatchIndexRow[] | null>(null);
  const [error, setError] = useState(''); const [revision, setRevision] = useState(0);
  const [open, setOpen] = useState<string | null>(null);
  useEffect(() => {
    let live = true; setError('');
    request<CaseBatchIndexRow[]>(`${base}/cases/batches`).then(result => { if (live) setRows(result); }).catch(caught => { if (live) setError(message(caught)); });
    return () => { live = false; };
  }, [revision]);
  if (open) return <BatchDetail batchKey={open} onBack={() => { setOpen(null); setRevision(value => value + 1); }} onDecided={onDecided}/>;
  return <>
    <p className="form-footnote">Un lote reúne casos del mismo origen que se revisan juntos (por ejemplo, las unificaciones automáticas anteriores que las reglas nuevas no harían). Ábrelo para decidir sus casos en bloque.</p>
    {error && <div className="notice error" role="alert">{error}<button type="button" onClick={() => setRevision(value => value + 1)}>Intentar de nuevo</button></div>}
    {!rows && !error && <div className="loading"><LoaderCircle className="spin" size={18}/>Cargando lotes…</div>}
    {rows && !rows.length && <div className="notice">No hay lotes de casos.</div>}
    {!!rows?.length && <ul className="matching-batches" aria-label="Lotes de casos">{rows.map(row => <li key={row.batch_key}>
      <div><strong>{batchLabels[row.batch_key] ?? row.batch_key}</strong><small>{row.batch_key}{row.created_at ? ` · desde ${day(row.created_at)}` : ''}</small>
        <span className="matching-batch-counts">{number(row.count)} por revisar · {number(row.counts.applied ?? 0)} aplicados · {number(row.counts.dismissed ?? 0)} descartados</span>
        {!!Object.keys(row.kinds).length && <span className="matching-batch-counts">{Object.entries(row.kinds).map(([kind, count]) => `${caseKindLabels[kind] ?? kind} · ${number(count)}`).join(' · ')}</span>}</div>
      <button type="button" className="button soft small" aria-label={`Abrir el lote ${row.batch_key}`} onClick={() => setOpen(row.batch_key)}>Abrir lote</button>
    </li>)}</ul>}
  </>;
}

function BatchDetail({ batchKey, onBack, onDecided }: { batchKey: string; onBack: () => void; onDecided: (text: string) => void }) {
  const [data, setData] = useState<CaseBatch | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [error, setError] = useState(''); const [busy, setBusy] = useState('');
  const [result, setResult] = useState<CaseBatchResult | null>(null);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    let live = true; setError('');
    request<CaseBatch>(`${base}/cases/batches/${encodeURIComponent(batchKey)}`).then(found => { if (live) { setData(found); setSelected(new Set(found.results.map(item => item.id))); } })
      .catch(caught => { if (live) setError(message(caught)); });
    return () => { live = false; };
  }, [batchKey, revision]);
  const cases = data?.results ?? [];
  const merges = cases.length > 0 && cases.every(item => item.kind === 'merge');
  const actions: CaseAction[] = merges ? ['merge', 'keep_apart'] : ['approve', 'dismiss'];
  const titles = new Map(cases.map(item => [item.id, caseTitle(item)]));
  async function decide(action: CaseAction) {
    const chosen = cases.filter(item => selected.has(item.id));
    if (!chosen.length) return;
    setBusy(action); setError(''); setResult(null);
    try {
      const done = await request<CaseBatchResult>(`${base}/cases/batches/${encodeURIComponent(batchKey)}`, { method: 'POST',
        body: JSON.stringify({ action, cases: chosen.map(item => ({ id: item.id, fingerprint: item.fingerprint })) }) });
      setResult(done);
      onDecided(`${batchLabels[batchKey] ?? batchKey}: ${number(done.decided.length)} ${done.decided.length === 1 ? 'caso decidido' : 'casos decididos'}${done.failed.length ? `, ${number(done.failed.length)} sin decidir` : ''}.`);
      setRevision(value => value + 1);
    } catch (caught) { setError(message(caught)); } finally { setBusy(''); }
  }
  function toggle(id: string) { setSelected(current => { const next = new Set(current); if (next.has(id)) next.delete(id); else next.add(id); return next; }); }
  return <div className="matching-batch" aria-label={`Lote ${batchKey}`} role="region">
    <div className="admin-toolbar-actions"><button type="button" className="button text small" onClick={onBack}>← Todos los lotes</button>
      <strong>{batchLabels[batchKey] ?? batchKey}</strong>{data && <span>{number(data.count)} por revisar · {number(data.counts.applied ?? 0)} aplicados · {number(data.counts.dismissed ?? 0)} descartados</span>}</div>
    {merges && <p className="form-footnote">Unificar conserva juntas las líneas OEM de cada caso (o las une, si están separadas) en la línea OEM que el caso propone; Mantener separadas las separa con Separar SKU y no se vuelven a proponer juntas. Cada caso se decide por separado: uno que cambió queda sin decidir con su motivo.</p>}
    {error && <div className="notice error" role="alert">{error}</div>}
    {result && !!result.failed.length && <div className="notice error" role="alert"><div><strong>Sin decidir:</strong><ul>{result.failed.map(row => <li key={row.id}>{titles.get(row.id) ?? row.id}: {row.detail}</li>)}</ul></div></div>}
    {!data && !error && <div className="loading"><LoaderCircle className="spin" size={18}/>Cargando el lote…</div>}
    {data && !cases.length && <div className="notice">Este lote no tiene casos por revisar.</div>}
    {!!cases.length && <>
      <div className="admin-toolbar-actions matching-batch-actions">
        <label className="admin-checkbox"><input type="checkbox" checked={selected.size === cases.length} onChange={event => setSelected(event.target.checked ? new Set(cases.map(item => item.id)) : new Set())}/>Seleccionar los {number(cases.length)}</label>
        {actions.map((action, index) => <button key={action} type="button" className={`button ${index === 0 ? 'primary' : 'soft'} small`} disabled={!!busy || !selected.size} onClick={() => void decide(action)}>{busy === action && <LoaderCircle size={14} className="spin"/>}{batchAction[action]} ({number(selected.size)})</button>)}
      </div>
      {data && data.count > cases.length && <p className="form-footnote">Se muestran los primeros {number(cases.length)} de {number(data.count)}; al decidirlos aparecen los siguientes.</p>}
      <ul className="matching-batch-cases" aria-label="Casos del lote">{cases.map(item => <li key={item.id}>
        <label><input type="checkbox" checked={selected.has(item.id)} disabled={!!busy || !isOpen(item)} onChange={() => toggle(item.id)} aria-label={caseTitle(item)}/>
          <span><strong>{item.kind === 'merge' ? caseLines(item).map(row => row.sku).join(' + ') : item.sources[0]?.sku}</strong>
            <small>{caseKindLabels[item.kind] ?? item.kind}{item.target_sku ? ` · se conserva ${item.target_sku}` : ''}</small>
            {item.reason && <small>{item.reason}</small>}{item.guard && <small className="matching-guard">{guardLabels[item.guard] ?? item.guard}</small>}</span></label>
      </li>)}</ul>
    </>}
  </div>;
}

// ---------------------------------------------------------------- the automatic merges, each undone by Separar SKU
function MergeDigest({ revision, onSplit }: { revision: number; onSplit: (target: { id: string; sku: string }) => void }) {
  const [search, setSearch] = useState(''); const [query, setQuery] = useState('');
  const [since, setSince] = useState('');
  const [page, setPage] = useState(1);
  const [data, setData] = useState<Page<MergeDigestRow> | null>(null);
  const [error, setError] = useState('');
  useEffect(() => { const timer = setTimeout(() => { setQuery(search.trim()); setPage(1); }, 300); return () => clearTimeout(timer); }, [search]);
  useEffect(() => {
    let live = true; setError('');
    const params = new URLSearchParams({ page: String(page) });
    if (query) params.set('search', query);
    if (since) params.set('since', since);
    request<Page<MergeDigestRow>>(`${base}/merges?${params}`).then(result => { if (live) setData(result); }).catch(caught => { if (live) setError(message(caught)); });
    return () => { live = false; };
  }, [query, since, page, revision]);
  return <>
    <p className="form-footnote">Las líneas OEM que el análisis unificó solo (piezas idénticas: mismo código con otra grafía, marcas socias, un número que reemplaza a otro). Si una no es la misma pieza, Separar SKU la devuelve con sus códigos, existencias e imágenes, y no se vuelve a unificar sola.</p>
    <div className="inventory-filters">
      <div className="catalog-search admin-search"><Search size={19}/><label className="sr-only" htmlFor="merge-digest-search">Buscar unificaciones</label><UppercaseInput id="merge-digest-search" value={search} onChange={event => setSearch(event.target.value)} placeholder="SKU de la línea OEM o de una línea OEM unificada…"/></div>
      <label className="inventory-filter-select"><span>Desde</span><input type="date" aria-label="Unificaciones desde" value={since} onChange={event => { setSince(event.target.value); setPage(1); }}/></label>
    </div>
    {error && <div className="notice error" role="alert">{error}</div>}
    {!data && !error && <div className="loading"><LoaderCircle className="spin" size={18}/>Cargando unificaciones…</div>}
    {data && !data.results.length && <div className="notice">No hay unificaciones automáticas{query || since ? ' con estos filtros' : ''}.</div>}
    {!!data?.results.length && <ul className="merge-digest" aria-label="Unificaciones automáticas">{data.results.map(row => {
      const back = row.sources.filter(source => !source.still_merged).length;
      return <li key={row.id} aria-label={`Unificación en ${row.target.sku}`}>
        <div className="merge-digest-head"><strong>{row.target.sku}</strong>{row.target.estado !== 'publicada' && <span className="pill">{lineEstadoLabels[row.target.estado] ?? row.target.estado}</span>}<small>{day(row.created_at)}</small></div>
        {row.target.description && <p>{row.target.description}</p>}
        <p className="merge-digest-sources">{row.sources.map(source => <span key={source.id} className={source.still_merged ? '' : 'split-back'}>{source.sku}{source.still_merged ? '' : ' · SEPARADA'}</span>)}</p>
        {row.reason && <small>{row.reason}</small>}
        <div className="admin-toolbar-actions">{back === row.sources.length ? <span className="form-footnote">Ya se separaron todas.</span>
          : <button type="button" className="button soft small" aria-label={`Separar SKU de ${row.target.sku}`} onClick={() => onSplit({ id: row.target.id, sku: row.target.sku })}><Ungroup size={14} aria-hidden="true"/>Separar SKU</button>}</div>
      </li>;
    })}</ul>}
    {data && <div className="pagination"><span>{number(data.count)} {data.count === 1 ? 'unificación' : 'unificaciones'}</span><div><button type="button" className="button soft small" disabled={!data.previous} onClick={() => setPage(page - 1)}>Anterior</button><span>Página {page}</span><button type="button" className="button soft small" disabled={!data.next} onClick={() => setPage(page + 1)}>Siguiente</button></div></div>}
  </>;
}
