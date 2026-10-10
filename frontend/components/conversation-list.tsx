'use client';
import { type DragEvent, type FormEvent, type KeyboardEvent as ReactKeyboardEvent, type MouseEvent as ReactMouseEvent, useCallback, useEffect, useRef, useState } from 'react';
import { Check, ChevronDown, ChevronRight, Ellipsis, Folder, FolderMinus, FolderPlus, LoaderCircle, Pencil, Pin, PinOff, Search, Trash2 } from 'lucide-react';
import type { Page } from '@/lib/types';
import { request } from '@/lib/types';
import { dealStatusLabels } from '@/lib/deal-types';
import { INBOX_EVENT, type InboxConversation, type InboxGroup, type InboxTarget, ago, targetOf } from '@/lib/inbox';

// The conversations column of the panel, organized like a desktop app's sidebar: Fijadas on top, then the person's own groups (made with
// Nuevo grupo, collapsible), then Sin grupo. Each conversation is a single line: a status dot, the other party, the order's reference and
// its unread count (the rest in its tooltip). Its ⋯ menu pins it or moves it to a group; on a desktop it can also be dragged onto one.
// Pins and groups live on the server (mall/inbox.py), so every device lists them alike; which groups are folded is kept on this device.
const LIST_POLL = 10000;
const COLLAPSED_KEY = 'motionpartes:inbox:collapsed';
const LOOSE = 'loose', PINNED = 'pinned';
type Place = { pinned?: boolean; group?: string | null };
type Menu = { kind: 'row' | 'group'; id: string; up: boolean; deleting?: boolean } | null;
const failure = (error: unknown) => error instanceof Error && !(error instanceof TypeError) ? error.message : 'Se perdió la conexión. Inténtalo de nuevo.';

function savedCollapsed(): string[] {
  try { const saved = JSON.parse(localStorage.getItem(COLLAPSED_KEY) || '[]'); return Array.isArray(saved) ? saved : []; } catch { return []; }
}

// The newest conversation, wherever the person keeps it (the list starts with the pinned ones).
export function mostRecent(rows: InboxConversation[]) {
  return rows.reduce<InboxConversation | undefined>((best, row) => !best || Date.parse(row.activity) > Date.parse(best.activity) ? row : best, undefined);
}

export default function ConversationList({ hidden, selectedId, onChoose, onLoaded }: {
  hidden: boolean; selectedId: string | null; onChoose: (target: InboxTarget) => void; onLoaded: (rows: InboxConversation[]) => void;
}) {
  const [rows, setRows] = useState<InboxConversation[] | null>(null);
  const [groups, setGroups] = useState<InboxGroup[]>([]);
  const [next, setNext] = useState<string | null>(null);
  const [search, setSearch] = useState('');
  const [unreadOnly, setUnreadOnly] = useState(false);
  const [error, setError] = useState('');
  const [collapsed, setCollapsed] = useState<string[]>(savedCollapsed);
  const [menu, setMenu] = useState<Menu>(null);
  // A new group (for that conversation, when it came from its menu) or a group's new name, typed in place.
  const [naming, setNaming] = useState<{ group?: string; order?: InboxConversation } | null>(null);
  const [dragging, setDragging] = useState<string | null>(null);
  const [over, setOver] = useState<string | null>(null);
  const list = useRef<HTMLElement>(null);
  const opener = useRef<HTMLButtonElement | null>(null);
  const onLoadedRef = useRef(onLoaded); onLoadedRef.current = onLoaded;
  const query = new URLSearchParams({ ...(search.trim() ? { search: search.trim() } : {}), ...(unreadOnly ? { unread: '1' } : {}) }).toString();
  const load = useCallback(async () => {
    try {
      const data = await request<Page<InboxConversation> & { groups?: InboxGroup[] }>(`/api/market/inbox/conversations${query ? `?${query}` : ''}`);
      setRows(data.results); setNext(data.next); setGroups(data.groups ?? []); setError('');
      onLoadedRef.current(data.results);
    } catch { setError('No se pudieron cargar tus conversaciones.'); }
  }, [query]);
  const hiddenRef = useRef(hidden); hiddenRef.current = hidden;
  useEffect(() => {
    const timer = setTimeout(() => void load(), search ? 250 : 0);
    const every = setInterval(() => { if (document.visibilityState === 'visible' && !hiddenRef.current) void load(); }, LIST_POLL);
    const now = () => void load();
    window.addEventListener(INBOX_EVENT, now);
    return () => { clearTimeout(timer); clearInterval(every); window.removeEventListener(INBOX_EVENT, now); };
  }, [load, search]);
  useEffect(() => { if (!hidden) void load(); }, [hidden, load]);
  useEffect(() => { try { localStorage.setItem(COLLAPSED_KEY, JSON.stringify(collapsed)); } catch { /* folded for this visit */ } }, [collapsed]);
  // An open menu closes on a click elsewhere or Escape (which then leaves the panel open), and gives the focus back to its button.
  useEffect(() => {
    if (!menu) return;
    list.current?.querySelector<HTMLElement>('.inbox-menu [role^="menuitem"]')?.focus();
    const away = (event: PointerEvent) => { if (!(event.target as Element).closest('.inbox-menu, .inbox-menu-button')) setMenu(null); };
    const escape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      event.preventDefault(); event.stopPropagation(); setMenu(null); opener.current?.focus();
    };
    document.addEventListener('pointerdown', away); document.addEventListener('keydown', escape, true);
    return () => { document.removeEventListener('pointerdown', away); document.removeEventListener('keydown', escape, true); };
  }, [menu]);

  async function more() {
    if (!next) return;
    const url = new URL(next, window.location.origin);
    try {
      const data = await request<Page<InboxConversation> & { groups?: InboxGroup[] }>(`/api/market/inbox/conversations?${url.searchParams}`);
      setRows(current => [...(current ?? []), ...data.results.filter(row => !current?.some(other => other.id === row.id))]); setNext(data.next);
      if (data.groups) setGroups(data.groups);
    } catch { setError('No se pudieron cargar más conversaciones.'); }
  }
  async function place(row: InboxConversation, change: Place) {
    setMenu(null);
    setRows(current => current?.map(item => item.id === row.id ? { ...item, ...change } : item) ?? current);
    try { await request(`/api/market/inbox/conversations/${row.id}/place`, { method: 'POST', body: JSON.stringify(change) }); }
    catch (caught) { setError(`No se pudo mover la conversación. ${failure(caught)}`); }
    void load();
  }
  async function createGroup(name: string, order?: InboxConversation) {
    const group = await request<InboxGroup>('/api/market/inbox/groups', { method: 'POST', body: JSON.stringify({ name }) });
    setGroups(current => [...current, group]); setNaming(null);
    if (order) await place(order, { group: group.id, pinned: false }); else void load();
  }
  async function renameGroup(group: InboxGroup, name: string) {
    const saved = await request<InboxGroup>(`/api/market/inbox/groups/${group.id}`, { method: 'POST', body: JSON.stringify({ name }) });
    setGroups(current => current.map(item => item.id === saved.id ? saved : item)); setNaming(null);
  }
  async function deleteGroup(group: InboxGroup) {
    setMenu(null);
    try {
      await request(`/api/market/inbox/groups/${group.id}`, { method: 'DELETE' });
      setGroups(current => current.filter(item => item.id !== group.id));
    } catch (caught) { setError(`No se pudo eliminar el grupo. ${failure(caught)}`); }
    void load();
  }
  function openMenu(kind: 'row' | 'group', id: string) {
    return (event: ReactMouseEvent<HTMLButtonElement>) => {
      if (menu?.kind === kind && menu.id === id) { setMenu(null); return; }
      opener.current = event.currentTarget;
      // Near the bottom of the list the menu opens upwards.
      const bottom = list.current?.getBoundingClientRect().bottom ?? window.innerHeight;
      setMenu({ kind, id, up: bottom - event.currentTarget.getBoundingClientRect().bottom < 250 });
    };
  }
  const toggle = (key: string) => setCollapsed(current => current.includes(key) ? current.filter(item => item !== key) : [...current, key]);
  // Where a dragged conversation lands; nothing happens when it is already there.
  function dropZone(key: string, change: Place) {
    const already = (row: InboxConversation) => key === PINNED ? !!row.pinned : !row.pinned && (key === LOOSE ? !row.group || !groups.some(group => group.id === row.group) : row.group === key);
    return {
      onDragOver: (event: DragEvent) => { if (!dragging) return; event.preventDefault(); event.dataTransfer.dropEffect = 'move'; setOver(key); },
      onDragLeave: (event: DragEvent) => { if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setOver(current => current === key ? null : current); },
      onDrop: (event: DragEvent) => {
        event.preventDefault();
        const row = rows?.find(item => item.id === dragging);
        setDragging(null); setOver(null);
        if (row && !already(row)) void place(row, change);
      },
    };
  }

  const filtering = !!search.trim() || unreadOnly;
  const known = new Set(groups.map(group => group.id));
  const pinned = rows?.filter(row => row.pinned) ?? [];
  const inGroup = (id: string) => rows?.filter(row => !row.pinned && row.group === id) ?? [];
  const loose = rows?.filter(row => !row.pinned && !(row.group && known.has(row.group))) ?? [];
  const organized = groups.length > 0 || pinned.length > 0;
  // Which of the person's accounts a deal belongs to matters only when they act for more than one.
  const severalAccounts = new Set(rows?.map(row => row.account.id)).size > 1;

  function line(row: InboxConversation) {
    const status = dealStatusLabels[row.status];
    const as = severalAccounts ? `${row.side === 'supplier' ? 'Como proveedor' : 'Como cliente'} · ${row.account.name}` : '';
    const last = row.last_message ? `${row.last_message.mine ? 'Tú' : row.last_message.actor_name}: ${row.last_message.body}` : 'Sin mensajes todavía';
    const open = menu?.kind === 'row' && menu.id === row.id;
    return <li key={row.id} className={`inbox-row${selectedId === row.id ? ' current' : ''}${dragging === row.id ? ' dragging' : ''}${open ? ' menu-open' : ''}`} draggable
      onDragStart={event => { event.dataTransfer.setData('text/plain', row.reference); event.dataTransfer.effectAllowed = 'move'; setDragging(row.id); setMenu(null); }}
      onDragEnd={() => { setDragging(null); setOver(null); }}>
      <button type="button" className={`inbox-row-open${row.unread ? ' unread' : ''}`} aria-current={selectedId === row.id ? 'true' : undefined}
        title={[`${row.counterpart.name} · ${row.reference}`, `${status} · ${ago(row.activity)}`, as, last].filter(Boolean).join('\n')} onClick={() => onChoose(targetOf(row))}>
        <span className={`inbox-row-dot ${row.status}`} aria-hidden="true"/>
        <span className="inbox-row-name">{row.counterpart.name}</span>
        <span className="inbox-row-ref">{row.reference}</span>
        <span className="sr-only">, {status}{as && `, ${as}`}</span>
        {row.unread > 0 && <span className="inbox-row-unread" aria-label={`${row.unread} sin leer`}>{row.unread}</span>}
      </button>
      <button type="button" className="icon-button inbox-menu-button" aria-label={`Organizar ${row.reference}`} aria-haspopup="menu" aria-expanded={open} title="Fijar o mover a un grupo" onClick={openMenu('row', row.id)}><Ellipsis size={15}/></button>
      {open && <div className={`inbox-menu${menu.up ? ' up' : ''}`} role="menu" aria-label={`Organizar ${row.reference}`} onKeyDown={arrows}>
        <button type="button" role="menuitem" onClick={() => void place(row, { pinned: !row.pinned })}>{row.pinned ? <PinOff size={15}/> : <Pin size={15}/>}{row.pinned ? 'Quitar de fijadas' : 'Fijar'}</button>
        {groups.length > 0 && <p className="inbox-menu-label" aria-hidden="true">Mover a</p>}
        {groups.map(group => { const here = !row.pinned && row.group === group.id; return <button type="button" role="menuitemradio" aria-checked={here} key={group.id}
          aria-label={`Mover a ${group.name}`} onClick={() => here ? setMenu(null) : void place(row, { group: group.id, pinned: false })}><Folder size={15}/><span>{group.name}</span>{here && <Check size={14}/>}</button>; })}
        <button type="button" role="menuitem" onClick={() => { setMenu(null); setNaming({ order: row }); }}><FolderPlus size={15}/>Nuevo grupo…</button>
        {!row.pinned && row.group && known.has(row.group) && <button type="button" role="menuitem" onClick={() => void place(row, { group: null })}><FolderMinus size={15}/>Quitar del grupo</button>}
      </div>}
    </li>;
  }

  function groupSection(group: InboxGroup) {
    const items = inGroup(group.id);
    if (filtering && !items.length) return null;
    const open = !collapsed.includes(group.id) || filtering;
    const unread = items.reduce((total, row) => total + row.unread, 0);
    const menuOpen = menu?.kind === 'group' && menu.id === group.id;
    return <div key={group.id} role="group" aria-label={group.name} className={`inbox-group${over === group.id ? ' drop' : ''}`} {...dropZone(group.id, { group: group.id, pinned: false })}>
      <div className={`inbox-group-head${menuOpen ? ' menu-open' : ''}`}>
        {naming?.group === group.id
          ? <GroupNameForm initial={group.name} label={`Nuevo nombre de ${group.name}`} onSubmit={name => renameGroup(group, name)} onCancel={() => setNaming(null)}/>
          : <button type="button" className="inbox-group-toggle" aria-expanded={open} onClick={() => toggle(group.id)}>
            <span>{group.name}</span>{open ? <ChevronDown size={14}/> : <ChevronRight size={14}/>}
            {!open && unread > 0 && <span className="inbox-row-unread" aria-label={`${unread} sin leer`}>{unread}</span>}
          </button>}
        {naming?.group !== group.id && <button type="button" className="icon-button inbox-menu-button" aria-label={`Opciones del grupo ${group.name}`} aria-haspopup="menu" aria-expanded={menuOpen} onClick={openMenu('group', group.id)}><Ellipsis size={15}/></button>}
        {menuOpen && <div className={`inbox-menu${menu.up ? ' up' : ''}`} role="menu" aria-label={`Opciones del grupo ${group.name}`} onKeyDown={arrows}>
          {menu.deleting ? <>
            <p className="inbox-menu-note">¿Eliminar «{group.name}»? Sus conversaciones quedan en Sin grupo.</p>
            <button type="button" role="menuitem" className="danger" onClick={() => void deleteGroup(group)}><Trash2 size={15}/>Eliminar grupo</button>
            <button type="button" role="menuitem" onClick={() => setMenu(null)}>Cancelar</button>
          </> : <>
            <button type="button" role="menuitem" onClick={() => { setMenu(null); setNaming({ group: group.id }); }}><Pencil size={15}/>Cambiar nombre</button>
            <button type="button" role="menuitem" className="danger" onClick={() => setMenu({ ...menu, deleting: true })}><Trash2 size={15}/>Eliminar grupo…</button>
          </>}
        </div>}
      </div>
      {open && (items.length ? <ul>{items.map(line)}</ul>
        : !filtering && group.count === 0 && <p className="inbox-group-empty">Vacío. Trae una conversación con su menú ⋯ o arrastrándola aquí.</p>)}
    </div>;
  }

  const empty = !rows?.length && (filtering || !groups.length);
  const looseOpen = !collapsed.includes(LOOSE) || filtering;
  return <aside ref={list} className="inbox-list" aria-label="Tus conversaciones" hidden={hidden}>
    <div className="inbox-list-tools">
      <label className="inbox-search"><Search size={15}/><span className="sr-only">Buscar conversación</span><input value={search} onChange={event => setSearch(event.target.value)} placeholder="Orden o cuenta…" autoComplete="off"/></label>
      <div className="inbox-list-filters">
        <div className="inbox-filter" role="group" aria-label="Filtrar conversaciones">
          <button type="button" aria-pressed={!unreadOnly} onClick={() => setUnreadOnly(false)}>Todas</button>
          <button type="button" aria-pressed={unreadOnly} onClick={() => setUnreadOnly(true)}>No leídas</button>
        </div>
        <button type="button" className="icon-button inbox-new-group" aria-label="Nuevo grupo" title="Nuevo grupo" onClick={() => setNaming({})}><FolderPlus size={16}/></button>
      </div>
    </div>
    {error && <p className="notice error" role="alert">{error}</p>}
    {!rows ? <p className="inbox-empty"><LoaderCircle size={15} className="spin"/>Cargando conversaciones…</p>
      : <div className="inbox-groups">
        {pinned.length > 0 && <div role="group" aria-label="Fijadas" className={`inbox-group${over === PINNED ? ' drop' : ''}`} {...dropZone(PINNED, { pinned: true })}>
          <p className="inbox-group-title"><Pin size={13}/>Fijadas</p>
          <ul>{pinned.map(line)}</ul>
        </div>}
        {groups.map(groupSection)}
        {naming && !naming.group && <div className="inbox-group">
          <GroupNameForm label={naming.order ? `Nuevo grupo para ${naming.order.reference}` : 'Nombre del nuevo grupo'} onSubmit={name => createGroup(name, naming.order)} onCancel={() => setNaming(null)}/>
        </div>}
        {empty ? <p className="inbox-empty">{unreadOnly ? 'No tienes mensajes sin leer.' : search ? 'Ninguna orden coincide.' : 'Cuando envíes o recibas una orden, su conversación aparecerá aquí.'}</p>
          : organized ? (!filtering || loose.length > 0) && <div role="group" aria-label="Sin grupo" className={`inbox-group${over === LOOSE ? ' drop' : ''}`} {...dropZone(LOOSE, { group: null, pinned: false })}>
            <div className="inbox-group-head"><button type="button" className="inbox-group-toggle" aria-expanded={looseOpen} onClick={() => toggle(LOOSE)}>
              <span>Sin grupo</span>{looseOpen ? <ChevronDown size={14}/> : <ChevronRight size={14}/>}
            </button></div>
            {looseOpen && <ul>{loose.map(line)}</ul>}
          </div>
          : <ul>{loose.map(line)}</ul>}
      </div>}
    {next && <button type="button" className="button text small" onClick={() => void more()}>Ver más conversaciones</button>}
  </aside>;
}

// Up and down move through a menu's items.
function arrows(event: ReactKeyboardEvent<HTMLDivElement>) {
  if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return;
  event.preventDefault();
  const items = [...event.currentTarget.querySelectorAll<HTMLElement>('[role^="menuitem"]')];
  const at = items.indexOf(document.activeElement as HTMLElement);
  items[(at + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length]?.focus();
}

// A group's name, typed in place: Enter (or leaving the field) saves it, Escape leaves it as it was.
function GroupNameForm({ initial = '', label, onSubmit, onCancel }: { initial?: string; label: string; onSubmit: (name: string) => Promise<void>; onCancel: () => void }) {
  const [name, setName] = useState(initial);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const done = useRef(false);
  async function submit(event?: FormEvent) {
    event?.preventDefault();
    if (done.current || busy) return;
    const value = name.trim();
    if (!value || value === initial) { done.current = true; onCancel(); return; }
    setBusy(true);
    try { done.current = true; await onSubmit(value); }
    catch (caught) { done.current = false; setError(failure(caught)); setBusy(false); }
  }
  return <form className="inbox-group-form" onSubmit={event => void submit(event)}>
    <input autoFocus value={name} maxLength={60} aria-label={label} placeholder="Nombre del grupo" readOnly={busy} aria-invalid={!!error}
      onChange={event => { setName(event.target.value); setError(''); }} onBlur={() => { if (!error) void submit(); }}
      onKeyDown={event => { if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); done.current = true; onCancel(); } }}/>
    {error && <small role="alert">{error}</small>}
  </form>;
}
