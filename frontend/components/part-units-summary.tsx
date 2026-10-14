'use client';
import { useEffect, useRef, useState } from 'react';
import { ArrowRight, ArrowUpRight, LoaderCircle, MessageCircle, PackageSearch, Repeat2, Trash2, TriangleAlert, X } from 'lucide-react';
import Modal from './modal';
import DealStatusBadge from './deal-status';
import QuantityInput from './quantity-input';
import { stockHelp, stockLabel, stockOf } from '@/lib/basket-stock';
import type { DealStatus } from '@/lib/deal-types';
import { previewOffers } from '@/lib/preview';
import { request, type Account, type BasketLine, type Offer, type Part } from '@/lib/types';

// What a catalog card's "N en cesta" and "N en órdenes" stand for (catalog-card.tsx), opened from them: the cesta's lines of the SKU, one per
// supplier article, with its Disponible and quantity (each editable, removable or swapped for another supplier or article), and the open orders that hold it (GET open-units/<part>: mall ClientOpenUnitOrders),
// each with its stage and units, to open. Each leads on to the whole cesta or the orders.
export type OpenUnitOrder = { id: string; reference: string; status: DealStatus; created_at: string; supplier: { id: string; name: string };
  units: number; requested: number; lines: { codigo: string; brand: string; requested: number; units: number }[] };

const units = (value: number) => `${value.toLocaleString('es-PA')} ${value === 1 ? 'unidad' : 'unidades'}`;
const day = (value: string) => new Date(value).toLocaleDateString('es-PA', { timeZone: 'America/Panama', dateStyle: 'medium' });
const skuOf = (part: Part) => part.name && part.name !== part.sku ? `${part.name} · ${part.sku}` : part.sku;

// The cesta's lines of the SKU, each one editable here: its quantity, Quitar, or Cambiar to another supplier or article of the same SKU (the
// part's offers, as its detail lists them; the account's own never).
// basket: the whole cesta (a chosen option may already be a line of it, even under another SKU); notice: what the last change said, shown here
// since the dialog covers the page's.
export function BasketSummary({ part, lines, basket, preview, accountId, notice, onNoticeClose, onQuantity, onRemove, onReplace, onClose, onBasket }: {
  part: Part; lines: BasketLine[]; basket: BasketLine[]; preview: boolean; accountId: string; notice?: string; onNoticeClose?: () => void;
  onQuantity: (key: string, quantity: number) => void; onRemove: (key: string) => void; onReplace: (key: string, offer: Offer) => void; onClose: () => void; onBasket: () => void;
}) {
  const [changing, setChanging] = useState<string | null>(null);
  // After Quitar or Elegir the row (or its button) goes away: the keyboard goes on from the row the change left (by supplier and article,
  // which outlive the line's key), or else from Cerrar.
  const list = useRef<HTMLUListElement>(null), closeButton = useRef<HTMLButtonElement>(null), focusNext = useRef<string | null>(null);
  const lineId = (line: BasketLine) => `${line.offer.supplier_id}:${line.offer.selected_item?.id ?? ''}`;
  useEffect(() => {
    if (focusNext.current === null) return;
    const wanted = focusNext.current;
    const target = wanted ? [...list.current?.querySelectorAll<HTMLElement>('li[data-line]') ?? []].find(row => row.dataset.line === wanted) : undefined;
    (target?.querySelector<HTMLElement>('.part-units-line-actions button') ?? closeButton.current)?.focus();
    focusNext.current = null;
  }, [lines]);
  const [offers, setOffers] = useState<Offer[] | null>(null);
  const [offersError, setOffersError] = useState('');
  const total = lines.reduce((sum, line) => sum + line.quantity, 0);
  const suppliers = new Set(lines.map(line => line.offer.supplier_id)).size;
  const sorted = [...lines].sort((a, b) => a.offer.supplier_name.localeCompare(b.offer.supplier_name, 'es'));
  // The options are read once, the first time Cambiar opens.
  useEffect(() => {
    if (!changing || offers) return;
    if (preview) { setOffers(previewOffers); return; }
    let current = true;
    request<Offer[]>(`/api/market/catalog/${part.id}/suppliers`).then(data => { if (current) { setOffersError(''); setOffers(Array.isArray(data) ? data : []); } })
      .catch(caught => { if (current) setOffersError(caught instanceof Error ? caught.message : 'No se pudieron cargar los proveedores.'); });
    return () => { current = false; };
  }, [changing, offers, preview, part.id]);
  const nameOf = (line: BasketLine) => `${line.offer.selected_item?.codigo || part.sku} de ${line.offer.supplier_name}`;
  return <Modal title={`En tu cesta · ${skuOf(part)}`} className="part-units-summary" onClose={onClose}>
    {!lines.length ? <p className="part-units-lead">Ya no tienes este repuesto en tu cesta.</p> : <>
      <p className="part-units-lead">{units(total)} en tu cesta, {suppliers === 1 ? 'con 1 proveedor' : `con ${suppliers} proveedores`}. Cada proveedor cotiza lo suyo por separado.</p>
      <ul ref={list} className="part-units-list" aria-label="Artículos en tu cesta">{sorted.map((line, index) => {
        const item = line.offer.selected_item, open = changing === line.key;
        // Everything else this SKU can be asked for: the other articles and suppliers, never the account's own.
        const options = (offers || []).filter(offer => preview || offer.supplier_id !== accountId).flatMap(offer => offer.items?.length
          ? offer.items.map(option => ({ offer: { ...offer, selected_item: option }, id: `${offer.supplier_id}:${option.id}`, codigo: option.codigo, brand: option.brand }))
          : preview ? [{ offer, id: offer.supplier_id, codigo: part.sku, brand: '' }] : [])
          .filter(option => !(option.offer.supplier_id === line.offer.supplier_id && (option.offer.selected_item?.id ?? null) === (item?.id ?? null)));
        const after = sorted[index + 1] ?? sorted[index - 1];
        return <li key={line.key} data-line={lineId(line)} className={open ? 'changing' : undefined}>
          <div className="part-units-main">
            <strong>{line.offer.supplier_name}</strong>
            <small>{item ? `${item.codigo} · ${item.brand || 'SIN MARCA INDICADA'}` : part.sku}</small>
            {line.source === 'whatsapp' && <span className="basket-page-source"><MessageCircle size={11}/>Agregado por WhatsApp</span>}
            {line.source === 'faltante' && <span className="basket-page-source missing"><PackageSearch size={11}/>Lo que faltó en otra orden</span>}
            {line.available === false ? <span className="basket-page-stale" role="note"><TriangleAlert size={13}/>Ya no se puede solicitar.</span>
              : line.changed ? <span className="basket-page-stale" role="note"><TriangleAlert size={13}/>Cambió desde que lo agregaste.</span> : null}
          </div>
          <span className={`basket-grid-stock ${stockOf(line)}`} title={stockHelp}>{stockLabel(line)}</span>
          <QuantityInput name={nameOf(line)} quantity={line.quantity} onChange={quantity => onQuantity(line.key, quantity)} className="basket-grid-number part-units-quantity"/>
          <div className="part-units-line-actions">
            <button type="button" className="button text small" aria-expanded={open} aria-label={`Cambiar ${nameOf(line)}`} onClick={() => { setOffersError(''); setChanging(open ? null : line.key); }}><Repeat2 size={14}/>Cambiar</button>
            <button type="button" className="button text small part-units-remove" aria-label={`Quitar ${nameOf(line)} de la cesta`} onClick={() => { if (open) setChanging(null); focusNext.current = after ? lineId(after) : ''; onRemove(line.key); }}><Trash2 size={14}/>Quitar</button>
          </div>
          {open && <div className="part-units-options" role="group" aria-label={`Otras opciones para ${nameOf(line)}`}>
            {offersError ? <div className="notice error" role="alert">{offersError}</div> : !offers ? <div className="loading" role="status"><LoaderCircle className="spin"/>Cargando proveedores…</div>
              : !options.length ? <p>No hay otro proveedor ni otro artículo para este repuesto.</p>
              : <>
                <p>Cambia este artículo por otro proveedor o variante del mismo repuesto. Sus {units(line.quantity)} pasan al elegido.</p>
                <ul>{options.map(option => <li key={option.id}>
                  <div><strong>{option.offer.supplier_name}</strong><small>{option.codigo} · {option.brand || 'SIN MARCA INDICADA'}</small>
                    {(() => {
                      const into = basket.find(other => other.offer.supplier_id === option.offer.supplier_id && other.offer.selected_item?.id === option.offer.selected_item?.id);
                      return into && <small className="part-units-merge">{(into.catalog_part_id || into.part.id) === part.id ? 'Ya está en tu cesta: las cantidades se suman'
                        : `Ya está en tu cesta como ${into.part.sku}: las cantidades se suman allí`}</small>;
                    })()}</div>
                  <span className={`part-units-option-stock ${option.offer.in_stock ? 'in' : 'out'}`}>{option.offer.in_stock ? 'Con existencias' : 'Sin existencias'}</span>
                  <button type="button" className="button soft small" aria-label={`Elegir ${option.codigo} de ${option.offer.supplier_name}`}
                    onClick={() => { setChanging(null); focusNext.current = `${option.offer.supplier_id}:${option.offer.selected_item?.id ?? ''}`; onReplace(line.key, option.offer); }}>Elegir</button>
                </li>)}</ul>
              </>}
          </div>}
        </li>;
      })}</ul>
    </>}
    {notice && <div className="notice part-units-notice" role="status"><span>{notice}</span>{onNoticeClose && <button type="button" className="icon-button" aria-label="Cerrar notificación" onClick={onNoticeClose}><X size={14}/></button>}</div>}
    <div className="part-units-actions">
      <button ref={closeButton} type="button" className="button soft" onClick={onClose}>Cerrar</button>
      <button type="button" className="button primary" onClick={onBasket}>Ir a la cesta<ArrowRight size={16}/></button>
    </div>
  </Modal>;
}

export function OrdersSummary({ part, account, onClose, onOpenOrder, onOrders }: {
  part: Part; account: Account; onClose: () => void; onOpenOrder: (order: OpenUnitOrder) => void; onOrders: () => void;
}) {
  const [orders, setOrders] = useState<OpenUnitOrder[] | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    let current = true;
    request<{ orders: OpenUnitOrder[] }>(`/api/market/accounts/${account.id}/open-units/${part.id}`)
      .then(data => { if (current) setOrders(Array.isArray(data?.orders) ? data.orders : []); })
      .catch(caught => { if (current) { setOrders([]); setError(caught instanceof Error ? caught.message : 'No se pudieron cargar tus órdenes.'); } });
    return () => { current = false; };
  }, [account.id, part.id]);
  const total = (orders || []).reduce((sum, order) => sum + order.units, 0);
  return <Modal title={`En órdenes · ${skuOf(part)}`} className="part-units-summary" onClose={onClose}>
    {!orders ? <div className="loading" role="status"><LoaderCircle className="spin"/>Cargando tus órdenes…</div> : <>
      {error && <div className="notice error" role="alert">{error}</div>}
      {!error && <p className="part-units-lead">{orders.length ? `${units(total)} en ${orders.length === 1 ? '1 orden abierta' : `${orders.length} órdenes abiertas`}: enviadas y aún sin recibir.`
        : 'Ya no tienes órdenes abiertas con este repuesto.'}</p>}
      {orders.length > 0 && <ul className="part-units-list" aria-label="Órdenes abiertas con este repuesto">{orders.map(order => <li key={order.id}>
        <div className="part-units-main">
          <button type="button" className="button text small client-activity-order" aria-label={`Abrir orden ${order.reference}`} onClick={() => onOpenOrder(order)}>{order.reference}<ArrowUpRight size={13}/></button>
          <small>{order.supplier.name} · enviada {day(order.created_at)}</small>
          {order.lines.length > 1 && <small className="part-units-lines">{order.lines.map(line => `${line.codigo}${line.brand ? ` (${line.brand})` : ''}: ${line.units}`).join(' · ')}</small>}
        </div>
        <DealStatusBadge status={order.status}/>
        <span className="part-units-count">{units(order.units)}{order.units !== order.requested && <> <small>de {order.requested.toLocaleString('es-PA')} pedidas</small></>}</span>
      </li>)}</ul>}
    </>}
    <div className="part-units-actions">
      <button type="button" className="button soft" onClick={onClose}>Cerrar</button>
      <button type="button" className="button primary" onClick={onOrders}>Ver Cotizaciones<ArrowRight size={16}/></button>
    </div>
  </Modal>;
}
