'use client';
import { useEffect, useId, useRef, useState } from 'react';
import { ArrowDownLeft, ArrowUpRight, Boxes, ChevronRight, CircleHelp, ClipboardList, FileSpreadsheet, History, LoaderCircle, Menu, PackagePlus, RefreshCw, Store, X } from 'lucide-react';
import Modal from './modal';
import SupplierInventoryImport from './supplier-inventory-import';
import SupplierRequests from './supplier-requests';
import { usePageViewport } from './app-shell';
import { UppercaseInput } from './uppercase-field';
import { Account, LedgerEntry, Page, StockItem, request } from '@/lib/types';
function initialSection(): 'inventory' | 'requests' {
  if (typeof window === 'undefined') return 'inventory';
  const params = new URL(window.location.href).searchParams;
  return params.get('vista') === 'proveedor' && params.get('seccion') === 'solicitudes' ? 'requests' : 'inventory';
}
export default function SupplierWorkspace({ account }: { account: Account }) {
  const [section, setSection] = useState<'inventory' | 'requests'>(initialSection);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const menuToggle = useRef<HTMLButtonElement>(null);
  const viewport = usePageViewport();
  const tabId = useId();
  const [items, setItems] = useState<Page<StockItem>>({ count: 0, next: null, previous: null, results: [] });
  const [page, setPage] = useState(1);
  const [revision, setRevision] = useState(0);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState('');
  const [editing, setEditing] = useState(false);
  const [importing, setImporting] = useState(false);
  const [notice, setNotice] = useState('');
  const [ledgerItem, setLedgerItem] = useState<StockItem | null>(null);
  useEffect(() => { setSection(initialSection()); setSidebarOpen(false); setPage(1); setEditing(false); setImporting(false); setLedgerItem(null); setNotice(''); }, [account.id]);
  useEffect(() => {
    if (section !== 'inventory') return;
    let cancelled = false; setBusy(true); setError('');
    request<Page<StockItem>>(`/api/market/accounts/${account.id}/inventory?page=${page}`).then(result => { if (!cancelled) setItems(result); }).catch(error => { if (!cancelled) setError(error.message); }).finally(() => { if (!cancelled) setBusy(false); });
    return () => { cancelled = true; };
  }, [account.id, page, revision, section]);
  function selectSection(next: 'inventory' | 'requests') { setSection(next); setEditing(false); setImporting(false); setLedgerItem(null); viewport.current?.scrollTo({ top: 0, behavior: 'instant' }); }
  function closeSidebar() { setSidebarOpen(false); if (window.matchMedia('(max-width: 760px)').matches) menuToggle.current?.focus(); }
  function tabKey(event: React.KeyboardEvent<HTMLButtonElement>) {
    let next: 'inventory' | 'requests';
    if (event.key === 'Home') next = 'inventory';
    else if (event.key === 'End') next = 'requests';
    else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') next = section === 'inventory' ? 'requests' : 'inventory';
    else if (event.key === 'Escape') { event.preventDefault(); closeSidebar(); return; }
    else return;
    event.preventDefault(); selectSection(next); document.getElementById(`${tabId}-${next}-tab`)?.focus();
  }
  return <section className="workspace section-container supplier-workspace-layout" aria-label="Panel de proveedor">
    <button ref={menuToggle} type="button" className="supplier-menu-toggle" aria-expanded={sidebarOpen} aria-controls={`${tabId}-side-menu`} onClick={() => setSidebarOpen(value => !value)}>{sidebarOpen ? <X size={18}/> : <Menu size={18}/>}<span>Menú de proveedor</span><small>{section === 'inventory' ? 'Inventario' : 'Solicitudes'}</small></button>
    <aside id={`${tabId}-side-menu`} className={`supplier-sidebar ${sidebarOpen ? 'is-open' : ''}`} aria-label="Navegación del proveedor">
      <div className="supplier-sidebar-heading"><span><Store size={22}/></span><div><strong>Panel de proveedor</strong><small>{account.name}</small></div></div>
      <div className="supplier-sidebar-label">GESTIÓN</div>
      <div className="supplier-workspace-tabs" role="tablist" aria-label="Secciones del proveedor" aria-orientation="vertical">{([{ key: 'inventory', label: 'Inventario', detail: 'Existencias y movimientos', Icon: Boxes }, { key: 'requests', label: 'Solicitudes', detail: 'Órdenes y cotizaciones', Icon: ClipboardList }] as const).map(({ key, label, detail, Icon }) => <button type="button" key={key} role="tab" aria-label={label} id={`${tabId}-${key}-tab`} aria-selected={section === key} aria-controls={`${tabId}-${key}-panel`} tabIndex={section === key ? 0 : -1} onClick={() => { selectSection(key); closeSidebar(); }} onKeyDown={tabKey}><Icon size={18}/><span><strong>{label}</strong><small>{detail}</small></span>{section === key && <ChevronRight size={15}/>}</button>)}</div>
    </aside>
    <div className="supplier-workspace-content">
    <div className="section-heading"><div><span className="eyebrow">Panel de proveedores</span><h1>{section === 'inventory' ? 'Tu inventario, en un solo lugar.' : 'Tus solicitudes, en un solo lugar.'}</h1><p>{section === 'inventory' ? `Administra el inventario de ${account.name} y revisa cada ajuste.` : `Consulta lo que los clientes solicitan a ${account.name}.`}</p></div>{section === 'inventory' && <div className="supplier-workspace-actions"><button className="button soft" onClick={() => setImporting(true)}><FileSpreadsheet size={18}/>Importar Excel</button><button className="button primary" onClick={() => setEditing(true)}><PackagePlus size={18}/>Actualizar existencias</button></div>}</div>
    <div role="tabpanel" id={`${tabId}-inventory-panel`} aria-labelledby={`${tabId}-inventory-tab`} hidden={section !== 'inventory'}>{section === 'inventory' && <>
    {notice && <div className="notice success" role="status">{notice}</div>}
    <div className="workspace-summary"><div><Boxes size={23}/><span>Registros de inventario</span><strong>{items.count}</strong></div><div><ClipboardList size={23}/><span>Coincidencias por revisar</span><strong>{items.results.filter(item => item.matching_status !== 'matched').length}<small> en esta página</small></strong></div><div><CircleHelp size={23}/><span>¿Falta una coincidencia en el catálogo?</span><p>Los artículos pendientes serán visibles para los clientes cuando tu administrador apruebe su coincidencia.</p></div></div>
    <div className="inventory-panel"><div className="panel-heading"><h2>Registros de existencias</h2><button className="button text" onClick={() => setRevision(revision+1)}><RefreshCw size={15}/>Actualizar</button></div>
      {error && <div className="notice error" role="alert">{error}</div>}
      {busy ? <div className="loading"><LoaderCircle className="spin"/>Cargando tu inventario…</div> : !items.results.length ? <div className="empty-state"><Boxes size={34}/><h3>Todo listo para tu primer artículo.</h3><p>Agrega un registro de existencias para vincular tus repuestos con el catálogo de MotionPartes.</p><button className="button primary" onClick={() => setEditing(true)}>Agregar registro de existencias<ArrowUpRight size={16}/></button></div> : <div className="table-scroll"><table><thead><tr><th>Repuesto / código</th><th>Tu ID de inventario</th><th>Origen</th><th>Disponibles</th><th>Reservadas</th><th>Coincidencia</th><th>Historial</th></tr></thead><tbody>{items.results.map(item => <tr key={item.id}><td><strong>{item.codigo}</strong><span>{item.brand}{item.description ? ` · ${item.description}` : ''}</span></td><td>{item.supplier_invent_id}</td><td>{item.source === 'apiag' ? 'apiag-cloud' : 'Carga manual'}</td><td className="number-cell">{item.available_quantity}</td><td className="number-cell">{item.reserved_quantity}</td><td><span className={`status ${item.matching_status}`}>{item.matching_status === 'matched' ? 'Vinculado' : item.matching_status === 'review' ? 'Por revisar' : 'Pendiente'}</span></td><td><button className="icon-button" aria-label={`Ver movimientos de ${item.codigo}`} onClick={() => setLedgerItem(item)}><History size={18}/></button></td></tr>)}</tbody></table></div>}
      <div className="pagination"><span>{items.count} registros</span><div><button className="button soft small" disabled={!items.previous || busy} onClick={() => setPage(page-1)}>Anterior</button><span>Página {page}</span><button className="button soft small" disabled={!items.next || busy} onClick={() => setPage(page+1)}>Siguiente</button></div></div>
    </div>
    <div className="integration-note"><span className="apiag-mark">A</span><div><h3>Conexión con apiag-cloud</h3><p>Importa tus saldos desde Excel o actualiza registros de carga manual. El inventario de apiag-cloud se actualiza mediante su integración.</p></div></div>
    {editing && <StockForm account={account} onClose={() => setEditing(false)} onSaved={() => {setEditing(false); setRevision(revision+1);}}/>}
    {importing && <SupplierInventoryImport key={account.id} account={account} onClose={() => { setImporting(false); setRevision(value => value + 1); }} onSaved={result => { setRevision(value => value + 1); setNotice(`Importación completada: ${result.applied_summary.created_items} artículos nuevos y ${result.applied_summary.updated_items} actualizados.${result.summary.rejected_rows ? ` ${result.summary.rejected_rows} filas pendientes; abre Importar Excel para descargar el archivo de correcciones.` : ''}`); }}/>} 
    {ledgerItem && <Ledger account={account} item={ledgerItem} onClose={() => setLedgerItem(null)}/>}
    </>}</div><div role="tabpanel" id={`${tabId}-requests-panel`} aria-labelledby={`${tabId}-requests-tab`} hidden={section !== 'requests'}>{section === 'requests' && <SupplierRequests key={account.id} account={account}/>}</div>
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
