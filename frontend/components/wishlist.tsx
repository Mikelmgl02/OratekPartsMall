'use client';
import { useEffect, useRef, useState } from 'react';
import { ArrowLeft, ArrowRight, Heart, Search, UserRound, X } from 'lucide-react';
import CatalogCard from './catalog-card';
import CatalogSkeleton from './catalog-skeleton';
import { Page, Part, RequestError, WishlistEntry, request } from '@/lib/types';

type Props = {
  userId?: number; sessionReady: boolean; configured: boolean; revision: number;
  pending: Set<string>; stateError: string; onRefresh: () => void;
  onToggle: (part: Part) => Promise<boolean | undefined>; onOpen: (part: Part) => void;
  onCatalog: () => void; onSignIn: () => void;
};

export default function Wishlist({ userId, sessionReady, configured, revision, pending, stateError, onRefresh, onToggle, onOpen, onCatalog, onSignIn }: Props) {
  const [search, setSearch] = useState('');
  const [appliedSearch, setAppliedSearch] = useState('');
  const [page, setPage] = useState(1);
  const [data, setData] = useState<Page<WishlistEntry>>();
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState('');
  const [retry, setRetry] = useState(0);
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => { heading.current?.focus({ preventScroll: true }); }, []);
  useEffect(() => {
    if (search.trim() === appliedSearch) return;
    const timer = setTimeout(() => { setAppliedSearch(search.trim()); setPage(1); }, 350);
    return () => clearTimeout(timer);
  }, [search, appliedSearch]);
  useEffect(() => {
    if (!userId || !configured) return;
    const controller = new AbortController();
    setBusy(true); setError('');
    const query = new URLSearchParams({ page: String(page), search: appliedSearch });
    request<Page<WishlistEntry>>(`/api/market/wishlist?${query}`, { signal: controller.signal })
      .then(result => {
        if (controller.signal.aborted) return;
        if (!result.results.length && page > 1) { setPage(page - 1); return; }
        setData(result);
      })
      .catch(cause => {
        if (controller.signal.aborted) return;
        // Another view/device may remove the last item on the current page.
        if (cause instanceof RequestError && cause.status === 404 && page > 1) setPage(value => value === page ? page - 1 : value);
        else setError(cause.message);
      })
      .finally(() => { if (!controller.signal.aborted) setBusy(false); });
    return () => controller.abort();
  }, [userId, configured, appliedSearch, page, revision, retry]);

  async function remove(part: Part) {
    const saved = await onToggle(part);
    if (saved === false && page > 1 && data?.results.length === 1) setPage(value => value === page ? page - 1 : value);
  }

  return <main className="basket-page wishlist-page" aria-label="Mis favoritos">
    <div className="section-container basket-page-container">
      <button className="basket-page-back" onClick={onCatalog}><ArrowLeft size={16}/>Volver al catálogo</button>
      <div className="basket-page-heading"><div><span className="eyebrow">TU LISTA PERSONAL</span><h1 ref={heading} tabIndex={-1}>Mis favoritos</h1><p>Guarda los repuestos que te interesan y elige un proveedor cuando los necesites.</p></div><span className="wishlist-heading-icon"><Heart size={27}/></span></div>
      {!sessionReady ? <div className="loading" role="status">Cargando tus favoritos…</div> : !userId ? <div className="wishlist-empty"><span><Heart size={32}/></span><h2>Tus próximos repuestos, en un solo lugar.</h2><p>Inicia sesión para guardar tu lista y consultarla desde cualquier dispositivo.</p><button className="button dark" onClick={onSignIn}><UserRound size={16}/>Iniciar sesión<ArrowRight size={16}/></button></div> : !configured ? <div className="notice">Tu administrador debe asignarte una cuenta para acceder al catálogo y guardar favoritos.</div> : <>
        <div className="wishlist-controls"><label className="wishlist-search"><Search size={18} aria-hidden="true"/><input aria-label="Buscar en mis favoritos" autoComplete="off" spellCheck={false} value={search} placeholder="BUSCAR SKU, DESCRIPCIÓN O ALTERNO…" onChange={event => setSearch(event.target.value.toUpperCase())}/>{search && <button className="icon-button" aria-label="Limpiar búsqueda de favoritos" onClick={() => setSearch('')}><X size={16}/></button>}</label><span aria-live="polite">{data ? `${data.count} ${data.count === 1 ? 'repuesto guardado' : 'repuestos guardados'}${appliedSearch ? ' en esta búsqueda' : ''}` : 'Cargando lista…'}</span></div>
        <p className="wishlist-note">Esta lista es tuya y se mantiene al cambiar de cuenta. Guardar un repuesto no reserva existencias.</p>
        {(error || stateError) && <div className="notice error" role="alert">{error || stateError}<button onClick={() => { setRetry(value => value + 1); onRefresh(); }}>Intentar de nuevo</button></div>}
        <section aria-label="Repuestos favoritos" aria-busy={busy}>
          {!data && busy ? <><span className="sr-only" role="status">Cargando repuestos favoritos…</span><div className="parts-grid" aria-hidden="true">{Array.from({length: 3}, (_, index) => <div className="part-card skeleton-card" key={index}><CatalogSkeleton/></div>)}</div></> : data?.results.length ? <div className="parts-grid">{data.results.map(entry => <CatalogCard key={entry.part.id} part={entry.part} preview={false} busy={busy} saved saving={pending.has(entry.part.id)} onFavorite={() => { void remove(entry.part); }} onOpen={() => onOpen(entry.part)} unavailable={!entry.available}/>)}</div> : !busy && !error ? <div className="wishlist-empty"><span>{appliedSearch ? <Search size={32}/> : <Heart size={32}/>}</span><h2>{appliedSearch ? 'No encontramos ese repuesto en tus favoritos.' : 'Aún no tienes repuestos guardados.'}</h2><p>{appliedSearch ? 'Prueba con otro código o descripción.' : 'Toca el corazón de cualquier repuesto del catálogo para guardarlo aquí.'}</p><button className="button primary" onClick={appliedSearch ? () => setSearch('') : onCatalog}>{appliedSearch ? 'Limpiar búsqueda' : 'Explorar el catálogo'}<ArrowRight size={16}/></button></div> : null}
        </section>
        {!!data?.count && <div className="pagination"><span>Más recientes primero</span><div><button className="button soft small" disabled={!data.previous || busy} onClick={() => setPage(value => value - 1)}>Anterior</button><span>Página {page}</span><button className="button soft small" disabled={!data.next || busy} onClick={() => setPage(value => value + 1)}>Siguiente</button></div></div>}
      </>}
    </div>
  </main>;
}
