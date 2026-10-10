'use client';
import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from 'react';
import dynamic from 'next/dynamic';
import { Bell, BellOff, BellRing, LoaderCircle, MessagesSquare, Smartphone, X } from 'lucide-react';
import { ApiError, request } from '@/lib/types';
import { INBOX_EVENT, InboxConversation, InboxNotification, InboxSummary, InboxTarget, ago, inboxChanged, targetOf } from '@/lib/inbox';
import { PushState, disablePush, enablePush, pushStatus, syncPush } from '@/lib/push';
import { connectRealtime } from '@/lib/realtime';
import type { Page } from '@/lib/types';

const ConversationsPanel = dynamic(() => import('./conversations-panel'), { ssr: false });
const POLL = 15000, LIVE_POLL = 60000, IDLE_POLL = 60000, TOAST_TIME = 7000;
type Toast = { key: string; title: string; body: string; target: InboxTarget };
type Inbox = { available: boolean; summary: InboxSummary | null; open: (target?: InboxTarget) => void; showing: string | null; register: () => () => void };
const Context = createContext<Inbox | null>(null);
export function useInbox() { return useContext(Context); }

// Mounted once by AppShell, so the summary, the panel and the toasts outlive page changes. It polls only while a page shows the inbox
// buttons (the storefront and the supplier's order page, not the administration). Signed-out visitors get 401 and see nothing.
export function InboxProvider({ children }: { children: ReactNode }) {
  const [summary, setSummary] = useState<InboxSummary | null>(null);
  const [available, setAvailable] = useState(false);
  const [panel, setPanel] = useState<{ target?: InboxTarget; key: number } | null>(null);
  const [showing, setShowing] = useState<string | null>(null);
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [consumers, setConsumers] = useState(0);
  const register = useCallback(() => { setConsumers(count => count + 1); return () => setConsumers(count => count - 1); }, []);
  const polling = consumers > 0;
  const [live, setLive] = useState(false);
  const seen = useRef<{ message: number; event: number } | null>(null);
  const showingRef = useRef(showing);
  showingRef.current = showing;
  const toast = useCallback((item: Toast) => {
    setToasts(items => [...items.filter(other => other.key !== item.key), item].slice(-3));
    setTimeout(() => setToasts(items => items.filter(other => other.key !== item.key)), TOAST_TIME);
  }, []);
  const poll = useCallback(async () => {
    if (document.visibilityState !== 'visible') return;
    try {
      const data = await request<InboxSummary>('/api/market/inbox');
      setSummary(data); setAvailable(true);
      const message = data.latest_message, event = data.latest_notification;
      // The first summary after a sign-in only sets the marks: what was already waiting shows as badges, not as toasts.
      if (seen.current) {
        if (message && message.id > seen.current.message && message.order_id !== showingRef.current)
          toast({ key: `m${message.id}`, title: `${message.account_name} · ${message.reference}`, body: message.body, target: { orderId: message.order_id, reference: message.reference, side: message.side, account: message.account, counterpart: message.counterpart } });
        if (event && event.id > seen.current.event) toast({ key: `e${event.id}`, title: event.title, body: event.reference, target: targetOf(event) });
      }
      seen.current = { message: Math.max(seen.current?.message ?? 0, message?.id ?? 0), event: Math.max(seen.current?.event ?? 0, event?.id ?? 0) };
    } catch (error) {
      if (error instanceof ApiError && [401, 403].includes(error.status)) { setAvailable(false); setSummary(null); setPanel(null); seen.current = null; }
    }
  }, [toast]);
  useEffect(() => {
    if (!polling) return;
    void poll();
    const timer = setInterval(() => void poll(), !available ? IDLE_POLL : live ? LIVE_POLL : POLL);
    const now = () => void poll();
    window.addEventListener(INBOX_EVENT, now); window.addEventListener('focus', now); document.addEventListener('visibilitychange', now);
    return () => { clearInterval(timer); window.removeEventListener(INBOX_EVENT, now); window.removeEventListener('focus', now); document.removeEventListener('visibilitychange', now); };
  }, [poll, available, polling, live]);
  // Signed in, the inbox's websocket nudges every open page (and the summary polls once a minute instead of every 15 s).
  useEffect(() => {
    if (!available || !polling) return;
    return connectRealtime(setLive);
  }, [available, polling]);
  const open = useCallback((target?: InboxTarget) => setPanel({ target, key: Date.now() }), []);
  // A push notification's link (?conversacion=<deal>) or its click on an open page names only the deal: look up its conversation.
  const openOrder = useCallback(async (orderId: string) => {
    try {
      const data = await request<Page<InboxConversation>>(`/api/market/inbox/conversations?order=${encodeURIComponent(orderId)}`);
      open(data.results[0] ? targetOf(data.results[0]) : undefined);
    } catch { open(); }
  }, [open]);
  const synced = useRef(false);
  useEffect(() => {
    if (!available) { synced.current = false; return; }
    if (synced.current) return;
    synced.current = true;
    void syncPush();
    const url = new URL(window.location.href), orderId = url.searchParams.get('conversacion');
    if (orderId) {
      url.searchParams.delete('conversacion');
      window.history.replaceState(window.history.state, '', `${url.pathname}${url.search}${url.hash}`);
      void openOrder(orderId);
    }
  }, [available, openOrder]);
  useEffect(() => {
    if (!('serviceWorker' in navigator)) return;
    const message = (event: MessageEvent) => {
      if (event.data?.type === 'motionpartes:push') inboxChanged();
      if (event.data?.type === 'motionpartes:open-conversation' && event.data.order_id) void openOrder(String(event.data.order_id));
    };
    navigator.serviceWorker.addEventListener('message', message);
    return () => navigator.serviceWorker.removeEventListener('message', message);
  }, [openOrder]);
  return <Context.Provider value={{ available, summary, open, showing, register }}>
    {children}
    {panel && <ConversationsPanel key={panel.key} initial={panel.target} onShowing={setShowing} onClose={() => { setPanel(null); setShowing(null); }}/>}
    {toasts.length > 0 && <div className="inbox-toasts" role="status" aria-live="polite">{toasts.map(item => <div key={item.key} className="inbox-toast">
      <button type="button" className="inbox-toast-open" onClick={() => { setToasts(items => items.filter(other => other.key !== item.key)); open(item.target); }}><strong>{item.title}</strong><span>{item.body}</span></button>
      <button type="button" className="icon-button" aria-label="Cerrar aviso" onClick={() => setToasts(items => items.filter(other => other.key !== item.key))}><X size={14}/></button>
    </div>)}</div>}
  </Context.Provider>;
}

const count = (value: number) => value > 99 ? '99+' : String(value);

// The header's messages button and notifications bell; a page header places them where they fit.
export function InboxButtons() {
  const inbox = useInbox();
  const register = inbox?.register;
  useEffect(() => register?.(), [register]);
  if (!inbox?.available) return null;
  const unread = inbox.summary?.unread_messages ?? 0;
  return <>
    <button type="button" className="basket-button inbox-button" onClick={() => inbox.open()} title="Mensajes" aria-label={unread ? `Mensajes, ${unread} sin leer` : 'Mensajes'}><MessagesSquare size={20}/>{unread > 0 && <span className="basket-count inbox-count">{count(unread)}</span>}</button>
    <NotificationsMenu/>
  </>;
}

function NotificationsMenu() {
  const inbox = useInbox()!;
  const [open, setOpen] = useState(false);
  const [items, setItems] = useState<InboxNotification[] | null>(null);
  const [more, setMore] = useState(false);
  const [error, setError] = useState('');
  const [top, setTop] = useState(0);
  const box = useRef<HTMLDivElement>(null);
  const unseen = inbox.summary?.unseen_notifications ?? 0;
  const load = useCallback(async (before?: number) => {
    try {
      const data = await request<{ results: InboxNotification[]; has_more: boolean }>(`/api/market/inbox/notifications${before ? `?before=${before}` : ''}`);
      setItems(current => before ? [...(current ?? []), ...data.results] : data.results); setMore(data.has_more); setError('');
      const newest = data.results[0];
      if (!before && newest?.unseen) {
        await request('/api/market/inbox/notifications/seen', { method: 'POST', body: JSON.stringify({ last_event_id: newest.id }) });
        inboxChanged();
      }
    } catch { setError('No se pudieron cargar las notificaciones.'); }
  }, []);
  useEffect(() => { if (open) void load(); }, [open, load]);
  useEffect(() => {
    if (!open) return;
    const outside = (event: PointerEvent) => { if (!box.current?.contains(event.target as Node)) setOpen(false); };
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') setOpen(false); };
    document.addEventListener('pointerdown', outside); document.addEventListener('keydown', escape);
    return () => { document.removeEventListener('pointerdown', outside); document.removeEventListener('keydown', escape); };
  }, [open]);
  return <div className="inbox-notifications" ref={box}>
    <button type="button" className="basket-button inbox-button" aria-expanded={open} aria-haspopup="true" onClick={() => { setTop(Math.round(box.current?.getBoundingClientRect().bottom ?? 0) + 8); setOpen(value => !value); }} title="Notificaciones" aria-label={unseen ? `Notificaciones, ${unseen} nuevas` : 'Notificaciones'}><Bell size={20}/>{unseen > 0 && <span className="basket-count inbox-count">{count(unseen)}</span>}</button>
    {/* On a phone the menu spans the screen under the bell (--inbox-menu-top) instead of hanging from it. */}
    {open && <section className="inbox-notifications-menu" aria-label="Notificaciones" style={{ '--inbox-menu-top': `${top}px` } as React.CSSProperties}>
      <header><h2>Notificaciones</h2><small>Lo que la otra parte hizo en tus órdenes</small></header>
      <PushSwitch/>
      {error && <p className="notice error" role="alert">{error}</p>}
      {!items && !error ? <p className="inbox-empty"><LoaderCircle size={15} className="spin"/>Cargando…</p> : items && !items.length ? <p className="inbox-empty">Aún no tienes notificaciones.</p>
        : <ul>{items?.map(item => <li key={item.id}><button type="button" className={item.unseen ? 'unseen' : ''} onClick={() => { setOpen(false); inbox.open(targetOf(item)); }}>
          <strong>{item.title}</strong><span>{item.reference} · <time dateTime={item.created_at}>{ago(item.created_at)}</time></span>{item.description && <small>{item.description}</small>}
        </button></li>)}</ul>}
      {more && items && <button type="button" className="button text small" onClick={() => void load(items.at(-1)?.id)}>Ver anteriores</button>}
      <EmailSwitches/>
      <WhatsAppSwitches/>
    </section>}
  </div>;
}

// Browser notifications: messages and deal updates arrive even with MotionPartes closed. Hidden where the browser or the server cannot send them.
function PushSwitch() {
  const [status, setStatus] = useState<{ state: PushState; key: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => { pushStatus().then(setStatus).catch(() => setStatus(null)); }, []);
  if (!status || status.state === 'unsupported' || status.state === 'unconfigured') return null;
  async function change(on: boolean) {
    setBusy(true); setError('');
    try { const state = on ? await enablePush(status!.key) : await disablePush(); setStatus({ ...status!, state }); }
    catch { setError(on ? 'No se pudieron activar las notificaciones en este navegador.' : 'No se pudieron desactivar las notificaciones.'); }
    finally { setBusy(false); }
  }
  return <div className={`inbox-push ${status.state}`}>
    {status.state === 'install' ? <p><Smartphone size={16}/><span>Para recibir notificaciones en iPhone o iPad, toca <strong>Compartir</strong> y luego <strong>Agregar a inicio</strong>; abre MotionPartes desde tu pantalla de inicio y actívalas aquí.</span></p>
      : status.state === 'blocked' ? <p><BellOff size={16}/>Las notificaciones están bloqueadas en este navegador. Permítelas en la configuración del sitio para recibirlas.</p>
      : status.state === 'on' ? <p><BellRing size={16}/><span>Notificaciones del navegador activas</span><button type="button" className="button text small" disabled={busy} onClick={() => void change(false)}>Desactivar</button></p>
      : <button type="button" className="button soft small" disabled={busy} onClick={() => void change(true)}>{busy ? <LoaderCircle size={15} className="spin"/> : <BellRing size={15}/>}Recibir notificaciones en este navegador</button>}
    {status.state === 'off' && <small>Te avisamos de mensajes y cambios en tus órdenes aunque MotionPartes esté cerrado.</small>}
    {error && <small className="inbox-push-error" role="alert">{error}</small>}
  </div>;
}

type EmailPreferences = { enabled: boolean; address: string; messages: boolean; updates: boolean };

// Email notices (mall/email_notifications.py): new messages while the person is away from MotionPartes, and changes in their orders.
function EmailSwitches() {
  const [preferences, setPreferences] = useState<EmailPreferences | null>(null);
  const [error, setError] = useState('');
  useEffect(() => { request<EmailPreferences>('/api/market/inbox/email').then(setPreferences).catch(() => setPreferences(null)); }, []);
  if (!preferences?.enabled) return null;
  async function change(key: 'messages' | 'updates', value: boolean) {
    setPreferences(current => current && { ...current, [key]: value }); setError('');
    try { setPreferences(await request<EmailPreferences>('/api/market/inbox/email', { method: 'POST', body: JSON.stringify({ [key]: value }) })); }
    catch { setPreferences(current => current && { ...current, [key]: !value }); setError('No se pudo guardar tu preferencia. Inténtalo de nuevo.'); }
  }
  return <fieldset className="inbox-email">
    <legend>Avisos por correo a <strong>{preferences.address}</strong></legend>
    <label><input type="checkbox" checked={preferences.messages} onChange={event => void change('messages', event.target.checked)}/>Mensajes nuevos, cuando no estés en MotionPartes</label>
    <label><input type="checkbox" checked={preferences.updates} onChange={event => void change('updates', event.target.checked)}/>Cambios en tus órdenes</label>
    {error && <small className="inbox-push-error" role="alert">{error}</small>}
  </fieldset>;
}

type WhatsAppPreferences = { enabled: boolean; number: string; opted_in: boolean; messages: boolean; updates: boolean };

// WhatsApp notices (mall/whatsapp.py): only after the person gives their number and explicit consent (Meta's opt-in rule).
function WhatsAppSwitches() {
  const [preferences, setPreferences] = useState<WhatsAppPreferences | null>(null);
  const [editing, setEditing] = useState(false);
  const [number, setNumber] = useState('');
  const [consent, setConsent] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => { request<WhatsAppPreferences>('/api/market/inbox/whatsapp').then(value => { setPreferences(value); setNumber(value.number); }).catch(() => setPreferences(null)); }, []);
  if (!preferences?.enabled) return null;
  async function save(changes: Record<string, unknown>) {
    setBusy(true); setError('');
    try {
      const value = await request<WhatsAppPreferences>('/api/market/inbox/whatsapp', { method: 'POST', body: JSON.stringify(changes) });
      setPreferences(value); setNumber(value.number); setEditing(false); setConsent(false);
    } catch (caught) { setError(caught instanceof Error ? caught.message : 'No se pudo guardar. Inténtalo de nuevo.'); }
    finally { setBusy(false); }
  }
  async function toggle(key: 'messages' | 'updates', value: boolean) {
    setPreferences(current => current && { ...current, [key]: value });  // at once; undone if it cannot be saved
    setError('');
    try { setPreferences(await request<WhatsAppPreferences>('/api/market/inbox/whatsapp', { method: 'POST', body: JSON.stringify({ [key]: value }) })); }
    catch { setPreferences(current => current && { ...current, [key]: !value }); setError('No se pudo guardar tu preferencia. Inténtalo de nuevo.'); }
  }
  const asking = editing || !preferences.opted_in;
  return <fieldset className="inbox-email inbox-whatsapp">
    <legend>Avisos por WhatsApp</legend>
    {asking ? <form className="inbox-whatsapp-form" onSubmit={event => { event.preventDefault(); void save({ number, opt_in: true }); }}>
      <label>Tu número de WhatsApp<input type="tel" inputMode="tel" autoComplete="tel" required value={number} disabled={busy} onChange={event => setNumber(event.target.value)} placeholder="+507 6123 4567"/></label>
      <label className="inbox-whatsapp-consent"><input type="checkbox" required checked={consent} disabled={busy} onChange={event => setConsent(event.target.checked)}/>Acepto recibir avisos de MotionPartes por WhatsApp. Puedo darme de baja cuando quiera respondiendo BAJA.</label>
      <div><button type="submit" className="button primary small" disabled={busy || !consent || !number.trim()}>{busy && <LoaderCircle size={14} className="spin"/>}Activar avisos</button>
        {editing && <button type="button" className="button text small" disabled={busy} onClick={() => { setEditing(false); setNumber(preferences.number); setConsent(false); }}>Cancelar</button>}</div>
    </form> : <>
      <p className="inbox-whatsapp-number">Activos en <strong>{preferences.number}</strong>
        <button type="button" className="button text small" disabled={busy} onClick={() => setEditing(true)}>Cambiar número</button>
        <button type="button" className="button text small" disabled={busy} onClick={() => void save({ opt_in: false })}>Desactivar</button></p>
      <label><input type="checkbox" checked={preferences.messages} disabled={busy} onChange={event => void toggle('messages', event.target.checked)}/>Mensajes nuevos, cuando no estés en MotionPartes</label>
      <label><input type="checkbox" checked={preferences.updates} disabled={busy} onChange={event => void toggle('updates', event.target.checked)}/>Cambios en tus órdenes</label>
    </>}
    {error && <small className="inbox-push-error" role="alert">{error}</small>}
  </fieldset>;
}
