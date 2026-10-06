'use client';
import { useCallback, useEffect, useId, useState } from 'react';
import dynamic from 'next/dynamic';
import { Download, FileSpreadsheet, History, ListPlus, LoaderCircle, LockKeyhole, Pencil, RefreshCw, Search, Star, Tags, Upload, X } from 'lucide-react';
import Modal from './modal';
import SupplierPriceImport from './supplier-price-import';
import { UppercaseInput } from './uppercase-field';
import { Account, ApiError, Page, request } from '@/lib/types';
import type { Currency, PriceList, PriceListsEnvelope, PricingAuditEntry } from '@/lib/pricing-types';

const PriceGrid = dynamic(() => import('./supplier-price-grid'), { ssr: false, loading: () => <div className="quotation-grid-loading" role="status"><LoaderCircle size={18} className="spin"/>Cargando precios…</div> });
const date = (value: string) => new Date(value).toLocaleString('es-PA', { timeZone: 'America/Panama', dateStyle: 'medium', timeStyle: 'short' });
const tabs = [['lists', 'Listas de precios', Tags], ['history', 'Historial', History]] as const;
const failure = (error: unknown, fallback: string) => error instanceof TypeError ? 'Se perdió la conexión. Inténtalo de nuevo.' : error instanceof Error ? error.message : fallback;

// Supplier-only: the whole section reads the account's private pricing endpoints and never renders on client screens.
export default function SupplierPricing({ account }: { account: Account }) {
  const [tab, setTab] = useState<'lists' | 'history'>('lists');
  const [lists, setLists] = useState<PriceListsEnvelope | null>(null);
  const [error, setError] = useState('');
  const [editing, setEditing] = useState<PriceList | 'new' | null>(null);
  const [notice, setNotice] = useState('');
  const [importing, setImporting] = useState(false);
  const [gridReload, setGridReload] = useState(0);
  const tabId = useId();
  // Horizontal tabs: arrows move between them, Home and End jump to the first and the last.
  function tabKey(event: React.KeyboardEvent<HTMLButtonElement>) {
    const index = tabs.findIndex(([key]) => key === tab), steps: Record<string, number> = { ArrowRight: 1, ArrowLeft: -1 };
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : event.key in steps ? (index + steps[event.key] + tabs.length) % tabs.length : -1;
    if (next < 0) return;
    event.preventDefault(); setTab(tabs[next][0]); document.getElementById(`${tabId}-${tabs[next][0]}-tab`)?.focus();
  }
  const load = useCallback(async () => {
    try { setLists(await request<PriceListsEnvelope>(`/api/market/accounts/${account.id}/price-lists`)); setError(''); }
    catch (caught) { setError(failure(caught, 'No se pudieron cargar tus listas de precios.')); }
  }, [account.id]);
  useEffect(() => { void load(); }, [load]);
  const active = lists?.results.filter(list => list.active) || [];
  return <section className="supplier-pricing" aria-label="Precios del proveedor">
    <div className="supplier-pricing-private" role="note"><LockKeyhole size={17}/><p>Estos precios son privados: nunca se muestran en el catálogo, a otros clientes ni a otros proveedores. Cada cliente solo ve los precios de las cotizaciones que le envías.</p></div>
    <div className="supplier-pricing-tabs" role="tablist" aria-label="Secciones de precios">
      {tabs.map(([key, label, Icon]) => <button type="button" role="tab" key={key} id={`${tabId}-${key}-tab`} aria-controls={`${tabId}-${key}-panel`} aria-selected={tab === key}
        tabIndex={tab === key ? 0 : -1} className={tab === key ? 'selected' : ''} onClick={() => setTab(key)} onKeyDown={tabKey}><Icon size={15}/>{label}</button>)}
    </div>
    {tab === 'lists' && <div role="tabpanel" id={`${tabId}-lists-panel`} aria-labelledby={`${tabId}-lists-tab`} className="supplier-pricing-panel">
      <div className="supplier-pricing-heading"><div><h2>Listas de precios</h2><p>Precios base de tu inventario. La lista predeterminada prepara el precio sugerido de cada cotización.</p></div>
        <div className="supplier-workspace-actions">
          {lists?.can_configure && <button type="button" className="button soft small" onClick={() => setEditing('new')}><ListPlus size={15}/>Nueva lista</button>}
          {lists?.can_configure && <button type="button" className="button soft small" onClick={() => setImporting(true)}><Upload size={15}/>Importar precios</button>}
          {!!active.length && <a className="button soft small" href={`/api/market/accounts/${account.id}/prices/export`} download><Download size={15}/>Descargar precios</a>}
          <a className="button soft small" href={`/api/market/accounts/${account.id}/prices/import/template`} download><FileSpreadsheet size={15}/>Descargar plantilla</a>
        </div></div>
      {notice && <div className="notice success" role="status">{notice}</div>}
      {error && <div className="notice error" role="alert"><span>{error}</span><button type="button" onClick={() => void load()}>Intentar de nuevo</button></div>}
      {!lists && !error ? <div className="loading"><LoaderCircle className="spin"/>Cargando tus listas de precios…</div> : lists && <>
        {!lists.can_configure && <p className="supplier-pricing-readonly">Pide a un administrador de tu cuenta que cambie esta configuración.</p>}
        {!lists.results.length ? <div className="empty-state compact"><Tags size={30}/><h3>Crea tu primera lista de precios.</h3><p>Será tu lista predeterminada: sus precios se sugieren automáticamente al preparar cada cotización.</p></div>
          : <div className="table-scroll"><table className="price-list-table"><thead><tr><th>Código</th><th>Nombre</th><th>Moneda</th><th>Predeterminada</th><th>Artículos con precio</th><th>Sin precio</th><th>Actualizada</th>{lists.can_configure && <th><span className="sr-only">Acciones</span></th>}</tr></thead>
            <tbody>{lists.results.map(list => <tr key={list.id} className={list.active ? '' : 'archived'}><td><strong>{list.code}</strong>{!list.active && <small>Archivada</small>}</td><td>{list.name}</td><td>{list.currency}</td>
              <td>{list.is_default ? <span className="price-list-default"><Star size={13}/>Predeterminada</span> : '—'}</td><td className="number-cell">{list.priced_count}</td><td className="number-cell">{list.missing_count}</td><td>{date(list.updated_at)}</td>
              {lists.can_configure && <td><button type="button" className="icon-button" aria-label={`Editar lista ${list.code}`} onClick={() => setEditing(list)}><Pencil size={16}/></button></td>}</tr>)}</tbody></table></div>}
        <PriceGrid account={account} lists={lists.results} canConfigure={lists.can_configure} reload={gridReload} onSaved={() => void load()}/>
      </>}
    </div>}
    {tab === 'history' && <div role="tabpanel" id={`${tabId}-history-panel`} aria-labelledby={`${tabId}-history-tab`} className="supplier-pricing-panel"><PricingHistory account={account}/></div>}
    {importing && <SupplierPriceImport key={account.id} account={account} onClose={() => { setImporting(false); void load(); setGridReload(value => value + 1); }}
      onSaved={result => { void load(); setGridReload(value => value + 1); setNotice(`Importación de precios completada: ${result.applied_summary.created} nuevos, ${result.applied_summary.updated} actualizados y ${result.applied_summary.removed} eliminados.${result.summary.rejected_rows ? ` ${result.summary.rejected_rows} filas pendientes; abre Importar precios para descargar el archivo de correcciones.` : ''}`); }}/>}
    {editing && <PriceListForm account={account} list={editing === 'new' ? null : editing} first={!lists?.results.length}
      onClose={() => setEditing(null)} onSaved={(saved, created) => { setEditing(null); setNotice(created ? `Lista ${saved.code} creada.` : `Lista ${saved.code} actualizada.`); void load(); }}/>}
  </section>;
}

function PriceListForm({ account, list, first, onClose, onSaved }: { account: Account; list: PriceList | null; first: boolean; onClose: () => void; onSaved: (list: PriceList, created: boolean) => void }) {
  const [code, setCode] = useState(list?.code || '');
  const [name, setName] = useState(list?.name || '');
  const [currency, setCurrency] = useState<Currency>(list?.currency || 'USD');
  const [isDefault, setIsDefault] = useState(list ? list.is_default : first);
  const [active, setActive] = useState(list ? list.active : true);
  const [version, setVersion] = useState(list?.version || 0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); setError('');
    const body = list ? { expected_version: version, name, currency, is_default: isDefault, active } : { code, name, currency, is_default: isDefault };
    try {
      onSaved(await request<PriceList>(`/api/market/accounts/${account.id}/price-lists${list ? `/${list.id}` : ''}`, { method: 'POST', body: JSON.stringify(body) }), !list);
    } catch (caught) {
      const current = caught instanceof ApiError && caught.status === 409 ? (caught.body as { price_list?: PriceList } | null)?.price_list : undefined;
      if (current) { setName(current.name); setCurrency(current.currency); setIsDefault(current.is_default); setActive(current.active); setVersion(current.version); }
      setError(failure(caught, 'No se pudo guardar la lista.'));
    } finally { setBusy(false); }
  }
  return <Modal title={list ? `Lista de precios · ${list.code}` : 'Nueva lista de precios'} onClose={() => { if (!busy) onClose(); }}>
    <form className="stack-form" autoComplete="off" onSubmit={submit}>
      {!list && <label>Código<UppercaseInput required maxLength={30} pattern="[A-Za-z0-9_]{1,30}" value={code} disabled={busy} onChange={event => setCode(event.target.value)} placeholder="EJ.: MAYORISTA"/>
        <small>Letras, números y guion bajo. Es el encabezado PRECIO_{code || 'CÓDIGO'} en Excel y no se puede cambiar.</small></label>}
      <label>Nombre<UppercaseInput required maxLength={120} value={name} disabled={busy} onChange={event => setName(event.target.value)} placeholder="EJ.: PRECIOS PARA TALLERES"/></label>
      <label>Moneda<select value={currency} disabled={busy || (!!list && list.priced_count > 0)} onChange={event => setCurrency(event.target.value as Currency)}><option value="USD">USD</option><option value="PAB">PAB</option></select>
        {!!list && list.priced_count > 0 && <small>La moneda no cambia mientras la lista tenga precios.</small>}</label>
      <label className="admin-checkbox"><input type="checkbox" checked={isDefault} disabled={busy || (!!list && list.is_default) || (!list && first)} onChange={event => setIsDefault(event.target.checked)}/>Lista predeterminada</label>
      {list && <label className="admin-checkbox"><input type="checkbox" checked={active} disabled={busy || list.is_default} onChange={event => setActive(event.target.checked)}/>Lista activa</label>}
      <p className="form-footnote">{list?.is_default ? 'Para archivar esta lista o quitarla como predeterminada, marca otra lista como predeterminada.' : 'Solo la lista predeterminada prepara los precios sugeridos de tus cotizaciones.'}</p>
      {error && <div className="notice error" role="alert">{error}</div>}
      <button className="button primary full" disabled={busy}>{busy && <LoaderCircle className="spin" size={18}/>}{list ? 'Guardar lista' : 'Crear lista'}</button>
    </form>
  </Modal>;
}

const kindLabels: Record<string, string> = {
  settings_changed: 'Configuración de precios', price_list_changed: 'Lista de precios', prices_edited: 'Precios editados', price_import_applied: 'Importación de precios',
  profile_changed: 'Perfil de cliente', rule_created: 'Regla creada', rule_updated: 'Regla actualizada', rule_archived: 'Regla archivada',
  draft_discarded: 'Borrador descartado', draft_review_requested: 'Borrador listo para revisión', quotation_published: 'Cotización publicada',
  accept_blocked_shortfall: 'Confirmación bloqueada por existencias', accept_with_shortfall: 'Confirmado con faltante',
  assistant_run: 'Asistente de cotización', assistant_applied: 'Propuesta del asistente aplicada',
};
function auditDetail(entry: PricingAuditEntry) {
  const payload = entry.payload as Record<string, number | string | null>;
  if (entry.kind === 'prices_edited' || entry.kind === 'price_import_applied') {
    return [['created', 'nuevos'], ['updated', 'actualizados'], ['removed', 'eliminados'], ['floor_updates', 'mínimos'], ['line_updates', 'líneas']]
      .filter(([key]) => Number(payload[key])).map(([key, label]) => `${payload[key]} ${label}`).join(' · ') || 'Sin cambios';
  }
  if (entry.kind === 'price_list_changed') return `${payload.action === 'created' ? 'Creada' : 'Actualizada'} · ${payload.code}`;
  if (entry.kind === 'quotation_published') return `Versión ${payload.revision}`;
  return '';
}

function PricingHistory({ account }: { account: Account }) {
  const [search, setSearch] = useState('');
  const [applied, setApplied] = useState('');
  const [page, setPage] = useState(1);
  const [data, setData] = useState<Page<PricingAuditEntry> | null>(null);
  const [error, setError] = useState('');
  const [revision, setRevision] = useState(0);
  useEffect(() => { const timer = setTimeout(() => { if (search.trim() !== applied) { setApplied(search.trim()); setPage(1); } }, 350); return () => clearTimeout(timer); }, [search, applied]);
  useEffect(() => {
    let cancelled = false; setData(null); setError('');
    const query = new URLSearchParams({ page: String(page), ...(applied ? { search: applied } : {}) });
    request<Page<PricingAuditEntry>>(`/api/market/accounts/${account.id}/pricing/history?${query}`).then(value => { if (!cancelled) setData(value); })
      .catch(caught => { if (!cancelled) setError(failure(caught, 'No se pudo cargar el historial.')); });
    return () => { cancelled = true; };
  }, [account.id, applied, page, revision]);
  return <>
    <div className="supplier-pricing-heading"><div><h2>Historial</h2><p>Cambios de precios y listas, borradores y cotizaciones de tu cuenta. Solo lo ve tu equipo.</p></div>
      <button type="button" className="button soft small" onClick={() => setRevision(value => value + 1)}><RefreshCw size={15}/>Actualizar</button></div>
    <div className="supplier-requests-search price-grid-search"><Search size={18}/><input aria-label="Buscar en el historial" value={search} maxLength={200} placeholder="Orden, cliente, usuario o tipo…" autoComplete="off" onChange={event => setSearch(event.target.value)}/>{search && <button type="button" className="icon-button" aria-label="Limpiar búsqueda del historial" onClick={() => setSearch('')}><X size={16}/></button>}</div>
    {error && <div className="notice error" role="alert">{error}</div>}
    {!data && !error ? <div className="loading"><LoaderCircle className="spin"/>Cargando el historial…</div> : data && (!data.results.length ? <p className="empty-state compact">Todavía no hay cambios registrados.</p>
      : <ol className="pricing-audit-list" aria-label="Historial de precios">{data.results.map(entry => <li key={entry.id}><div><strong>{kindLabels[entry.kind] || entry.kind}</strong><span>{auditDetail(entry)}</span></div>
        <small>{date(entry.created_at)} · {entry.actor.name}{entry.order ? ` · Orden ${entry.order.reference}` : ''}{entry.client ? ` · ${entry.client.name}` : ''}</small></li>)}</ol>)}
    {data && <div className="pagination"><span>{data.count} {data.count === 1 ? 'registro' : 'registros'}</span><div><button type="button" className="button soft small" disabled={!data.previous} onClick={() => setPage(value => value - 1)}>Anterior</button><span>Página {page}</span><button type="button" className="button soft small" disabled={!data.next} onClick={() => setPage(value => value + 1)}>Siguiente</button></div></div>}
  </>;
}
