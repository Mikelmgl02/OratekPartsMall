'use client';
import { type KeyboardEvent as ReactKeyboardEvent, type PointerEvent as ReactPointerEvent, useCallback, useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { ArrowLeft, ArrowUpRight, FileText, Handshake, History, MessagesSquare, PanelLeft } from 'lucide-react';
import Modal from './modal';
import ConversationList, { mostRecent } from './conversation-list';
import DealDetail, { DealChat, type DealTab } from './deal-detail';
import type { Account } from '@/lib/types';
import { InboxConversation, InboxTarget, targetOf } from '@/lib/inbox';

// The chat and the deal detail need only the account's id (and its side) to talk to the account-scoped deal API.
const accountOf = (target: InboxTarget): Account => ({ id: target.account.id, name: target.account.name, roles: [], capabilities: [target.side] });

// Laid out like a desktop chat app: the conversations on the left (grouped, conversation-list.tsx), the chat in the middle, and the order's sections (Cotización,
// Artículos, Actividad) toggled from the header's top-right corner into a panel on the right. Both side columns are resizable (drag
// or arrow keys on the dividers) and the list can be hidden; the layout is remembered in this browser. On a phone the list, the chat
// and a section take turns.
type Layout = { list: number; side: number; listOpen: boolean; sideOpen: boolean };
const LAYOUT_KEY = 'motionpartes:inbox:layout';
const DEFAULT_LAYOUT: Layout = { list: 300, side: 540, listOpen: true, sideOpen: true };
const LIST_MIN = 220, LIST_MAX = 440, SIDE_MIN = 340, CHAT_MIN = 360, STEP = 24;
const SECTIONS: [Exclude<DealTab, 'chat'>, string, typeof FileText][] = [['quote', 'Cotización', Handshake], ['order', 'Artículos', FileText], ['history', 'Actividad', History]];
const wide = () => window.matchMedia('(min-width: 901px)').matches;
const clamp = (value: number, min: number, max: number) => Math.round(Math.min(Math.max(value, min), Math.max(min, max)));

function savedLayout(): Layout {
  let saved: Partial<Layout> = {};
  try { saved = JSON.parse(localStorage.getItem(LAYOUT_KEY) || '{}'); } catch { /* the default layout */ }
  // On a phone the list and the chat take turns, and a section opens over the chat from the header.
  return { ...DEFAULT_LAYOUT, ...saved, ...(wide() ? {} : { listOpen: true, sideOpen: false }) };
}

export default function ConversationsPanel({ initial, onClose, onShowing }: { initial?: InboxTarget; onClose: () => void; onShowing: (orderId: string | null) => void }) {
  const [selected, setSelected] = useState<InboxTarget | null>(initial ?? null);
  const [dealOpen, setDealOpen] = useState(false);
  // A question the quote assistant suggests lands in the panel's chat composer (never sent by itself).
  const [chatInsert, setChatInsert] = useState<{ text: string; id: number } | null>(null);
  const [layout, setLayout] = useState<Layout>(savedLayout);
  const [section, setSection] = useState<DealTab>('order');
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
  useEffect(() => { onShowing(selected?.orderId ?? null); }, [selected, onShowing]);
  useEffect(() => {
    if (beforePricing.current || !wide()) return;
    try { localStorage.setItem(LAYOUT_KEY, JSON.stringify(layout)); } catch { /* the layout lasts this visit */ }
  }, [layout]);
  function choose(target: InboxTarget) {
    setSelected(target); setSection('order');  // the deal shows its quotation instead once it loads one
    unpeek();
    if (!wide()) setLayout(current => ({ ...current, sideOpen: false }));
  }
  // A section button opens its section, switches to it, or (pressed again) closes the panel.
  function toggleSection(value: DealTab) {
    const open = layout.sideOpen && section === value;
    setSection(value);
    setLayout(current => ({ ...current, sideOpen: !open }));
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
  const separator = (which: 'list' | 'side', label: string) => <div className="inbox-resizer" role="separator" aria-orientation="vertical" aria-label={label} tabIndex={0}
    aria-valuenow={layout[which]} aria-valuemin={which === 'list' ? LIST_MIN : SIDE_MIN} onPointerDown={drag(which)} onKeyDown={nudge(which)}
    onDoubleClick={() => setLayout(current => ({ ...current, [which]: DEFAULT_LAYOUT[which] }))}/>;
  const listToggle = <button type="button" className="icon-button inbox-list-toggle" aria-pressed={layout.listOpen} aria-label={layout.listOpen ? 'Ocultar conversaciones' : 'Mostrar conversaciones'}
    title={layout.listOpen ? 'Ocultar conversaciones' : 'Mostrar conversaciones'} onClick={() => { unpeek(); setLayout(current => ({ ...current, listOpen: !current.listOpen })); }}
    onPointerEnter={event => { if (event.pointerType === 'mouse' && !layout.listOpen) peek(true); }} onPointerLeave={() => { if (!layout.listOpen) peek(false); }}><PanelLeft size={18}/></button>;

  return <Modal title="Conversaciones" className={`inbox-modal${selected ? ' has-selection' : ''}`} onClose={onClose}>
    <div ref={box} className={`inbox-layout${layout.listOpen ? '' : ' list-closed'}`} style={{ '--inbox-list': `${layout.list}px`, '--inbox-side': `${layout.side}px` } as React.CSSProperties}>
      {/* Kept mounted while hidden, so its search, folded groups and scroll stay as they were. */}
      <ConversationList hidden={!layout.listOpen && peekTop === null} floatTop={layout.listOpen ? null : peekTop} onPeek={layout.listOpen ? undefined : peekEvent}
        selectedId={selected?.orderId ?? null} selectedSide={selected?.side} onChoose={choose} onLoaded={loaded}/>
      {layout.listOpen && separator('list', 'Ajustar el ancho de las conversaciones')}
      {selected ? <section className="inbox-conversation" aria-label={`Orden ${selected.reference} con ${selected.counterpart.name}`}>
        <header ref={header} className="inbox-conversation-header">
          <button type="button" className="icon-button inbox-back" aria-label="Volver a conversaciones" onClick={() => setSelected(null)}><ArrowLeft size={18}/></button>
          {listToggle}
          <div className="inbox-conversation-title"><strong>{selected.counterpart.name}</strong><span>{selected.reference} · {selected.side === 'supplier' ? 'tu cliente' : 'tu proveedor'}</span></div>
          <div className="inbox-sections" role="group" aria-label="Secciones de la orden">
            {SECTIONS.map(([value, label, Icon]) => <button type="button" key={value} aria-pressed={layout.sideOpen && section === value} title={label} onClick={() => toggleSection(value)}><Icon size={15}/><span>{label}</span></button>)}
          </div>
          {selected.side === 'supplier'
            ? <Link className="button soft small inbox-open-order" href={`/proveedor/${selected.account.id}/ordenes/${selected.orderId}`} onClick={onClose} title="Abrir orden"><ArrowUpRight size={15}/><span>Abrir orden</span></Link>
            : <button type="button" className="button soft small inbox-open-order" onClick={() => setDealOpen(true)} title="Abrir orden"><ArrowUpRight size={15}/><span>Abrir orden</span></button>}
        </header>
        <div className={`inbox-conversation-body${layout.sideOpen ? ' side-open' : ''}`}>
          <div className="inbox-chat"><DealChat key={selected.orderId} account={accountOf(selected)} orderId={selected.orderId} active supplier={selected.side === 'supplier'} insert={chatInsert} compact/></div>
          {layout.sideOpen && separator('side', 'Ajustar el ancho de la orden')}
          {/* Kept mounted while closed: an open editor, a half-written adjustment or a confirmation keeps its state. */}
          <div className="inbox-side" hidden={!layout.sideOpen}>
            <DealDetail key={selected.orderId} embedded account={accountOf(selected)} orderId={selected.orderId} reference={selected.reference} side={selected.side}
              tab={section} onTab={setSection} onPricing={pricingChanged}
              onCopyToChat={text => { setChatInsert({ text, id: Date.now() }); if (!wide()) setLayout(current => ({ ...current, sideOpen: false })); }}/>
          </div>
        </div>
      </section> : <section className="inbox-conversation inbox-placeholder">{!layout.listOpen && listToggle}<MessagesSquare size={30}/><p>Elige una conversación para ver su chat y su orden.</p></section>}
    </div>
    {dealOpen && selected && <DealDetail account={accountOf(selected)} orderId={selected.orderId} reference={selected.reference} side="client" onClose={() => setDealOpen(false)}/>}
  </Modal>;
}
