// The person's deal inbox (mall/inbox.py): conversations across every account they belong to, one per client and supplier (a linear chat
// with all their orders, each message tagged with its order when it has one), and notifications of what the other party did on their orders.
// Everything is polled; INBOX_EVENT asks every inbox view to poll now (after a read mark, a sign-in or a sign-out).
import { request } from './types';
import type { DealStatus } from './deal-types';

export type InboxParty = { id: string; name: string };
export type InboxSide = 'client' | 'supplier';
export type InboxMessage = { id: number; body: string; account_id: string; account_name: string; actor_name: string; mine: boolean; created_at: string; order_id?: string | null; reference?: string | null };
export type InboxOrder = { id: string; reference: string; status: DealStatus };
// id: the conversation; order_id (with its reference and status): the order it opens on (the one in progress most recently changed);
// orders: its orders, latest first. pinned and group: where the person keeps it in their list (Fijadas, one of their groups); the list
// response also carries their groups.
export type InboxConversation = { id: string; order_id: string | null; reference: string; status: DealStatus; orders?: InboxOrder[]; order_count?: number; open_orders?: number;
  side: InboxSide; account: InboxParty; counterpart: InboxParty; unread: number; activity: string; last_message: InboxMessage | null; pinned?: boolean; group?: string | null };
export type InboxGroup = { id: string; name: string; count: number };
// Per side, how many conversations and unread messages the person has: someone who both buys and sells sees Compras and Ventas apart.
export type InboxSides = Record<InboxSide, { conversations: number; unread: number }>;
export type InboxNotification = { id: number; kind: string; title: string; description: string; order_id: string; thread_id?: string; reference: string; status: DealStatus; side: InboxSide; account: InboxParty; counterpart: InboxParty; created_at: string; unseen: boolean };
export type InboxSummary = {
  unread_messages: number; unread_conversations: number; unseen_notifications: number;
  latest_message: (InboxMessage & { thread_id?: string; order_id: string; reference: string; side: InboxSide; account: InboxParty; counterpart: InboxParty }) | null;
  latest_notification: InboxNotification | null;
  // Orders the supplier marked Entregado that wait for the person's Recibido, across their client accounts (mall/inbox.py awaiting_receipt):
  // how many, the newest Entregado (it brings a closed reminder back) and the three oldest. Optional: older servers do not send it.
  awaiting_receipt?: { count: number; latest_at: string | null; orders: InboxReceiptOrder[] };
};
export type InboxReceiptOrder = { id: string; thread_id: string; reference: string; delivered_at: string; account: InboxParty; counterpart: InboxParty };
// What the panel needs to open a conversation (threadId) on one of its orders (orderId: its document, and the tag of what is written)
// without loading the conversation list first.
// draft: a message prefilled in the composer, in general (Preguntar al proveedor, from the catalog). orderId '' : a conversation without orders.
export type InboxTarget = { threadId?: string; orderId: string; reference: string; side: InboxSide; account: InboxParty; counterpart: InboxParty; draft?: string };

export const INBOX_EVENT = 'motionpartes:inbox';
export function inboxChanged() { window.dispatchEvent(new Event(INBOX_EVENT)); }

export async function markConversationRead(orderId: string, lastMessageId: number) {
  try {
    await request(`/api/market/inbox/conversations/${orderId}/read`, { method: 'POST', body: JSON.stringify({ last_message_id: lastMessageId }) });
    inboxChanged();
  } catch { /* The next message read marks it again. */ }
}

// A dated reference (ORD-20261010-55791743) on a conversation's line: its last part, "…55791743" (the whole one is in the line's tooltip,
// the chat's header and the order). Other references stay whole.
export function shortReference(reference: string) {
  const match = /^[A-Z]+-\d{8}-([A-Z0-9]+)$/.exec(reference);
  return match ? `…${match[1]}` : reference;
}

export function targetOf(row: InboxConversation | InboxNotification, orderId?: string): InboxTarget {
  if ('title' in row) return { threadId: row.thread_id, orderId: row.order_id, reference: row.reference, side: row.side, account: row.account, counterpart: row.counterpart };
  const named = orderId ? row.orders?.find(order => order.id === orderId) : undefined;  // opened on that order (a notice's, a push's)
  return { threadId: row.id, orderId: named?.id ?? row.order_id ?? '', reference: named?.reference ?? row.reference, side: row.side, account: row.account, counterpart: row.counterpart };
}

// Same conversation? A target opened from elsewhere may know only its order.
export function sameConversation(target: InboxTarget | null | undefined, row: InboxConversation) {
  return !!target && (target.threadId ? target.threadId === row.id : !!row.orders?.some(order => order.id === target.orderId));
}

export function ago(value: string) {
  const minutes = Math.round((Date.now() - new Date(value).getTime()) / 60000);
  if (minutes < 1) return 'ahora';
  if (minutes < 60) return `hace ${minutes} min`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `hace ${hours} h`;
  return new Date(value).toLocaleDateString('es-PA', { timeZone: 'America/Panama', day: 'numeric', month: 'short' });
}
