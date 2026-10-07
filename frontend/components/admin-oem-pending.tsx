'use client';

import { useEffect, useRef, useState } from 'react';
import { Check, Eye, History, ListChecks, LoaderCircle, Play, RefreshCw, Search, Send, ShieldCheck, X } from 'lucide-react';
import { UppercaseInput } from './uppercase-field';
import { ApiError, request } from '@/lib/types';
import { lexiconLabel, unitLabels } from '@/lib/oem-review-types';
import { OEMApplyTier, OEMAutoTier, OEMPendingApplyResult, OEMPendingFilters, OEMPendingMode, OEMPendingPage, OEMPendingPreview, OEMPendingRow, OEMRun,
  OEMSendToReviewResult, applyTiersOf, autoTierLabels, pendingExcludedLabels, skipLabels, stoppedLabels, tierLabels } from '@/lib/oem-finder-types';

const base = '/api/management/oem-finder/pending';
const number = (value: number) => value.toLocaleString('es-PA');
const stamp = (value: string) => new Date(value).toLocaleString('es-PA', { timeZone: 'America/Panama' });
const errorMessage = (error: unknown) => error instanceof Error ? error.message : 'No se pudo completar la acción. Inténtalo de nuevo.';
const empty: OEMPendingFilters = { chain: '', make: '', in_stock: '', search: '' };
const tabs: OEMAutoTier[] = ['AUTO_FLAG_CURRENT', 'AUTO_RENAME_BASE'];
const total = (values: Record<string, number> = {}) => Object.values(values).reduce((sum, n) => sum + n, 0);
const tokenClass = { TAG: 'ETIQUETA', VARIANT: 'VARIANTE', UNKNOWN: 'DESCONOCIDO' };
// One id per confirmed apply: a retry after a lost response returns the same run instead of applying twice.
const operationId = () => typeof crypto !== 'undefined' && 'randomUUID' in crypto ? crypto.randomUUID()
  : 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => { const r = Math.random() * 16 | 0; return (c === 'x' ? r : (r & 0x3) | 0x8).toString(16); });

type Props = { revision: number; onApplied: (run: OEMRun) => void; onShowRun: (run: number) => void };
type Confirm = { tier: OEMApplyTier; mode: OEMPendingMode; operation: string; count: number; pending: number; uncertain?: boolean };

function runNotice(run: OEMRun, replayed: boolean) {
  const again = replayed ? ' Esta operación ya se había ejecutado: no se aplicó nada nuevo.' : '';
  if (run.status !== 'completed') return `Ejecución #${run.id} ${run.status === 'failed' ? 'falló' : 'quedó sin terminar (se interrumpió)'}: ${number(run.changes)} ${run.changes === 1 ? 'cambio escrito' : 'cambios escritos'}. Revísala en el historial antes de continuar; allí puedes deshacerla.${again}`;
  const s = run.applied, skipped = total(s.skipped);
  const parts = [`${number(s.applied ?? 0)} ${s.applied === 1 ? 'aplicado' : 'aplicados'}`];
  if (s.conflicts) parts.push(`${number(s.conflicts)} a revisión por conflicto`);
  if (skipped) parts.push(`${number(skipped)} ${skipped === 1 ? 'omitido' : 'omitidos'} (${Object.entries(s.skipped ?? {}).map(([reason, n]) => `${n} ${(skipLabels[reason] || reason).toLowerCase()}`).join(', ')})`);
  if (s.errors) parts.push(`${number(s.errors)} errores`);
  return `Ejecución #${run.id}${run.scope.canary ? ` (canario de ${number(run.scope.canary)})` : ''}: ${parts.join(' · ')}.${s.stopped ? ` ${stoppedLabels[s.stopped]}.` : ''}`
    + (again || ' Revisa la muestra de control y, si hace falta, deshaz la ejecución en el historial.');
}

export default function OEMPending({ revision, onApplied, onShowRun }: Props) {
  const [tier, setTier] = useState<OEMAutoTier>('AUTO_FLAG_CURRENT');
  const [filters, setFilters] = useState<OEMPendingFilters>(empty); const [search, setSearch] = useState(''); const [page, setPage] = useState(1);
  const [data, setData] = useState<OEMPendingPage | null>(null); const [loadError, setLoadError] = useState(''); const [error, setError] = useState(''); const [notice, setNotice] = useState<{ text: string; run?: number } | null>(null);
  const [busy, setBusy] = useState(''); const [local, setLocal] = useState(0); const [selected, setSelected] = useState<string[]>([]);
  const [preview, setPreview] = useState<OEMPendingPreview | null>(null); const [confirm, setConfirm] = useState<Confirm | null>(null);
  const requestId = useRef(0); const cancelButton = useRef<HTMLButtonElement>(null); const confirmOperation = confirm?.operation;
  useEffect(() => { if (confirmOperation) cancelButton.current?.focus(); }, [confirmOperation]);  // keyboard users land in the confirmation, on its safe action
  useEffect(() => { const timer = setTimeout(() => { setFilters(current => current.search === search.trim() ? current : { ...current, search: search.trim() }); setPage(1); }, 300); return () => clearTimeout(timer); }, [search]);
  useEffect(() => {
    const id = ++requestId.current;
    const params = new URLSearchParams({ status: 'pending', tier, page: String(page) });
    Object.entries(filters).forEach(([key, value]) => { if (value) params.set(key, value); });
    request<OEMPendingPage>(`${base}?${params}`).then(result => { if (id === requestId.current) { setData(result); setLoadError(''); } })
      .catch(e => {
        if (id !== requestId.current) return;
        if (e instanceof ApiError && e.status === 404 && page > 1) { setPage(1); return; }  // the rows of this page were applied or sent to review
        setData(null); setLoadError(errorMessage(e));
      });
  }, [tier, filters, page, revision, local]);
  const refresh = () => setLocal(v => v + 1);
  function filter(patch: Partial<OEMPendingFilters>) { setFilters(current => ({ ...current, ...patch })); setPage(1); setSelected([]); }
  function chooseTab(next: OEMAutoTier) { setTier(next); setFilters(empty); setSearch(''); setPage(1); setSelected([]); setPreview(null); setConfirm(null); setError(''); setNotice(null); }
  function blocked(t: OEMApplyTier) {
    if (!data) return 'Cargando…';
    if (data.halt) return `Aplicación detenida: ${data.halt.reason}. Levanta la detención (arriba) para continuar.`;
    if (data.busy.lock) return 'Hay un análisis de coincidencias o una búsqueda OEM en curso; actualiza cuando termine.';
    if (!data.apply[t].pending) return 'No hay SKU pendientes en este nivel.';
    return '';
  }
  function batchBlocked(t: OEMApplyTier) {
    return blocked(t) || (data && !data.apply[t].canary_done ? `Aplica primero un canario de ${data.sizes.canary}${rules ? ` con las reglas ${rules}` : ''}: los lotes de ${data.sizes.batch} se habilitan cuando termina.` : '');
  }
  async function runPreview(t: OEMApplyTier) {
    if (!data) return;
    const mode: OEMPendingMode = data.apply[t].canary_done ? 'batch' : 'canary';
    setBusy(`preview:${t}`); setError(''); setNotice(null); setConfirm(null); setPreview(null);
    try { setPreview(await request<OEMPendingPreview>(`${base}/preview`, { method: 'POST', body: JSON.stringify({ tier: t, mode }) })); }
    catch (e) { setError(errorMessage(e)); }
    finally { setBusy(''); }
  }
  async function apply() {
    if (!confirm) return;
    setBusy(`apply:${confirm.tier}`); setError(''); setNotice(null);
    try {
      const result = await request<OEMPendingApplyResult>(`${base}/apply`, { method: 'POST', body: JSON.stringify({ tier: confirm.tier, mode: confirm.mode, operation_id: confirm.operation }) });
      setConfirm(null); setPreview(null); setSelected([]);
      setNotice({ text: runNotice(result.run, result.replayed), run: result.run.id });
      onApplied(result.run);
    } catch (e) {
      // A refusal (409 with a reason) or a failed run (500) is final. Busy (409 without one: a matching pass, another run or an import, maybe
      // this operation's own lost first request) and a lost response keep the same operation id, so retrying never applies twice.
      const reason = e instanceof ApiError ? (e.body as { reason?: string } | null)?.reason : undefined;
      if (e instanceof ApiError && e.status === 409 && !reason) setError(`${errorMessage(e)} La confirmación sigue abierta con la misma operación: vuelve a pulsar «Aplicar» cuando termine.`);
      else if (e instanceof ApiError && e.status < 502) { setConfirm(null); setError(errorMessage(e)); }
      else { setConfirm(current => current && { ...current, uncertain: true }); setError('No se pudo confirmar el resultado. Vuelve a pulsar «Aplicar»: si la ejecución ya terminó, se mostrará sin aplicar nada dos veces.'); }
    } finally { setBusy(''); refresh(); }
  }
  async function send(ids: string[]) {
    setBusy('send'); setError(''); setNotice(null);
    try {
      const result = await request<OEMSendToReviewResult>(`${base}/send-to-review`, { method: 'POST', body: JSON.stringify({ parts: ids }) });
      const parts = [`${number(result.sent.length)} ${result.sent.length === 1 ? 'SKU enviado' : 'SKU enviados'} a la Revisión OEM: la aplicación automática ya no los tomará`];
      if (result.already.length) parts.push(`${number(result.already.length)} ya estaban en revisión`);
      if (result.skipped.length) parts.push(`${number(result.skipped.length)} sin enviar (${result.skipped.slice(0, 3).map(row => `${row.sku || 'SKU'}: ${row.label}`).join('; ')})`);
      setNotice({ text: parts.join(' · ') + '.' });
      setSelected(current => current.filter(id => !ids.includes(id))); setPreview(null);
    } catch (e) { setError(errorMessage(e)); }
    finally { setBusy(''); refresh(); }
  }
  const rows = data?.results ?? [];
  const pageIds = rows.map(row => row.part_id); const allPicked = !!pageIds.length && pageIds.every(id => selected.includes(id));
  const sizeOf = (mode: OEMPendingMode) => data?.sizes[mode] ?? (mode === 'canary' ? 50 : 100);
  const narrowed = !!(filters.chain || filters.make || filters.in_stock || filters.search || selected.length);
  const rules = data?.rules_version ?? '', refreshedWith = data?.last_refresh?.rules_version ?? '';
  const olderRules = rules && refreshedWith && refreshedWith !== rules ? refreshedWith : '';
  function openConfirm(t: OEMApplyTier, mode: OEMPendingMode) {  // the count is frozen: a retry after a lost response states the same one
    if (!data) return;
    const pending = data.apply[t].pending;
    setNotice(null); setError('');
    setConfirm({ tier: t, mode, operation: operationId(), pending, count: preview && preview.tier === t && preview.mode === mode ? preview.selected : Math.min(sizeOf(mode), pending) });
  }
  return <section className="oem-pending" aria-labelledby="oem-pending-title">
    <div className="oem-pending-heading"><h3 id="oem-pending-title">Pendientes de aplicación automática</h3>
      {data?.last_refresh && <span>ACTUALIZADO POR LA EJECUCIÓN #{data.last_refresh.id} ({data.last_refresh.mode === 'dry_run' ? 'ANÁLISIS' : 'APLICACIÓN'}{refreshedWith && `, REGLAS ${refreshedWith.toUpperCase()}`}) · {stamp(data.last_refresh.finished_at)}</span>}
      <button className="button text small" aria-label="Actualizar pendientes" disabled={!!busy} onClick={refresh}><RefreshCw size={14}/>Actualizar</button></div>
    <p className="form-footnote">SKU que el buscador OEM aplicaría solo, con existencias primero. Cada nivel empieza con un canario de {sizeOf('canary')} y después aplica lotes de hasta {sizeOf('batch')}, cada SKU en su propia transacción, con muestra de control y deshacer en el historial. El canario vale para la versión de las reglas del buscador con que se aplicó{rules && ` (ahora ${rules})`}: cuando las reglas cambian, cada nivel vuelve a empezar con un canario. Si una fila no te convence, envíala a la Revisión OEM: la aplicación automática ya no la tomará.</p>
    {olderRules && <p className="oem-apply-reason">Estos pendientes se calcularon con las reglas {olderRules}. El próximo análisis OEM (python -m mall.oem_finder --dry-run) o la próxima aplicación los recalcula con {rules}, sin cambiar los enviados a revisión, los aplicados ni los excluidos.</p>}
    {data && !data.last_refresh && !total(data.statuses) && <p className="oem-review-run">AÚN NO HAY PENDIENTES CALCULADOS. Se llenan con el próximo análisis OEM (python -m mall.oem_finder --dry-run) o la próxima aplicación.</p>}
    <div className="admin-inventory-tabs oem-tier-tabs" role="group" aria-label="Nivel automático">
      {tabs.map(t => <button key={t} className={tier === t ? 'selected' : ''} aria-pressed={tier === t} onClick={() => chooseTab(t)}>{autoTierLabels[t]} ({number(data?.tiers[t] ?? 0)})</button>)}
    </div>
    {data?.busy.running && !data.busy.lock && <p className="oem-apply-reason">La ejecución #{data.busy.running} figura sin terminar (se interrumpió): lo que aplicó sigue en el historial, con su muestra de control y deshacer.</p>}
    {data && <div className="oem-apply-tiers">{applyTiersOf[tier].filter(t => tier === 'AUTO_FLAG_CURRENT' || data.apply[t].pending || data.apply[t].canary_done).map(t => {
      const state = data.apply[t], reason = blocked(t), batchReason = batchBlocked(t), hint = reason || batchReason;
      return <div key={t} className="oem-apply-tier" role="group" aria-label={`Aplicación de ${tierLabels[t]}`}>
        <div className="oem-apply-tier-text"><strong>{tierLabels[t]}</strong><span>{number(state.pending)} PENDIENTES · {number(state.in_stock)} CON EXISTENCIAS · <span className={`status ${state.canary_done ? 'matched' : 'review'}`}>{state.canary_done ? 'CANARIO HECHO: LOTES HABILITADOS' : `FALTA EL CANARIO${rules && ` DE ${rules.toUpperCase()}`}`}</span></span></div>
        <div className="suffix-actions">
          <button className="button soft small" aria-label={`Vista previa de ${tierLabels[t]}`} disabled={!!busy || !state.pending} onClick={() => void runPreview(t)}>{busy === `preview:${t}` ? <LoaderCircle size={14} className="spin"/> : <Eye size={14}/>}Vista previa</button>
          {!state.canary_done && <button className="button primary small" aria-label={`Aplicar canario (${sizeOf('canary')}) de ${tierLabels[t]}`} aria-describedby={reason ? `oem-reason-${t}` : undefined} disabled={!!busy || !!reason} onClick={() => openConfirm(t, 'canary')}><Play size={14}/>Aplicar canario ({sizeOf('canary')})</button>}
          <button className={`button ${state.canary_done ? 'primary' : 'soft'} small`} aria-label={`Aplicar lote (${sizeOf('batch')}) de ${tierLabels[t]}`} aria-describedby={batchReason ? `oem-reason-${t}` : undefined} disabled={!!busy || !!batchReason} onClick={() => openConfirm(t, 'batch')}><ListChecks size={14}/>Aplicar lote ({sizeOf('batch')})</button>
        </div>
        {hint && <p className="oem-apply-reason" id={`oem-reason-${t}`}>{hint}{data.busy.lock && !data.halt && <button className="button text small" onClick={refresh}>Comprobar de nuevo</button>}</p>}
      </div>; })}
    </div>}
    {confirm && <div className="oem-bulk oem-confirm" role="alertdialog" aria-modal="false" aria-labelledby="oem-confirm-title" aria-describedby="oem-confirm-text">
      <div className="oem-bulk-heading"><h3 id="oem-confirm-title">{confirm.mode === 'canary' ? 'Aplicar canario' : 'Aplicar lote'} · {tierLabels[confirm.tier]}</h3>{busy !== `apply:${confirm.tier}` && <button className="icon-button" aria-label="Cerrar confirmación" onClick={() => setConfirm(null)}><X size={16}/></button>}</div>
      <p id="oem-confirm-text"><strong>Se aplicarán hasta {number(confirm.count)} SKU</strong> de {number(confirm.pending)} pendientes de este nivel, con existencias primero y cada uno en su propia transacción. {confirm.tier === 'AUTO_FLAG_CURRENT' ? 'Cada SKU se marca como OEM; su código no cambia.' : 'Cada SKU se renombra a su base OEM con el mismo SKU interno, existencias, fotos y alternos; el SKU anterior queda como alterno.'} Al terminar podrás revisar la muestra de control y deshacer la ejecución completa o SKU por SKU.</p>
      {narrowed && <p className="form-footnote">Los filtros y la selección solo afectan la lista: la aplicación toma los primeros SKU pendientes del nivel completo, con existencias primero.</p>}
      {confirm.uncertain && <p className="form-footnote">Se reintentará la misma operación: si ya terminó, no se aplicará nada dos veces.</p>}
      {busy === `apply:${confirm.tier}` && <div className="loading" role="status"><LoaderCircle className="spin" size={18}/>Aplicando: se evalúa el catálogo y se aplican hasta {number(confirm.count)} SKU. Puede tardar hasta un minuto; no cierres la ventana.</div>}
      <div className="suffix-actions"><button className="button primary small" disabled={!!busy} onClick={() => void apply()}>{busy === `apply:${confirm.tier}` ? <LoaderCircle size={14} className="spin"/> : <ShieldCheck size={14}/>}Aplicar {number(confirm.count)} SKU</button><button ref={cancelButton} className="button text small" disabled={!!busy} onClick={() => setConfirm(null)}>Cancelar</button></div>
    </div>}
    {preview && <Preview preview={preview} onClose={() => setPreview(null)}/>}
    {(error || loadError) && <div className="notice error" role="alert">{error || loadError}{loadError && !data && <button onClick={refresh}>Intentar de nuevo</button>}</div>}
    {notice && <div className="notice success" role="status"><Check size={16}/><span>{notice.text}</span>{notice.run && <button className="button soft small" onClick={() => onShowRun(notice.run!)}><History size={14}/>Ver ejecución #{notice.run} en el historial</button>}</div>}
    <div className="suffix-filters oem-review-filters">
      <label className="suffix-search"><Search size={16}/><span className="sr-only">Buscar pendiente</span><UppercaseInput value={search} onChange={e => setSearch(e.target.value)} placeholder="SKU, DESCRIPCIÓN U OEM" aria-label="Buscar pendiente"/></label>
      {tier === 'AUTO_RENAME_BASE' && <Facet label="SUFIJO" aria="Sufijo" value={filters.chain} options={data?.facets.chains} format={value => `-${value}`} onChange={chain => filter({ chain })}/>}
      <Facet label="MARCA" aria="Marca" value={filters.make} options={data?.facets.makes} onChange={make => filter({ make })}/>
      <label>EXISTENCIAS<select aria-label="Existencias" value={filters.in_stock} onChange={e => filter({ in_stock: e.target.value })}><option value="">TODAS</option><option value="true">CON EXISTENCIAS</option><option value="false">SIN EXISTENCIAS</option></select></label>
      {(filters.chain || filters.make || filters.in_stock || filters.search) && <button className="button text small" onClick={() => { setSearch(''); filter(empty); }}><X size={14}/>Quitar filtros</button>}
    </div>
    {!!selected.length && <div className="oem-bulk-bar" role="group" aria-label="Seleccionados">
      <span>{number(selected.length)} {selected.length === 1 ? 'SKU seleccionado' : 'SKU seleccionados'}</span>
      <div className="suffix-actions"><button className="button soft small" disabled={!!busy} onClick={() => void send(selected)}>{busy === 'send' ? <LoaderCircle size={14} className="spin"/> : <Send size={14}/>}Enviar seleccionados a revisión</button><button className="button text small" onClick={() => setSelected([])}>Quitar selección</button></div>
    </div>}
    {!data && !loadError && <div className="loading"><LoaderCircle className="spin" size={20}/>Cargando pendientes…</div>}
    {data && !rows.length && <div className="empty-state"><Check size={30}/><h3>No hay SKU pendientes con estos filtros.</h3><p>{total(data.statuses) ? 'Prueba con otra pestaña o quita los filtros.' : 'El próximo análisis OEM calculará los pendientes.'}</p></div>}
    {!!rows.length && <div className="suffix-table-wrap"><table className="suffix-table oem-pending-table"><caption className="sr-only">Pendientes de {autoTierLabels[tier]}</caption>
      <thead><tr><th><input type="checkbox" aria-label="Seleccionar todos los de esta página" checked={allPicked} onChange={() => setSelected(current => allPicked ? current.filter(id => !pageIds.includes(id)) : [...new Set([...current, ...pageIds])])}/></th><th>SKU actual → OEM propuesto</th><th>Marca</th><th>Cadena de sufijos</th><th>Evidencia</th><th>Existencias</th><th>Acciones</th></tr></thead>
      <tbody>{rows.map(row => <PendingRow key={row.id} row={row} busy={busy} picked={selected.includes(row.part_id)} onPick={() => setSelected(current => current.includes(row.part_id) ? current.filter(id => id !== row.part_id) : [...current, row.part_id])} onSend={() => void send([row.part_id])}/>)}</tbody>
    </table></div>}
    {data && <div className="pagination"><span>{number(data.count)} {data.count === 1 ? 'pendiente' : 'pendientes'}</span><div><button className="button soft small" aria-label="Página anterior de pendientes" disabled={!data.previous} onClick={() => setPage(page - 1)}>Anterior</button><span>Página {page}</span><button className="button soft small" aria-label="Página siguiente de pendientes" disabled={!data.next} onClick={() => setPage(page + 1)}>Siguiente</button></div></div>}
  </section>;
}

function Facet({ label, aria, value, options, format = text => text, onChange }: { label: string; aria: string; value: string; options?: [string, number][]; format?: (value: string) => string; onChange: (value: string) => void }) {
  const list = options ?? []; const present = !value || list.some(([key]) => key === value);
  return <label>{label}<select aria-label={aria} value={value} onChange={e => onChange(e.target.value)}><option value="">TODAS</option>{!present && <option value={value}>{format(value)} (0)</option>}{list.map(([key, n]) => <option key={key} value={key}>{format(key)} ({number(n)})</option>)}</select></label>;
}

function PendingRow({ row, busy, picked, onPick, onSend }: { row: OEMPendingRow; busy: string; picked: boolean; onPick: () => void; onSend: () => void }) {
  const lex = row.evidence.lexicon;
  return <tr className={picked ? 'oem-picked' : ''}>
    <td data-label="Seleccionar" className="oem-pick"><input type="checkbox" aria-label={`Seleccionar ${row.sku}`} checked={picked} onChange={onPick}/></td>
    <td data-label="SKU actual → OEM propuesto"><strong>{row.method === 'flag' ? row.sku : `${row.sku} → ${row.candidate}`}</strong>
      <span>{row.method === 'flag' ? 'EL SKU YA ES EL OEM: SE MARCA COMO OEM' : `${row.sku} QUEDA COMO ALTERNO`}{row.written_form && row.written_form !== row.candidate ? ` · ESCRITO ${row.written_form}` : ''}</span>
      <span>{row.description || 'SIN DESCRIPCIÓN'}</span>{row.stale && <span className="danger-text">{row.stale_label.toUpperCase()}: SE OMITIRÁ AL APLICAR</span>}</td>
    <td data-label="Marca">{row.brand || '—'}<span>{row.system}{row.grade && ` · ${row.grade}`}</span></td>
    <td data-label="Cadena de sufijos">{row.chain_tokens.length ? <div className="oem-chain">{row.chain_tokens.map((token, index) => <span key={index} className={`suffix-class ${token.class.toLowerCase()}`} title={`${token.token} · ${token.kind || 'sin tipo'} · ${token.status}`}>{token.sep}{token.tok} · {tokenClass[token.class] || token.class}{row.owner_tags.includes(token.token) ? ' · CONFIRMADA' : token.auto_eligible ? ' · AUTOMÁTICA' : ''}</span>)}</div> : <span>SIN SUFIJO</span>}</td>
    <td data-label="Evidencia">LÉXICO {lexiconLabel(lex.result)}{lex.n_other_keys ? ` · ${number(lex.n_other_keys)} CLAVES` : ''}{lex.share != null && lex.n_other_keys ? ` · ${Math.round(lex.share * 100)} %` : ''}
      {!!lex.top_heads.length && <span>{lex.top_heads.map(([head, n]) => `${head.replace('|', ' ')} (${n})`).join(' · ')}</span>}
      <span>{row.evidence.units.map(unit => unitLabels[unit] || unit.toUpperCase()).join(' · ') || 'SIN UNIDADES'}{row.evidence.n_suppliers ? ` · ${number(row.evidence.n_suppliers)} ${row.evidence.n_suppliers === 1 ? 'PROVEEDOR' : 'PROVEEDORES'}` : ''}{row.evidence.verified_oem_alterno ? ' · ALTERNO OEM VERIFICADO' : ''}</span></td>
    <td data-label="Existencias" className="number-cell">{row.in_stock ? <strong>{number(row.available_quantity)}</strong> : <span>SIN EXISTENCIAS</span>}</td>
    <td data-label="Acciones"><div className="suffix-actions"><button className="button text small" aria-label={`Enviar ${row.sku} a revisión`} disabled={!!busy} onClick={onSend}><Send size={14}/>Enviar a revisión</button></div></td>
  </tr>;
}

function Preview({ preview, onClose }: { preview: OEMPendingPreview; onClose: () => void }) {
  const skipped = total(preview.skipped);
  return <section className="oem-bulk" aria-label="Vista previa de la aplicación">
    <div className="oem-bulk-heading"><h3>Vista previa · {tierLabels[preview.tier]} · {preview.mode === 'canary' ? `canario de ${number(preview.size)}` : `lote de ${number(preview.size)}`}</h3><button className="icon-button" aria-label="Cerrar vista previa" onClick={onClose}><X size={16}/></button></div>
    <p><strong>Se tomarían {number(preview.selected)} de {number(preview.eligible)} SKU:</strong> {number(preview.would_apply)} se aplicarían, {number(preview.conflicts)} pasarían a revisión por conflicto y {number(skipped)} se omitirían. Solo lectura: no se escribió nada.</p>
    {!!Object.keys(preview.excluded).length && <ul className="oem-reasons">{Object.entries(preview.excluded).map(([key, n]) => <li key={key}>{number(n)} excluidos: {pendingExcludedLabels[key] || key}.</li>)}</ul>}
    {!!preview.rows.length && <div className="suffix-table-wrap"><table className="suffix-table"><caption className="sr-only">Filas de la vista previa</caption>
      <thead><tr><th>SKU</th><th>OEM</th><th>Cambio</th><th>Hoy</th></tr></thead>
      <tbody>{preview.rows.map(row => <tr key={row.part_id}><td data-label="SKU"><strong>{row.sku}</strong><span>{row.description}</span><span>{row.in_stock ? `${number(row.available_quantity)} EN EXISTENCIA` : 'SIN EXISTENCIAS'}</span></td><td data-label="OEM">{row.oem}{row.brand && ` · ${row.brand}`}</td><td data-label="Cambio">{row.method === 'flag' ? 'MARCAR COMO OEM' : `RENOMBRAR ${row.sku} → ${row.oem}`}</td><td data-label="Hoy"><span className={`status ${row.outcome === 'applied' ? 'matched' : 'review'}`}>{row.outcome === 'applied' ? 'SE APLICARÍA' : row.outcome === 'conflict' ? 'PASARÍA A CONFLICTO' : 'SE OMITIRÍA'}</span>{row.detail && <span>{skipLabels[row.detail] || row.detail}</span>}</td></tr>)}</tbody>
    </table></div>}
  </section>;
}
