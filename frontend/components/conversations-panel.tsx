'use client';
import { type KeyboardEvent as ReactKeyboardEvent, type PointerEvent as ReactPointerEvent, useCallback, useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { ArrowLeft, ArrowUpRight, FileText, MessagesSquare, PanelLeft } from 'lucide-react';
import Modal from './modal';
import ConversationList, { mostRecent } from './conversation-list';
import ConversationDocuments, { type ConversationDocument } from './conversation-documents';
import DealDetail, { DealChat } from './deal-detail';
import DealStatusBadge from './deal-status';
import { type Account, type Page, request } from '@/lib/types';
import { InboxConversation, InboxTarget, targetOf } from '@/lib/inbox';
import { onDealNudge } from '@/lib/realtime';

// The chat and the deal detail need only the account's id (and its side) to talk to the account-scoped deal API.
const accountOf = (target: InboxTarget): Account => ({ id: target.account.id, name: target.account.name, roles: [], capabilities: [target.side] });

// Laid out like a desktop chat app: the conversations on the left (one per client and supplier, grouped, conversation-list.tsx), the
// conversation's chat in the middle (all its orders, linear), and on the right its Cotizaciones: a table of its orders
// (conversation-documents.tsx), where choosing one opens its document (items, quotation, activity) in its place until Cotizaciones
// brings the table back. What is written goes about the order last opened (or the one in progress), as the composer says. Both side
// columns are resizable (drag or arrow keys on the dividers), the list and the table can be hidden, and the layout is remembered in this
// browser. On a phone the list, the chat and the table take turns.
type Layout = { list: number; side: number; listOpen: boolean; sideOpen: boolean };
const LAYOUT_KEY = 'motionpartes:inbox:layout';
const DEFAULT_LAYOUT: Layout = { list: 300, side: 540, listOpen: true, sideOpen: true };
const LIST_MIN = 220, LIST_MAX = 440, SIDE_MIN = 340, CHAT_MIN = 360, STEP = 24;
const wide = () => window.matchMedia('(min-width: 901px)').matches;
const clamp = (value: number, min: number, max: number) => Math.round(Math.min(Math.max(value, min), Math.max(min, max)));

function savedLayout(): Layout {
  let saved: Partial<Layout> = {};
  try { saved = JSON.parse(localStorage.getItem(LAYOUT_KEY) || '{}'); } catch { /* the default layout */ }
  // On a phone the list and the chat take turns, and the table opens over the chat from the header.
  return { ...DEFAULT_LAYOUT, ...saved, ...(wide() ? {} : { listOpen: true, sideOpen: false }) };
}

export default function ConversationsPanel({ initial, onClose, onShowing }: { initial?: InboxTarget; onClose: () => void; onShowing: (orderId: string | null) => void }) {
  const [selected, setSelected] = useState<InboxTarget | null>(initial ?? null);
  // The open conversation's row: its orders, for the header's order picker (read again when another conversation opens).
  const [thread, setThread] = useState<InboxConversation | null>(null);
  const conversationKey = selected ? selected.threadId ?? selected.orderId : null;
  useEffect(() => {
    setThread(null);
    if (!conversationKey) return;
    let current = true;
    request<Page<InboxConversation>>(`/api/market/inbox/conversations?order=${encodeURIComponent(conversationKey)}`).then(data => {
      const row = data.results[0];
      if (!current || !row) return;
      setThread(row);
      // A conversation opened by its id alone (from the catalog) shows its current order, if it has one; the question still goes in general.
      setSelected(target => target && (!target.threadId || (!target.orderId && row.order_id)) ? { ...target, threadId: row.id, orderId: target.orderId || row.order_id || '',
        reference: target.reference || row.reference } : target);
    }).catch(() => {});
    return () => { current = false; };
  }, [conversationKey]);
  // The conversation's orders for the table, read again when one of them changes (the websocket's nudge) and every half minute.
  const [docs, setDocs] = useState<ConversationDocument[] | null>(null);
  const [docsFailed, setDocsFailed] = useState(false);
  const docsPath = selected?.threadId ? `/api/market/accounts/${selected.account.id}/threads/${selected.threadId}/orders` : null;
  const docsRequest = useRef(0);
  const loadDocs = useCallback(async () => {
    if (!docsPath) return;
    const ticket = ++docsRequest.current;
    try {
      const data = await request<{ results: ConversationDocument[] }>(docsPath);
      if (ticket === docsRequest.current) { setDocs(data.results); setDocsFailed(false); }
    } catch { if (ticket === docsRequest.current) setDocsFailed(true); }
  }, [docsPath]);
  useEffect(() => {
    setDocs(null); setDocsFailed(false);
    if (!docsPath || !selected?.threadId) return;
    void loadDocs();
    const timer = setInterval(() => { if (document.visibilityState === 'visible') void loadDocs(); }, 30000);
    const off = onDealNudge(selected.threadId, ['event'], () => void loadDocs());
    return () => { clearInterval(timer); off(); docsRequest.current++; };
  }, [docsPath, loadDocs]);  // eslint-disable-line react-hooks/exhaustive-deps
  // doc: the order whose document the right column opened (kept mounted under the table, so an open editor keeps its state); docOpen: it
  // shows instead of the table. A notice about one order opens on its document; a conversation chosen in the list, on its table.
  const [doc, setDoc] = useState<string | null>(initial?.orderId && !initial.draft ? initial.orderId : null);
  const [docOpen, setDocOpen] = useState(!!initial?.orderId && !initial.draft);
  const [dealOpen, setDealOpen] = useState(false);
  // A question the quote assistant suggests lands in the panel's chat composer (never sent by itself).
  const [chatInsert, setChatInsert] = useState<{ text: string; id: number; note?: string } | null>(
    initial?.draft ? { text: initial.draft, id: Date.now(), note: 'Escribe tu pregunta y envíala: le llega al proveedor en su conversación contigo.' } : null);
  const [layout, setLayout] = useState<Layout>(savedLayout);
  const box = useRef<HTMLDivElement>(null);
  // Setting prices widens the order's panel for a while; the layout the person chose comes back afterwards.
  const beforePricing = useRef<Layout | null>(null);
  // With the list hidden, hovering its button shows it floating over the chat (as in a desktop app). It hides a moment after the pointer
  // leaves both, unless the person is typing in it; choosing a conversation or Escape hides it too, and pressing the button docks it.
  const header = useRef<HTMLElement>(null);
  const [peekTop, setPeekTop] = useState<number | null>(null);
  const peekTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const peekHovered = useRef(false);
  function peek(inside: boolean) {
    peekHovered.current = inside;
    clearTimeout(peekTimer.current);
    peekTimer.current = setTimeout(() => {
      if (inside) setPeekTop(Math.round((header.current?.getBoundingClientRect().bottom ?? 0) - (box.current?.getBoundingClientRect().top ?? 0)) + 6);
      else if (!(document.activeElement instanceof HTMLInputElement && document.activeElement.closest('.inbox-list'))) setPeekTop(null);
    }, inside ? 90 : 300);
  }
  function peekEvent(kind: 'enter' | 'leave' | 'blur') {
    if (kind !== 'blur') peek(kind === 'enter');
    else if (!peekHovered.current) peek(false);  // done typing in it, with the pointer already elsewhere
  }
  function unpeek() { clearTimeout(peekTimer.current); peekHovered.current = false; setPeekTop(null); }
  useEffect(() => () => clearTimeout(peekTimer.current), []);
  useEffect(() => {
    if (peekTop === null) return;
    // Escape hides the floating list (not the panel), unless one of its menus or a group's name is taking it.
    const escape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' || document.querySelector('.inbox-list .inbox-menu') || (event.target as Element | null)?.closest?.('.inbox-group-form')) return;
      event.preventDefault(); event.stopPropagation(); unpeek();
    };
    document.addEventListener('keydown', escape, true);
    return () => document.removeEventListener('keydown', escape, true);
  }, [peekTop]);
  // On a wide screen the most recent conversation opens by itself.
  const loaded = useCallback((rows: InboxConversation[]) => {
    const recent = mostRecent(rows);
    setSelected(current => current ?? (recent && wide() ? targetOf(recent) : null));
  }, []);
  useEffect(() => { onShowing(selected?.threadId ?? selected?.orderId ?? null); }, [selected, onShowing]);
  useEffect(() => {
    if (beforePricing.current || !wide()) return;
    try { localStorage.setItem(LAYOUT_KEY, JSON.stringify(layout)); } catch { /* the layout lasts this visit */ }
  }, [layout]);
  function choose(target: InboxTarget) {
    setSelected(target); setDoc(null); setDocOpen(false);
    unpeek();
    if (!wide()) setLayout(current => ({ ...current, sideOpen: false }));
  }
  // A row of the table (or an order's chip in the chat) opens its document; the chat then writes about it.
  function openDoc(order: { id: string; reference: string }) {
    setSelected(target => target && { ...target, orderId: order.id, reference: order.reference });
    setDoc(order.id); setDocOpen(true);
    setLayout(current => current.sideOpen ? current : { ...current, sideOpen: true });
  }
  const widthOf = () => box.current?.getBoundingClientRect().width ?? 1200;
  function limits(which: 'list' | 'side', current = layout) {
    return which === 'list' ? [LIST_MIN, Math.min(LIST_MAX, widthOf() - CHAT_MIN - (current.sideOpen ? current.side : 0))]
      : [SIDE_MIN, widthOf() - CHAT_MIN - (current.listOpen ? current.list : 0)];
  }
  function drag(which: 'list' | 'side') {
    return (event: ReactPointerEvent<HTMLDivElement>) => {
      event.preventDefault();
      const startX = event.clientX, start = layout[which], [min, max] = limits(which), sign = which === 'list' ? 1 : -1;
      document.body.classList.add('inbox-resizing');
      const move = (moved: PointerEvent) => setLayout(current => ({ ...current, [which]: clamp(start + sign * (moved.clientX - startX), min, max) }));
      const up = () => { window.removeEventListener('pointermove', move); window.removeEventListener('pointerup', up); document.body.classList.remove('inbox-resizing'); };
      window.addEventListener('pointermove', move); window.addEventListener('pointerup', up);
    };
  }
  function nudge(which: 'list' | 'side') {
    return (event: ReactKeyboardEvent<HTMLDivElement>) => {
      if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
      event.preventDefault();
      const grow = (event.key === 'ArrowRight') === (which === 'list');
      setLayout(current => { const [min, max] = limits(which, current); return { ...current, [which]: clamp(current[which] + (grow ? STEP : -STEP), min, max) }; });
    };
  }
  const [pricing, setPricing] = useState(false);
  const pricingChanged = useCallback((pricing: boolean) => {
    if (!wide()) return;
    setLayout(current => {
      if (pricing && !beforePricing.current) {
        beforePricing.current = current;
        const room = (box.current?.getBoundingClientRect().width ?? 1200) - CHAT_MIN;
        return { ...current, listOpen: false, sideOpen: true, side: clamp(Math.max(current.side, room * 0.62), SIDE_MIN, room) };
      }
      if (!pricing && beforePricing.current) {
        const restore = beforePricing.current; beforePricing.current = null;
        return { ...current, listOpen: restore.listOpen, side: restore.side };
      }
      return current;
    });
  }, []);
  // Setting prices widens the column only while the document shows (not the table over it).
  useEffect(() => { pricingChanged(pricing && docOpen); }, [pricing, docOpen, pricingChanged]);
  const separator = (which: 'list' | 'side', label: string) => <div className="inbox-resizer" role="separator" aria-orientation="vertical" aria-label={label} tabIndex={0}
    aria-valuenow={layout[which]} aria-valuemin={which === 'list' ? LIST_MIN : SIDE_MIN} onPointerDown={drag(which)} onKeyDown={nudge(which)}
    onDoubleClick={() => setLayout(current => ({ ...current, [which]: DEFAULT_LAYOUT[which] }))}/>;
  const listToggle = <button type="button" className="icon-button inbox-list-toggle" aria-pressed={layout.listOpen} aria-label={layout.listOpen ? 'Ocultar conversaciones' : 'Mostrar conversaciones'}
    title={layout.listOpen ? 'Ocultar conversaciones' : 'Mostrar conversaciones'} onClick={() => { unpeek(); setLayout(current => ({ ...current, listOpen: !current.listOpen })); }}
    onPointerEnter={event => { if (event.pointerType === 'mouse' && !layout.listOpen) peek(true); }} onPointerLeave={() => { if (!layout.listOpen) peek(false); }}><PanelLeft size={18}/></button>;

  // A conversation with orders has the table; one without (a question from the catalog) is only its chat.
  const hasOrders = !!selected?.orderId;
  const sideShown = hasOrders && layout.sideOpen;
  const count = docs?.length ?? thread?.order_count ?? 0;
  const opened = docs?.find(item => item.id === selected?.orderId);
  const toggleSide = () => setLayout(current => ({ ...current, sideOpen: !current.sideOpen }));

  return <Modal title="Conversaciones" className={`inbox-modal${selected ? ' has-selection' : ''}`} onClose={onClose}>
    <div ref={box} className={`inbox-layout${layout.listOpen ? '' : ' list-closed'}`} style={{ '--inbox-list': `${layout.list}px`, '--inbox-side': `${layout.side}px` } as React.CSSProperties}>
      {/* Kept mounted while hidden, so its search, folded groups and scroll stay as they were. */}
      <ConversationList hidden={!layout.listOpen && peekTop === null} floatTop={layout.listOpen ? null : peekTop} onPeek={layout.listOpen ? undefined : peekEvent}
        selected={selected} selectedSide={selected?.side} onChoose={choose} onLoaded={loaded}/>
      {layout.listOpen && separator('list', 'Ajustar el ancho de las conversaciones')}
      {selected ? <section className="inbox-conversation" aria-label={`Conversación con ${selected.counterpart.name}`}>
        <header ref={header} className="inbox-conversation-header">
          <button type="button" className="icon-button inbox-back" aria-label="Volver a conversaciones" onClick={() => setSelected(null)}><ArrowLeft size={18}/></button>
          {listToggle}
          <div className="inbox-conversation-title"><strong>{selected.counterpart.name}</strong><span>{selected.side === 'supplier' ? 'Tu cliente' : 'Tu proveedor'} · {!hasOrders
            ? 'Consulta, sin órdenes' : `${count || 1} ${count === 1 || !count ? 'orden' : 'órdenes'}`}</span></div>
          {hasOrders && <button type="button" className="button soft small inbox-docs-toggle" aria-pressed={layout.sideOpen} onClick={toggleSide}
            title={layout.sideOpen ? 'Ocultar las cotizaciones' : 'Ver las cotizaciones de esta conversación'}><FileText size={15}/><span>Cotizaciones</span></button>}
        </header>
        <div className={`inbox-conversation-body${sideShown ? ' side-open' : ''}`}>
          <div className="inbox-chat"><DealChat key={selected.threadId ?? selected.orderId} account={accountOf(selected)} orderId={selected.orderId || undefined} thread={selected.threadId}
            reference={selected.reference} active supplier={selected.side === 'supplier'} insert={chatInsert} startGeneral={!!initial?.draft && initial.threadId === selected.threadId}
            onOrder={hasOrders ? openDoc : undefined} compact/></div>
          {sideShown && separator('side', 'Ajustar el ancho de las cotizaciones')}
          {hasOrders && <div className="inbox-side" hidden={!layout.sideOpen}>
            <ConversationDocuments docs={docs} failed={docsFailed} current={selected.orderId} supplier={selected.side === 'supplier'} counterpart={selected.counterpart.name}
              hidden={docOpen && doc === selected.orderId} onOpen={openDoc} onRetry={() => void loadDocs()}/>
            {/* Kept mounted under the table: an open editor, a half-written adjustment or a confirmation keeps its state. */}
            {doc === selected.orderId && <div className="inbox-doc" hidden={!docOpen}>
              <div className="inbox-doc-bar">
                <button type="button" className="button text small inbox-doc-back" onClick={() => setDocOpen(false)} aria-label="Volver a las cotizaciones" title="Volver a la tabla de cotizaciones"><ArrowLeft size={15}/>Cotizaciones</button>
                {selected.side === 'supplier'
                  ? <Link className="button soft small inbox-open-order" href={`/proveedor/${selected.account.id}/ordenes/${selected.orderId}`} onClick={onClose} title="Abrir orden"><ArrowUpRight size={15}/><span>Abrir orden</span></Link>
                  : <button type="button" className="button soft small inbox-open-order" onClick={() => setDealOpen(true)} title="Abrir orden"><ArrowUpRight size={15}/><span>Abrir orden</span></button>}
              </div>
              <div className="inbox-doc-title"><h3>{selected.reference}</h3>{opened && <DealStatusBadge status={opened.status}/>}</div>
              <DealDetail key={selected.orderId} embedded account={accountOf(selected)} orderId={selected.orderId} reference={selected.reference} side={selected.side}
                onChanged={() => void loadDocs()} onPricing={setPricing}
                onCopyToChat={text => { setChatInsert({ text, id: Date.now() }); if (!wide()) setLayout(current => ({ ...current, sideOpen: false })); }}/>
            </div>}
          </div>}
        </div>
      </section> : <section className="inbox-conversation inbox-placeholder">{!layout.listOpen && listToggle}<MessagesSquare size={30}/><p>Elige una conversación para ver su chat y sus cotizaciones.</p></section>}
    </div>
    {dealOpen && selected && <DealDetail account={accountOf(selected)} orderId={selected.orderId} reference={selected.reference} side="client" onClose={() => setDealOpen(false)}/>}
  </Modal>;
}
