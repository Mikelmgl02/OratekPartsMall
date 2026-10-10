'use client';
import { type KeyboardEvent as ReactKeyboardEvent, type PointerEvent as ReactPointerEvent, useCallback, useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { ArrowLeft, ArrowUpRight, FileText, Handshake, History, LoaderCircle, MessagesSquare, PanelLeft, Search } from 'lucide-react';
import Modal from './modal';
import DealStatusBadge from './deal-status';
import DealDetail, { DealChat, type DealTab } from './deal-detail';
import type { Account, Page } from '@/lib/types';
import { request } from '@/lib/types';
import { INBOX_EVENT, InboxConversation, InboxTarget, ago, targetOf } from '@/lib/inbox';

const LIST_POLL = 10000;
// The chat and the deal detail need only the account's id (and its side) to talk to the account-scoped deal API.
const accountOf = (target: InboxTarget): Account => ({ id: target.account.id, name: target.account.name, roles: [], capabilities: [target.side] });

// Laid out like a desktop chat app: the conversations on the left, the chat in the middle, and the order's sections (Cotización,
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
  // On a phone the chat comes first; a section opens over it from the header.
  return { ...DEFAULT_LAYOUT, ...saved, ...(wide() ? {} : { sideOpen: false }) };
}

export default function ConversationsPanel({ initial, onClose, onShowing }: { initial?: InboxTarget; onClose: () => void; onShowing: (orderId: string | null) => void }) {
  const [rows, setRows] = useState<InboxConversation[] | null>(null);
  const [next, setNext] = useState<string | null>(null);
  const [search, setSearch] = useState('');
  const [unreadOnly, setUnreadOnly] = useState(false);
  const [error, setError] = useState('');
  const [selected, setSelected] = useState<InboxTarget | null>(initial ?? null);
  const [dealOpen, setDealOpen] = useState(false);
  // A question the quote assistant suggests lands in the panel's chat composer (never sent by itself).
  const [chatInsert, setChatInsert] = useState<{ text: string; id: number } | null>(null);
  const [layout, setLayout] = useState<Layout>(savedLayout);
  const [section, setSection] = useState<DealTab>('order');
  const box = useRef<HTMLDivElement>(null);
  // Setting prices widens the order's panel for a while; the layout the person chose comes back afterwards.
  const beforePricing = useRef<Layout | null>(null);
  const query = new URLSearchParams({ ...(search.trim() ? { search: search.trim() } : {}), ...(unreadOnly ? { unread: '1' } : {}) }).toString();
  const load = useCallback(async () => {
    try {
      const data = await request<Page<InboxConversation>>(`/api/market/inbox/conversations${query ? `?${query}` : ''}`);
      setRows(data.results); setNext(data.next); setError('');
      // On a wide screen the most recent conversation opens by itself.
      setSelected(current => current ?? (data.results[0] && wide() ? targetOf(data.results[0]) : null));
    } catch { setError('No se pudieron cargar tus conversaciones.'); }
  }, [query]);
  useEffect(() => {
    const timer = setTimeout(() => void load(), search ? 250 : 0);
    const every = setInterval(() => { if (document.visibilityState === 'visible') void load(); }, LIST_POLL);
    const now = () => void load();
    window.addEventListener(INBOX_EVENT, now);
    return () => { clearTimeout(timer); clearInterval(every); window.removeEventListener(INBOX_EVENT, now); };
  }, [load, search]);
  useEffect(() => { onShowing(selected?.orderId ?? null); }, [selected, onShowing]);
  useEffect(() => {
    if (beforePricing.current || !wide()) return;
    try { localStorage.setItem(LAYOUT_KEY, JSON.stringify(layout)); } catch { /* the layout lasts this visit */ }
  }, [layout]);
  async function more() {
    if (!next) return;
    const url = new URL(next, window.location.origin);
    try {
      const data = await request<Page<InboxConversation>>(`/api/market/inbox/conversations?${url.searchParams}`);
      setRows(current => [...(current ?? []), ...data.results.filter(row => !current?.some(other => other.id === row.id))]); setNext(data.next);
    } catch { setError('No se pudieron cargar más conversaciones.'); }
  }
  function choose(target: InboxTarget) {
    setSelected(target); setSection('order');  // the deal shows its quotation instead once it loads one
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
  // Which of the person's accounts a deal belongs to matters only when they act for more than one.
  const severalAccounts = new Set(rows?.map(row => row.account.id)).size > 1;
  const separator = (which: 'list' | 'side', label: string) => <div className="inbox-resizer" role="separator" aria-orientation="vertical" aria-label={label} tabIndex={0}
    aria-valuenow={layout[which]} aria-valuemin={which === 'list' ? LIST_MIN : SIDE_MIN} onPointerDown={drag(which)} onKeyDown={nudge(which)}
    onDoubleClick={() => setLayout(current => ({ ...current, [which]: DEFAULT_LAYOUT[which] }))}/>;

  return <Modal title="Conversaciones" className={`inbox-modal${selected ? ' has-selection' : ''}`} onClose={onClose}>
    <div ref={box} className={`inbox-layout${layout.listOpen ? '' : ' list-closed'}`} style={{ '--inbox-list': `${layout.list}px`, '--inbox-side': `${layout.side}px` } as React.CSSProperties}>
      {layout.listOpen && <><aside className="inbox-list" aria-label="Tus conversaciones">
        <div className="inbox-list-tools">
          <label className="inbox-search"><Search size={15}/><span className="sr-only">Buscar conversación</span><input value={search} onChange={event => setSearch(event.target.value)} placeholder="Orden o cuenta…" autoComplete="off"/></label>
          <div className="inbox-filter" role="group" aria-label="Filtrar conversaciones">
            <button type="button" aria-pressed={!unreadOnly} onClick={() => setUnreadOnly(false)}>Todas</button>
            <button type="button" aria-pressed={unreadOnly} onClick={() => setUnreadOnly(true)}>No leídas</button>
          </div>
        </div>
        {error && <p className="notice error" role="alert">{error}</p>}
        {!rows ? <p className="inbox-empty"><LoaderCircle size={15} className="spin"/>Cargando conversaciones…</p>
          : !rows.length ? <p className="inbox-empty">{unreadOnly ? 'No tienes mensajes sin leer.' : search ? 'Ninguna orden coincide.' : 'Cuando envíes o recibas una orden, su conversación aparecerá aquí.'}</p>
          : <ul>{rows.map(row => <li key={row.id}><button type="button" aria-current={selected?.orderId === row.id ? 'true' : undefined} className={row.unread ? 'unread' : ''} onClick={() => choose(targetOf(row))}>
            <span className="inbox-row-top"><strong>{row.counterpart.name}</strong><time dateTime={row.activity}>{ago(row.activity)}</time></span>
            <span className="inbox-row-meta">{row.reference}<DealStatusBadge status={row.status}/></span>
            <span className="inbox-row-message">{row.last_message ? `${row.last_message.mine ? 'Tú' : row.last_message.actor_name}: ${row.last_message.body}` : 'Sin mensajes todavía'}</span>
            {row.unread > 0 && <span className="inbox-row-unread" aria-label={`${row.unread} sin leer`}>{row.unread}</span>}
            {severalAccounts && <small className="inbox-row-account">{row.side === 'supplier' ? 'Como proveedor' : 'Como cliente'} · {row.account.name}</small>}
          </button></li>)}</ul>}
        {next && <button type="button" className="button text small" onClick={() => void more()}>Ver más conversaciones</button>}
      </aside>
      {separator('list', 'Ajustar el ancho de las conversaciones')}</>}
      {selected ? <section className="inbox-conversation" aria-label={`Orden ${selected.reference} con ${selected.counterpart.name}`}>
        <header className="inbox-conversation-header">
          <button type="button" className="icon-button inbox-back" aria-label="Volver a conversaciones" onClick={() => setSelected(null)}><ArrowLeft size={18}/></button>
          <button type="button" className="icon-button inbox-list-toggle" aria-pressed={layout.listOpen} aria-label={layout.listOpen ? 'Ocultar conversaciones' : 'Mostrar conversaciones'}
            title={layout.listOpen ? 'Ocultar conversaciones' : 'Mostrar conversaciones'} onClick={() => setLayout(current => ({ ...current, listOpen: !current.listOpen }))}><PanelLeft size={18}/></button>
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
      </section> : <section className="inbox-conversation inbox-placeholder"><MessagesSquare size={30}/><p>Elige una conversación para ver su chat y su orden.</p></section>}
    </div>
    {dealOpen && selected && <DealDetail account={accountOf(selected)} orderId={selected.orderId} reference={selected.reference} side="client" onClose={() => setDealOpen(false)}/>}
  </Modal>;
}
