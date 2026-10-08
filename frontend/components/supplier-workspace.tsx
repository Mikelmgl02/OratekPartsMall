'use client';
import { useEffect, useId, useMemo, useRef, useState } from 'react';
import { ArrowDownLeft, ArrowUpRight, Boxes, ChevronRight, CircleHelp, ClipboardList, FileSpreadsheet, LoaderCircle, Menu, PackagePlus, PanelLeftClose, PanelLeftOpen, RefreshCw, Search, Store, Tags, X } from 'lucide-react';
import ServerGrid from './server-grid';
import { inventoryColumns, inventoryOrdering } from './supplier-grids';
import Modal from './modal';
import SupplierInventoryImport from './supplier-inventory-import';
import SupplierPricing from './supplier-pricing';
import SupplierRequests from './supplier-requests';
import { usePageViewport } from './app-shell';
import { UppercaseInput } from './uppercase-field';
import { Account, LedgerEntry, Page, StockItem, request } from '@/lib/types';
import { type SupplierSection, supplierSections } from '@/lib/supplier-navigation';
const sections = [{ key: 'inventory', label: 'Inventario', detail: 'Existencias y movimientos', Icon: Boxes },
  { key: 'pricing', label: 'Precios', detail: 'Listas, clientes y reglas', Icon: Tags },
  { key: 'requests', label: 'Solicitudes', detail: 'Órdenes y cotizaciones', Icon: ClipboardList }] as const;
const headings: Record<SupplierSection, [string, (name: string) => string]> = {
  inventory: ['Tu inventario, en un solo lugar.', name => `Administra el inventario de ${name} y revisa cada ajuste.`],
  pricing: ['Tus precios privados, en un solo lugar.', name => `Mantén las listas de precios, los perfiles de tus clientes y las reglas comerciales de ${name} para cotizar más rápido.`],
  requests: ['Tus solicitudes, en un solo lugar.', name => `Consulta lo que los clientes solicitan a ${name}.`],
};
const SIDEBAR_KEY = 'motionpartes.supplier.sidebar.collapsed';
function initialSection(): SupplierSection {
  if (typeof window === 'undefined') return 'inventory';
  const params = new URL(window.location.href).searchParams;
  const match = sections.find(({ key }) => supplierSections[key] === params.get('seccion'));
  return params.get('vista') === 'proveedor' && match ? match.key : 'inventory';
}
export default function SupplierWorkspace({ account }: { account: Account }) {
  const [section, setSection] = useState<SupplierSection>(initialSection);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  // Desktop only: the side menu folds into an icon rail so the grids get the whole window. Remembered in this browser.
  const [collapsed, setCollapsed] = useState(false);
  useEffect(() => { try { setCollapsed(localStorage.getItem(SIDEBAR_KEY) === '1'); } catch { /* storage blocked: start expanded */ } }, []);
  const menuToggle = useRef<HTMLButtonElement>(null);
  const viewport = usePageViewport();
  const tabId = useId();
  const [revision, setRevision] = useState(0);
  const [search, setSearch] = useState(''); const [query, setQuery] = useState('');
  const [matchFilter, setMatchFilter] = useState('');
  const [count, setCount] = useState<number | null>(null);
  const [totals, setTotals] = useState<{ all: number; unmatched: number } | null>(null);
  const [editing, setEditing] = useState(false);
  const [importing, setImporting] = useState(false);
  const [notice, setNotice] = useState('');
  const [ledgerItem, setLedgerItem] = useState<StockItem | null>(null);
  useEffect(() => { setSection(initialSection()); setSidebarOpen(false); setEditing(false); setImporting(false); setLedgerItem(null); setNotice(''); setSearch(''); setMatchFilter(''); }, [account.id]);
  useEffect(() => { const timer = setTimeout(() => setQuery(search.trim()), 300); return () => clearTimeout(timer); }, [search]);
  // The summary counts the whole inventory, whatever the grid is filtered to.
  useEffect(() => {
    if (section !== 'inventory') return;
    let cancelled = false;
    const base = `/api/market/accounts/${account.id}/inventory`;
    Promise.all([request<Page<StockItem>>(`${base}?page=1`), request<Page<StockItem>>(`${base}?matching_status=unmatched&page=1`)])
      .then(([all, unmatched]) => { if (!cancelled) setTotals({ all: all.count, unmatched: unmatched.count }); })
      .catch(() => { if (!cancelled) setTotals(null); });
    return () => { cancelled = true; };
  }, [account.id, revision, section]);
  const inventoryParams = useMemo(() => ({ search: query, matching_status: matchFilter }), [query, matchFilter]);
  const ledgerContext = useMemo(() => ({ openLedger: (item: StockItem) => setLedgerItem(item) }), []);
  function selectSection(next: SupplierSection) { setSection(next); setEditing(false); setImporting(false); setLedgerItem(null); viewport.current?.scrollTo({ top: 0, behavior: 'instant' }); }
  function toggleSidebar() {
    const next = !collapsed; setCollapsed(next);
    try { localStorage.setItem(SIDEBAR_KEY, next ? '1' : '0'); } catch { /* not remembered */ }
  }
  function closeSidebar() { setSidebarOpen(false); if (window.matchMedia('(max-width: 760px)').matches) menuToggle.current?.focus(); }
  // Vertical tabs: arrows cycle through every section, Home and End jump to the first and last.
  function tabKey(event: React.KeyboardEvent<HTMLButtonElement>) {
    const index = sections.findIndex(({ key }) => key === section), steps: Record<string, number> = { ArrowDown: 1, ArrowUp: -1 };
    let next: SupplierSection;
    if (event.key === 'Home') next = sections[0].key;
    else if (event.key === 'End') next = sections[sections.length - 1].key;
    else if (event.key in steps) next = sections[(index + steps[event.key] + sections.length) % sections.length].key;
    else if (event.key === 'Escape') { event.preventDefault(); closeSidebar(); return; }
    else return;
    event.preventDefault(); selectSection(next); document.getElementById(`${tabId}-${next}-tab`)?.focus();
  }
  const current = sections.find(({ key }) => key === section)!;
  const railLabel = collapsed ? 'Mostrar menú lateral' : 'Ocultar menú lateral';
  return <section className={`workspace section-container supplier-workspace-layout${collapsed ? ' is-collapsed' : ''}`} aria-label="Panel de proveedor">
    <button ref={menuToggle} type="button" className="supplier-menu-toggle" aria-expanded={sidebarOpen} aria-controls={`${tabId}-side-menu`} onClick={() => setSidebarOpen(value => !value)}>{sidebarOpen ? <X size={18}/> : <Menu size={18}/>}<span>Menú de proveedor</span><small>{current.label}</small></button>
    <aside id={`${tabId}-side-menu`} className={`supplier-sidebar ${sidebarOpen ? 'is-open' : ''}`} aria-label="Navegación del proveedor">
      <div className="supplier-sidebar-heading"><span><Store size={22}/></span><div><strong>Panel de proveedor</strong><small>{account.name}</small></div></div>
      <div className="supplier-sidebar-label"><span>GESTIÓN</span><button type="button" className="supplier-sidebar-collapse" aria-label={railLabel} title={railLabel} onClick={toggleSidebar}>{collapsed ? <PanelLeftOpen size={16}/> : <PanelLeftClose size={16}/>}</button></div>
      <div className="supplier-workspace-tabs" role="tablist" aria-label="Secciones del proveedor" aria-orientation="vertical">{sections.map(({ key, label, detail, Icon }) => <button type="button" key={key} role="tab" aria-label={label} id={`${tabId}-${key}-tab`} aria-selected={section === key} aria-controls={`${tabId}-${key}-panel`} tabIndex={section === key ? 0 : -1} title={collapsed ? label : undefined} onClick={() => { selectSection(key); closeSidebar(); }} onKeyDown={tabKey}><Icon size={18}/><span><strong>{label}</strong><small>{detail}</small></span>{section === key && <ChevronRight size={15}/>}</button>)}</div>
    </aside>
    <div className="supplier-workspace-content">
    <div className="section-heading"><div><span className="eyebrow">Panel de proveedores</span><h1>{headings[section][0]}</h1><p>{headings[section][1](account.name)}</p></div>{section === 'inventory' && <div className="supplier-workspace-actions"><button className="button soft" onClick={() => setImporting(true)}><FileSpreadsheet size={18}/>Importar Excel</button><button className="button primary" onClick={() => setEditing(true)}><PackagePlus size={18}/>Actualizar existencias</button></div>}</div>
    <div role="tabpanel" id={`${tabId}-inventory-panel`} aria-labelledby={`${tabId}-inventory-tab`} hidden={section !== 'inventory'}>{section === 'inventory' && <>
    {notice && <div className="notice success" role="status">{notice}</div>}
    <div className="workspace-summary"><div><Boxes size={23}/><span>Registros de inventario</span><strong>{totals ? totals.all.toLocaleString('es-PA') : '—'}</strong></div><div><ClipboardList size={23}/><span>Coincidencias por revisar</span><strong>{totals ? totals.unmatched.toLocaleString('es-PA') : '—'}</strong>{!!totals?.unmatched && matchFilter !== 'unmatched' && <button type="button" className="button text small" onClick={() => setMatchFilter('unmatched')}>Ver solo estas</button>}</div><div><CircleHelp size={23}/><span>¿Falta una coincidencia en el catálogo?</span><p>Los artículos pendientes serán visibles para los clientes cuando tu administrador apruebe su coincidencia.</p></div></div>
    <div className="inventory-panel"><div className="panel-heading"><h2>Registros de existencias</h2><button className="button text" onClick={() => setRevision(revision+1)}><RefreshCw size={15}/>Actualizar</button></div>
      <div className="inventory-filters">
        <div className="catalog-search admin-search"><Search size={19}/><label className="sr-only" htmlFor="supplier-inventory-search">Buscar en tu inventario</label><UppercaseInput id="supplier-inventory-search" value={search} onChange={event => setSearch(event.target.value)} placeholder="CÓDIGO, MARCA, ID O DESCRIPCIÓN…"/></div>
        <label className="inventory-filter-select"><span>Coincidencia</span><select aria-label="Coincidencia" value={matchFilter} onChange={event => setMatchFilter(event.target.value)}><option value="">TODAS</option><option value="unmatched">POR REVISAR</option><option value="matched">VINCULADAS</option></select></label>
        <span className="inventory-count" aria-live="polite">{count === null ? 'Cargando…' : `${count.toLocaleString('es-PA')} ${count === 1 ? 'registro' : 'registros'}`}</span>
      </div>
      <ServerGrid<StockItem> storageKey="supplier-inventory" label="Registros de existencias" path={`/api/market/accounts/${account.id}/inventory`} params={inventoryParams} columns={inventoryColumns}
        rowId={item => String(item.id)} ordering={inventoryOrdering} revision={revision} rowHeight={58} context={ledgerContext} onCount={setCount}
        empty={query || matchFilter ? <div className="empty-state"><Search size={30}/><h3>No hay registros con estos filtros.</h3><p>Prueba con otro código o quita el filtro de coincidencia.</p></div>
          : <div className="empty-state"><Boxes size={34}/><h3>Todo listo para tu primer artículo.</h3><p>Agrega un registro de existencias para vincular tus repuestos con el catálogo de MotionPartes.</p><button className="button primary" onClick={() => setEditing(true)}>Agregar registro de existencias<ArrowUpRight size={16}/></button></div>}/>
    </div>
    <div className="integration-note"><span className="apiag-mark">A</span><div><h3>Conexión con apiag-cloud</h3><p>Importa tus saldos desde Excel o actualiza registros de carga manual. El inventario de apiag-cloud se actualiza mediante su integración.</p></div></div>
    {editing && <StockForm account={account} onClose={() => setEditing(false)} onSaved={() => {setEditing(false); setRevision(revision+1);}}/>}
    {importing && <SupplierInventoryImport key={account.id} account={account} onClose={() => { setImporting(false); setRevision(value => value + 1); }} onSaved={result => { setRevision(value => value + 1); setNotice(`Importación completada: ${result.applied_summary.created_items} artículos nuevos y ${result.applied_summary.updated_items} actualizados.${result.summary.rejected_rows ? ` ${result.summary.rejected_rows} filas pendientes; abre Importar Excel para descargar el archivo de correcciones.` : ''}`); }}/>} 
    {ledgerItem && <Ledger account={account} item={ledgerItem} onClose={() => setLedgerItem(null)}/>}
    </>}</div><div role="tabpanel" id={`${tabId}-pricing-panel`} aria-labelledby={`${tabId}-pricing-tab`} hidden={section !== 'pricing'}>{section === 'pricing' && <SupplierPricing key={account.id} account={account}/>}</div><div role="tabpanel" id={`${tabId}-requests-panel`} aria-labelledby={`${tabId}-requests-tab`} hidden={section !== 'requests'}>{section === 'requests' && <SupplierRequests key={account.id} account={account}/>}</div>
    </div>
  </section>;
}
function StockForm({ account, onClose, onSaved }: { account: Account; onClose: () => void; onSaved: () => void }) {
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const retry = useRef<{ fingerprint: string; id: string } | null>(null);
  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault(); setError('');
    const data = Object.fromEntries(new FormData(event.currentTarget));
    const quantity = Number(data.quantity);
    if (!String(data.quantity).trim() || !Number.isSafeInteger(quantity) || quantity < 0) { setError('Introduce un saldo total válido. Usa cero para vaciar las existencias.'); return; }
    const payload = { supplier_invent_id: String(data.supplier_invent_id), codigo: String(data.codigo).toUpperCase(), brand: String(data.brand).toUpperCase(), description: String(data.description).toUpperCase(), quantity, source: 'upload' };
    const fingerprint = JSON.stringify(payload);
    if (!retry.current || retry.current.fingerprint !== fingerprint) retry.current = { fingerprint, id: crypto.randomUUID() };
    setBusy(true);
    try { await request(`/api/market/accounts/${account.id}/inventory/ingest`, { method: 'POST', body: JSON.stringify({ ...payload, update_id: retry.current.id }) }); onSaved(); }
    catch (error) { setError(error instanceof TypeError ? 'Se perdió la conexión. Inténtalo de nuevo para confirmar el saldo.' : error instanceof Error ? error.message : 'No se pudo actualizar el inventario.'); }
    finally { setBusy(false); }
  }
  return <Modal title="Actualizar un registro de existencias" onClose={() => { if (!busy) onClose(); }}><p className="modal-description">Usa tu ID de inventario permanente. Si ya existe, se actualizará el artículo. Introduce el saldo total actual de existencias.</p><form className="stack-form" autoComplete="off" onSubmit={submit}><label>ID de inventario del proveedor<input name="supplier_invent_id" required maxLength={120} placeholder="Ej.: 12345" autoComplete="off" autoCorrect="off" autoCapitalize="none" spellCheck={false} disabled={busy}/></label><div className="form-row"><label>Código del repuesto<UppercaseInput name="codigo" required maxLength={120} placeholder="Ej.: ABC-123" disabled={busy}/></label><label>Marca<UppercaseInput name="brand" maxLength={120} placeholder="FABRICANTE (OPCIONAL)" disabled={busy}/></label></div><label>Descripción<UppercaseInput name="description" placeholder="DESCRIPCIÓN OPCIONAL" disabled={busy}/></label><label>Cantidad de existencias reportadas<input name="quantity" type="number" required min={0} step={1} placeholder="0" autoComplete="off" disabled={busy}/></label><p className="form-footnote">Se creará un registro de carga manual. El inventario gestionado por apiag-cloud debe actualizarse mediante su integración.</p>{error && <div className="notice error" role="alert">{error}</div>}<button className="button primary full" disabled={busy}>{busy && <LoaderCircle className="spin" size={18}/>}Guardar saldo de existencias<ArrowUpRight size={18}/></button></form></Modal>;
}
function Ledger({ account, item, onClose }: { account: Account; item: StockItem; onClose: () => void }) {
  const [entries, setEntries] = useState<Page<LedgerEntry> | null>(null);
  const [page, setPage] = useState(1);
  const [error, setError] = useState('');
  useEffect(() => { let cancelled = false; setEntries(null); setError(''); request<Page<LedgerEntry>>(`/api/market/accounts/${account.id}/inventory/${item.id}/ledger?page=${page}`).then(data => { if (!cancelled) setEntries(data); }).catch(error => { if (!cancelled) setError(error.message); }); return () => {cancelled = true;}; }, [account.id, item.id, page]);
  return <Modal title={`Movimientos de existencias · ${item.codigo}`} onClose={onClose}><p className="modal-description">Todos los cambios, del más reciente al más antiguo. Un crédito aumenta el saldo y un débito lo reduce; las cantidades siempre son positivas.</p>{error && <div className="notice error" role="alert">{error}</div>}{!entries && !error && <div className="loading"><LoaderCircle className="spin"/>Cargando el historial…</div>}{entries && !entries.results.length && <p className="empty-state compact">Todavía no hay movimientos de existencias.</p>}<div className="ledger-list supplier-ledger">{entries?.results.map(entry => <div className={`ledger-entry ${entry.direction}`} key={entry.id}><span className="round-icon">{entry.direction === 'debit' ? <ArrowUpRight size={18}/> : <ArrowDownLeft size={18}/>}</span><div><h4>{entry.direction === 'credit' ? 'Crédito' : 'Débito'} de {entry.balance_type === 'reservation' ? 'reservas' : 'existencias'}</h4><p>{{sync: 'Ajuste de existencias', reserve: 'Reserva', release: 'Liberación de reserva', delivery: 'Entrega'}[entry.kind] || 'Movimiento de existencias'} · {new Date(entry.created_at).toLocaleString('es-PA', { timeZone: 'America/Panama' })} · {entry.reference}</p><small>Saldo de existencias: {entry.reported_after} · Reservadas: {entry.reserved_after}</small>{entry.balance_type !== 'reservation' && entry.reserved_delta > 0 && <small>Reservas: {entry.reserved_direction === 'credit' ? 'Crédito' : 'Débito'} de {entry.reserved_delta}</small>}</div><strong aria-label={`Cantidad: ${entry.quantity}`}>{entry.quantity}</strong></div>)}</div>{entries && <div className="pagination"><button className="button soft small" disabled={!entries.previous} onClick={() => setPage(page-1)}>Anterior</button><span>Página {page}</span><button className="button soft small" disabled={!entries.next} onClick={() => setPage(page+1)}>Siguiente</button></div>}</Modal>;
}
