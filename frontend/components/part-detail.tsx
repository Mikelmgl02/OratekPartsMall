'use client';

import { referenceLabel, visibleReferences } from '@/lib/reference-label';
import { useEffect, useState } from 'react';
import { ArrowUpRight, Check, CheckCircle2, LoaderCircle, LockKeyhole, Package, Store } from 'lucide-react';
import Modal from './modal';
import PartGallery from './part-image';
import PartTechnical from './part-technical';
import StockStatus from './stock-status';
import FavoriteButton from './favorite-button';
import PartRequestStates, { ItemRequestStates } from './part-request-states';
import { BasketLine, Offer, Part, request } from '@/lib/types';
import type { ClientPartRequestState } from '@/lib/request-types';
import { previewOffers } from '@/lib/preview';
export default function PartDetail({ part, preview, canQuote, accountId, basketLines, onClose, onAdd, onBasket, saved, saving, onFavorite }: { part: Part; preview: boolean; canQuote: boolean; accountId: string; basketLines: BasketLine[]; onClose: () => void; onAdd: (offer: Offer, quantity: number) => void; onBasket: () => void; saved: boolean; saving: boolean; onFavorite: () => void }) {
  const [offers, setOffers] = useState<Offer[]>([]);
  const [busy, setBusy] = useState(!preview && canQuote);
  const [error, setError] = useState('');
  const [quantity, setQuantity] = useState(1);
  const [retry, setRetry] = useState(0);
  const [requestState, setRequestState] = useState<ClientPartRequestState>();
  const [stateLoading, setStateLoading] = useState(!preview && canQuote);
  const [stateError, setStateError] = useState('');
  const [stateRetry, setStateRetry] = useState(0);
  const draftQuantity = basketLines.filter(line => line.part.id === part.id).reduce((sum,line) => sum + line.quantity, 0);
  function quantityInBasket(offer: Offer, itemId?: string, codigo?: string, brand?: string) {
    return basketLines.filter(line => line.part.id === part.id && line.offer.supplier_id === offer.supplier_id &&
      (itemId ? line.offer.selected_item?.id === itemId && line.offer.selected_item.codigo === codigo && line.offer.selected_item.brand === brand : !line.offer.selected_item))
      .reduce((sum,line) => sum + line.quantity, 0);
  }
  useEffect(() => {
    if (preview || !canQuote || !accountId) return;
    const controller = new AbortController();
    setStateLoading(true); setStateError(''); setRequestState(undefined);
    request<ClientPartRequestState>(`/api/market/accounts/${accountId}/catalog/${part.id}/request-state`, {signal:controller.signal})
      .then(data => {if (!controller.signal.aborted && data.part_id === part.id) setRequestState(data);})
      .catch(error => {if (!controller.signal.aborted) setStateError(error.message);})
      .finally(() => {if (!controller.signal.aborted) setStateLoading(false);});
    return () => controller.abort();
  }, [part.id, accountId, preview, canQuote, stateRetry]);
  useEffect(() => {
    let cancelled = false;
    if (preview) { setOffers(previewOffers); return; }
    if (!canQuote) return;
    setBusy(true); setError('');
    request<Offer[]>(`/api/market/catalog/${part.id}/suppliers`).then(data => { if (!cancelled) setOffers(data); }).catch(error => { if (!cancelled) setError(error.message); }).finally(() => { if (!cancelled) setBusy(false); });
    return () => { cancelled = true; };
  }, [part.id, preview, canQuote, retry]);
  return <Modal title="Detalles del repuesto" onClose={onClose} wide className="product-modal">
    <div className="product-page">
    <div className="product-media"><PartGallery key={part.id} part={part} preview={preview}/></div>
    <div className="product-info">
    <div className="detail-favorite"><FavoriteButton sku={part.sku} saved={saved} busy={saving} onToggle={onFavorite}/></div>
    <div className="detail-copy"><span className="eyebrow">SKU INTERNO</span><h3>{part.sku}</h3><StockStatus availability={part.availability} preview={preview}/><dl className="part-classification detail-classification"><div><dt>GRUPO</dt><dd>{part.category || 'SIN CLASIFICAR'}</dd></div><div><dt>SUBGRUPO</dt><dd>{part.subcategory || 'SIN CLASIFICAR'}</dd></div></dl>{part.name && part.name !== part.sku && <h4>{part.name}</h4>}<div className="code-list" aria-label="Alternos">{visibleReferences(part).map(code => <span key={`${code.brand}:${code.code}`}>{code.code} · {referenceLabel(code)}</span>)}</div><p>{part.description || 'Este SKU agrupa repuestos idénticos con distintos códigos y marcas. Consulta al proveedor para confirmar su aplicación en tu vehículo.'}</p><div className="private-note"><LockKeyhole size={16}/>El precio se proporciona en una cotización privada.</div></div>
    {!preview && <PartTechnical partId={part.id}/>}
    {canQuote && <PartRequestStates draftQuantity={draftQuantity} history={requestState?.totals} loading={stateLoading} preview={preview} error={stateError} onRetry={() => setStateRetry(stateRetry + 1)}/>}
    </div>
    <div className="product-buybox">
    <div className="detail-supplier-heading"><div><h3>Elige tu proveedor</h3><p>{preview ? 'Proveedores de ejemplo para esta vista.' : 'Artículos disponibles que corresponden a este SKU, con sus códigos y marcas.'}</p></div><label className="quantity-field">Cantidad<input aria-label="Cantidad solicitada" type="number" min={1} max={9999} step={1} autoComplete="off" value={quantity} onChange={event => setQuantity(Math.max(1, Math.min(9999, Math.trunc(Number(event.target.value) || 1))))}/></label></div>
    {busy && <div className="loading"><LoaderCircle className="spin" size={20}/>Buscando proveedores…</div>}
    {error && <div className="notice error" role="alert">{error}<button onClick={() => setRetry(retry+1)}>Intentar de nuevo</button></div>}
    {!preview && !canQuote && <div className="notice"><Store size={18}/>Tu cuenta activa necesita un rol de cliente para consultar las existencias de los proveedores.</div>}
    {!busy && !error && canQuote && offers.length === 0 && <div className="empty-state compact"><Package size={28}/><h3>Aún no hay proveedores con existencias disponibles.</h3><p>Este repuesto sigue en el catálogo. Consulta de nuevo cuando los proveedores actualicen su inventario.</p></div>}
    <div className="supplier-options">{offers.map(offer => {
      const genericDraft = quantityInBasket(offer);
      return <div key={offer.supplier_id} className={offer.items?.length ? 'sku-supplier-group' : ''}>
        <div className="supplier-option"><span className="supplier-avatar"><Store size={21}/></span><div><h4>{offer.supplier_name}</h4><p><CheckCircle2 size={13}/>Disponible <span className="dot-separator">·</span> Sin reseñas todavía</p>{!offer.items?.length && <ItemRequestStates draftQuantity={genericDraft}/>}</div>{!offer.items?.length && <div className="supplier-item-actions">{genericDraft > 0 ? <><button className="button soft" onClick={onBasket}><Check size={16}/>Agregado</button><button className="button text small" aria-label={`Agregar más de ${offer.supplier_name}`} onClick={() => onAdd(offer, quantity)}>Agregar más<ArrowUpRight size={14}/></button></> : <button className="button primary" onClick={() => onAdd(offer, quantity)}>Agregar a la cesta<ArrowUpRight size={16}/></button>}</div>}</div>
        {offer.items?.map(item => {
          const draft = quantityInBasket(offer, item.id, item.codigo, item.brand);
          const history = requestState?.items.find(row => row.supplier_item_id === item.id && row.supplier_id === offer.supplier_id && row.codigo === item.codigo && row.brand === item.brand);
          return <div className="sku-supplier-item" role="group" aria-label={`Artículo ${item.codigo} · ${item.brand || 'Sin marca'}`} key={item.id}><div><strong>{item.codigo}</strong><span>{item.brand || 'Sin marca indicada'}</span>{item.description && <p>{item.description}</p>}<ItemRequestStates draftQuantity={draft} history={history}/></div><div className="supplier-item-actions">{draft > 0 ? <><button className="button soft small" aria-label={`${item.codigo} agregado, ${draft} en cesta`} onClick={onBasket}><Check size={16}/>Agregado · {draft}</button><button className="button text small" aria-label={`Agregar más ${item.codigo} de ${offer.supplier_name}`} onClick={() => onAdd({...offer, selected_item:item}, quantity)}>Agregar más<ArrowUpRight size={14}/></button></> : <button className="button primary small" aria-label={`Agregar ${item.codigo} de ${offer.supplier_name} a la cesta`} onClick={() => onAdd({...offer, selected_item:item}, quantity)}>Agregar a la cesta<ArrowUpRight size={16}/></button>}</div></div>;
        })}
      </div>;
    })}</div>
    </div>
    </div>
  </Modal>;
}
