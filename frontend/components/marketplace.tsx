'use client';
import Link from 'next/link';
import BrandLogo from './brand-logo';
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { ArrowDown, ArrowRight, ArrowUpRight, Boxes, Check, ChevronDown, CircleHelp, Disc3, Filter, Globe2, Grid2X2, Heart, List, LoaderCircle, LockKeyhole, Package, Search, Settings2, ShieldCheck, ShoppingBag, SlidersHorizontal, Sparkles, Store, Truck, UserRound, Wrench, X, Zap } from 'lucide-react';
import PartArt from './part-art';
import CatalogSkeleton from './catalog-skeleton';
import CatalogCard from './catalog-card';
import CatalogFilterPanel, { ActiveCatalogFilters } from './catalog-filters';
import { catalogQuery, emptyFacets, emptyFilters, filterCount, matchesFilters, previewFacets } from '@/lib/catalog-filters';
import AuthModal from './auth-modal';
import AccountMenu from './account-menu';
import PartDetail from './part-detail';
import Basket from './basket';
import ClientRequests from './client-requests';
import PurchaseHistory from './purchase-history';
import Wishlist from './wishlist';
import { useWishlist } from '@/lib/use-wishlist';
import { recoverSentDraft } from '@/lib/basket-submissions';
import { trackUsage } from '@/lib/analytics';
import type { RequestSubmissionResult } from '@/lib/request-types';
import SupplierWorkspace from './supplier-workspace';
import Modal from './modal';
import { AppToolbar, usePageViewport } from './app-shell';
import { Account, BasketLine, CatalogFilters, CatalogPage, Offer, Page, Part, SessionUser, request, roleLabel } from '@/lib/types';
import { previewParts, previewOffers } from '@/lib/preview';

const categories = [
  { name: 'Todos los repuestos', value: '', Icon: Grid2X2 },
  { name: 'Frenos', value: 'FRENOS', Icon: Disc3 },
  { name: 'Filtros', value: 'FILTROS', Icon: Filter },
  { name: 'Encendido', value: 'ENCENDIDO', Icon: Zap },
  { name: 'Rodamientos', value: 'RODAMIENTOS', Icon: Settings2 },
];
const emptyPage = { count: 0, results: [], next: null, previous: null };
type CatalogSnapshot = { accountId: string; search: string; page: number; data: CatalogPage };

export default function Catálogo() {
  const viewport = usePageViewport();
  const [authenticated, setAuthenticated] = useState(false);
  const [user, setUser] = useState<SessionUser | null>(null);
  const [sessionReady, setSessionReady] = useState(false);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [accountId, setAccountId] = useState('');
  const [view, setView] = useState<'catalog' | 'inventory'>('catalog');
  const [authOpen, setAuthOpen] = useState(false);
  const [authMode, setAuthMode] = useState<'login' | 'signup'>('login');
  const [helpOpen, setHelpOpen] = useState(false);
  const [basketOpen, setBasketOpen] = useState(false);
  const [sentOpen, setSentOpen] = useState(false);
  const [wishlistOpen, setWishlistOpen] = useState(false);
  const [purchasesOpen, setPurchasesOpen] = useState(false);
  const [sentReceipt, setSentReceipt] = useState<{ accountId: string; receipt: RequestSubmissionResult } | null>(null);
  const [detail, setDetail] = useState<Part | null>(null);
  const [search, setSearch] = useState('');
  const [appliedSearch, setAppliedSearch] = useState('');
  const [filters, setFilters] = useState<CatalogFilters>(emptyFilters);
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [page, setPage] = useState(1);
  const [catalogSnapshot, setCatalogSnapshot] = useState<CatalogSnapshot | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [revision, setRevision] = useState(0);
  const [lines, setLines] = useState<BasketLine[]>([]);
  const [loadedBasketKey, setLoadedBasketKey] = useState('');
  const [listView, setListView] = useState(false);
  const searchRef = useRef<HTMLInputElement>(null);
  const catalogRef = useRef<HTMLElement>(null);
  const catalogControlsRef = useRef<HTMLDivElement>(null);
  const sessionGeneration = useRef(0);
  const lastTrackedSearch = useRef('');
  const account = accounts.find(a => a.id === accountId);
  const preview = !authenticated;
  const wishlist = useWishlist(user?.id, authenticated && accounts.length > 0);
  const hasCatalog = authenticated && catalogSnapshot?.accountId === accountId;
  const catalog = hasCatalog ? catalogSnapshot.data : emptyPage;
  const completedSearch = preview ? appliedSearch : hasCatalog ? catalogSnapshot.search : '';
  const initialLoading = !sessionReady || (authenticated && !!accountId && !hasCatalog && !error);
  const canSupply = account?.capabilities.includes('supplier') ?? false;
  const canQuote = preview || (account?.capabilities.includes('client') ?? false);
  const basketKey = `partsmall:basket:${preview ? 'preview' : accountId || 'unassigned'}`;
  const currentBasket = loadedBasketKey === basketKey ? lines : [];
  const activeBasketKey = useRef(basketKey);
  activeBasketKey.current = basketKey;

  function openAuth(mode: 'login' | 'signup' = 'login') { setAuthMode(mode); setAuthOpen(true); }

  const loadSession = useCallback(async () => {
    const generation = ++sessionGeneration.current;
    const session = await request<{ authenticated: boolean; accounts?: Page<Account>; user?: SessionUser }>('/api/session');
    const all = session.accounts?.results ?? [];
    if (session.accounts?.next) {
      let next = 2;
      let more = true;
      while (more) {
        const result = await request<Page<Account>>(`/api/market/accounts?page=${next++}`);
        all.push(...result.results);
        more = !!result.next;
      }
    }
    let savedAccount = '';
    try { if (session.user) savedAccount = localStorage.getItem(`motionpartes:active-account:${session.user.id}`) || ''; } catch { /* Account switching also works without browser storage. */ }
    if (generation !== sessionGeneration.current) return;
    const requestedAccount = new URL(window.location.href).searchParams.get('cuenta');
    setAuthenticated(session.authenticated);
    setUser(session.user ?? null);
    setAccounts(all);
    setAccountId(current => all.find(a => a.id === requestedAccount)?.id || (all.some(a => a.id === current) ? current : all.find(a => a.id === savedAccount)?.id || all[0]?.id || ''));
    setSessionReady(true);
  }, []);
  useEffect(() => { loadSession().catch(error => { setError(error.message); setSessionReady(true); }); }, [loadSession]);
  useEffect(() => {
    if (!authenticated || !user || user.is_superuser || !accountId) return;
    let lastRecorded = 0;
    const activity = () => {
      if (document.visibilityState !== 'visible' || Date.now() - lastRecorded < 60000) return;
      lastRecorded = Date.now(); trackUsage(user, accountId, { kind: 'visit' });
    };
    activity();
    document.addEventListener('visibilitychange', activity);
    document.addEventListener('pointerdown', activity);
    document.addEventListener('keydown', activity);
    window.addEventListener('focus', activity);
    return () => {
      document.removeEventListener('visibilitychange', activity);
      document.removeEventListener('pointerdown', activity);
      document.removeEventListener('keydown', activity);
      window.removeEventListener('focus', activity);
    };
  }, [authenticated, user?.id, user?.is_superuser, accountId]);
  useEffect(() => {
    const restorePage = () => { const destination = new URL(window.location.href).searchParams.get('vista'); setBasketOpen(destination === 'cesta'); setSentOpen(destination === 'solicitudes'); setWishlistOpen(destination === 'favoritos'); setPurchasesOpen(destination === 'compras'); setView(destination === 'proveedor' ? 'inventory' : 'catalog'); setDetail(null); };
    restorePage();
    window.addEventListener('popstate', restorePage);
    return () => window.removeEventListener('popstate', restorePage);
  }, []);
  useEffect(() => {
    if (search.trim() === appliedSearch) return;
    const timer = setTimeout(() => {setAppliedSearch(search.trim()); setPage(1);}, 350);
    return () => clearTimeout(timer);
  }, [search, appliedSearch]);
  useEffect(() => {
    try {
      const saved = JSON.parse(localStorage.getItem(basketKey) || '[]');
      let draft: BasketLine[] = Array.isArray(saved) ? saved.filter(line => line?.key && line?.part?.id && line?.offer?.supplier_id && Number.isInteger(line.quantity) && line.quantity > 0).map(line => ({...line, part: preview ? previewParts.find(part => part.id === line.part.id) || line.part : line.part, offer: preview ? previewOffers.find(offer => offer.supplier_id === line.offer.supplier_id) || line.offer : line.offer, quantity: Math.min(9999, line.quantity)})) : [];
      if (!preview && account?.capabilities.includes('client')) {
        try {
          const recovered = recoverSentDraft(account.id, draft); draft = recovered.lines;
          if (recovered.receipt) {
            setSentReceipt({ accountId: account.id, receipt: recovered.receipt });
            if (new URL(window.location.href).searchParams.get('vista') === 'cesta') setClientPage('solicitudes', true);
          }
        } catch { setNotice('No se pudo completar la limpieza del borrador. Tus solicitudes enviadas siguen guardadas en tu cuenta.'); }
      }
      setLines(draft);
    }
    catch { setLines([]); }
    setLoadedBasketKey(basketKey);
  }, [basketKey]);
  useEffect(() => { if (notice) { const timer = setTimeout(() => setNotice(''), 4500); return () => clearTimeout(timer); } }, [notice]);
  useEffect(() => {
    if (!authenticated || !accountId) {setCatalogSnapshot(null); setBusy(false); return;}
    let cancelled = false; setBusy(true); setError('');
    request<CatalogPage>(`/api/market/catalog?${catalogQuery(appliedSearch, page, filters)}`).then(data => { if (!cancelled) {
      setCatalogSnapshot({ accountId, search: appliedSearch, page, data });
      const searchKey = `${user?.id}:${accountId}:${appliedSearch}`;
      if (page === 1 && searchKey !== lastTrackedSearch.current) {
        lastTrackedSearch.current = searchKey;
        if (appliedSearch) trackUsage(user, accountId, { kind: 'search', term: appliedSearch });
      }
    } }).catch(error => { if (!cancelled) setError(error.message); }).finally(() => { if (!cancelled) setBusy(false); });
    return () => {cancelled = true;};
  }, [authenticated, accountId, appliedSearch, filters, page, revision, user?.id, user?.is_superuser]);
  const searchedPreview = previewParts.filter(part => `${part.sku} ${part.name} ${part.description} ${part.category} ${part.subcategory} ${part.codes.map(c => `${c.brand} ${c.code}`).join(' ')}`.toLocaleLowerCase('es').includes(appliedSearch.toLocaleLowerCase('es')));
  const visibleParts = preview ? searchedPreview.filter(part => matchesFilters(part, filters)) : catalog.results;
  const facets = preview ? previewFacets(searchedPreview, filters) : hasCatalog ? catalogSnapshot.data.facets || emptyFacets : emptyFacets;
  useLayoutEffect(() => {
    const section = catalogRef.current, controls = catalogControlsRef.current, scroller = viewport.current;
    if (!section || !controls || !scroller) return;
    const measure = () => {
      section.style.setProperty('--catalog-controls-height', `${Math.ceil(controls.getBoundingClientRect().height)}px`);
      section.style.setProperty('--page-toolbar-height', `${Math.ceil(scroller.getBoundingClientRect().top)}px`);
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(controls); observer.observe(scroller);
    return () => observer.disconnect();
  }, [basketOpen, sentOpen, wishlistOpen, purchasesOpen, view, sessionReady, viewport]);
  useLayoutEffect(() => {
    const section = catalogRef.current;
    const controls = catalogControlsRef.current;
    const scroller = viewport.current;
    if (!section || !controls || !scroller || !sessionReady || (authenticated && !hasCatalog)) return;
    const naturalTop = section.getBoundingClientRect().top - scroller.getBoundingClientRect().top + scroller.scrollTop + parseFloat(getComputedStyle(section).paddingTop) + parseFloat(getComputedStyle(controls).marginTop);
    // Keep pinned controls in place and show the first new result before the browser paints a shorter grid.
    if (scroller.scrollTop > naturalTop + 1) scroller.scrollTo({ top: naturalTop, behavior: 'instant' });
  }, [catalogSnapshot, preview, completedSearch, filters, viewport]);
  function updateBasket(next: BasketLine[]) {
    setLines(next);
    try { localStorage.setItem(basketKey, JSON.stringify(next)); } catch { setNotice('Tu navegador no pudo guardar el borrador. Mantén esta página abierta.'); }
  }
  function addToBasket(part: Part, offer: Offer, quantity: number) {
    if (loadedBasketKey !== basketKey || !Number.isFinite(quantity)) return;
    const units = Math.max(1, Math.min(9999, Math.trunc(quantity)));
    const key = `${part.id}:${offer.supplier_id}${offer.selected_item ? `:${offer.selected_item.id}` : ''}`;
    const existing = currentBasket.find(line => line.offer.supplier_id === offer.supplier_id &&
      (offer.selected_item ? line.offer.selected_item?.id === offer.selected_item.id || (!line.offer.selected_item && line.part.id === part.id) : line.key === key));
    updateBasket(existing ? currentBasket.map(line => line.key === existing.key ? {key, part, offer, quantity: Math.min(9999, line.quantity+units)} : line) : [...currentBasket, {key, part, offer, quantity: units}]);
    const added = existing ? Math.min(units, 9999 - existing.quantity) : units;
    if (!preview && offer.selected_item && added > 0) trackUsage(user, accountId, { kind: 'basket_add', part_id: part.id, supplier_item_id: offer.selected_item.id, quantity: added });
    setNotice(`${offer.selected_item?.codigo || part.sku} se agregó a tu cesta.`);
  }
  function openPart(part: Part) {
    setDetail(part);
    if (!preview) trackUsage(user, accountId, { kind: 'part_view', part_id: part.id });
  }
  async function toggleFavorite(part: Part) {
    if (!authenticated) { setDetail(null); openAuth(); return; }
    if (!accounts.length) { setNotice('Tu administrador debe asignarte una cuenta para guardar favoritos.'); return; }
    try {
      const saved = await wishlist.toggle(part.id);
      if (saved !== undefined) setNotice(saved ? `${part.sku} se guardó en tus favoritos.` : `${part.sku} se quitó de tus favoritos.`);
      return saved;
    } catch (cause) { setNotice(cause instanceof Error ? cause.message : 'No se pudieron actualizar tus favoritos. Inténtalo de nuevo.'); }
  }
  function changeFilters(next: CatalogFilters) { setFilters(next); setPage(1); }
  function selectCategory(value: string) { changeFilters({ ...filters, category: value ? [value] : [], subcategory: [] }); }
  function setClientPage(destination: 'cesta' | 'solicitudes' | 'favoritos' | 'compras' | null, replace = false) {
    const url = new URL(window.location.href);
    if (destination) url.searchParams.set('vista', destination); else url.searchParams.delete('vista');
    for (const key of ['seccion', 'cuenta', 'pestana', 'cliente']) url.searchParams.delete(key);
    const next = `${url.pathname}${url.search}${url.hash}`;
    if (next !== `${window.location.pathname}${window.location.search}${window.location.hash}`) window.history[replace ? 'replaceState' : 'pushState'](window.history.state, '', next);
    setBasketOpen(destination === 'cesta'); setSentOpen(destination === 'solicitudes'); setWishlistOpen(destination === 'favoritos');
    setPurchasesOpen(destination === 'compras');
    if (destination) { setDetail(null); viewport.current?.scrollTo({ top: 0, behavior: 'instant' }); }
  }
  function setBasketPage(open: boolean) { setClientPage(open ? 'cesta' : null); }
  function requestSent(receipt: RequestSubmissionResult, remaining: BasketLine[]) {
    if (activeBasketKey.current !== basketKey) return;
    setLines(remaining); setSentReceipt({ accountId, receipt }); setClientPage('solicitudes', true);
  }
  function explore() { setBasketPage(false); setView('catalog'); setTimeout(() => { document.getElementById('catalog')?.scrollIntoView({ behavior: 'smooth', block: 'start' }); searchRef.current?.focus({ preventScroll: true }); }, 20); }
  function selectAccount(id: string) {
    setAccountId(id); setView('catalog'); setDetail(null); setPage(1);
    const url = new URL(window.location.href);
    if (url.searchParams.get('vista') === 'proveedor') url.searchParams.delete('vista');
    for (const key of ['seccion', 'cuenta', 'pestana', 'cliente']) url.searchParams.delete(key);
    window.history.replaceState(window.history.state, '', `${url.pathname}${url.search}${url.hash}`);
    try { if (user) localStorage.setItem(`motionpartes:active-account:${user.id}`, id); } catch { /* The active account remains usable for this visit. */ }
  }
  async function logout() {
    sessionGeneration.current += 1;
    try { await request('/api/session', { method: 'DELETE' }); sessionGeneration.current += 1; setAuthenticated(false); setUser(null); setAccounts([]); setAccountId(''); setView('catalog'); setDetail(null); setBasketPage(false); setError(''); }
    catch (error) { setNotice(error instanceof Error ? error.message : 'No se pudo cerrar la sesión.'); }
  }
  const totalParts = preview ? visibleParts.length : catalog.count;
  return <>
    <AppToolbar>
    <header className="header"><div className="header-inner"><button className="wordmark" aria-label="Inicio de MotionPartes" onClick={() => {setBasketPage(false); setView('catalog'); setSearch(''); changeFilters(emptyFilters); viewport.current?.scrollTo({top: 0, behavior: 'smooth'});}}><BrandLogo/></button>
      <nav className="main-nav" aria-label="Navegación principal"><button className={!basketOpen && !sentOpen && !wishlistOpen && !purchasesOpen && view === 'catalog' ? 'active' : ''} onClick={explore}>Catálogo</button><button onClick={() => setHelpOpen(true)}>Cómo funciona<ArrowUpRight size={12}/></button><button className={!basketOpen && !sentOpen && !wishlistOpen && !purchasesOpen && view === 'inventory' ? 'active' : ''} onClick={() => { if (!authenticated) openAuth(); else if (canSupply) { setBasketPage(false); setView('inventory'); viewport.current?.scrollTo({ top: 0, behavior: 'instant' }); } else setNotice('Tu cuenta activa necesita un rol de proveedor para acceder al panel.'); }}>Para proveedores</button></nav>
      <div className="header-actions"><button className="basket-button wishlist-nav" onClick={() => setClientPage('favoritos')} aria-current={wishlistOpen ? 'page' : undefined} aria-label="Mis favoritos"><Heart size={20}/><span>Favoritos</span>{wishlist.ready && wishlist.count > 0 && <span className="wishlist-nav-count basket-count">{wishlist.count}</span>}</button>{authenticated && canQuote && <button className="basket-button request-history-button" onClick={() => setClientPage('solicitudes')} aria-current={sentOpen ? 'page' : undefined} aria-label="Mis solicitudes enviadas"><List size={20}/><span>Mis solicitudes</span></button>}<button className="basket-button" onClick={() => setBasketPage(true)} aria-current={basketOpen ? 'page' : undefined} aria-label={`Abrir cesta, ${currentBasket.length} ${currentBasket.length === 1 ? 'artículo' : 'artículos'}`}><ShoppingBag size={20}/><span>Cesta</span><span className="basket-count">{currentBasket.length}</span></button><AccountMenu user={user} ready={sessionReady} accounts={accounts} accountId={accountId} favoriteCount={wishlist.ready ? wishlist.count : 0} basketCount={currentBasket.length} onFavorites={() => setClientPage('favoritos')} onRequests={() => setClientPage('solicitudes')} onPurchases={() => setClientPage('compras')} onBasket={() => setBasketPage(true)} onSupplier={() => { setBasketPage(false); setView('inventory'); viewport.current?.scrollTo({ top: 0, behavior: 'instant' }); }} onSignIn={() => openAuth()} onInvitation={() => openAuth('signup')} onSwitchAccount={selectAccount} onLogout={logout}/></div>
    </div></header>
    </AppToolbar>
    {authenticated && <div className="account-bar section-container"><div><span className="live-dot"/><span>Cuenta conectada</span><label className="sr-only" htmlFor="active-account">Cuenta activa</label><select id="active-account" disabled={!accounts.length} value={accountId} onChange={event => selectAccount(event.target.value)}>{!accounts.length && <option value="">Sin cuenta asignada</option>}{accounts.map(account => <option key={account.id} value={account.id}>{account.name}</option>)}</select></div><div className="account-tools"><span className="account-roles">{account?.roles.map(role => roleLabel[role] || role).join(' · ')}</span>{user?.is_superuser && <Link className="button soft small" href="/administracion"><ShieldCheck size={15}/>Administración</Link>}</div></div>}
    {purchasesOpen ? <PurchaseHistory key={accountId} account={account} authenticated={authenticated} sessionReady={sessionReady} onCatalog={explore} onRequests={() => setClientPage('solicitudes')} onSignIn={() => openAuth()}/> : wishlistOpen ? <Wishlist key={user?.id || 'guest'} userId={user?.id} sessionReady={sessionReady} configured={accounts.length > 0} revision={wishlist.revision} pending={wishlist.pending} stateError={wishlist.error} onRefresh={() => { wishlist.refresh().catch(() => {}); }} onToggle={toggleFavorite} onOpen={openPart} onCatalog={explore} onSignIn={() => openAuth()}/> : sentOpen ? !sessionReady || loadedBasketKey !== basketKey ? <main className="section-container"><div className="loading" role="status"><LoaderCircle className="spin"/>Cargando tus solicitudes…</div></main> : <ClientRequests key={accountId} account={account} draftCount={currentBasket.length} onDraft={() => setBasketPage(true)} onCatalog={explore} receipt={sentReceipt?.accountId === accountId ? sentReceipt.receipt : undefined}/> : basketOpen ? !sessionReady || loadedBasketKey !== basketKey ? <main className="section-container"><div className="loading" role="status"><LoaderCircle className="spin"/>Cargando tu cesta…</div></main> : <Basket key={basketKey} lines={currentBasket} onChange={updateBasket} onClose={explore} preview={preview} account={account} onSent={requestSent} onSentView={() => setClientPage('solicitudes')}/> : view === 'inventory' && canSupply && account ? <SupplierWorkspace key={account.id} account={account}/> : <main>
      <section className="hero section-container"><div className="hero-content"><span className="hero-kicker"><span className="live-dot"/>CONECTAMOS REPUESTOS Y PROVEEDORES</span><h1>El repuesto indicado.<br/>El proveedor <span>ideal.</span></h1><p>Encuentra los repuestos que necesitas. Conecta con proveedores que los tienen y recibe una cotización para ti.</p><button className="button dark" onClick={explore}>Explorar el catálogo<ArrowDown size={17}/></button><div className="hero-proof"><span><ShieldCheck size={16}/>Un catálogo compartido</span><span><LockKeyhole size={15}/>Cotizaciones privadas</span></div></div>
        <div className="hero-visual"><div className="orbit orbit-one"/><div className="orbit orbit-two"/><div className="hero-visual-caption"><span>EL REPUESTO QUE MUEVE TU PROYECTO</span><span className="crosshair">+</span></div><PartArt name="brake disc" hero/><div className="floating-label"><span className="floating-icon"><Check size={15}/></span><div><strong>Tu repuesto. Tú eliges.</strong><small>Conecta con el proveedor indicado</small></div><ArrowUpRight size={17}/></div><span className="hero-part-label">01 / SISTEMA DE FRENOS</span></div>
      </section>
      <div className="value-strip section-container"><div><Boxes size={23}/><p><strong>Un catálogo que conecta.</strong><span>Repuestos de múltiples proveedores.</span></p></div><div><Store size={23}/><p><strong>Más opciones. Menos búsquedas.</strong><span>Elige a quién solicitar una cotización.</span></p></div><div><LockKeyhole size={23}/><p><strong>Cotizaciones entre tú y tu proveedor.</strong><span>Precios privados para tu solicitud.</span></p></div></div>
      <section ref={catalogRef} id="catalog" className="catalog section-container">
        <div ref={catalogControlsRef} className="catalog-sticky">
        <div className="section-heading"><div><span className="eyebrow">LOS REPUESTOS QUE BUSCAS</span><h2>Encuentra tu próximo repuesto.</h2></div><span className="catalog-label">{preview ? <><Sparkles size={14}/>Catálogo de ejemplo</> : <><span className="live-dot"/>Catálogo real</>}</span></div>
        <div className="search-toolbar"><div className="catalog-search"><Search size={21}/><label className="sr-only" htmlFor="catalog-search">Buscar repuestos, marcas o códigos</label><input ref={searchRef} id="catalog-search" value={search} onChange={event => setSearch(event.target.value)} autoComplete="off" placeholder="Busca un repuesto, marca o código…"/>{search && <button className="icon-button" aria-label="Limpiar búsqueda" onClick={() => setSearch('')}><X size={16}/></button>}<span className="search-hint">CÓDIGO / MARCA</span></div><button className="button filter-button catalog-filter-trigger" onClick={() => setFiltersOpen(true)} aria-haspopup="dialog" aria-label="Abrir filtros del catálogo" aria-expanded={filtersOpen}><SlidersHorizontal size={17}/>Filtros{filterCount(filters) > 0 && <span className="filter-trigger-count">{filterCount(filters)}</span>}</button></div>
        <div className="category-row" role="group" aria-label="Categorías de repuestos">{categories.map(({name, value, Icon}) => <button key={name} className={`category-chip ${value ? filters.category.length === 1 && filters.category[0] === value ? 'selected' : '' : !filters.category.length ? 'selected' : ''}`} aria-pressed={value ? filters.category.length === 1 && filters.category[0] === value : !filters.category.length} onClick={() => selectCategory(value)}><Icon size={16}/>{name}</button>)}<span className="category-tail"><Wrench size={15}/>Para cada reparación.</span></div>
        {preview && <div className="preview-notice"><span>Estás explorando un catálogo de ejemplo.</span><button onClick={() => openAuth()}>Inicia sesión para ver el inventario real<ArrowUpRight size={14}/></button></div>}
        {authenticated && !accounts.length && <div className="notice setup-notice"><UserRound size={22}/><div><strong>Estamos preparando tu cuenta.</strong><p>Tu administrador debe asignarte una cuenta y sus roles antes de que puedas consultar el inventario real.</p></div></div>}
        <ActiveCatalogFilters facets={facets} filters={filters} onChange={changeFilters}/>
        <div className="results-heading"><p><strong>{totalParts}</strong> {preview ? (totalParts === 1 ? 'repuesto de ejemplo' : 'repuestos de ejemplo') : (totalParts === 1 ? 'repuesto del catálogo' : 'repuestos del catálogo')}{completedSearch && <> para <strong>“{completedSearch}”</strong></>}</p><div><span className="catalog-update-status" role="status"><LoaderCircle size={12} className={busy ? 'spin' : 'catalog-progress-idle'} aria-hidden="true"/><span>{busy ? 'Actualizando…' : preview ? 'Vista de ejemplo' : 'Ordenados por SKU'}</span></span><div className="view-toggle"><button aria-label="Vista de cuadrícula" aria-pressed={!listView} className={!listView ? 'selected' : ''} onClick={() => setListView(false)}><Grid2X2 size={16}/></button><button aria-label="Vista de lista" aria-pressed={listView} className={listView ? 'selected' : ''} onClick={() => setListView(true)}><List size={18}/></button></div></div></div>
        </div>
        <div className="catalog-layout">
        <aside className="catalog-sidebar" aria-label="Filtros del catálogo"><CatalogFilterPanel facets={facets} filters={filters} loading={busy || initialLoading} onChange={changeFilters}/></aside>
        <div className="catalog-results-column">
        {error && <div className="notice error" role="alert">{error}<button onClick={() => {setRevision(revision+1); loadSession().catch(error => setError(error.message));}}>Intentar de nuevo</button></div>}
        <div className="catalog-results" role="region" aria-label="Resultados del catálogo" aria-busy={busy || initialLoading}>
        {initialLoading || (busy && !visibleParts.length) ? <><span className="sr-only" role="status">Cargando el catálogo…</span><div className={`parts-grid ${listView ? 'list-view' : ''}`} aria-hidden="true">{Array.from({length:6}, (_, index) => <div className="part-card skeleton-card" key={index}><CatalogSkeleton/></div>)}</div></> : visibleParts.length ? <div className={`parts-grid ${listView ? 'list-view' : ''}`}>{visibleParts.map(part => <CatalogCard key={part.id} part={part} preview={preview} busy={busy} onOpen={() => openPart(part)} saved={wishlist.ids.has(part.id)} saving={wishlist.pending.has(part.id)} onFavorite={() => toggleFavorite(part)}/>)}</div> : (!authenticated || accountId) && !error ? <div className="empty-state"><Search size={35}/><h3>{filterCount(filters) ? 'No encontramos repuestos con estos criterios.' : completedSearch ? 'No encontramos repuestos para esta búsqueda.' : 'Tu catálogo está listo para crecer.'}</h3><p>{filterCount(filters) ? 'Quita algún filtro o cambia tu búsqueda para ver más repuestos.' : completedSearch ? 'Prueba con otro nombre, marca o código de repuesto.' : 'Los repuestos aprobados aparecerán aquí cuando tu equipo los agregue.'}</p>{filterCount(filters) > 0 ? <button className="button primary" onClick={() => changeFilters(emptyFilters)}>Limpiar filtros<ArrowRight size={16}/></button> : completedSearch && <button className="button primary" onClick={() => setSearch('')}>Limpiar búsqueda<ArrowRight size={16}/></button>}</div> : null}
        {authenticated && catalog.count > 0 && <div className="pagination"><span>{catalog.count} repuestos</span><div><button className="button soft small" onClick={() => setPage(page-1)} disabled={!catalog.previous || busy}>Anterior</button><span>Página {catalogSnapshot?.page ?? page}</span><button className="button soft small" onClick={() => setPage(page+1)} disabled={!catalog.next || busy}>Siguiente</button></div></div>}
        </div>
        </div>
        </div>
      </section>
      <section className="supplier-cta section-container"><div className="cta-illustration"><Boxes size={58} strokeWidth={1}/><span className="crosshair">+</span></div><div><span className="eyebrow">PARA PROVEEDORES DE REPUESTOS</span><h2>Tu inventario. Más oportunidades.</h2><p>Publica tu inventario en MotionPartes y conecta con clientes que buscan lo que tienes.</p></div><button className="button dark" onClick={() => { if (canSupply) {setView('inventory'); viewport.current?.scrollTo({top: 0, behavior: 'smooth'});} else if (!authenticated) openAuth(); else setNotice('Solicita a tu administrador que habilite un rol de proveedor en tu cuenta.'); }}>{canSupply ? 'Abrir tu panel' : 'Acceder al panel de proveedores'}<ArrowUpRight size={17}/></button></section>
    </main>}
    <footer className="footer section-container"><div className="footer-brand"><BrandLogo/><p>Conectamos repuestos. Conectamos personas.</p></div><div className="footer-links"><button onClick={explore}>Catálogo</button><button onClick={() => setHelpOpen(true)}>Cómo funciona</button>{user?.is_superuser && <Link href="/administracion">Administración<ArrowUpRight size={12}/></Link>}</div><span className="footer-caption"><Globe2 size={15}/>Para tu próxima reparación.</span></footer>
    {filtersOpen && <Modal title="Filtrar repuestos" drawer className="catalog-filter-drawer" onClose={() => setFiltersOpen(false)}><CatalogFilterPanel facets={facets} filters={filters} loading={busy || initialLoading} onChange={changeFilters}/><div className="filter-drawer-footer"><button className="button primary" onClick={() => setFiltersOpen(false)}>{busy ? 'Ver resultados · Actualizando…' : `Ver ${totalParts.toLocaleString('es')} ${totalParts === 1 ? 'repuesto' : 'repuestos'}`}<ArrowRight size={16}/></button></div></Modal>}
    {authOpen && <AuthModal initialMode={authMode} onClose={() => setAuthOpen(false)} onSuccess={async () => {setSearch(''); changeFilters(emptyFilters); setView('catalog'); await loadSession();}}/>}
    {detail && <PartDetail key={`${accountId}:${detail.id}`} part={detail} preview={preview} canQuote={canQuote} accountId={accountId} basketLines={currentBasket} onClose={() => setDetail(null)} onAdd={(offer, quantity) => addToBasket(detail, offer, quantity)} onBasket={() => setBasketPage(true)} saved={wishlist.ids.has(detail.id)} saving={wishlist.pending.has(detail.id)} onFavorite={() => toggleFavorite(detail)}/>}
    {helpOpen && <Modal title="Una forma más fácil de encontrar repuestos" onClose={() => setHelpOpen(false)}><div className="how-steps">{[{Icon: Search, title: 'Encuentra tu repuesto', text: 'Busca en el catálogo compartido por nombre, marca o código.'}, {Icon: Store, title: 'Elige tus proveedores', text: 'Consulta quién tiene existencias y agrega repuestos a tu cesta.'}, {Icon: LockKeyhole, title: 'Solicita una cotización privada', text: 'Cada proveedor cotizará sus propios repuestos. Los precios serán privados.'}, {Icon: Truck, title: 'Confirma tu acuerdo', text: 'Revisa la cotización, solicita ajustes si los necesitas y confirma las condiciones con el proveedor.'}].map(({Icon, title, text}, i) => <div key={title}><span className="round-icon"><Icon size={21}/></span><div><small>0{i+1}</small><h3>{title}</h3><p>{text}</p></div></div>)}</div><div className="notice">Envía tu cesta para crear órdenes por proveedor. El proveedor revisa y cotiza; tú confirmas o solicitas ajustes. Cuando ambas partes confirman, el acuerdo queda cerrado y aparece en tu historial.</div><p className="private-note">Estadísticas para mejorar el catálogo: al iniciar sesión registramos tus visitas, búsquedas, repuestos consultados y artículos agregados a la cesta. Solo los superusuarios pueden consultar estas estadísticas; no usamos rastreadores externos.</p></Modal>}
    {notice && <div className="toast" role="status"><Check size={17}/>{notice}<button aria-label="Cerrar notificación" onClick={() => setNotice('')}><X size={14}/></button></div>}
  </>;
}
