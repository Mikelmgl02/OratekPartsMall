'use client';
import { useEffect, useState, type ReactNode } from 'react';
import { flushSync } from 'react-dom';
import dynamic from 'next/dynamic';
import Link from 'next/link';
import { ArrowLeft, FileText, GitCompare, Handshake, History, LoaderCircle, LockKeyhole, Maximize2, MessageSquare, Pencil, RefreshCw, Send, UserRoundCog } from 'lucide-react';
import Modal from './modal';
import { percent } from './price-explanation';
import DealStatusBadge from './deal-status';
import DealStages, { NextStepNote, STAGES } from './deal-stages';
import DealAfterHandshake, { type AfterMode } from './deal-after-handshake';
import QuoteChanges from './quote-changes';
import OrderLinesGrid from './order-lines-grid';
import { DealChat, QuoteTrace, QuoteView } from './deal-detail';
import type { Account, ApiError } from '@/lib/types';
import { AFTER_HANDSHAKE, PRE_HANDSHAKE, type Deal, type QuotePayload } from '@/lib/deal-types';
import type { DraftProfile, QuoteDraft } from '@/lib/pricing-types';
import { supplierPricingHref, supplierRequestsHref, supplierSectionHref } from '@/lib/supplier-navigation';

const QuoteEditor = dynamic(() => import('./quotation-editor'), { ssr: false, loading: () => <div className="quotation-grid-loading" aria-busy="true"><LoaderCircle size={18} className="spin"/>Cargando editor de cotización…</div> });
const DealLinesGrid = dynamic(() => import('./deal-lines-grid'), { ssr: false, loading: () => <div className="quotation-grid-loading" aria-busy="true"><LoaderCircle size={18} className="spin"/>Cargando artículos…</div> });
const ClientPricingProfile = dynamic(() => import('./client-pricing-profile'), { ssr: false, loading: () => <div className="loading"><LoaderCircle className="spin"/>Cargando perfil comercial…</div> });

const day = (value: string) => new Date(value).toLocaleDateString('es-PA', { timeZone: 'America/Panama', dateStyle: 'medium' });
const date = (value: string) => new Date(value).toLocaleString('es-PA', { timeZone: 'America/Panama', dateStyle: 'medium', timeStyle: 'short' });
const money = (value: string | number, currency = 'USD') => new Intl.NumberFormat('es-PA', { style: 'currency', currency }).format(Number(value));

export type WorkspaceTab = 'quote' | 'chat' | 'versions' | 'history';
// What DealDetail (deal-detail.tsx) holds for the order and hands to its workspace: the order, its state, the draft and the actions.
// clientActions and missing: the client's decisions (Confirmar, Solicitar ajuste, Rechazar, with their forms) and what the order will not
// bring (Pedir lo que falta), built where their state lives.
export type WorkspaceControl = {
  side: 'client' | 'supplier';
  account: Account; orderId: string; reference?: string; deal: Deal | null; error: string; busy: boolean; notice: string;
  reload: (open?: boolean) => void; dismissError: () => void; act: (action: string, extra?: Record<string, unknown>) => Promise<true | ApiError | false>;
  tab: WorkspaceTab; setTab: (tab: WorkspaceTab) => void;
  viewing: number | null; setViewing: (index: number | null) => void; reached: number; lookingBack: boolean;
  canDecide: boolean; stockWarnings: number; suggestedPrice?: (line: Deal['lines'][number]) => ReactNode;
  editorMounted: boolean; revising: boolean; startPricing: () => void | Promise<void>; startRevising: () => void; closeEditor: () => void;
  draft: { key: string; draft: QuoteDraft | null } | null; draftKey: string; draftPath: string; retryDraft: () => Promise<void>; pricingRevision: number;
  onDraftChange: (persisted: boolean, draft: QuoteDraft) => void; hasDraft: boolean; publishBlocked: boolean;
  pairProfile?: DraftProfile; priceListCode?: string; profileOpen: boolean; setProfileOpen: (open: boolean) => void; profileSaved: () => void;
  afterMode: AfterMode; setAfterMode: (mode: AfterMode) => void; chatInsert: { text: string; id: number } | null; copyToChat: (text: string) => void;
  events: ReactNode; clientActions?: ReactNode; missing?: ReactNode;
};

// An order, laid out like a point-of-sale quotation: a one-line header (the order, its status, the other party and date, the stages), the
// lines as one grid that fills the room (the supplier's editor takes its place while the quotation is prepared), Conversación, Versiones and
// Actividad beside it, and a side column with the summary and total, the next step, the terms and the actions at its foot.
// mode: 'page' (the supplier's order page: back to Solicitudes, the order as its title), 'embedded' (the conversations panel, beside its
// chat: no Conversación) or 'modal' (the client's order dialog, whose title names the order).
export default function OrderWorkspace({ c, mode = 'page' }: { c: WorkspaceControl; mode?: 'page' | 'embedded' | 'modal' }) {
  const { account, deal, busy, tab, setTab, lookingBack } = c;
  const supplier = c.side === 'supplier', embedded = mode === 'embedded';
  // The editor puts its draft, total, terms and alerts in the side column, and its send button with the actions.
  const [aside, setAside] = useState<HTMLDivElement | null>(null);
  const [actions, setActions] = useState<HTMLDivElement | null>(null);
  const [expanded, setExpanded] = useState(false);
  // When the next step is the client's (Recibido, or answering a shortfall) the order opens scrolled to its actions, which can sit below
  // the fold: at the foot of a long side column, or under the lines when a narrow panel or dialog stacks them.
  const [body, setBody] = useState<HTMLDivElement | null>(null);
  const waiting = !supplier && mode !== 'page' && !!deal && !lookingBack && (deal.status === 'delivered' || !!deal.shortfalls?.some(item => item.status === 'pending'));
  useEffect(() => {
    const foot = body?.querySelector('.order-ws-actions');
    if (!waiting || !body || !foot) return;
    for (const box of [foot.closest('.order-ws-side'), body]) {
      if (box && box.scrollHeight > box.clientHeight) box.scrollTop += Math.max(0, foot.getBoundingClientRect().bottom - box.getBoundingClientRect().bottom);
    }
  }, [waiting, body]);
  const stage = c.viewing ?? c.reached, current = stage === c.reached;
  const quote = deal && stage > 0 ? deal.quotation : null;
  const agreement = deal && quote && stage >= 2 && deal.agreement?.lines.some(line => line.missing || line.pending_missing) ? deal.agreement : null;
  const agreedAt = stage >= 2 && deal?.handshaked_at;
  const open = !!deal && PRE_HANDSHAKE.includes(deal.status);
  const showStock = supplier && open && current;
  const editing = supplier && c.editorMounted && !lookingBack;
  const hold = deal && current ? deal.stock_hold : null, held = hold && `${hold.units} ${hold.units === 1 ? 'unidad' : 'unidades'}`;
  // Versiones: what each version changed, each one's document and, for the supplier, where the current one's prices came from.
  const versions = !!deal && ((deal.quotations?.length ?? 0) > 1 || (supplier && !!deal.quotation));
  const tabs = ([['quote', supplier ? 'Líneas' : 'Cotización', Handshake], ['chat', 'Conversación', MessageSquare], ['versions', 'Versiones', GitCompare],
    ['history', 'Actividad', History]] as const).filter(([value]) => !(embedded && value === 'chat') && (value !== 'versions' || versions));
  const head = <header className="order-ws-head">
    {mode === 'page' && <Link className="order-ws-back" href={supplierRequestsHref(account.id)} aria-label="Volver a solicitudes"><ArrowLeft size={15}/>Solicitudes</Link>}
    <div className="order-ws-title">
      {mode === 'page' ? <h1>Orden {deal?.reference || c.reference || ''}</h1> : embedded ? <h2>{deal?.reference || c.reference || ''}</h2> : null}
      {deal && <DealStatusBadge status={deal.status} onClick={() => c.setViewing(null)}/>}
      {deal && <small>{supplier ? deal.client.name : deal.supplier.name} · {day(deal.created_at)}</small>}
    </div>
    {deal && <DealStages deal={deal} viewing={stage} onView={index => c.setViewing(index === c.reached ? null : index)}/>}
  </header>;
  return <section className={`deal-order-content order-ws mode-${mode} side-${c.side}${mode !== 'page' ? ' contained' : ''}`} aria-label={`Orden ${deal?.reference || c.reference || ''}`.trim()}>
    {head}
    {c.error && <div className="notice error" role="alert"><span>{c.error}</span><button type="button" disabled={busy} onClick={c.dismissError}>Actualizar acuerdo</button></div>}
    {!deal && !c.error && <div className="supplier-request-detail-loading" role="status"><span className="sr-only">Cargando orden…</span><div className="skeleton-block"/><div className="skeleton-block"/></div>}
    {deal && <div ref={setBody} className="order-ws-body">
      <div className="order-ws-main">
        <nav className="deal-tabs" aria-label="Secciones del acuerdo">{tabs.map(([value, label, Icon]) => <button type="button" key={value} className={tab === value ? 'selected' : ''} aria-pressed={tab === value}
            title={value === 'quote' && c.hasDraft ? 'Hay un borrador de cotización guardado' : undefined} onClick={() => setTab(value)}>
            <Icon size={15}/>{label}{value === 'quote' && c.hasDraft && <span className="deal-tab-dot" aria-hidden="true"/>}</button>)}
          {tab === 'quote' && !editing && <div className="order-ws-tools">
            {showStock && <button type="button" className="button soft small" disabled={busy} onClick={() => c.reload()}><RefreshCw size={14}/>Actualizar existencias</button>}
            <button type="button" className="button soft small" aria-label="Ampliar la tabla" title="Ver la tabla en grande, con todas sus columnas" onClick={() => setExpanded(true)}><Maximize2 size={14}/></button>
          </div>}
        </nav>
        <div className="order-ws-panel" hidden={tab !== 'quote'}>
          {showStock && !editing && c.stockWarnings > 0 && <div className="notice supplier-request-stock-warning" role="status">{c.stockWarnings} {c.stockWarnings === 1 ? 'artículo requiere' : 'artículos requieren'} confirmar disponibilidad. La orden conserva todas las unidades pedidas.</div>}
          {!editing && <OrderLinesGrid deal={deal} quote={quote} agreement={agreement} showStock={showStock} price={supplier && c.suggestedPrice && !lookingBack ? c.suggestedPrice : undefined}/>}
          {supplier && c.editorMounted && <div className="order-ws-editor" hidden={lookingBack}>{c.draft?.key === c.draftKey
            ? <QuoteEditor key={`${c.draftKey}:${c.draft.draft ? c.draft.draft.persisted ? 'saved' : 'virtual' : 'fallback'}`} deal={deal} draft={c.draft.draft} draftPath={c.draftPath}
                disabled={busy} onSend={(values: QuotePayload) => c.act('quote', values)} onRetryDraft={c.retryDraft} pricingRevision={c.pricingRevision}
                simulatorHref={supplierPricingHref(account.id, 'simulator')} onCopyToChat={c.copyToChat} onDraftChange={c.onDraftChange}
                layout="workspace" aside={aside} actions={actions} onClose={c.closeEditor}
                // Shown at once (not after the click), so the grid can scroll to the line and focus it.
                onReveal={() => { if (tab !== 'quote') flushSync(() => setTab('quote')); }}/>
            : <div className="quotation-grid-loading" aria-busy="true"><LoaderCircle size={18} className="spin"/>Cargando borrador de cotización…</div>}</div>}
        </div>
        {/* Kept mounted, so refreshes and a message being written survive the tabs. */}
        {!embedded && <div className="order-ws-panel chat" hidden={tab !== 'chat'}>
          <DealChat account={account} orderId={c.orderId} reference={deal.reference} active={tab === 'chat'} supplier={supplier} insert={c.chatInsert} compact/>
        </div>}
        {tab === 'versions' && <div className="order-ws-panel versions">
          <QuoteChanges deal={deal} renderVersion={version => <QuoteView quote={version} historical/>}/>
          {supplier && deal.quotation && <QuoteTrace key={deal.quotation.id} path={`/api/market/accounts/${account.id}/requests/${c.orderId}/quotations/${deal.quotation.id}/trace`} currency={deal.quotation.currency}/>}
        </div>}
        {tab === 'history' && <div className="order-ws-panel history">{c.events}</div>}
      </div>
      <aside className="order-ws-side" aria-label="Resumen de la orden">
        {c.notice && !lookingBack && <div className="notice success" role="status">{c.notice}</div>}
        {/* While a new version is prepared, the one the client has stays in view; before the first, the editor's total says it all. */}
        {(!editing || quote) && <section className="deal-quote order-ws-summary" aria-label={quote ? `Cotización vigente v${quote.revision}` : 'Artículos solicitados'}>
          <div className="deal-quote-heading"><div><h3>{quote ? `Cotización v${quote.revision}` : 'Artículos solicitados'}</h3>
            <small>{!quote ? supplier ? 'Por cotizar' : 'Sin cotizar' : agreedAt ? `Acordada · ${date(agreedAt)}` : `Enviada por el proveedor · ${date(quote.supplier_confirmed_at)}`}</small>
            {quote && !agreedAt && <div className="deal-handshake-parties">
              <span className="ok" title={date(quote.supplier_confirmed_at)}>Proveedor ✓ confirmó</span>
              <span className={quote.client_confirmed_at ? 'ok' : ''}>{quote.client_confirmed_at ? 'Cliente ✓ confirmó' : 'Cliente · por confirmar'}</span></div>}</div></div>
          <dl className="order-ws-figures">
            <div><dt>Artículos</dt><dd>{deal.line_count.toLocaleString('es-PA')}</dd></div>
            <div><dt>Unidades pedidas</dt><dd>{deal.unit_count.toLocaleString('es-PA')}</dd></div>
            {quote && <div className="total"><dt>Total</dt><dd>{money(agreement ? agreement.total : quote.total, quote.currency)}</dd></div>}
          </dl>
          {!quote && !supplier && <p className="deal-document-hint">El proveedor preparará tu cotización: aquí verás lo que ofrece de cada artículo, sus precios y condiciones.</p>}
          {quote?.terms && <div className="deal-terms"><h4>Condiciones</h4><p>{quote.terms}</p></div>}
        </section>}
        {supplier && <div ref={setAside} className="order-ws-slot" hidden={lookingBack}/>}
        {lookingBack ? <div className="deal-looking-back" role="status"><span>Estás viendo una etapa anterior ({STAGES[stage].label}); la orden está en <strong>{STAGES[c.reached].label}</strong>.</span>
          <button type="button" className="button soft small" onClick={() => c.setViewing(null)}>Volver a la etapa actual</button></div>
          : <NextStepNote deal={deal} supplier={supplier} stockWarnings={c.stockWarnings}/>}
        {hold && <p className={`deal-hold ${hold.state}`} role="note" aria-label="Reserva de existencias"><LockKeyhole size={14}/><span>{hold.state === 'held'
          ? supplier ? `Esta cotización reserva ${held} de tu inventario${hold.until ? ` hasta el ${date(hold.until)}` : ''}: tus otras cotizaciones no las cuentan.`
            : `${deal.supplier.name} reserva estas ${held} para ti. Confírmala${hold.until ? ` antes del ${date(hold.until)}` : ''} para asegurarlas.`
          : supplier ? `La reserva venció${hold.until ? ` el ${date(hold.until)}` : ''}: sus ${held} volvieron a tu inventario. Si el cliente confirma, se revisan tus existencias.`
            : `La reserva de existencias venció${hold.until ? ` el ${date(hold.until)}` : ''}: si confirmas, se revisa que el proveedor aún tenga las unidades.`}</span></p>}
        {supplier && c.pairProfile && <div className="deal-pair-profile" role="group" aria-label="Perfil comercial del cliente">
          <p><UserRoundCog size={15}/>{c.pairProfile.exists ? [c.priceListCode && `Lista ${c.priceListCode}`, Number(c.pairProfile.discount_percent) > 0 && `Desc. ${percent(c.pairProfile.discount_percent)}`,
            c.pairProfile.customer_code && `Código ${c.pairProfile.customer_code}`].filter(Boolean).join(' · ') || 'Perfil comercial sin condiciones especiales'
            : `Sin perfil comercial${c.priceListCode ? ` · se usa la lista ${c.priceListCode}` : ''}`}
            <button type="button" className="button text small" onClick={() => c.setProfileOpen(true)}><Pencil size={13}/>{c.pairProfile.exists ? 'Editar perfil' : 'Configurar cliente'}</button></p>
          {c.pairProfile.internal_notes && <small title="Solo tu equipo ve estas notas">Notas internas: {c.pairProfile.internal_notes}</small>}
        </div>}
        {!supplier && current && c.missing}
        {/* A shortfall the supplier reported waits for the client's answer, in the actions below. */}
        {!supplier && !lookingBack && deal.shortfalls?.some(item => item.status === 'pending') && <div className="notice deal-shortfall-alert" role="status">
          <span>El proveedor reportó un faltante. Revisa el acuerdo reducido y decide.</span>
          <button type="button" className="button soft small" onClick={event => event.currentTarget.closest('.order-ws-side')?.querySelector('.deal-after')?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })}>Ver faltante</button></div>}
        {deal.notes && <div className="deal-terms"><h4>Notas del cliente</h4><p>{deal.notes}</p></div>}
        <div className="order-ws-actions">
          {!lookingBack && (supplier ? <>
            {['pending', 'reviewed', 'adjustment'].includes(deal.status) && !c.editorMounted && <button type="button" className="button primary" disabled={busy} onClick={event => { if (event.detail < 2) void c.startPricing(); }}>Preparar cotización<FileText size={16}/></button>}
            {c.canDecide && deal.status === 'quoted' && !c.revising && <button type="button" className="button primary" disabled={busy} onClick={event => { if (event.detail < 2) c.startRevising(); }}><Pencil size={15}/>Editar cotización</button>}
            <div ref={setActions} className="order-ws-slot"/>
            {deal.status === 'adjustment' && deal.quotation && <button type="button" className="button soft" disabled={busy || c.publishBlocked} onClick={() => void c.act('return_quote', { quotation_id: deal.quotation!.id })}>Devolver la misma cotización para confirmar<Send size={15}/></button>}
            {deal.status === 'adjustment' && deal.quotation && c.publishBlocked && <small className="deal-draft-warning">Solo un miembro con permiso para enviar cotizaciones puede devolverla.</small>}
            {deal.status === 'adjustment' && deal.quotation && c.hasDraft && <small className="deal-draft-warning">Si devuelves la misma cotización, se descartará el borrador en curso.</small>}
            {AFTER_HANDSHAKE.includes(deal.status) && <DealAfterHandshake key={deal.version} deal={deal} supplier busy={busy} act={c.act} canDecide={c.canDecide}
              inventoryHref={supplierSectionHref(account.id, 'inventory')} mode={c.afterMode} onMode={c.setAfterMode} stepButtons/>}
          </> : <>
            {c.clientActions}
            {AFTER_HANDSHAKE.includes(deal.status) && <DealAfterHandshake key={deal.version} deal={deal} supplier={false} busy={busy} act={c.act} canDecide
              mode={c.afterMode} onMode={c.setAfterMode} stepButtons/>}
          </>)}
        </div>
      </aside>
    </div>}
    {supplier && c.profileOpen && deal && <Modal wide title={`Perfil comercial · ${deal.client.name}`} className="client-profile-modal" onClose={() => c.setProfileOpen(false)}>
      <ClientPricingProfile account={account} clientId={deal.client.id} embedded onSaved={c.profileSaved}/>
      <p className="form-footnote">Este perfil también está en <a href={supplierPricingHref(account.id, 'clients', deal.client.id)}>Precios › Clientes</a>.</p>
    </Modal>}
    {expanded && deal && <Modal wide title={`${deal.reference} · ${quote ? `Cotización v${quote.revision}` : 'Artículos solicitados'}`} className="deal-document-modal" onClose={() => setExpanded(false)}>
      <p className="deal-document-modal-summary"><span>{deal.line_count} {deal.line_count === 1 ? 'artículo' : 'artículos'} · {deal.unit_count.toLocaleString('es-PA')} {deal.unit_count === 1 ? 'unidad solicitada' : 'unidades solicitadas'}</span>
        {quote && <strong>{money(quote.total, quote.currency)}</strong>}
        {quote && agreement && agreement.total !== quote.total && <small>Acordado tras faltantes: {money(agreement.total, quote.currency)}</small>}</p>
      <DealLinesGrid lines={deal.lines} supplier={supplier} price={supplier && c.suggestedPrice && !lookingBack ? c.suggestedPrice : undefined} quote={quote} maxRows={40}/>
      {quote?.terms && <div className="deal-terms"><h4>Condiciones</h4><p>{quote.terms}</p></div>}
      {deal.notes && <div className="deal-terms"><h4>Notas del cliente</h4><p>{deal.notes}</p></div>}
    </Modal>}
  </section>;
}
