'use client';

import Link from 'next/link';
import { useEffect, useState } from 'react';
import { ArrowLeft, LogOut, Store } from 'lucide-react';
import BrandLogo from './brand-logo';
import AuthModal from './auth-modal';
import DealDetail from './deal-detail';
import SupplierOrderSkeleton from './supplier-order-skeleton';
import { AppToolbar } from './app-shell';
import { Account, Page, SessionUser, request } from '@/lib/types';
import { supplierRequestsHref } from '@/lib/supplier-navigation';
import { useRouter } from 'next/navigation';

type Access = { authenticated: boolean; account?: Account; error?: string };

export default function SupplierOrderPage({ accountId, orderId }: { accountId: string; orderId: string }) {
  const router = useRouter();
  const [access, setAccess] = useState<Access | null>(null);
  const [revision, setRevision] = useState(0);
  const [authOpen, setAuthOpen] = useState(false);
  const [signingOut, setSigningOut] = useState(false);
  const [error, setError] = useState('');
  const back = supplierRequestsHref(accountId);
  useEffect(() => {
    const controller = new AbortController();
    let cancelled = false;
    async function load() {
      setAccess(null);
      try {
        const session = await request<{ authenticated: boolean; accounts?: Page<Account>; user?: SessionUser }>('/api/session', { signal: controller.signal });
        if (!session.authenticated) { if (!cancelled) setAccess({ authenticated: false }); return; }
        let accounts = session.accounts;
        let account = accounts?.results.find(value => value.id === accountId);
        let page = 2;
        while (!account && accounts?.next) {
          accounts = await request<Page<Account>>(`/api/market/accounts?page=${page++}`, { signal: controller.signal });
          account = accounts.results.find(value => value.id === accountId);
        }
        if (!cancelled) setAccess({ authenticated: true, account: account?.capabilities.includes('supplier') ? account : undefined });
      } catch (caught) {
        if (!cancelled) setAccess({ authenticated: true, error: caught instanceof Error ? caught.message : 'No se pudo comprobar tu acceso.' });
      }
    }
    void load();
    return () => { cancelled = true; controller.abort(); };
  }, [accountId, revision]);
  async function logout() {
    if (signingOut) return;
    setSigningOut(true); setError('');
    try { await request('/api/session', { method: 'DELETE' }); router.replace('/'); router.refresh(); }
    catch { setError('No se pudo cerrar la sesión. Inténtalo de nuevo.'); }
    finally { setSigningOut(false); }
  }
  return <>
    <AppToolbar><header className="header"><div className="header-inner supplier-order-toolbar">
      <Link className="wordmark" aria-label="Inicio de MotionPartes" href="/"><BrandLogo/></Link>
      <div className="supplier-order-toolbar-actions"><Link className="button soft small" href={back}><ArrowLeft size={15}/>Volver a solicitudes</Link>{access?.authenticated && <button type="button" className="icon-button" aria-label="Cerrar sesión" disabled={signingOut} onClick={() => void logout()}><LogOut size={19}/></button>}</div>
    </div></header></AppToolbar>
    <main className="section-container supplier-order-page" aria-label="Detalle de orden de proveedor">
      {error && <div className="notice error" role="alert">{error}</div>}
      {!access ? <SupplierOrderSkeleton/> : access.account ? <>
        <nav className="supplier-order-breadcrumbs" aria-label="Ubicación de la orden"><Link href={back}>Solicitudes</Link><span aria-hidden="true">/</span><span>Orden de cliente</span></nav>
        <div className="supplier-order-account"><Store size={17}/><span>{access.account.name}</span></div>
        <DealDetail key={`${accountId}:${orderId}`} account={access.account} orderId={orderId} side="supplier"/>
      </> : <section className="empty-state supplier-order-access">
        <Store size={36}/><h1>{access.error ? 'No pudimos cargar esta orden.' : !access.authenticated ? 'Inicia sesión para ver esta orden.' : 'No tienes acceso a esta cuenta de proveedor.'}</h1>
        <p>{access.error || (!access.authenticated ? 'Usa un usuario autorizado por el proveedor.' : 'Comprueba que la cuenta pertenece a tu usuario y tiene un rol de proveedor.')}</p>
        {access.error ? <button type="button" className="button primary" onClick={() => setRevision(value => value + 1)}>Intentar de nuevo</button> : !access.authenticated && <button type="button" className="button primary" onClick={() => setAuthOpen(true)}>Iniciar sesión</button>}
        <Link className="button soft" href="/">Volver al catálogo</Link>
      </section>}
    </main>
    {authOpen && <AuthModal onClose={() => setAuthOpen(false)} onSuccess={async () => { setAuthOpen(false); setRevision(value => value + 1); }}/>}
  </>;
}
