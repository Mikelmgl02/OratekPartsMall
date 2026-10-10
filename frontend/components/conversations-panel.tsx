'use client';
import { useCallback, useEffect, useState } from 'react';
import Link from 'next/link';
import { ArrowLeft, ArrowUpRight, FileText, LoaderCircle, MessagesSquare, Search } from 'lucide-react';
import Modal from './modal';
import DealStatusBadge from './deal-status';
import DealDetail, { DealChat } from './deal-detail';
import type { Account, Page } from '@/lib/types';
import { request } from '@/lib/types';
import { INBOX_EVENT, InboxConversation, InboxTarget, ago, targetOf } from '@/lib/inbox';

const LIST_POLL = 10000;
// The chat and the deal detail need only the account's id (and its side) to talk to the account-scoped deal API.
const accountOf = (target: InboxTarget): Account => ({ id: target.account.id, name: target.account.name, roles: [], capabilities: [target.side] });

// The global conversations panel: every deal the person takes part in, with the document being discussed (the deal itself, embedded:
// its quotation or ordered items, and its decisions — Fijar precios, Confirmar, Solicitar ajuste, Rechazar) next to its chat. On a
// phone the list, the chat and the document take turns.
export default function ConversationsPanel({ initial, onClose, onShowing }: { initial?: InboxTarget; onClose: () => void; onShowing: (orderId: string | null) => void }) {
  const [rows, setRows] = useState<InboxConversation[] | null>(null);
  const [next, setNext] = useState<string | null>(null);
  const [search, setSearch] = useState('');
  const [unreadOnly, setUnreadOnly] = useState(false);
  const [error, setError] = useState('');
  const [selected, setSelected] = useState<InboxTarget | null>(initial ?? null);
  const [view, setView] = useState<'chat' | 'document'>('chat');
  const [dealOpen, setDealOpen] = useState(false);
  // A question the quote assistant suggests lands in the panel's chat composer (never sent by itself).
  const [chatInsert, setChatInsert] = useState<{ text: string; id: number } | null>(null);
  const [pricing, setPricing] = useState(false);
  const query = new URLSearchParams({ ...(search.trim() ? { search: search.trim() } : {}), ...(unreadOnly ? { unread: '1' } : {}) }).toString();
  const load = useCallback(async () => {
    try {
      const data = await request<Page<InboxConversation>>(`/api/market/inbox/conversations${query ? `?${query}` : ''}`);
      setRows(data.results); setNext(data.next); setError('');
      // On a wide screen the most recent conversation opens by itself.
      setSelected(current => current ?? (data.results[0] && window.matchMedia('(min-width: 901px)').matches ? targetOf(data.results[0]) : null));
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
  async function more() {
    if (!next) return;
    const url = new URL(next, window.location.origin);
    try {
      const data = await request<Page<InboxConversation>>(`/api/market/inbox/conversations?${url.searchParams}`);
      setRows(current => [...(current ?? []), ...data.results.filter(row => !current?.some(other => other.id === row.id))]); setNext(data.next);
    } catch { setError('No se pudieron cargar más conversaciones.'); }
  }
  function choose(target: InboxTarget) { setSelected(target); setView('chat'); }
  // Which of the person's accounts a deal belongs to matters only when they act for more than one.
  const severalAccounts = new Set(rows?.map(row => row.account.id)).size > 1;

  return <Modal title="Conversaciones" className={`inbox-modal${selected ? ' has-selection' : ''}${pricing ? ' pricing' : ''}`} onClose={onClose}>
    <div className="inbox-layout">
      <aside className="inbox-list" aria-label="Tus conversaciones">
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
      {selected ? <section className="inbox-conversation" aria-label={`Orden ${selected.reference} con ${selected.counterpart.name}`}>
        <header className="inbox-conversation-header">
          <button type="button" className="icon-button inbox-back" aria-label="Volver a conversaciones" onClick={() => setSelected(null)}><ArrowLeft size={18}/></button>
          <div><strong>{selected.counterpart.name}</strong><span>{selected.reference} · {selected.side === 'supplier' ? 'tu cliente' : 'tu proveedor'}</span></div>
          <div className="inbox-view-switch" role="group" aria-label="Ver">
            <button type="button" aria-pressed={view === 'chat'} onClick={() => setView('chat')}><MessagesSquare size={15}/>Conversación</button>
            <button type="button" aria-pressed={view === 'document'} onClick={() => setView('document')}><FileText size={15}/>Documento</button>
          </div>
          {selected.side === 'supplier'
            ? <Link className="button soft small" href={`/proveedor/${selected.account.id}/ordenes/${selected.orderId}`} onClick={onClose}>Abrir orden<ArrowUpRight size={15}/></Link>
            : <button type="button" className="button soft small" onClick={() => setDealOpen(true)}>Abrir orden<ArrowUpRight size={15}/></button>}
        </header>
        <div className={`inbox-conversation-body view-${view}`}>
          <div className="inbox-document"><DealDetail key={selected.orderId} embedded account={accountOf(selected)} orderId={selected.orderId} reference={selected.reference} side={selected.side}
            onCopyToChat={text => { setChatInsert({ text, id: Date.now() }); setView('chat'); }} onPricing={setPricing}/></div>
          <div className="inbox-chat"><DealChat key={selected.orderId} account={accountOf(selected)} orderId={selected.orderId} active supplier={selected.side === 'supplier'} insert={chatInsert} compact/></div>
        </div>
      </section> : <section className="inbox-conversation inbox-placeholder"><MessagesSquare size={30}/><p>Elige una conversación para ver su documento y sus mensajes.</p></section>}
    </div>
    {dealOpen && selected && <DealDetail account={accountOf(selected)} orderId={selected.orderId} reference={selected.reference} side="client" onClose={() => setDealOpen(false)}/>}
  </Modal>;
}
