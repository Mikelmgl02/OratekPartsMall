'use client';
import { useEffect, useId, useState } from 'react';
import { History, LoaderCircle, Store } from 'lucide-react';
import SupplierLedger, { LedgerView, arrowTab } from './supplier-ledger';
import { request } from '@/lib/types';

type AccountRow = { id: string; name: string; active: boolean; roles: string[]; apiag?: { server_url: string } | null };
type Role = { code: string; name: string; capability: string };
type AccountPage = { results: AccountRow[]; next: string | null };
const views: [LedgerView, string][] = [['movements', 'Libro de movimientos'], ['runs', 'Cargas y sincronizaciones']];

// Administración → Inventario → Libro de movimientos: a supplier's ledger as the supplier sees it (units only, no prices), chosen from the
// supplier accounts (&proveedor=<id> deep-links one).
export default function AdminSupplierLedger() {
  const [suppliers, setSuppliers] = useState<AccountRow[] | null>(null); const [error, setError] = useState('');
  const [supplier, setSupplier] = useState(''); const [view, setView] = useState<LedgerView>('movements');
  const tabId = useId();
  useEffect(() => {
    let live = true;
    (async () => {
      const roles = await request<Role[]>('/api/management/roles');
      const codes = new Set(roles.filter(role => role.capability === 'supplier').map(role => role.code));
      const rows: AccountRow[] = [];
      for (let page = 1; page <= 20; page++) {
        const result = await request<AccountPage | AccountRow[]>(`/api/management/accounts?page=${page}`);
        const list = Array.isArray(result) ? result : result.results;
        rows.push(...list);
        if (Array.isArray(result) || !result.next) break;
      }
      return rows.filter(row => row.roles.some(code => codes.has(code))).sort((a, b) => a.name.localeCompare(b.name, 'es'));
    })().then(rows => {
      if (!live) return;
      setSuppliers(rows);
      const wanted = new URL(window.location.href).searchParams.get('proveedor');
      setSupplier(rows.find(row => row.id === wanted)?.id ?? rows[0]?.id ?? '');
    }).catch(reason => { if (live) setError((reason as Error).message); });
    return () => { live = false; };
  }, []);
  function choose(id: string) {
    setSupplier(id);
    const url = new URL(window.location.href);
    url.searchParams.set('proveedor', id);
    window.history.replaceState(window.history.state, '', url);
  }
  const current = suppliers?.find(row => row.id === supplier);
  if (error) return <div className="notice error" role="alert">{error}</div>;
  if (!suppliers) return <div className="loading"><LoaderCircle className="spin"/>Cargando proveedores…</div>;
  if (!suppliers.length) return <div className="empty-state"><Store size={30}/><h3>Todavía no hay cuentas de proveedor.</h3></div>;
  return <div className="admin-supplier-ledger">
    <div className="inventory-filters">
      <label className="inventory-filter-select"><span>Proveedor</span><select aria-label="Proveedor" value={supplier} onChange={event => choose(event.target.value)}>
        {suppliers.map(row => <option key={row.id} value={row.id}>{row.name}{row.active ? '' : ' (INACTIVA)'}</option>)}</select></label>
      {current?.apiag && <span className="admin-grid-muted">Sincronizada con apiag-cloud ({current.apiag.server_url}): la carga Excel no está disponible.</span>}
    </div>
    <div className="supplier-pricing-tabs" role="tablist" aria-label="Vistas del libro">{views.map(([key, label]) => <button type="button" role="tab" key={key} id={`${tabId}-${key}-tab`}
      aria-controls={`${tabId}-panel`} aria-selected={view === key} tabIndex={view === key ? 0 : -1} className={view === key ? 'selected' : ''} onClick={() => setView(key)}
      onKeyDown={event => { const next = arrowTab(views.map(([value]) => value), view, event); if (next) { setView(next); document.getElementById(`${tabId}-${next}-tab`)?.focus(); } }}>
      {key === 'movements' && <History size={15}/>}{label}</button>)}</div>
    <div role="tabpanel" id={`${tabId}-panel`} aria-labelledby={`${tabId}-${view}-tab`}>
      {supplier && <SupplierLedger key={supplier} basePath={`/api/management/accounts/${supplier}/ledger`} view={view}/>}</div>
  </div>;
}
