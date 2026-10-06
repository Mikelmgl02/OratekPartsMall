'use client';

import Link from 'next/link';
import { useEffect, useId, useRef, useState } from 'react';
import { ChevronDown, Heart, History, List, LoaderCircle, LogOut, ShieldCheck, ShoppingBag, Store, UserRound } from 'lucide-react';
import { Account, SessionUser, roleLabel } from '@/lib/types';

type AccountMenuProps = {
  user: SessionUser | null;
  ready: boolean;
  accounts: Account[];
  accountId: string;
  favoriteCount: number;
  basketCount: number;
  onFavorites: () => void;
  onRequests: () => void;
  onPurchases: () => void;
  onBasket: () => void;
  onSupplier: () => void;
  onSignIn: () => void;
  onInvitation: () => void;
  onSwitchAccount: (id: string) => void;
  onLogout: () => Promise<void>;
};

export default function AccountMenu({ user, ready, accounts, accountId, favoriteCount, basketCount, onFavorites, onRequests, onPurchases, onBasket, onSupplier, onSignIn, onInvitation, onSwitchAccount, onLogout }: AccountMenuProps) {
  const [open, setOpen] = useState(false);
  const [signingOut, setSigningOut] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const closeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const focusNext = useRef(false);
  const pointerType = useRef('');
  const keyboardNavigation = useRef(false);
  const logoutPending = useRef(false);
  const panelId = useId();
  const account = accounts.find(item => item.id === accountId);
  const name = user?.first_name?.trim() || user?.username || '';
  const fullName = [user?.first_name, user?.last_name].filter(Boolean).join(' ').trim() || name;

  function cancelClose() {
    if (closeTimer.current) clearTimeout(closeTimer.current);
    closeTimer.current = null;
  }
  function close(restoreFocus = false) {
    cancelClose();
    focusNext.current = false;
    setOpen(false);
    if (restoreFocus) trigger.current?.focus();
  }
  function go(action: () => void) { close(); action(); }
  function focusFirst() { root.current?.querySelector<HTMLElement>('[data-account-menu-item]')?.focus(); }

  useEffect(() => () => { if (closeTimer.current) clearTimeout(closeTimer.current); }, []);
  useEffect(() => {
    if (!open) return;
    if (focusNext.current) { focusNext.current = false; focusFirst(); }
    const dismissOutside = (event: PointerEvent) => {
      if (!root.current?.contains(event.target as Node)) close();
    };
    const dismissEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); close(true); }
    };
    document.addEventListener('pointerdown', dismissOutside);
    document.addEventListener('keydown', dismissEscape);
    return () => {
      document.removeEventListener('pointerdown', dismissOutside);
      document.removeEventListener('keydown', dismissEscape);
    };
  }, [open]);

  return <><div className={`account-menu${open ? ' is-open' : ''}`} ref={root}
    onPointerEnter={event => {
      cancelClose();
      if (ready && event.pointerType === 'mouse' && window.matchMedia('(hover: hover)').matches) {
        keyboardNavigation.current = false;
        setOpen(true);
      }
    }}
    onPointerDown={() => { keyboardNavigation.current = false; }}
    onKeyDown={() => { keyboardNavigation.current = true; }}
    onPointerLeave={() => {
      cancelClose();
      closeTimer.current = setTimeout(() => {
        if (!logoutPending.current && (!keyboardNavigation.current || !root.current?.contains(document.activeElement))) setOpen(false);
      }, 180);
    }}
    onBlur={event => {
      if (!logoutPending.current && !event.currentTarget.contains(event.relatedTarget as Node | null)) close();
    }}>
    <button className="account-menu-trigger" ref={trigger} disabled={!ready}
      aria-label="Cuenta y listas" aria-expanded={open} aria-controls={panelId}
      onPointerDown={event => { pointerType.current = event.pointerType; }}
      onClick={event => {
        cancelClose();
        if (event.detail > 0 && pointerType.current === 'mouse' && window.matchMedia('(hover: hover)').matches) setOpen(true);
        else setOpen(current => !current);
      }}
      onKeyDown={event => {
        if (event.key !== 'ArrowDown') return;
        event.preventDefault(); cancelClose();
        if (open) focusFirst(); else { focusNext.current = true; setOpen(true); }
      }}>
      <UserRound className="account-menu-mobile-icon" size={20}/>
      <span className="account-menu-trigger-copy"><span>{user ? `Hola, ${name}` : 'Hola, inicia sesión'}</span><strong>Cuenta y listas</strong></span>
      <ChevronDown className="account-menu-chevron" size={13}/>
    </button>
    {open && <>
      <div className="account-menu-popover">
        <section className="account-menu-panel" id={panelId} aria-label="Cuenta y listas">
          {user ? <div className="account-menu-identity">
            <span className="account-menu-avatar"><UserRound size={21}/></span>
            <div><strong>{fullName}</strong><span>{account?.name || 'Sin cuenta asignada'}</span></div>
            <span className="account-menu-connected"><span className="live-dot"/>Sesión activa</span>
          </div> : <div className="account-menu-signin">
            <button data-account-menu-item className="button primary" onClick={() => go(onSignIn)}>Iniciar sesión</button>
            <p>¿Tienes una invitación? <button onClick={() => go(onInvitation)}>Actívala aquí</button></p>
          </div>}
          <div className="account-menu-columns">
            <div className="account-menu-section">
              <h2>Tus listas</h2>
              <button data-account-menu-item className="account-menu-link" onClick={() => go(onFavorites)}><Heart size={17}/><span>Mis favoritos</span>{favoriteCount > 0 && <span className="account-menu-count">{favoriteCount}</span>}</button>
              {user && account?.capabilities.includes('client') && <button className="account-menu-link" onClick={() => go(onRequests)}><List size={17}/><span>Mis solicitudes</span></button>}
              <button className="account-menu-link" onClick={() => go(onBasket)}><ShoppingBag size={17}/><span>Mi cesta</span>{basketCount > 0 && <span className="account-menu-count">{basketCount}</span>}</button>
              <p className="account-menu-hint">Guarda repuestos y prepara tus próximas solicitudes.</p>
            </div>
            <div className="account-menu-section">
              <h2>Tu cuenta</h2>
              {user ? <>
                <label className="account-menu-account" htmlFor={`${panelId}-account`}>Cuenta activa
                  <select id={`${panelId}-account`} aria-label="Cambiar cuenta desde el menú" disabled={!accounts.length || signingOut} value={accountId}
                    onChange={event => { onSwitchAccount(event.target.value); close(); }}>
                    {!accounts.length && <option value="">Sin cuenta asignada</option>}
                    {accounts.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}
                  </select>
                </label>
                {account && <p className="account-menu-roles">{account.roles.map(role => roleLabel[role] || role).join(' · ')}</p>}
                {account?.capabilities.includes('client') && <button className="account-menu-link" onClick={() => go(onPurchases)}><History size={17}/><span>Historial de compras</span></button>}
                {account?.capabilities.includes('supplier') && <button className="account-menu-link" onClick={() => go(onSupplier)}><Store size={17}/><span>Panel de proveedor</span></button>}
                {user.is_superuser && <Link className="account-menu-link" href="/administracion" onClick={() => close()}><ShieldCheck size={17}/><span>Administración</span></Link>}
              </> : <>
                <button className="account-menu-link" onClick={() => go(onSignIn)}><UserRound size={17}/><span>Acceder a mi cuenta</span></button>
                <p className="account-menu-hint">El acceso es por invitación. Tu administrador configura tu cuenta y sus permisos.</p>
              </>}
            </div>
          </div>
          {user && <div className="account-menu-footer"><span>@{user.username}</span><button className="account-menu-link" disabled={signingOut} onClick={async () => {
            cancelClose(); logoutPending.current = true;
            trigger.current?.focus({preventScroll:true});
            setSigningOut(true);
            try { await onLogout(); close(); } finally { logoutPending.current = false; setSigningOut(false); }
          }}>{signingOut ? <LoaderCircle className="spin" size={16}/> : <LogOut size={16}/>}<span>{signingOut ? 'Cerrando sesión…' : 'Cerrar sesión'}</span></button></div>}
        </section>
      </div>
    </>}
  </div>{open && <div className="account-menu-backdrop" aria-hidden="true" onClick={() => close()}/>}</>;
}
