'use client';

import { useEffect, useRef, useState } from 'react';
import { ArrowLeft, ArrowRight, Boxes, LockKeyhole, Minus, Plus, ShoppingBag, Sparkles, Store, Trash2, Truck } from 'lucide-react';
import { CatalogPhoto } from './part-image';
import BasketSubmission from './basket-submission';
import ClientRequestNavigation from './client-request-navigation';
import { Account, BasketLine } from '@/lib/types';
import type { RequestSubmissionResult } from '@/lib/request-types';
import { visibleReferences } from '@/lib/reference-label';

function QuantityControl({ name, quantity, onChange, disabled }: { name: string; quantity: number; onChange: (quantity: number) => void; disabled: boolean }) {
  const [value, setValue] = useState(String(quantity));
  useEffect(() => { setValue(String(quantity)); }, [quantity]);
  function finish() {
    const parsed = Number(value);
    const next = Number.isFinite(parsed) ? Math.max(1, Math.min(9999, Math.trunc(parsed))) : quantity;
    setValue(String(next));
    if (next !== quantity) onChange(next);
  }
  return <div className="basket-page-quantity"><span>Cantidad</span><div>
    <button type="button" aria-label={`Reducir cantidad de ${name}`} disabled={disabled || quantity <= 1} onClick={() => onChange(quantity - 1)}><Minus size={15}/></button>
    <input type="number" min={1} max={9999} step={1} inputMode="numeric" autoComplete="off" disabled={disabled} aria-label={`Cantidad de ${name}`} value={value} onChange={event => {
      const draft = event.target.value; setValue(draft);
      const next = Number(draft);
      if (draft && Number.isInteger(next) && next >= 1 && next <= 9999) onChange(next);
    }} onBlur={finish} onKeyDown={event => { if (event.key === 'Enter') event.currentTarget.blur(); }}/>
    <button type="button" aria-label={`Aumentar cantidad de ${name}`} disabled={disabled || quantity >= 9999} onClick={() => onChange(quantity + 1)}><Plus size={15}/></button>
  </div></div>;
}

export default function Basket({ lines, onChange, onClose, preview, account, onSent, onSentView }: { lines: BasketLine[]; onChange: (lines: BasketLine[]) => void; onClose: () => void; preview: boolean; account?: Account; onSent: (receipt: RequestSubmissionResult, remaining: BasketLine[]) => void; onSentView: () => void }) {
  const heading = useRef<HTMLHeadingElement>(null);
  const [sending, setSending] = useState(false);
  useEffect(() => { heading.current?.focus({ preventScroll: true }); }, []);
  const groups = Map.groupBy(lines, line => line.offer.supplier_id);
  const articleCount = new Set(lines.map(line => line.key)).size;
  const unitCount = lines.reduce((sum, line) => sum + line.quantity, 0);
  function setQuantity(key: string, quantity: number) { onChange(lines.map(line => line.key === key ? { ...line, quantity } : line)); }

  return <main className="basket-page" aria-label="Cesta de solicitudes"><div className="section-container basket-page-container">
    <button type="button" className="basket-page-back" onClick={onClose}><ArrowLeft size={16}/>Volver al catálogo</button>
    <ClientRequestNavigation selected="draft" count={articleCount} canViewSent={!preview && !!account?.capabilities.includes('client')} onDraft={() => {}} onSent={onSentView}/>
    <div className="basket-page-heading"><div><span className="eyebrow">{preview ? 'BORRADOR DE EJEMPLO' : 'BORRADOR DE SOLICITUD'}</span><h1 tabIndex={-1} ref={heading}>Tu cesta de solicitudes</h1><p>Revisa tus repuestos y cantidades. Cada proveedor recibirá solo los artículos que le solicites.</p></div><span className="basket-page-total"><ShoppingBag size={16}/>{articleCount} {articleCount === 1 ? 'artículo' : 'artículos'}</span></div>
    {preview && <div className="basket-page-preview"><Sparkles size={17}/><span>Esta cesta usa repuestos y proveedores de ejemplo para que pruebes cómo preparar una solicitud.</span></div>}
    <div className="basket-page-layout">
      <div className="basket-page-items">
        {!lines.length ? <section className="basket-page-empty" aria-label="Cesta vacía"><span className="basket-page-empty-icon"><ShoppingBag size={43} strokeWidth={1.4}/></span><h2>Tu cesta está vacía.</h2><p>Encuentra el repuesto que necesitas, elige un proveedor y agrégalo a tu cesta. Puedes reunir artículos de varios proveedores en una sola búsqueda.</p><button type="button" className="button primary" onClick={onClose}>Explorar repuestos<ArrowRight size={17}/></button></section> : [...groups.entries()].map(([supplierId, entries]) => <section className="basket-page-supplier" key={supplierId} aria-label={`Solicitud para ${entries[0].offer.supplier_name}`}>
          <div className="basket-page-supplier-heading"><span className="basket-page-store"><Store size={21}/></span><div><span>Solicitud para</span><h2>{entries[0].offer.supplier_name}</h2></div><span className="basket-page-supplier-count">{entries.length} {entries.length === 1 ? 'artículo' : 'artículos'}</span></div>
          {entries.map(line => {
            const selected = line.offer.selected_item;
            const name = selected?.codigo || line.part.name || line.part.sku;
            const description = selected?.description || line.part.description;
            const firstCode = visibleReferences({ codes: line.part.codes ?? [], equivalents: line.part.equivalents })[0];
            return <article className="basket-page-item" data-line-key={line.key} key={line.key} aria-label={`Artículo ${name} de ${line.offer.supplier_name}`}>
              <div className="basket-page-art"><CatalogPhoto image={line.part.images?.[0]} alt={line.part.name || description || line.part.sku} thumbnail/></div>
              <div className="basket-page-item-info"><span className="basket-page-sku">SKU INTERNO {line.part.sku || line.part.name}</span><h3>{name}</h3>{selected ? <span className="basket-page-code">{selected.codigo} · {selected.brand || 'SIN MARCA INDICADA'}</span> : <span className="basket-page-code">{firstCode ? `${firstCode.code} · ${firstCode.brand || 'SIN MARCA INDICADA'}` : line.part.sku}</span>}{selected && line.part.name && line.part.name !== name && <p className="basket-page-part-name">{line.part.name}</p>}{description && <p className="basket-page-description">{description}</p>}</div>
              <div className="basket-page-item-service"><div><LockKeyhole size={16}/><span><strong>Cotización privada</strong><small>El precio lo indica tu proveedor.</small></span></div><div><Truck size={17}/><span><strong>Entrega del proveedor</strong><small>Coordina la entrega o el retiro al reservar.</small></span></div></div>
              <div className="basket-page-item-actions"><QuantityControl name={name} quantity={line.quantity} disabled={sending} onChange={quantity => setQuantity(line.key, quantity)}/><button type="button" className="basket-page-remove" disabled={sending} aria-label={`Eliminar ${name}`} onClick={() => onChange(lines.filter(item => item.key !== line.key))}><Trash2 size={15}/>Eliminar</button></div>
            </article>;
          })}
          <div className="basket-page-supplier-note"><LockKeyhole size={14}/><span>Los artículos de esta sección se cotizan únicamente con este proveedor.</span></div>
        </section>)}
        <section className="basket-page-guide" aria-label="Cómo se prepara la solicitud"><span className="basket-page-guide-icon"><Boxes size={23}/></span><div><h2>Una cesta, varios proveedores.</h2><p>Prepara tu solicitud con los códigos que usa cada proveedor. Recibirás una cotización privada por proveedor y podrás coordinar la entrega directamente con él.</p></div></section>
      </div>
      <aside className="basket-page-summary" aria-label="Resumen de solicitud"><h2>Resumen de tu solicitud</h2><dl><div><dt>Proveedores</dt><dd>{groups.size}</dd></div><div><dt>Artículos</dt><dd>{articleCount}</dd></div><div className="basket-page-summary-total"><dt>Unidades solicitadas</dt><dd>{unitCount.toLocaleString('es-PA')}</dd></div></dl><div className="basket-page-private"><LockKeyhole size={18}/><div><strong>Precios en cotizaciones privadas</strong><p>Cada proveedor comparte sus precios por separado, según los artículos de tu solicitud.</p></div></div>{lines.length ? <><BasketSubmission lines={lines} account={account} preview={preview} onPending={setSending} onSent={onSent}/><button type="button" className="button soft full" onClick={onClose}>Seguir buscando repuestos<ArrowRight size={16}/></button></> : <><button type="button" className="button primary full" onClick={onClose}>Seguir buscando repuestos<ArrowRight size={16}/></button><p className="basket-page-draft-note">Agrega un repuesto y elige un proveedor para empezar tu solicitud.</p></>}<p className="basket-page-storage-note">Tu borrador se guarda en este navegador.</p></aside>
    </div>
  </div></main>;
}
