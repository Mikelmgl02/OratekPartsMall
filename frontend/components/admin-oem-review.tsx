'use client';

import { FormEvent, useCallback, useEffect, useRef, useState } from 'react';
import { Check, Download, GitMerge, History, Layers3, ListChecks, LoaderCircle, OctagonPause, RotateCcw, Search, Tags, Undo2, X } from 'lucide-react';
import { UppercaseInput } from './uppercase-field';
import { request } from '@/lib/types';
import type { CatalogGroupingCandidate } from '@/lib/types';
import type { OEMRevertResult, OEMRun, OEMRunPage, OEMSpotCheck } from '@/lib/oem-finder-types';
import { stoppedLabels } from '@/lib/oem-finder-types';
import { tagKindLabels, variantKindLabels } from '@/lib/suffix-types';
import { OEMBulkPreview, OEMBulkProgress, OEMDecisionResult, OEMDismissReason, OEMReviewCase, OEMReviewFilters, OEMReviewPage, OEMReviewStatus,
  blockerLabel, bulkTiers, conflictKindLabel, dismissReasons, excludedLabels, lexiconLabel, reviewSkipLabels, reviewStatusLabels, reviewTierLabels, reviewTiers,
  unitLabels } from '@/lib/oem-review-types';

const base = '/api/management/oem-review';
const runsBase = '/api/management/oem-finder/runs';
const number = (value: number) => value.toLocaleString('es-PA');
const stamp = (value: string) => new Date(value).toLocaleString('es-PA', { timeZone: 'America/Panama' });
const errorMessage = (error: unknown) => error instanceof Error ? error.message : 'No se pudo completar la acción. Inténtalo de nuevo.';
const empty: OEMReviewFilters = { status: 'review', tier: '', blocker: '', chain: '', make: '', system: '', in_stock: '', search: '' };
const bulkable = (tier: string) => (bulkTiers as string[]).includes(tier);
const query = (filters: Partial<OEMReviewFilters>, extra: Record<string, string> = {}) => {
  const params = new URLSearchParams(extra);
  Object.entries(filters).forEach(([key, value]) => { if (value) params.set(key, value); });
  return params.toString();
};

function download(name: string, body: string, type: string) {
  const url = URL.createObjectURL(new Blob([body], { type }));
  const link = document.createElement('a');
  link.href = url; link.download = name; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

type Props = { onChanged: () => void; onCounts?: () => void; onOpenSuffixes?: (token: string) => void; onOpenGrouping?: (family: CatalogGroupingCandidate) => void };

export default function OEMReview({ onChanged, onCounts, onOpenSuffixes, onOpenGrouping }: Props) {
  const [view, setView] = useState<'cases' | 'runs'>('cases');
  const [filters, setFilters] = useState<OEMReviewFilters>(empty); const [search, setSearch] = useState(''); const [page, setPage] = useState(1);
  const [data, setData] = useState<OEMReviewPage | null>(null); const [error, setError] = useState(''); const [notice, setNotice] = useState('');
  const [revision, setRevision] = useState(0); const [busy, setBusy] = useState(''); const [bulkOpen, setBulkOpen] = useState(false); const [runFocus, setRunFocus] = useState<number | null>(null);
  const requestId = useRef(0);
  useEffect(() => { const timer = setTimeout(() => { setFilters(current => current.search === search.trim() ? current : { ...current, search: search.trim() }); setPage(1); }, 300); return () => clearTimeout(timer); }, [search]);
  useEffect(() => {
    const id = ++requestId.current;
    request<OEMReviewPage>(`${base}?${query(filters, { page: String(page) })}`).then(result => { if (id === requestId.current) { setData(result); setError(''); } })
      .catch(e => { if (id === requestId.current) { setData(null); setError(errorMessage(e)); } });
  }, [filters, page, revision]);
  function filter(patch: Partial<OEMReviewFilters>) { setFilters(current => ({ ...current, ...patch })); setPage(1); setNotice(''); setBulkOpen(false); }
  const refresh = useCallback(() => setRevision(v => v + 1), []);
  async function decide(item: OEMReviewCase, action: string, extra: Record<string, string> = {}) {
    setBusy(`${item.id}:${action}`); setError(''); setNotice('');
    try {
      const result = await request<OEMDecisionResult>(`${base}/${item.id}`, { method: 'POST', body: JSON.stringify({ action, fingerprint: item.fingerprint, ...extra }) });
      if (action === 'approve' || action === 'choose_oem') {
        setNotice(result.case.decision.method === 'flag' ? `${item.sku} marcado como OEM ${result.case.decision.code} (ejecución #${result.run}).`
          : `${item.sku} renombrado a ${result.case.decision.code}; ${item.sku} queda como alterno (ejecución #${result.run}).`);
        onChanged();
      } else {
        if (action === 'send_to_merge' && result.family) {
          setNotice(`Caso enviado a Agrupar SKU con la familia de ${result.family.target_sku}. Si no la agrupas, vuelve a la revisión en el próximo análisis OEM (o usa Reabrir).`);
          onOpenGrouping?.(result.family);
        } else setNotice(action === 'dismiss' ? `Caso de ${item.sku} descartado.` : `Caso de ${item.sku} devuelto a la revisión.`);
        onCounts?.();
      }
      refresh(); return true;
    } catch (e) { setError(errorMessage(e)); refresh(); return false; }
    finally { setBusy(''); }
  }
  async function undo(item: OEMReviewCase) {
    if (!item.decision.run) return;
    setBusy(`${item.id}:undo`); setError(''); setNotice('');
    try {
      const result = await request<OEMRevertResult>(`${runsBase}/${item.decision.run}/revert`, { method: 'POST', body: JSON.stringify({ part: item.part_id }) });
      setNotice(result.reverted ? `${item.decision.code} deshecho: ${item.decision.previous_sku || item.sku} vuelve a la revisión.` : result.skipped.length ? `No se deshizo: ${result.skipped[0].label}.` : 'El cambio ya estaba deshecho.');
      if (result.reverted) onChanged();
      refresh();
    } catch (e) { setError(errorMessage(e)); }
    finally { setBusy(''); }
  }
  const tierCounts = data?.tiers ?? {}; const total = Object.values(tierCounts).reduce((sum, n) => sum + (n ?? 0), 0);
  const scenario = data?.last_run?.scenario; const pending = scenario?.tiers && data?.last_run ? (scenario.tiers.AUTO_RENAME_BASE ?? 0) - (data.last_run.tiers.AUTO_RENAME_BASE?.[0] ?? 0) : 0;
  return <section className="oem-review" aria-label="Revisión OEM">
    <div className="admin-inventory-tabs oem-review-views" role="group" aria-label="Vista de la revisión OEM">
      <button className={view === 'cases' ? 'selected' : ''} aria-pressed={view === 'cases'} onClick={() => setView('cases')}><ListChecks size={14}/>Casos</button>
      <button className={view === 'runs' ? 'selected' : ''} aria-pressed={view === 'runs'} onClick={() => { setView('runs'); setRunFocus(null); }}><History size={14}/>Ejecuciones</button>
    </div>
    {view === 'runs' ? <Runs focus={runFocus} onChanged={() => { onChanged(); refresh(); }}/> : <>
      <p className="form-footnote">Propuestas del buscador OEM que necesitan una persona. Aprobar marca el SKU como OEM o lo renombra al OEM (mismo SKU interno, existencias, fotos y alternos; el SKU anterior queda como alterno) en una ejecución que puedes deshacer. Los conflictos se resuelven en Agrupar SKU; los sufijos desconocidos, en Sufijos.</p>
      {data?.last_run && <p className="oem-review-run">ÚLTIMO ANÁLISIS OEM #{data.last_run.id} · {stamp(data.last_run.finished_at)} · TABLA DE SUFIJOS {data.last_run.suffix_table_version}{pending > 0 && scenario?.tokens?.length ? ` · SI EL PROPIETARIO CONFIRMA ${scenario.tokens.slice(0, 6).join(', ')}${scenario.tokens.length > 6 ? '…' : ''}: +${number(pending)} RENOMBRES AUTOMÁTICOS` : ''}</p>}
      {data && !data.last_run && <p className="oem-review-run">AÚN NO HAY UN ANÁLISIS OEM GUARDADO. Se ejecuta en el servidor con python -m mall.oem_finder --dry-run.</p>}
      <div className="admin-inventory-tabs oem-tier-tabs" role="group" aria-label="Nivel OEM">
        <button className={!filters.tier ? 'selected' : ''} aria-pressed={!filters.tier} onClick={() => filter({ tier: '' })}>TODOS · {number(total)}</button>
        {reviewTiers.filter(tier => tierCounts[tier] || filters.tier === tier).map(tier => <button key={tier} className={filters.tier === tier ? 'selected' : ''} aria-pressed={filters.tier === tier} onClick={() => filter({ tier })}>{reviewTierLabels[tier]} · {number(tierCounts[tier] ?? 0)}</button>)}
      </div>
      <div className="suffix-filters oem-review-filters">
        <label className="suffix-search"><Search size={16}/><span className="sr-only">Buscar caso OEM</span><UppercaseInput value={search} onChange={e => setSearch(e.target.value)} placeholder="SKU, DESCRIPCIÓN U OEM" aria-label="Buscar caso OEM"/></label>
        <label>ESTADO<select aria-label="Estado del caso" value={filters.status} onChange={e => filter({ status: e.target.value as OEMReviewStatus })}>{(Object.keys(reviewStatusLabels) as OEMReviewStatus[]).map(key => <option key={key} value={key}>{reviewStatusLabels[key]}{data?.statuses[key] !== undefined ? ` (${number(data.statuses[key] ?? 0)})` : ''}</option>)}</select></label>
        <Facet label="BLOQUEO" aria="Bloqueo" value={filters.blocker} options={data?.facets.blockers} format={blockerLabel} onChange={blocker => filter({ blocker })}/>
        <Facet label="CADENA DE SUFIJOS" aria="Cadena de sufijos" value={filters.chain} options={data?.facets.chains} format={value => `-${value}`} onChange={chain => filter({ chain })}/>
        <Facet label="MARCA" aria="Marca" value={filters.make} options={data?.facets.makes} onChange={make => filter({ make })}/>
        <Facet label="SISTEMA" aria="Sistema" value={filters.system} options={data?.facets.systems} onChange={system => filter({ system })}/>
        <label>EXISTENCIAS<select aria-label="Existencias" value={filters.in_stock} onChange={e => filter({ in_stock: e.target.value })}><option value="">TODAS</option><option value="true">CON EXISTENCIAS</option><option value="false">SIN EXISTENCIAS</option></select></label>
        {(filters.tier || filters.blocker || filters.chain || filters.make || filters.system || filters.in_stock || filters.search) && <button className="button text small" onClick={() => { setSearch(''); filter({ ...empty, status: filters.status }); }}><X size={14}/>Quitar filtros</button>}
      </div>
      {filters.status === 'review' && data && <div className="oem-bulk-bar">
        <span>{number(data.count)} {data.count === 1 ? 'caso por revisar' : 'casos por revisar'} con estos filtros{filters.tier && !bulkable(filters.tier) ? ' · este nivel no se aprueba en lote' : ''}.</span>
        <button className="button soft small" disabled={!!busy || bulkOpen || !data.count || (!!filters.tier && !bulkable(filters.tier))} onClick={() => { setBulkOpen(true); setNotice(''); setError(''); }}><ListChecks size={14}/>Aprobar en lote…</button>
      </div>}
      {bulkOpen && <Bulk filters={filters} onClose={() => setBulkOpen(false)} onDone={(text, changed) => { setNotice(text); if (changed) onChanged(); refresh(); }} onShowRun={run => { setBulkOpen(false); setRunFocus(run); setView('runs'); }}/>}
      {error && <div className="notice error" role="alert">{error}{!data && <button onClick={refresh}>Intentar de nuevo</button>}</div>}
      {notice && <div className="notice success" role="status"><Check size={16}/>{notice}</div>}
      {!data && !error && <div className="loading"><LoaderCircle className="spin" size={20}/>Cargando casos OEM…</div>}
      {data && !data.results.length && <div className="empty-state"><ListChecks size={30}/><h3>No hay casos con estos filtros.</h3><p>Prueba con otro nivel, estado o búsqueda.</p></div>}
      <div className="oem-cases">{data?.results.map(item => <CaseCard key={`${item.id}:${item.fingerprint}`} item={item} busy={busy} onDecide={(action, extra) => decide(item, action, extra)} onUndo={() => undo(item)} onOpenSuffixes={onOpenSuffixes}/>)}</div>
      {data && <div className="pagination"><span>{number(data.count)} {data.count === 1 ? 'caso' : 'casos'}</span><div><button className="button soft small" disabled={!data.previous} onClick={() => setPage(page - 1)}>Anterior</button><span>Página {page}</span><button className="button soft small" disabled={!data.next} onClick={() => setPage(page + 1)}>Siguiente</button></div></div>}
    </>}
  </section>;
}

function Facet({ label, aria, value, options, format = text => text, onChange }: { label: string; aria: string; value: string; options?: [string, number][]; format?: (value: string) => string; onChange: (value: string) => void }) {
  const list = options ?? []; const present = !value || list.some(([key]) => key === value);
  return <label>{label}<select aria-label={aria} value={value} onChange={e => onChange(e.target.value)}><option value="">TODOS</option>{!present && <option value={value}>{format(value)} (0)</option>}{list.map(([key, n]) => <option key={key} value={key}>{format(key)} ({number(n)})</option>)}</select></label>;
}

function CaseCard({ item, busy, onDecide, onUndo, onOpenSuffixes }: { item: OEMReviewCase; busy: string; onDecide: (action: string, extra?: Record<string, string>) => Promise<boolean>; onUndo: () => void; onOpenSuffixes?: (token: string) => void }) {
  const [dismissing, setDismissing] = useState(false); const [reason, setReason] = useState<OEMDismissReason | ''>(''); const [note, setNote] = useState('');
  const [choice, setChoice] = useState(item.choices[0]?.code ?? ''); const [confirmUndo, setConfirmUndo] = useState(false);
  const ev = item.evidence, lex = ev.lexicon, locked = !!busy;
  const unknown = [...new Set([...ev.blocking_unknowns, ...item.chain_tokens.filter(token => token.class === 'UNKNOWN').map(token => token.token)])];  // the tokens that block the case
  const decision = item.decision;
  async function dismiss(event: FormEvent) { event.preventDefault(); if (reason && await onDecide('dismiss', { reason, note })) setDismissing(false); }
  return <article className={`oem-case ${item.status}`} aria-label={`Caso OEM ${item.sku}`}>
    <div className="oem-case-heading"><strong>{item.sku}</strong><span className="pill">{reviewTierLabels[item.tier] || item.tier_label}</span>{item.status !== 'review' && <span className={`status ${item.status === 'applied' ? 'matched' : 'review'}`}>{reviewStatusLabels[item.status]}</span>}<span className={`status ${item.in_stock ? 'matched' : ''}`}>{item.in_stock ? `EN EXISTENCIA · ${number(item.available_quantity)}` : 'SIN EXISTENCIAS'}</span></div>
    <p>{item.description || 'SIN DESCRIPCIÓN'}</p>
    {item.evaluated_sku !== item.sku && <small>ANALIZADO COMO {item.evaluated_sku}</small>}
    {item.candidate && <div className="oem-proposal"><span>OEM MAIN PROPUESTO</span><strong>{item.candidate}{item.brand && ` · ${item.brand}`}</strong>
      {item.written_form && item.written_form !== item.candidate && <small>ESCRITO EN EL CATÁLOGO: {item.written_form} (QUEDA COMO ALTERNO)</small>}
      {item.status === 'review' && item.tier !== 'MULTI_OEM' && item.tier !== 'CONFLICT' && <small>{item.method === 'flag' ? 'EL SKU YA ES ESTE NÚMERO: APROBAR LO MARCA COMO OEM.' : `APROBAR RENOMBRA ${item.sku} → ${item.candidate}; ${item.sku} QUEDA COMO ALTERNO.`}</small>}
    </div>}
    <dl className="oem-evidence">
      {item.system && <div><dt>FORMATO</dt><dd>{item.system}{item.grade && ` · ${item.grade}`}{!!ev.flags.length && <small>{ev.flags.map(flag => blockerLabel(`flag:${flag}`)).join(' · ')}</small>}</dd></div>}
      {lex.result && <div><dt>LÉXICO DE LA CLASE</dt><dd>{lexiconLabel(lex.result)}{lex.n_other_keys ? ` · ${number(lex.n_other_keys)} CLAVES` : ''}{lex.share != null && lex.n_other_keys ? ` · ${Math.round(lex.share * 100)} %` : ''}{!!lex.top_heads?.length && <small>{lex.top_heads.slice(0, 3).map(([head, n]) => `${head.replace('|', ' ')} (${n})`).join(' · ')}</small>}</dd></div>}
      {(!!ev.units.length || !!ev.attributions.length || ev.n_suppliers > 0) && <div><dt>EVIDENCIA</dt><dd>{ev.units.map(unit => unitLabels[unit] || unit).join(' · ') || 'SIN UNIDADES'}{!!ev.attributions.length && <small>ATRIBUCIONES: {ev.attributions.join(', ')}</small>}<small>{number(ev.n_suppliers)} {ev.n_suppliers === 1 ? 'PROVEEDOR' : 'PROVEEDORES'}{Object.keys(ev.source_counts).length ? ` · FUENTES: ${Object.entries(ev.source_counts).map(([source, n]) => `${source.toUpperCase()} ${n}`).join(', ')}` : ''}{ev.verified_oem_alterno ? ' · ALTERNO OEM VERIFICADO' : ''}</small></dd></div>}
      {!!item.chain_tokens.length && <div><dt>CADENA DE SUFIJOS</dt><dd className="oem-chain">{item.chain_tokens.map((token, index) => <span key={index} className={`suffix-class ${token.class.toLowerCase()}`} title={`${token.token} · ${token.kind || 'sin tipo'} · ${token.status}`}>{token.sep}{token.tok} · {token.class === 'TAG' ? 'ETIQUETA' : token.class === 'VARIANT' ? 'VARIANTE' : 'DESCONOCIDO'}{token.kind ? ` · ${(token.class === 'TAG' ? tagKindLabels[token.kind] : variantKindLabels[token.kind]) || token.kind.toUpperCase()}` : ''}{token.auto_eligible ? ' · AUTOMÁTICA' : ''}</span>)}</dd></div>}
      {!!item.blockers.length && <div className="wide"><dt>BLOQUEOS</dt><dd className="oem-chips">{item.blockers.map(blocker => <span key={blocker}>{blockerLabel(blocker)}</span>)}</dd></div>}
      {(!!ev.shared_with.length || !!ev.conflicts.length || ev.conflict_kind) && <div className="wide"><dt>CONFLICTO</dt><dd>{ev.conflict_kind ? conflictKindLabel(ev.conflict_kind) : 'OTRO SKU USA EL NÚMERO'}{!!ev.shared_with.length && <small>COMPARTIDO CON: {ev.shared_with.join(', ')}</small>}{!!ev.family_incompatible.length && <small className="danger-text">INCOMPATIBLES: {ev.family_incompatible.join(', ')}</small>}</dd></div>}
      {ev.apply_conflict && <div className="wide"><dt>RECHAZADO AL APLICAR</dt><dd>{ev.apply_conflict.detail}</dd></div>}
      {(!!ev.siblings.length || !!ev.siblings_to_merge_into_this.length) && <div><dt>HERMANOS</dt><dd>{(ev.siblings_to_merge_into_this.length ? ev.siblings_to_merge_into_this : ev.siblings).join(', ')}{!!ev.inconsistent_siblings.length && <small className="danger-text">INCOMPATIBLES: {ev.inconsistent_siblings.join(', ')}</small>}</dd></div>}
      {!!ev.secondary_company_refs.length && <div><dt>REFERENCIAS DE EMPRESA</dt><dd>{ev.secondary_company_refs.map(ref => `${ref.brand} ${ref.code}`).join(' · ')}</dd></div>}
      {!!ev.warnings.length && <div><dt>ADVERTENCIAS</dt><dd>{ev.warnings.map(warning => warning.replace('desc_variant:', 'DESCRIPCIÓN CON ')).join(' · ')}</dd></div>}
      {!!item.choices.length && item.status !== 'review' && <div><dt>NÚMEROS OEM</dt><dd>{item.choices.map(option => option.code).join(' · ')}</dd></div>}
      {!!item.codes.length && <div><dt>ALTERNOS</dt><dd>{item.codes.map(code => `${code.code}${code.brand ? ` · ${code.brand}` : ''}`).join(' · ')}</dd></div>}
      {!!item.supplier_codes.length && <div><dt>CÓDIGOS DE PROVEEDOR</dt><dd>{item.supplier_codes.map(code => `${code.codigo}${code.brand ? ` (${code.brand})` : ''} · ${code.supplier}`).join(' · ')}</dd></div>}
    </dl>
    {!!ev.reasons.length && <ul className="oem-reasons">{ev.reasons.map(text => <li key={text}>{text}</li>)}</ul>}
    {item.stale && <div className="notice" role="status">{item.stale === 'suffix_changed' ? 'LA TABLA DE SUFIJOS CAMBIÓ DESDE EL ANÁLISIS' : 'EL SKU CAMBIÓ DESDE EL ANÁLISIS'} ({item.stale_label.toUpperCase()}): no se puede aprobar. El próximo análisis OEM actualizará el caso.</div>}
    {item.status !== 'review' && decision.action && <p className="oem-decision">{decision.action === 'approve' || decision.action === 'choose_oem' ? `APROBADO: ${decision.method === 'flag' ? 'MARCADO COMO OEM' : `RENOMBRADO A ${decision.code}`} · EJECUCIÓN #${decision.run}${decision.bulk ? ' (EN LOTE)' : ''}` : decision.action === 'dismiss' ? `DESCARTADO: ${decision.label?.toUpperCase() || ''}${decision.note ? ` · ${decision.note}` : ''}` : decision.action === 'send_to_merge' ? `ENVIADO A AGRUPAR SKU (${decision.target_sku})` : decision.action.toUpperCase()}{item.decided_by && ` · ${item.decided_by}`}{item.decided_at && ` · ${stamp(item.decided_at)}`}</p>}
    {item.status === 'review' && decision.action === 'reverted' && <p className="oem-decision">APROBACIÓN DESHECHA EN LA EJECUCIÓN #{decision.run}{item.decided_by && ` · ${item.decided_by}`}</p>}
    {item.tier === 'MULTI_OEM' && item.actions.includes('choose_oem') && <fieldset className="oem-choices"><legend>ELIGE EL OEM MAIN (LOS DEMÁS QUEDAN COMO ALTERNOS OEM)</legend>{item.choices.map(option => <label key={option.code} className="admin-checkbox"><input type="radio" name={`oem-choice-${item.id}`} value={option.code} checked={choice === option.code} disabled={locked} onChange={() => setChoice(option.code)}/>{option.code} · {option.brand}{option.grade ? ` · F${option.grade}` : ''}{option.written !== option.code && ` (ESCRITO ${option.written})`}</label>)}</fieldset>}
    {(item.tier === 'VARIANT_REVIEW' || item.tier === 'WEAK') && item.actions.includes('approve') && <small className="danger-text">REVISA QUE LA BASE SEA LA MISMA PIEZA: LAS VARIANTES (MEDIDA, MATERIAL, KIT) Y LOS CANDIDATOS DÉBILES SUELEN TENER OTRO OEM.</small>}
    {item.tier === 'CONFLICT' && item.status === 'review' && !!ev.family_incompatible.length && <small className="danger-text">LA FAMILIA MEZCLA PIEZAS INCOMPATIBLES: NO SE PUEDE AGRUPAR. CORRIGE LOS DATOS O DESCARTA EL CASO.</small>}
    {dismissing ? <form className="suffix-inline-form oem-dismiss" onSubmit={dismiss} aria-label={`Descartar ${item.sku}`}>
      <label>Motivo<select aria-label={`Motivo para descartar ${item.sku}`} required value={reason} onChange={e => setReason(e.target.value as OEMDismissReason)}><option value="">ELIGE UN MOTIVO</option>{(Object.keys(dismissReasons) as OEMDismissReason[]).map(key => <option key={key} value={key}>{dismissReasons[key]}</option>)}</select></label>
      <label>Nota{reason === 'other' ? '' : ' (opcional)'}<UppercaseInput aria-label={`Nota para descartar ${item.sku}`} value={note} maxLength={300} required={reason === 'other'} onChange={e => setNote(e.target.value)}/></label>
      <button className="button primary small" disabled={locked || !reason}>{busy === `${item.id}:dismiss` && <LoaderCircle size={14} className="spin"/>}Confirmar descarte</button><button type="button" className="button text small" onClick={() => setDismissing(false)}>Cancelar</button>
    </form> : <div className="suffix-actions oem-actions">
      {item.actions.includes('approve') && <button className="button primary small" aria-label={`Aprobar ${item.sku}`} disabled={locked} onClick={() => void onDecide('approve')}>{busy === `${item.id}:approve` ? <LoaderCircle size={14} className="spin"/> : <Check size={14}/>}{item.method === 'flag' ? 'Aprobar: marcar como OEM' : `Aprobar: renombrar a ${item.candidate}`}</button>}
      {item.actions.includes('choose_oem') && <button className="button primary small" aria-label={`Elegir OEM para ${item.sku}`} disabled={locked || !choice} onClick={() => void onDecide('choose_oem', { code: choice })}>{busy === `${item.id}:choose_oem` ? <LoaderCircle size={14} className="spin"/> : <Check size={14}/>}Elegir {choice}</button>}
      {item.actions.includes('send_to_merge') && <button className="button primary small" aria-label={`Enviar ${item.sku} a Agrupar SKU`} disabled={locked} onClick={() => void onDecide('send_to_merge')}>{busy === `${item.id}:send_to_merge` ? <LoaderCircle size={14} className="spin"/> : <GitMerge size={14}/>}Enviar a Agrupar SKU</button>}
      {item.status === 'review' && unknown.map(token => <button key={token} className="button soft small" aria-label={`Etiquetar sufijo ${token}`} disabled={!onOpenSuffixes} onClick={() => onOpenSuffixes?.(token)}><Tags size={14}/>Etiquetar sufijo {token} en Sufijos</button>)}
      {item.actions.includes('dismiss') && <button className="button text small" aria-label={`Descartar ${item.sku}`} disabled={locked} onClick={() => { setDismissing(true); setReason(''); setNote(''); }}><X size={14}/>Descartar</button>}
      {item.actions.includes('reopen') && <button className="button soft small" aria-label={`Reabrir ${item.sku}`} disabled={locked} onClick={() => void onDecide('reopen')}><RotateCcw size={14}/>Reabrir</button>}
      {item.status === 'applied' && decision.run && (confirmUndo ? <>
        <button className="button primary small" aria-label={`Confirmar deshacer ${item.sku}`} disabled={locked} onClick={() => { setConfirmUndo(false); onUndo(); }}>{busy === `${item.id}:undo` ? <LoaderCircle size={14} className="spin"/> : <Undo2 size={14}/>}Deshacer {decision.method === 'flag' ? 'la marca OEM' : `y volver a ${decision.previous_sku}`}</button>
        <button className="button text small" onClick={() => setConfirmUndo(false)}>Cancelar</button>
      </> : <button className="button text small" aria-label={`Deshacer ${item.sku}`} disabled={locked} onClick={() => setConfirmUndo(true)}><Undo2 size={14}/>Deshacer</button>)}
    </div>}
  </article>;
}

function Bulk({ filters, onClose, onDone, onShowRun }: { filters: OEMReviewFilters; onClose: () => void; onDone: (text: string, changed: boolean) => void; onShowRun: (run: number) => void }) {
  const [preview, setPreview] = useState<OEMBulkPreview | null>(null); const [error, setError] = useState(''); const [progress, setProgress] = useState<OEMBulkProgress | null>(null);
  const [running, setRunning] = useState(false); const stop = useRef(false);
  useEffect(() => {
    let live = true;
    request<OEMBulkPreview>(`${base}/bulk?${query({ ...filters, status: 'review' })}`).then(result => { if (live) setPreview(result); }).catch(e => { if (live) setError(errorMessage(e)); });
    return () => { live = false; };
  }, [filters]);
  async function loop(first: OEMBulkProgress) {
    let current = first; setProgress(current);
    while (!current.finished && !current.waiting) {
      if (stop.current) { current = await request<OEMBulkProgress>(`${base}/bulk/${current.run.id}`, { method: 'POST', body: JSON.stringify({ action: 'stop' }) }); break; }
      current = await request<OEMBulkProgress>(`${base}/bulk/${current.run.id}`, { method: 'POST', body: JSON.stringify({ action: 'continue' }) });
      setProgress(current);
    }
    setProgress(current);
    const s = current.run.applied;
    if (current.waiting) onDone(`Aprobación en lote #${current.run.id} en pausa: hay una importación del catálogo en curso. Continúala desde Ejecuciones cuando termine.`, !!s.applied);
    else onDone(`Aprobación en lote #${current.run.id}: ${number(s.applied ?? 0)} aprobados${s.conflicts ? `, ${number(s.conflicts)} pasaron a conflicto` : ''}${Object.values(s.skipped ?? {}).reduce((a, b) => a + b, 0) ? `, ${number(Object.values(s.skipped ?? {}).reduce((a, b) => a + b, 0))} omitidos` : ''}${s.errors ? `, ${number(s.errors)} errores` : ''}.${s.stopped ? ` ${stoppedLabels[s.stopped]}.` : ''} Puedes deshacerla desde Ejecuciones.`, !!s.applied);
  }
  async function start() {
    if (!preview) return;
    setRunning(true); setError(''); stop.current = false;
    try {
      const body = { tier: filters.tier, blocker: filters.blocker, chain: filters.chain, make: filters.make, system: filters.system, in_stock: filters.in_stock, search: filters.search, selection: preview.selection };
      const first = await request<OEMBulkProgress>(`${base}/bulk`, { method: 'POST', body: JSON.stringify(body) });
      await loop(first);
    } catch (e) { setError(errorMessage(e)); }
    finally { setRunning(false); }
  }
  async function resume(run: number) {
    setRunning(true); setError(''); stop.current = false;
    try { await loop(await request<OEMBulkProgress>(`${base}/bulk/${run}`, { method: 'POST', body: JSON.stringify({ action: 'continue' }) })); }
    catch (e) { setError(errorMessage(e)); }
    finally { setRunning(false); }
  }
  const s = progress?.run.applied; const done = s?.done ?? 0, totalRows = s?.total ?? preview?.selected ?? 0;
  return <section className="oem-bulk" aria-label="Aprobación en lote">
    <div className="oem-bulk-heading"><h3>Aprobar en lote</h3>{!running && <button className="icon-button" aria-label="Cerrar aprobación en lote" onClick={onClose}><X size={16}/></button>}</div>
    {error && <div className="notice error" role="alert">{error}</div>}
    {!preview && !error && <div className="loading"><LoaderCircle className="spin" size={18}/>Calculando la vista previa…</div>}
    {preview && !progress && <>
      <p><strong>Se aprobarán {number(preview.selected)} de {number(preview.count)} casos por revisar</strong> con estos filtros: con existencias primero, hasta {number(preview.limit)} por ejecución, en lotes de {number(preview.batch_size)}, comparando el fingerprint de cada caso. {number(preview.methods.flag ?? 0)} se marcarán como OEM y {number(preview.methods.rename ?? 0)} se renombrarán (el SKU anterior queda como alterno).</p>
      {!!Object.keys(preview.tiers).length && <p className="oem-chips">{Object.entries(preview.tiers).map(([tier, n]) => <span key={tier}>{reviewTierLabels[tier as keyof typeof reviewTierLabels] || tier} · {number(n)}</span>)}</p>}
      {!!Object.keys(preview.excluded).length && <ul className="oem-reasons">{Object.entries(preview.excluded).map(([key, n]) => <li key={key}>{number(n)} {excludedLabels[key] || key}.</li>)}</ul>}
      {preview.halt && <div className="notice error" role="alert"><OctagonPause size={16}/>LA APLICACIÓN OEM ESTÁ DETENIDA: {preview.halt.reason}. Levántala en Inventario → Aplicación OEM antes de aprobar en lote.</div>}
      {preview.running && <div className="notice" role="status">La aprobación en lote #{preview.running} no ha terminado.<button className="button soft small" onClick={() => void resume(preview.running!)}>Continuar #{preview.running}</button></div>}
      {!!preview.sample.length && <div className="suffix-table-wrap"><table className="suffix-table"><caption className="sr-only">Muestra de la vista previa</caption>
        <thead><tr><th>SKU</th><th>OEM</th><th>Cambio</th><th>Hoy</th></tr></thead>
        <tbody>{preview.sample.map(row => <tr key={row.case}><td data-label="SKU"><strong>{row.sku}</strong><span>{row.description}</span></td><td data-label="OEM">{row.code} · {row.brand}<span>{reviewTierLabels[row.tier as keyof typeof reviewTierLabels] || row.tier}</span></td><td data-label="Cambio">{row.method === 'flag' ? 'MARCAR COMO OEM' : `RENOMBRAR ${row.sku} → ${row.code}`}</td><td data-label="Hoy"><span className={`status ${row.outcome === 'applied' ? 'matched' : 'review'}`}>{row.outcome === 'applied' ? 'SE APLICARÍA' : row.outcome === 'conflict' ? 'PASARÍA A CONFLICTO' : 'SE OMITIRÍA'}</span>{row.detail && <span>{row.detail}</span>}</td></tr>)}</tbody>
      </table></div>}
      {preview.selected > preview.sample.length && <p className="form-footnote">La muestra muestra los primeros {number(preview.sample.length)} casos; cada caso se vuelve a comprobar al aplicarse.</p>}
      <div className="suffix-actions"><button className="button primary small" disabled={running || !preview.selected || !!preview.halt || !!preview.running} onClick={() => void start()}>{running && <LoaderCircle size={14} className="spin"/>}Aprobar {number(preview.selected)} {preview.selected === 1 ? 'caso' : 'casos'}</button><button className="button text small" disabled={running} onClick={onClose}>Cancelar</button></div>
    </>}
    {progress && s && <>
      <div className="oem-progress" role="progressbar" aria-label="Progreso de la aprobación en lote" aria-valuemin={0} aria-valuemax={totalRows} aria-valuenow={done}><span style={{ width: `${totalRows ? Math.round(done / totalRows * 100) : 100}%` }}/></div>
      <p role="status">{progress.finished ? 'TERMINADA' : progress.waiting ? 'EN PAUSA' : `APLICANDO LOTE ${number((s.batches?.length ?? 0) + 1)}`} · {number(done)} / {number(totalRows)} · {number(s.applied ?? 0)} APROBADOS{s.conflicts ? ` · ${number(s.conflicts)} A CONFLICTO` : ''}{Object.entries(s.skipped ?? {}).map(([reason, n]) => ` · ${number(n)} OMITIDOS: ${reviewSkipLabels[reason] || reason}`).join('')}{s.errors ? ` · ${number(s.errors)} ERRORES` : ''} · EJECUCIÓN #{progress.run.id}</p>
      <div className="suffix-actions">{running ? <button className="button soft small" onClick={() => { stop.current = true; }}><OctagonPause size={14}/>Detener después de este lote</button> : <><button className="button soft small" onClick={() => onShowRun(progress.run.id)}><History size={14}/>Ver ejecución #{progress.run.id}</button><button className="button text small" onClick={onClose}>Cerrar</button></>}</div>
    </>}
  </section>;
}

function scopeText(run: OEMRun) {
  const scope = run.scope;
  if (scope.action === 'approve_batch') {
    const filters = Object.entries(scope.filters ?? {}).map(([key, value]) => key === 'tier' ? reviewTierLabels[value as keyof typeof reviewTierLabels] || value : key === 'blocker' ? blockerLabel(value) : key === 'in_stock' ? (value === 'true' ? 'CON EXISTENCIAS' : 'SIN EXISTENCIAS') : `${key.toUpperCase()} ${value}`);
    return `APROBACIÓN EN LOTE${filters.length ? ` · ${filters.join(' · ')}` : ' · TODOS LOS CASOS APROBABLES'}`;
  }
  if (scope.action) return `${scope.action === 'choose_oem' ? 'ELECCIÓN DE OEM' : 'APROBACIÓN INDIVIDUAL'} · ${scope.sku}`;
  return scope.canary ? `AUTOMÁTICA · CANARIO ${number(scope.canary)}` : 'AUTOMÁTICA';
}

function Runs({ focus, onChanged }: { focus: number | null; onChanged: () => void }) {
  const [stage, setStage] = useState('review'); const [page, setPage] = useState(1); const [data, setData] = useState<OEMRunPage | null>(null);
  const [error, setError] = useState(''); const [notice, setNotice] = useState(''); const [busy, setBusy] = useState(''); const [revision, setRevision] = useState(0);
  const [confirming, setConfirming] = useState<number | null>(null); const [resuming, setResuming] = useState<number | null>(null);
  useEffect(() => {
    let live = true;
    request<OEMRunPage>(`${runsBase}?${new URLSearchParams({ mode: 'apply_auto', page: String(page), ...(stage ? { stage } : {}) })}`).then(result => { if (live) { setData(result); setError(''); } }).catch(e => { if (live) { setData(null); setError(errorMessage(e)); } });
    return () => { live = false; };
  }, [stage, page, revision]);
  async function revert(run: OEMRun) {
    setBusy(`revert:${run.id}`); setError(''); setNotice('');
    try {
      const result = await request<OEMRevertResult>(`${runsBase}/${run.id}/revert`, { method: 'POST', body: JSON.stringify({}) });
      setNotice(`Ejecución #${run.id}: ${number(result.reverted)} ${result.reverted === 1 ? 'cambio deshecho' : 'cambios deshechos'}${result.already_reverted ? `, ${number(result.already_reverted)} ya estaban deshechos` : ''}${result.skipped.length ? `, ${number(result.skipped.length)} sin deshacer (${result.skipped.slice(0, 3).map(row => `${row.sku}: ${row.label}`).join('; ')})` : ''}.${run.scope.stage === 'review' && result.reverted ? ' Sus casos vuelven a la revisión.' : ''}`);
      setConfirming(null); setRevision(v => v + 1); if (result.reverted) onChanged();
    } catch (e) { setError(errorMessage(e)); }
    finally { setBusy(''); }
  }
  async function spot(run: OEMRun) {
    setBusy(`spot:${run.id}`); setError('');
    try { const result = await request<OEMSpotCheck>(`${runsBase}/${run.id}/spot-check`); download(`oem-run-${run.id}-spot-check.csv`, '﻿' + result.csv, 'text/csv;charset=utf-8'); }
    catch (e) { setError(errorMessage(e)); }
    finally { setBusy(''); }
  }
  async function resume(run: OEMRun) {
    setResuming(run.id); setError(''); setNotice('');
    try {
      let current = await request<OEMBulkProgress>(`${base}/bulk/${run.id}`, { method: 'POST', body: JSON.stringify({ action: 'continue' }) });
      while (!current.finished && !current.waiting) current = await request<OEMBulkProgress>(`${base}/bulk/${run.id}`, { method: 'POST', body: JSON.stringify({ action: 'continue' }) });
      setNotice(current.waiting ? 'Hay una importación del catálogo en curso; continúa cuando termine.' : `Aprobación en lote #${run.id} terminada: ${number(current.run.applied.applied ?? 0)} aprobados.`);
      onChanged();
    } catch (e) { setError(errorMessage(e)); }
    finally { setResuming(null); setRevision(v => v + 1); }
  }
  return <div className="oem-runs">
    <p className="form-footnote">Cada aprobación (individual o en lote) es una ejecución con su auditoría: descarga la muestra de control para verificarla y deshazla completa si algo salió mal. Deshacer devuelve el SKU anterior si sigue libre, borra el OEM y los alternos que creó y devuelve los casos a la revisión.</p>
    <div className="suffix-filters"><label>TIPO<select aria-label="Tipo de ejecución" value={stage} onChange={e => { setStage(e.target.value); setPage(1); }}><option value="review">REVISIÓN OEM</option><option value="auto">AUTOMÁTICAS</option><option value="">TODAS</option></select></label></div>
    {data?.halt && <div className="notice error" role="alert"><OctagonPause size={16}/>APLICACIÓN DETENIDA: {data.halt.reason}. Las aprobaciones en lote no empiezan ni continúan hasta levantarla (Inventario → Aplicación OEM).</div>}
    {error && <div className="notice error" role="alert">{error}</div>}
    {notice && <div className="notice success" role="status"><Check size={16}/>{notice}</div>}
    {!data && !error && <div className="loading"><LoaderCircle className="spin" size={20}/>Cargando ejecuciones…</div>}
    {data && !data.results.length && <div className="empty-state"><Layers3 size={30}/><h3>Aún no hay ejecuciones.</h3><p>Aparecen al aprobar casos de la revisión OEM.</p></div>}
    {!!data?.results.length && <div className="suffix-table-wrap"><table className="suffix-table">
      <thead><tr><th>Ejecución</th><th>Alcance</th><th>Resultado</th><th>Deshechos</th><th>Acciones</th></tr></thead>
      <tbody>{data.results.map(run => { const s = run.applied; const batch = run.scope.action === 'approve_batch'; return <tr key={run.id} className={focus === run.id ? 'oem-run-focus' : ''}>
        <td data-label="Ejecución"><strong>#{run.id}</strong><span>{stamp(run.started_at)}</span><span>{run.status === 'failed' ? 'FALLIDA' : run.status === 'running' ? 'EN CURSO' : 'COMPLETADA'} · {run.actor || 'SISTEMA'}</span></td>
        <td data-label="Alcance">{scopeText(run)}{batch && <span>{number(s.done ?? 0)} / {number(s.total ?? s.selected ?? 0)} PROCESADOS · {number(s.batches?.length ?? 0)} {s.batches?.length === 1 ? 'LOTE' : 'LOTES'}</span>}</td>
        <td data-label="Resultado"><strong>{number(s.applied ?? 0)} APLICADOS</strong>{!!s.conflicts && <span>{number(s.conflicts)} A CONFLICTO</span>}{Object.entries(s.skipped ?? {}).map(([reason, n]) => <span key={reason}>{number(n)} OMITIDOS: {reviewSkipLabels[reason] || reason}</span>)}{!!s.errors && <span>{number(s.errors)} ERRORES</span>}{s.stopped && <span>{stoppedLabels[s.stopped]}</span>}</td>
        <td data-label="Deshechos" className="number-cell">{number(run.reverted)} / {number(run.changes)}</td>
        <td data-label="Acciones"><div className="suffix-actions">
          {run.status === 'running' && batch && <button className="button primary small" aria-label={`Continuar la ejecución ${run.id}`} disabled={!!busy || resuming !== null} onClick={() => void resume(run)}>{resuming === run.id && <LoaderCircle size={14} className="spin"/>}Continuar</button>}
          <button className="button soft small" aria-label={`Descargar la muestra de la ejecución ${run.id}`} disabled={!!busy || !s.spot_check?.length} onClick={() => void spot(run)}>{busy === `spot:${run.id}` ? <LoaderCircle size={14} className="spin"/> : <Download size={14}/>}Muestra de control</button>
          {confirming === run.id ? <><button className="button primary small" aria-label={`Confirmar deshacer la ejecución ${run.id}`} disabled={!!busy} onClick={() => void revert(run)}>{busy === `revert:${run.id}` && <LoaderCircle size={14} className="spin"/>}Deshacer {number(run.changes - run.reverted)} {run.changes - run.reverted === 1 ? 'cambio' : 'cambios'}</button><button className="button text small" onClick={() => setConfirming(null)}>Cancelar</button></>
            : <button className="button text small" aria-label={`Deshacer la ejecución ${run.id}`} disabled={!!busy || run.changes === run.reverted || run.status === 'running'} onClick={() => setConfirming(run.id)}><Undo2 size={14}/>Deshacer ejecución</button>}
        </div></td>
      </tr>; })}</tbody>
    </table></div>}
    {data && <div className="pagination"><span>{number(data.count)} {data.count === 1 ? 'ejecución' : 'ejecuciones'}</span><div><button className="button soft small" disabled={!data.previous} onClick={() => setPage(page - 1)}>Anterior</button><span>Página {page}</span><button className="button soft small" disabled={!data.next} onClick={() => setPage(page + 1)}>Siguiente</button></div></div>}
  </div>;
}
