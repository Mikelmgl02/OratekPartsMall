'use client';

import { useEffect, useRef, useState } from 'react';
import { ArrowLeft, ArrowRight, Boxes, History, LockKeyhole, MessageCircle, Minus, Plus, ShoppingBag, Sparkles, Store, Trash2, TriangleAlert, Truck } from 'lucide-react';
import { CatalogPhoto } from './part-image';
import BasketSubmission from './basket-submission';
import ClientRequestNavigation from './client-request-navigation';
import { Account, BasketEvent, BasketLine, request } from '@/lib/types';
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

// What each entry of the cesta's ledger says, in the person's words.
function eventText(entry: BasketEvent) {
  const article = `${entry.codigo} · ${entry.supplier_name || 'proveedor'}`;
  switch (entry.kind) {
    case 'added': return `Agregaste ${entry.quantity_after} × ${article}`;
    case 'changed': return `Cambiaste ${article} de ${entry.quantity_before} a ${entry.quantity_after}`;
    case 'removed': return `Quitaste ${entry.quantity_before} × ${article}`;
    case 'moved_in': return `Trajiste ${entry.quantity_after} × ${article} de la cesta de este navegador`;
    case 'sent': return `Enviaste ${entry.quantity_before - entry.quantity_after} × ${article}${entry.reference ? ` en ${entry.reference}` : ''}`;
  }
}
function eventTime(value: string) {
  const at = new Date(value), today = new Date();
  const time = at.toLocaleTimeString('es-PA', { hour: '2-digit', minute: '2-digit' });
  return at.toDateString() === today.toDateString() ? time : `${at.toLocaleDateString('es-PA', { day: 'numeric', month: 'short' })} · ${time}`;
}

// Actividad: the cesta's ledger, newest first; read again after each change (version), older entries on demand.
function BasketActivity({ accountId, version }: { accountId: string; version: string }) {
  const [entries, setEntries] = useState<BasketEvent[]>([]);
  const [hasMore, setHasMore] = useState(false);
  const [shown, setShown] = useState(5);
  const [failed, setFailed] = useState(false);
  const path = `/api/market/accounts/${accountId}/basket/activity`;
  useEffect(() => {
    let current = true;
    const timer = setTimeout(() => {
      request<{ results: BasketEvent[]; has_more: boolean }>(path)
        .then(data => { if (current) { setEntries(data.results); setHasMore(data.has_more); setFailed(false); } })
        .catch(() => { if (current) setFailed(true); });
    }, 400);
    return () => { current = false; clearTimeout(timer); };
  }, [path, version]);
  async function more() {
    if (shown < entries.length) { setShown(count => count + 10); return; }
    const data = await request<{ results: BasketEvent[]; has_more: boolean }>(`${path}?before=${entries.at(-1)!.id}`);
    setEntries(rows => [...rows, ...data.results]); setHasMore(data.has_more); setShown(count => count + 10);
  }
  if (!entries.length && !failed) return null;
  return <section className="basket-page-activity" aria-label="Actividad de tu cesta"><h2><History size={17}/>Actividad de tu cesta</h2>
    {failed && !entries.length ? <p className="basket-page-draft-note">No se pudo cargar la actividad de tu cesta.</p> : <ol>{entries.slice(0, shown).map(entry =>
      <li key={entry.id} className={entry.kind}><time dateTime={entry.created_at}>{eventTime(entry.created_at)}</time><span>{eventText(entry)}</span>{entry.source === 'whatsapp' && <em><MessageCircle size={11}/>WhatsApp</em>}</li>)}</ol>}
    {(shown < entries.length || hasMore) && <button type="button" className="button soft small" onClick={() => void more().catch(() => setFailed(true))}>Ver más actividad</button>}
  </section>;
}

// synced: the cesta lives on the server (a client account signed in), the same on every device; otherwise in this browser.
export default function Basket({ lines, onChange, onClose, preview, account, synced = false, onSent, onSentView }: { lines: BasketLine[]; onChange: (lines: BasketLine[]) => void; onClose: () => void; preview: boolean; account?: Account; synced?: boolean; onSent: (receipt: RequestSubmissionResult, remaining: BasketLine[]) => void; onSentView: () => void }) {
  const heading = useRef<HTMLHeadingElement>(null);
  const [sending, setSending] = useState(false);
  useEffect(() => { heading.current?.focus({ preventScroll: true }); }, []);
  const groups = Map.groupBy(lines, line => line.offer.supplier_id);
  const articleCount = new Set(lines.map(line => line.key)).size;
  const unitCount = lines.reduce((sum, line) => sum + line.quantity, 0);
  // Items of the active account's own stock (added before it could also buy, or from another device): it never orders from itself.
  const ownSupplier = !preview && account ? account.id : null;
  const ownLines = ownSupplier ? lines.filter(line => line.offer.supplier_id === ownSupplier) : [];
  // Lines the server says can no longer go as they are: the article changed since it was added, or it cannot be asked for anymore.
  const staleLines = lines.filter(line => line.offer.supplier_id !== ownSupplier && (line.available === false || line.changed));
  function setQuantity(key: string, quantity: number) { onChange(lines.map(line => line.key === key ? { ...line, quantity } : line)); }

  return <main className="basket-page" aria-label="Cesta de solicitudes"><div className="section-container basket-page-container">
    <button type="button" className="basket-page-back" onClick={onClose}><ArrowLeft size={16}/>Volver al catálogo</button>
    <ClientRequestNavigation selected="draft" count={articleCount} canViewSent={!preview && !!account?.capabilities.includes('client')} onDraft={() => {}} onSent={onSentView}/>
    <div className="basket-page-heading"><div><span className="eyebrow">{preview ? 'BORRADOR DE EJEMPLO' : 'BORRADOR DE SOLICITUD'}</span><h1 tabIndex={-1} ref={heading}>Tu cesta de solicitudes</h1><p>Revisa tus repuestos y cantidades. Cada proveedor recibirá solo los artículos que le solicites.</p></div><span className="basket-page-total"><ShoppingBag size={16}/>{articleCount} {articleCount === 1 ? 'artículo' : 'artículos'}</span></div>
    {preview && <div className="basket-page-preview"><Sparkles size={17}/><span>Esta cesta usa repuestos y proveedores de ejemplo para que pruebes cómo preparar una solicitud.</span></div>}
    <div className="basket-page-layout">
      <div className="basket-page-items">
        {!lines.length ? <section className="basket-page-empty" aria-label="Cesta vacía"><span className="basket-page-empty-icon"><ShoppingBag size={43} strokeWidth={1.4}/></span><h2>Tu cesta está vacía.</h2><p>Encuentra el repuesto que necesitas, elige un proveedor y agrégalo a tu cesta. Puedes reunir artículos de varios proveedores en una sola búsqueda.</p><button type="button" className="button primary" onClick={onClose}>Explorar repuestos<ArrowRight size={17}/></button></section> : [...groups.entries()].map(([supplierId, entries]) => <section className={`basket-page-supplier${supplierId === ownSupplier ? ' own' : ''}`} key={supplierId} aria-label={`Solicitud para ${entries[0].offer.supplier_name}`}>
          <div className="basket-page-supplier-heading"><span className="basket-page-store"><Store size={21}/></span><div><span>Solicitud para</span><h2>{entries[0].offer.supplier_name}</h2></div><span className="basket-page-supplier-count">{entries.length} {entries.length === 1 ? 'artículo' : 'artículos'}</span></div>
          {supplierId === ownSupplier && <div className="notice basket-own-warning" role="alert"><span>Estos artículos son de tu propia cuenta. No puedes pedirte una cotización a ti mismo: quítalos para enviar el resto de tu solicitud.</span>
            <button type="button" className="button soft small" disabled={sending} onClick={() => onChange(lines.filter(line => line.offer.supplier_id !== supplierId))}><Trash2 size={14}/>Quitar estos artículos</button></div>}
          {entries.map(line => {
            const selected = line.offer.selected_item;
            const name = selected?.codigo || line.part.name || line.part.sku;
            const description = selected?.description || line.part.description;
            const firstCode = visibleReferences({ codes: line.part.codes ?? [], equivalents: line.part.equivalents })[0];
            return <article className="basket-page-item" data-line-key={line.key} key={line.key} aria-label={`Artículo ${name} de ${line.offer.supplier_name}`}>
              <div className="basket-page-art"><CatalogPhoto image={line.part.images?.[0]} alt={line.part.name || description || line.part.sku} thumbnail/></div>
              <div className="basket-page-item-info">{line.source === 'whatsapp' && <span className="basket-page-source"><MessageCircle size={12}/>Agregado por WhatsApp</span>}<span className="basket-page-sku">SKU INTERNO {line.part.sku || line.part.name}</span><h3>{name}</h3>{selected ? <span className="basket-page-code">{selected.codigo} · {selected.brand || 'SIN MARCA INDICADA'}</span> : <span className="basket-page-code">{firstCode ? `${firstCode.code} · ${firstCode.brand || 'SIN MARCA INDICADA'}` : line.part.sku}</span>}{selected && line.part.name && line.part.name !== name && <p className="basket-page-part-name">{line.part.name}</p>}{description && <p className="basket-page-description">{description}</p>}
                {line.available === false ? <p className="basket-page-stale" role="note"><TriangleAlert size={14}/>Este artículo ya no se puede solicitar. Quítalo de la cesta.</p>
                  : line.changed ? <p className="basket-page-stale" role="note"><TriangleAlert size={14}/>Cambió desde que lo agregaste. Quítalo y vuelve a elegirlo en el catálogo.</p> : null}</div>
              <div className="basket-page-item-service"><div><LockKeyhole size={16}/><span><strong>Cotización privada</strong><small>El precio lo indica tu proveedor.</small></span></div><div><Truck size={17}/><span><strong>Entrega del proveedor</strong><small>Coordina la entrega o el retiro al reservar.</small></span></div></div>
              <div className="basket-page-item-actions"><QuantityControl name={name} quantity={line.quantity} disabled={sending} onChange={quantity => setQuantity(line.key, quantity)}/><button type="button" className="basket-page-remove" disabled={sending} aria-label={`Eliminar ${name}`} onClick={() => onChange(lines.filter(item => item.key !== line.key))}><Trash2 size={15}/>Eliminar</button></div>
            </article>;
          })}
          <div className="basket-page-supplier-note"><LockKeyhole size={14}/><span>Los artículos de esta sección se cotizan únicamente con este proveedor.</span></div>
        </section>)}
        {synced && account && <BasketActivity accountId={account.id} version={lines.map(line => `${line.key}:${line.quantity}`).join('|')}/>}
        <section className="basket-page-guide" aria-label="Cómo se prepara la solicitud"><span className="basket-page-guide-icon"><Boxes size={23}/></span><div><h2>Una cesta, varios proveedores.</h2><p>Prepara tu solicitud con los códigos que usa cada proveedor. Recibirás una cotización privada por proveedor y podrás coordinar la entrega directamente con él.</p></div></section>
      </div>
      <aside className="basket-page-summary" aria-label="Resumen de solicitud"><h2>Resumen de tu solicitud</h2><dl><div><dt>Proveedores</dt><dd>{groups.size}</dd></div><div><dt>Artículos</dt><dd>{articleCount}</dd></div><div className="basket-page-summary-total"><dt>Unidades solicitadas</dt><dd>{unitCount.toLocaleString('es-PA')}</dd></div></dl><div className="basket-page-private"><LockKeyhole size={18}/><div><strong>Precios en cotizaciones privadas</strong><p>Cada proveedor comparte sus precios por separado, según los artículos de tu solicitud.</p></div></div>{lines.length ? <><BasketSubmission lines={lines} account={account} preview={preview} ownItems={ownLines.length} staleItems={staleLines.length} onPending={setSending} onSent={onSent}/><button type="button" className="button soft full" onClick={onClose}>Seguir buscando repuestos<ArrowRight size={16}/></button></> : <><button type="button" className="button primary full" onClick={onClose}>Seguir buscando repuestos<ArrowRight size={16}/></button><p className="basket-page-draft-note">Agrega un repuesto y elige un proveedor para empezar tu solicitud.</p></>}<p className="basket-page-storage-note">{synced ? 'Tu cesta se guarda en tu cuenta: la ves igual en todos tus dispositivos.' : 'Tu borrador se guarda en este navegador.'}</p></aside>
    </div>
  </div></main>;
}
