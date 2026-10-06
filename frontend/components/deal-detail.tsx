'use client';
import { useCallback, useEffect, useRef, useState } from 'react';
import dynamic from 'next/dynamic';
import { Check, FileText, Handshake, History, LoaderCircle, LockKeyhole, MessageSquare, RefreshCw, Send } from 'lucide-react';
import Modal from './modal';
import DealStatusBadge from './deal-status';
import { UppercaseTextarea } from './uppercase-field';
import { Account, ApiError, request } from '@/lib/types';
import type { SupplierRequestLine } from '@/lib/request-types';
import type { Deal, DealMessage, Quotation } from '@/lib/deal-types';
import type { QuotationTrace, QuoteDraft, QuoteDraftEnvelope } from '@/lib/pricing-types';

const QuoteEditor = dynamic(() => import('./quotation-editor'), { ssr: false, loading: () => <div className="quotation-grid-loading" role="status"><LoaderCircle size={18} className="spin"/>Cargando editor de cotización…</div> });

const date = (value: string) => new Date(value).toLocaleString('es-PA', { timeZone: 'America/Panama', dateStyle: 'medium', timeStyle: 'short' });
const money = (value: string | number, currency = 'USD') => new Intl.NumberFormat('es-PA', { style: 'currency', currency }).format(Number(value));
const failure = (error: unknown) => error instanceof TypeError ? 'Se perdió la conexión. Puedes reintentar sin duplicar la operación.' : error instanceof Error ? error.message : 'No se pudo actualizar el acuerdo.';
const stateDescription = {
  pending: 'Puedes agregar artículos enviando otra cesta a este proveedor. Al abrir la orden, el proveedor la bloqueará para revisión.',
  reviewed: 'Los artículos de esta orden están bloqueados. El proveedor está preparando la cotización.',
  quoted: 'La cotización está bloqueada. El cliente debe confirmarla o solicitar un ajuste.',
  adjustment: 'El cliente solicitó un ajuste. El proveedor puede enviar una nueva versión o devolver la misma cotización.',
  handshaked: 'Ambas partes confirmaron la cotización. El acuerdo está cerrado y sus condiciones quedan guardadas.',
};

export default function DealDetail({ account, orderId, reference, side, onClose, onChanged }: {
  account: Account; orderId: string; reference?: string; side: 'client' | 'supplier'; onClose?: () => void; onChanged?: () => void;
}) {
  const supplier = side === 'supplier';
  const base = `/api/market/accounts/${account.id}`;
  const detailPath = `${base}/${supplier ? 'requests' : 'sent-requests'}/${orderId}`;
  const actionPath = `${base}/deals/${orderId}/actions`;
  const [deal, setDeal] = useState<Deal | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState('');
  const [tab, setTab] = useState<'order' | 'quote' | 'chat' | 'history'>('order');
  const [quoteVisited, setQuoteVisited] = useState(false);
  const [adjusting, setAdjusting] = useState(false);
  const [reason, setReason] = useState('');
  const [confirming, setConfirming] = useState(false);
  // A refused accept (for example, stock the supplier relied on changed) points the client to "Solicitar ajuste".
  const [suggestAdjust, setSuggestAdjust] = useState(false);
  const mounted = useRef(false);
  const inFlight = useRef(false);
  const retry = useRef<{ payload: string; id: string } | null>(null);
  const onChangedRef = useRef(onChanged); onChangedRef.current = onChanged;
  const latestVersion = useRef<number | undefined>(undefined);
  // The supplier's private draft for the next revision; a failed load leaves the editor without autosave.
  const [draftLoad, setDraftLoad] = useState<{ key: string; draft: QuoteDraft | null } | null>(null);
  const [hasDraft, setHasDraft] = useState(false);
  const draftPath = `${base}/requests/${orderId}/draft`;
  const acceptDetail = useCallback((value: Deal) => {
    if (!mounted.current) return;
    if (latestVersion.current !== undefined && value.version < latestVersion.current) return;
    const changed = latestVersion.current !== undefined && latestVersion.current !== value.version;
    latestVersion.current = value.version;
    setDeal(value);
    if (changed) { setConfirming(false); setSuggestAdjust(false); onChangedRef.current?.(); }
  }, []);
  const load = useCallback(async (open = false) => {
    try {
      const value = await request<Deal>(open && supplier ? `${detailPath}/review` : detailPath,
        open && supplier ? { method: 'POST', body: '{}' } : {});
      if (!mounted.current) return;
      acceptDetail(value);
      if (open && supplier) onChangedRef.current?.();
    } catch (caught) { if (mounted.current) setError(failure(caught)); }
  }, [detailPath, supplier, acceptDetail]);
  useEffect(() => {
    mounted.current = true;
    void load(true);
    const timer = setInterval(() => { if (!inFlight.current && document.visibilityState === 'visible') void load(); }, 15000);
    return () => { mounted.current = false; clearInterval(timer); };
  }, [load]);
  const editable = supplier && !!deal && ['reviewed', 'adjustment'].includes(deal.status);
  const draftKey = deal ? `${deal.id}:${deal.quotation?.id || 'new'}` : '';
  useEffect(() => {
    if (!editable) { setDraftLoad(null); setHasDraft(false); return; }
    if (draftLoad?.key === draftKey) return;
    let cancelled = false;
    request<QuoteDraftEnvelope>(draftPath).then(value => {
      if (cancelled) return;
      setDraftLoad({ key: draftKey, draft: value.draft }); setHasDraft(!!value.draft?.persisted);
      if (!value.editable) void load();
    }).catch(() => { if (!cancelled) setDraftLoad({ key: draftKey, draft: null }); });
    return () => { cancelled = true; };
  }, [editable, draftKey, draftPath, draftLoad?.key, load]);
  async function act(action: string, extra: Record<string, unknown> = {}): Promise<true | ApiError | false> {
    if (!deal || inFlight.current) return false;
    inFlight.current = true; setBusy(true); setError(''); setNotice('');
    const payload = { action, expected_version: deal.version, ...extra };
    const serialized = JSON.stringify(payload);
    if (retry.current?.payload !== serialized) retry.current = { payload: serialized, id: crypto.randomUUID() };
    try {
      const result = await request<Deal>(actionPath, { method: 'POST', body: JSON.stringify({ ...payload, operation_id: retry.current.id }) });
      if (!mounted.current) return true;
      retry.current = null; acceptDetail(result); setAdjusting(false); setReason(''); setConfirming(false); setSuggestAdjust(false);
      setTab('quote');
      setNotice(action === 'accept' ? 'Acuerdo confirmado por ambas partes · HANDSHAKED.' : action === 'request_adjustment' ? 'Ajuste enviado al proveedor.' : 'Cotización confirmada por el proveedor y enviada al cliente.');
      onChangedRef.current?.();
      return true;
    } catch (caught) {
      if (!mounted.current) return false;
      const refused = caught instanceof ApiError && caught.status === 409;
      // Quote alerts are shown by the editor next to the affected lines.
      if (!(refused && action === 'quote' && Array.isArray((caught.body as { exceptions?: unknown } | null)?.exceptions))) setError(failure(caught));
      if (refused && action === 'accept') { setConfirming(false); setSuggestAdjust(true); void load(); }
      return caught instanceof ApiError ? caught : false;
    } finally { inFlight.current = false; if (mounted.current) setBusy(false); }
  }
  const stockWarnings = supplier && deal ? (deal.lines as SupplierRequestLine[]).filter(line => !line.stock || line.stock.shortfall > 0).length : 0;
  const activeStep = !deal ? 0 : deal.status === 'pending' ? 0 : deal.status === 'reviewed' ? 1 : deal.status === 'handshaked' ? 3 : 2;
  const content = <>
    <div className="deal-detail">
      {error && <div className="notice error" role="alert"><span>{error}</span><button type="button" disabled={busy} onClick={() => { setError(''); void load(!deal); }}>Actualizar acuerdo</button></div>}
      {!deal && !error && <div className="supplier-request-detail-loading" role="status"><span className="sr-only">Cargando orden…</span><div className="skeleton-block"/><div className="skeleton-block"/></div>}
      {deal && <>
        <div className="supplier-request-detail-header"><div><span>{supplier ? 'Cliente' : 'Proveedor'}</span><h3>{supplier ? deal.client.name : deal.supplier.name}</h3><p>{date(deal.created_at)}</p></div><DealStatusBadge status={deal.status}/></div>
        <ol className="deal-progress" aria-label="Seguimiento del acuerdo">{['Enviada', 'En revisión', 'Cotización', 'Acuerdo'].map((label, index) => <li key={label} className={index <= activeStep ? 'reached' : ''} aria-current={index === activeStep ? 'step' : undefined}><span>{index < activeStep ? <Check size={12}/> : index + 1}</span>{label}</li>)}</ol>
        <div className={`deal-state-note ${deal.status}`}><LockKeyhole size={16}/><p>{stateDescription[deal.status]}{deal.handshaked_at && <strong> {date(deal.handshaked_at)}</strong>}</p></div>
        <nav className="deal-tabs" aria-label="Secciones del acuerdo">{([
          ['order', 'Artículos', FileText], ['quote', 'Cotización', Handshake], ['chat', 'Conversación', MessageSquare], ['history', 'Actividad', History],
        ] as const).map(([value, label, Icon]) => <button type="button" key={value} className={tab === value ? 'selected' : ''} aria-pressed={tab === value} title={value === 'quote' && hasDraft ? 'Hay un borrador de cotización guardado' : undefined} onClick={() => { setTab(value); if (value === 'quote') setQuoteVisited(true); }}><Icon size={15}/>{label}{value === 'quote' && hasDraft && <span className="deal-tab-dot" aria-hidden="true"/>}</button>)}</nav>
        {notice && <div className="notice success" role="status">{notice}</div>}
        {tab === 'order' && <>
          <div className="supplier-request-detail-counts"><span>{deal.line_count} artículos</span><strong>{deal.unit_count.toLocaleString('es-PA')} unidades solicitadas</strong></div>
          {deal.notes && <section className="supplier-request-notes"><h4>Notas del cliente</h4><p>{deal.notes}</p></section>}
          {supplier && <div className="supplier-request-stock-heading"><div><h4>Existencias de tu inventario</h4><p>Disponibles = reportadas menos reservadas. Confirma las unidades que puedes suministrar.</p></div><button type="button" className="button soft small" disabled={busy} onClick={() => void load()}><RefreshCw size={15}/>Actualizar existencias</button></div>}
          {stockWarnings > 0 && <div className="notice supplier-request-stock-warning" role="status">{stockWarnings} {stockWarnings === 1 ? 'artículo requiere' : 'artículos requieren'} confirmar disponibilidad. La orden conserva todas las unidades pedidas.</div>}
          <div className="table-scroll deal-lines supplier-request-lines"><table><thead><tr><th>Código / descripción</th><th>SKU interno</th>{supplier && <th>Tu ID de inventario</th>}<th>Solicitadas</th>{supplier && <th>Disponibles</th>}</tr></thead><tbody>{deal.lines.map(line => <tr key={line.id}><td><strong>{line.codigo}</strong><span>{line.brand || 'SIN MARCA INDICADA'}</span><small>{line.description}</small></td><td>{line.sku}</td>{supplier && <td>{'supplier_invent_id' in line && line.supplier_invent_id}</td>}<td>{line.quantity}</td>{supplier && <td>{'stock' in line && line.stock ? <><strong>{line.stock.available_quantity}</strong><small>Reportadas: {line.stock.reported_quantity} · Reservadas: {line.stock.reserved_quantity}</small><small className={line.stock.shortfall ? "stock-shortfall" : "stock-sufficient"}>{line.stock.shortfall ? `Faltan ${line.stock.shortfall} unidades` : "Existencias suficientes"}</small></> : <><strong className="stock-shortfall">Revisar artículo</strong><small>El código o el vínculo con el SKU cambió. Confirma la equivalencia en Inventario.</small></>}</td>}</tr>)}</tbody></table></div>
          {supplier && ['reviewed', 'adjustment'].includes(deal.status) && <button type="button" className="button primary" onClick={() => { setQuoteVisited(true); setTab('quote'); }}>Preparar cotización<FileText size={16}/></button>}
          {deal.quotation && <button type="button" className="button soft" onClick={() => setTab('quote')}>Ver cotización · v{deal.quotation.revision}<FileText size={16}/></button>}
        </>}
        <div className="deal-quote-tab" hidden={tab !== 'quote'}>
          {deal.quotation && <QuoteView quote={deal.quotation}/>}
          {supplier && deal.quotation && <QuoteTrace key={deal.quotation.id} path={`${base}/requests/${orderId}/quotations/${deal.quotation.id}/trace`}/>}
          {!deal.quotation && !supplier && <div className="notice">El proveedor preparará tu cotización. Te aparecerán aquí las cantidades, precios y condiciones.</div>}
          {editable && (tab === 'quote' || quoteVisited) && (draftLoad?.key === draftKey
            ? <QuoteEditor key={`${draftKey}:${draftLoad.draft ? draftLoad.draft.persisted ? 'saved' : 'virtual' : 'fallback'}`} deal={deal} draft={draftLoad.draft} draftPath={draftPath}
                disabled={busy} onSend={values => act('quote', values)} onDraftChange={setHasDraft}/>
            : <div className="quotation-grid-loading" role="status"><LoaderCircle size={18} className="spin"/>Cargando borrador de cotización…</div>)}
          {supplier && deal.status === 'adjustment' && deal.quotation && <button type="button" className="button soft" disabled={busy} onClick={() => void act('return_quote', { quotation_id: deal.quotation!.id })}>Devolver la misma cotización para confirmar<Send size={15}/></button>}
          {supplier && deal.status === 'adjustment' && deal.quotation && hasDraft && <small className="deal-draft-warning">Si devuelves la misma cotización, se descartará el borrador en curso.</small>}
          {!supplier && deal.status === 'quoted' && deal.quotation && <div className="deal-client-decisions">
            {confirming ? <div className="notice deal-confirmation"><strong>¿Confirmar este acuerdo por {money(deal.quotation.total, deal.quotation.currency)}?</strong><p>Aceptas las cantidades y condiciones de la cotización v{deal.quotation.revision}. El proveedor ya las confirmó.</p><div><button type="button" className="button primary" disabled={busy} onClick={() => void act('accept', { quotation_id: deal.quotation!.id })}>{busy ? <LoaderCircle size={15} className="spin"/> : <Handshake size={15}/>}Sí, confirmar acuerdo</button><button type="button" className="button soft" disabled={busy} onClick={() => setConfirming(false)}>Volver</button></div></div> : <button type="button" className={`button ${suggestAdjust ? 'soft' : 'primary'}`} disabled={busy} onClick={() => { setConfirming(true); setAdjusting(false); }}>Confirmar cotización<Handshake size={16}/></button>}
            {!confirming && <button type="button" className={`button ${suggestAdjust ? 'primary' : 'soft'}`} disabled={busy} aria-describedby={suggestAdjust ? 'deal-adjust-hint' : undefined} onClick={() => setAdjusting(value => !value)}>Solicitar ajuste</button>}
            {suggestAdjust && !confirming && <small id="deal-adjust-hint" className="deal-adjust-hint">Pide al proveedor una nueva versión con las unidades que puede confirmar.</small>}
            {adjusting && <form className="deal-adjustment" onSubmit={event => { event.preventDefault(); void act('request_adjustment', { quotation_id: deal.quotation!.id, reason }); }}><label>¿Qué necesitas ajustar?<UppercaseTextarea required maxLength={4000} value={reason} disabled={busy} onChange={event => setReason(event.target.value)} placeholder="INDICA LAS CANTIDADES O CONDICIONES QUE NECESITAS CAMBIAR…"/></label><button type="submit" className="button primary" disabled={busy || !reason.trim()}>{busy ? <LoaderCircle size={15} className="spin"/> : <Send size={15}/>}Enviar ajuste</button></form>}
          </div>}
          {deal.status === 'handshaked' && <div className="deal-handshake"><Handshake size={25}/><div><strong>HANDSHAKED · Acuerdo confirmado</strong><p>Confirmación del proveedor: {deal.quotation && date(deal.quotation.supplier_confirmed_at)}</p><p>Confirmación del cliente: {deal.quotation?.client_confirmed_at && date(deal.quotation.client_confirmed_at)}</p></div></div>}
          {(deal.quotations?.length || 0) > 1 && <details className="deal-quote-history"><summary>Versiones anteriores ({deal.quotations.length - 1})</summary>{deal.quotations.slice(0, -1).map(quote => <QuoteView key={quote.id} quote={quote} historical/>)}</details>}
        </div>
        {/* Keep conversation mounted so refreshes and draft messages survive tab changes. */}
        <div hidden={tab !== 'chat'}><DealChat account={account} orderId={orderId} active={tab === 'chat'}/></div>
        {tab === 'history' && <ol className="deal-events">{deal.events?.length ? deal.events.map(event => <li key={event.id}><span>{date(event.created_at)} · {event.account_name} · {event.actor_name}</span><p>{event.description}</p></li>) : <li>Esta orden se creó antes de habilitar el registro de actividad.</li>}</ol>}
        <div className="deal-footer"><small>Orden {deal.reference} · La entrega se coordina con el proveedor.</small><button type="button" className="button soft small" disabled={busy} onClick={() => { setError(''); void load(); }}><RefreshCw size={14}/>Actualizar</button></div>
      </>}
    </div>
  </>;
  return supplier ? <section className="deal-order-content" aria-label={`Orden ${deal?.reference || reference || 'del cliente'}`}>
    <div className="deal-page-heading"><span className="eyebrow">ORDEN DE CLIENTE</span><h1>{deal ? `Orden ${deal.reference}` : 'Detalle de la orden'}</h1></div>
    {content}
  </section> : <Modal title={`Orden ${reference}`} wide className="deal-modal supplier-request-modal" onClose={() => { if (!inFlight.current) onClose?.(); }}>{content}</Modal>;
}

function QuoteView({ quote, historical = false }: { quote: Quotation; historical?: boolean }) {
  return <section className={`deal-quote ${historical ? 'historical' : ''}`} aria-label={`${historical ? 'Cotización anterior' : 'Cotización vigente'} v${quote.revision}`}><div className="deal-quote-heading"><div><h3>Cotización v{quote.revision}</h3><small>Confirmada por el proveedor · {date(quote.supplier_confirmed_at)}</small></div><strong>{money(quote.total, quote.currency)}</strong></div><div className="table-scroll"><table><thead><tr><th>Artículo</th><th>Ofrecidas</th><th>Precio unitario</th><th>Importe</th></tr></thead><tbody>{quote.lines.map(line => <tr key={line.order_line_id}><td><strong>{line.codigo}</strong><small>{line.description}</small></td><td>{line.quantity === 0 ? 'No disponible' : line.quantity}</td><td>{money(line.unit_price, quote.currency)}</td><td>{money(line.total, quote.currency)}</td></tr>)}</tbody></table></div>{quote.terms && <div className="deal-terms"><h4>Condiciones</h4><p>{quote.terms}</p></div>}</section>;
}

const priceSources = { engine: 'Lista o regla', previous: 'Versión anterior', manual: 'Manual', none: 'Sin precio', unspecified: 'Sin especificar' };
const alertNames: Record<string, string> = { offered_gt_available: 'Más que las disponibles', offered_gt_requested: 'Más que las solicitadas',
  identity_changed: 'Artículo cambiado', zero_price: 'Precio 0,00' };
const acceptResults = { ok: 'Existencias verificadas al confirmar', blocked: 'Confirmación bloqueada por existencias', accepted_with_shortfall: 'Confirmado con faltante' };

// Supplier-only publication trace: read each time it is opened (an accept check may have been added), never shown to the client.
function QuoteTrace({ path }: { path: string }) {
  const [trace, setTrace] = useState<QuotationTrace | null>(null);
  const [error, setError] = useState('');
  const loading = useRef(false);
  const open = (event: React.SyntheticEvent<HTMLDetailsElement>) => {
    if (!event.currentTarget.open || loading.current) return;
    loading.current = true; setError('');
    request<QuotationTrace>(path).then(setTrace).catch(caught => setError(failure(caught))).finally(() => { loading.current = false; });
  };
  const check = trace?.available ? trace.accept_check : null;
  return <details className="deal-quote-trace" onToggle={open}><summary>Ver origen de precios</summary>
    {error ? <div className="notice error" role="alert">{error}</div> : !trace ? <p role="status">Cargando origen de la cotización…</p>
      : !trace.available ? <p>Publicada antes de los precios privados.</p> : <>
        <p>Publicada por {trace.publisher.name} · {trace.draft_version ? `borrador v${trace.draft_version}` : 'sin borrador guardado'} · {date(trace.published_at)}</p>
        {check && <p className={`deal-trace-check ${check.result}`}>{acceptResults[check.result]} · {date(check.at)}</p>}
        <div className="table-scroll"><table><thead><tr><th>Artículo</th><th>Ofrecidas</th><th>Disponibles al cotizar</th><th>Al confirmar</th><th>Origen del precio</th><th>Alertas</th></tr></thead>
          <tbody>{trace.lines.map(line => <tr key={line.order_line_id}><td><strong>{line.codigo}</strong><small>{line.description}</small></td><td>{line.quantity}</td>
            <td>{line.identity_ok_at_quote ? line.available_at_quote : 'Artículo cambiado'}</td><td>{line.available_at_accept ?? '—'}</td><td>{priceSources[line.price_source]}</td>
            <td>{line.exceptions.some(item => item.severity !== 'info') ? line.exceptions.filter(item => item.severity !== 'info').map(item => <small key={item.code}>{alertNames[item.code] || item.code} ({item.context}) · {item.acknowledged_by ? `confirmada por ${item.acknowledged_by.name}` : 'sin confirmar'}</small>) : '—'}</td></tr>)}</tbody></table></div>
      </>}
  </details>;
}

function DealChat({ account, orderId, active }: { account: Account; orderId: string; active: boolean }) {
  const path = `/api/market/accounts/${account.id}/deals/${orderId}/messages`;
  const [messages, setMessages] = useState<DealMessage[]>([]);
  const [body, setBody] = useState('');
  const [error, setError] = useState('');
  const [ready, setReady] = useState(false);
  const [sending, setSending] = useState(false);
  const [hasEarlier, setHasEarlier] = useState(false);
  const [loadingEarlier, setLoadingEarlier] = useState(false);
  const log = useRef<HTMLDivElement>(null);
  const mounted = useRef(false);
  const loading = useRef(false);
  const cursor = useRef<number | null>(null);
  const pending = useRef<{ body: string; id: string } | null>(null);
  const sendLock = useRef(false);
  const merge = (items: DealMessage[]) => setMessages(values => [...new Map([...values, ...items].map(value => [value.id, value])).values()].sort((a, b) => a.id - b.id));
  const fetchMessages = useCallback(async () => {
    if (loading.current) return;
    loading.current = true;
    try {
      let more = true;
      while (more && mounted.current) {
        const data = await request<{ results: DealMessage[]; cursor: number; has_more: boolean; has_earlier: boolean }>(`${path}${cursor.current === null ? '' : `?after=${cursor.current}`}`);
        if (!mounted.current) return;
        if (cursor.current === null) setHasEarlier(data.has_earlier);
        const wasAtBottom = !log.current || log.current.scrollHeight - log.current.scrollTop - log.current.clientHeight < 70;
        merge(data.results);
        if (wasAtBottom && data.results.length) requestAnimationFrame(() => { if (log.current) log.current.scrollTop = log.current.scrollHeight; });
        cursor.current = data.cursor; more = data.has_more; setReady(true); setError('');
      }
    } catch (caught) { if (mounted.current) setError(failure(caught)); }
    finally { loading.current = false; }
  }, [path]);
  useEffect(() => { mounted.current = true; void fetchMessages(); const timer = setInterval(() => { if (document.visibilityState === 'visible') void fetchMessages(); }, 5000); return () => { mounted.current = false; clearInterval(timer); }; }, [fetchMessages]);
  useEffect(() => { if (active) void fetchMessages(); }, [active, fetchMessages]);
  async function earlier() {
    if (!messages.length || loadingEarlier) return;
    setLoadingEarlier(true);
    try {
      const data = await request<{ results: DealMessage[]; has_earlier: boolean }>(`${path}?before=${messages[0].id}`);
      if (!mounted.current) return;
      const height = log.current?.scrollHeight || 0;
      merge(data.results); setHasEarlier(data.has_earlier);
      requestAnimationFrame(() => { if (log.current) log.current.scrollTop += log.current.scrollHeight - height; });
    } catch (caught) { if (mounted.current) setError(failure(caught)); }
    finally { if (mounted.current) setLoadingEarlier(false); }
  }
  async function send() {
    const text = body.trim(); if (!text || sendLock.current) return;
    sendLock.current = true; setSending(true); setError('');
    if (pending.current?.body !== text) pending.current = { body: text, id: crypto.randomUUID() };
    try {
      const message = await request<DealMessage>(path, { method: 'POST', body: JSON.stringify({ body: text, message_id: pending.current.id }) });
      if (!mounted.current) return;
      merge([message]); setBody(''); pending.current = null; void fetchMessages();
    } catch (caught) { if (mounted.current) setError(failure(caught)); }
    finally { sendLock.current = false; if (mounted.current) setSending(false); }
  }
  return <section className="deal-chat" aria-label="Conversación del acuerdo"><h3>Conversación privada</h3><p>Cliente y proveedor pueden conversar durante todo el acuerdo. Las decisiones sobre la cotización se realizan en la pestaña Cotización.</p>
    {error && <div className="notice error" role="alert">{error}<button type="button" onClick={() => void fetchMessages()}>Actualizar mensajes</button></div>}
    {hasEarlier && <button type="button" className="button soft small" disabled={loadingEarlier} onClick={() => void earlier()}>Cargar mensajes anteriores</button>}
    <div ref={log} className="deal-chat-messages" role="log" aria-label="Mensajes del acuerdo" aria-live="polite">{!ready && !error ? <p>Cargando mensajes…</p> : !messages.length ? <p className="deal-chat-empty">Inicia la conversación sobre esta orden.</p> : messages.map(message => <article key={message.id} className={message.account_id === account.id ? 'own' : ''}><header><strong>{message.account_name}</strong><span>{message.actor_name} · {date(message.created_at)}</span></header><p>{message.body}</p></article>)}</div>
    <form onSubmit={event => { event.preventDefault(); void send(); }}><label>Mensaje<UppercaseTextarea aria-label="Mensaje del acuerdo" required maxLength={4000} disabled={sending} value={body} onChange={event => setBody(event.target.value)} placeholder="ESCRIBE A LA OTRA PARTE…"/></label><button type="submit" className="button primary" disabled={sending || !body.trim()}>{sending ? <LoaderCircle size={15} className="spin"/> : <Send size={15}/>}Enviar mensaje</button></form>
  </section>;
}
