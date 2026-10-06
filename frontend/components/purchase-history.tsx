'use client';

import { useEffect, useRef } from 'react';
import { ArrowLeft, ArrowRight, History, List, LoaderCircle, ShoppingBag, UserRound } from 'lucide-react';
import { SentRequestList } from './client-requests';
import type { Account } from '@/lib/types';

type Props = {
  account?: Account;
  authenticated: boolean;
  sessionReady: boolean;
  onCatalog: () => void;
  onRequests: () => void;
  onSignIn: () => void;
};

// Only mutually confirmed quotations populate this history; an agreement is not proof of delivery.
export default function PurchaseHistory({ account, authenticated, sessionReady, onCatalog, onRequests, onSignIn }: Props) {
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => { heading.current?.focus({preventScroll:true}); }, []);
  const canView = account?.capabilities.includes('client');

  return <main className="basket-page purchase-history-page" aria-label="Historial de compras"><div className="section-container basket-page-container">
    <button className="basket-page-back" onClick={onCatalog}><ArrowLeft size={16}/>Volver al catálogo</button>
    <div className="basket-page-heading"><div><span className="eyebrow">TU CUENTA</span><h1 ref={heading} tabIndex={-1}>Historial de compras</h1><p>Consulta las cotizaciones que confirmaste con tus proveedores.</p></div><span className="wishlist-heading-icon"><History size={26}/></span></div>
    {!sessionReady ? <div className="loading" role="status"><LoaderCircle className="spin" size={20}/>Cargando tu cuenta…</div> : !authenticated ? <div className="wishlist-empty"><span><UserRound size={32}/></span><h2>Tu historial está en tu cuenta.</h2><p>Inicia sesión para acceder al historial de compras de tu negocio.</p><button className="button dark" onClick={onSignIn}><UserRound size={16}/>Iniciar sesión<ArrowRight size={16}/></button></div> : !account ? <div className="notice">Tu administrador debe asignarte una cuenta para consultar su historial de compras.</div> : !canView ? <div className="notice">Selecciona una cuenta con rol de cliente para consultar su historial de compras.</div> : <>
      <p className="purchase-history-account">Cuenta activa: <strong>{account.name}</strong></p>
      <SentRequestList key={account.id} account={account} agreementsOnly/>
      <div className="purchase-history-actions"><button className="button soft" onClick={onRequests}><List size={17}/>Ver mis solicitudes<ArrowRight size={16}/></button><button className="button soft" onClick={onCatalog}>Explorar el catálogo<ArrowRight size={16}/></button></div>
    </>}
  </div></main>;
}
