'use client';

import { useEffect, useRef, useState } from 'react';
import { ArrowRight, Boxes, LockKeyhole, ShoppingBag, Sparkles, Store, Trash2 } from 'lucide-react';
import BasketSubmission from './basket-submission';
import dynamic from 'next/dynamic';
import ClientRequestNavigation from './client-request-navigation';
import { Account, BasketLine } from '@/lib/types';

const BasketGrid = dynamic(() => import('./basket-grid'), { ssr: false, loading: () => <div className="quotation-grid-loading" role="status">Cargando artículos…</div> });
import type { RequestSubmissionResult } from '@/lib/request-types';

// synced: the cesta lives on the server (a client account signed in), the same on every device; otherwise in this browser.
export default function Basket({ lines, onChange, onClose, preview, account, synced = false, onSent, onSentView, onActivityView, flush }: { lines: BasketLine[]; onChange: (lines: BasketLine[]) => void; onClose: () => void; preview: boolean; account?: Account; synced?: boolean; onSent: (receipt: RequestSubmissionResult, remaining: BasketLine[]) => void; onSentView: () => void; onActivityView: () => void; flush?: () => Promise<unknown> }) {
  const page = useRef<HTMLElement>(null);
  const [sending, setSending] = useState(false);
  useEffect(() => { page.current?.focus({ preventScroll: true }); }, []);  // keyboard and screen readers start on the cesta
  const groups = Map.groupBy(lines, line => line.offer.supplier_id);
  const articleCount = new Set(lines.map(line => line.key)).size;
  const unitCount = lines.reduce((sum, line) => sum + line.quantity, 0);
  // Items of the active account's own stock (added before it could also buy, or from another device): it never orders from itself.
  const ownSupplier = !preview && account ? account.id : null;
  const ownLines = ownSupplier ? lines.filter(line => line.offer.supplier_id === ownSupplier) : [];
  // Lines the server says can no longer go as they are: the article changed since it was added, or it cannot be asked for anymore.
  const staleLines = lines.filter(line => line.offer.supplier_id !== ownSupplier && (line.available === false || line.changed));
  function setQuantity(key: string, quantity: number) { onChange(lines.map(line => line.key === key ? { ...line, quantity } : line)); }

  return <main ref={page} tabIndex={-1} className="basket-page cesta-page" aria-label="Cesta de solicitudes"><div className="section-container basket-page-container">
    <ClientRequestNavigation selected="draft" count={articleCount} canViewSent={!preview && !!account?.capabilities.includes('client')} onDraft={() => {}} onSent={onSentView} onActivity={onActivityView}/>
    {preview && <div className="basket-page-preview"><Sparkles size={17}/><span>Esta cesta usa repuestos y proveedores de ejemplo para que pruebes cómo preparar una solicitud.</span></div>}
    <div className="basket-page-layout">
      <div className="basket-page-items">
        {!lines.length ? <section className="basket-page-empty" aria-label="Cesta vacía"><span className="basket-page-empty-icon"><ShoppingBag size={43} strokeWidth={1.4}/></span><h2>Tu cesta está vacía.</h2><p>Encuentra el repuesto que necesitas, elige un proveedor y agrégalo a tu cesta. Puedes reunir artículos de varios proveedores en una sola búsqueda.</p><button type="button" className="button primary" onClick={onClose}>Explorar repuestos<ArrowRight size={17}/></button></section> : [...groups.entries()].map(([supplierId, entries]) => <section className={`basket-page-supplier${supplierId === ownSupplier ? ' own' : ''}`} key={supplierId} aria-label={`Solicitud para ${entries[0].offer.supplier_name}`}>
          <div className="basket-page-supplier-heading"><span className="basket-page-store"><Store size={21}/></span><div><span>Solicitud para</span><h2>{entries[0].offer.supplier_name}</h2></div><span className="basket-page-supplier-count">{entries.length} {entries.length === 1 ? 'artículo' : 'artículos'}</span></div>
          {supplierId === ownSupplier && <div className="notice basket-own-warning" role="alert"><span>Estos artículos son de tu propia cuenta. No puedes pedirte una cotización a ti mismo: quítalos para enviar el resto de tu solicitud.</span>
            <button type="button" className="button soft small" disabled={sending} onClick={() => onChange(lines.filter(line => line.offer.supplier_id !== supplierId))}><Trash2 size={14}/>Quitar estos artículos</button></div>}
          <BasketGrid supplier={entries[0].offer.supplier_name} lines={entries} sending={sending} setQuantity={setQuantity} remove={key => onChange(lines.filter(item => item.key !== key))}/>
          <div className="basket-page-supplier-note"><LockKeyhole size={14}/><span>Los artículos de esta sección se cotizan únicamente con este proveedor.</span></div>
        </section>)}
        <section className="basket-page-guide" aria-label="Cómo se prepara la solicitud"><span className="basket-page-guide-icon"><Boxes size={23}/></span><div><h2>Una cesta, varios proveedores.</h2><p>Prepara tu solicitud con los códigos que usa cada proveedor. Recibirás una cotización privada por proveedor y podrás coordinar la entrega directamente con él.</p></div></section>
      </div>
      <aside className="basket-page-summary" aria-label="Resumen de solicitud"><h2>Resumen de tu solicitud</h2><dl><div><dt>Proveedores</dt><dd>{groups.size}</dd></div><div><dt>Artículos</dt><dd>{articleCount}</dd></div><div className="basket-page-summary-total"><dt>Unidades solicitadas</dt><dd>{unitCount.toLocaleString('es-PA')}</dd></div></dl><div className="basket-page-private"><LockKeyhole size={18}/><div><strong>Precios en cotizaciones privadas</strong><p>Cada proveedor comparte sus precios por separado, según los artículos de tu solicitud.</p></div></div>{lines.length ? <><BasketSubmission lines={lines} account={account} preview={preview} ownItems={ownLines.length} staleItems={staleLines.length} onPending={setSending} onSent={onSent} flush={flush}/><button type="button" className="button soft full" onClick={onClose}>Seguir buscando repuestos<ArrowRight size={16}/></button></> : <><button type="button" className="button primary full" onClick={onClose}>Seguir buscando repuestos<ArrowRight size={16}/></button><p className="basket-page-draft-note">Agrega un repuesto y elige un proveedor para empezar tu solicitud.</p></>}<p className="basket-page-storage-note">{synced ? 'Tu cesta se guarda en tu cuenta: la ves igual en todos tus dispositivos.' : 'Tu borrador se guarda en este navegador.'}</p></aside>
    </div>
  </div></main>;
}
