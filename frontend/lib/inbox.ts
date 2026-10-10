// The person's deal inbox (mall/inbox.py): conversations across every account they belong to, and notifications of what the other party
// did. Everything is polled; INBOX_EVENT asks every inbox view to poll now (after a read mark, a sign-in or a sign-out).
import { request } from './types';
import type { DealStatus } from './deal-types';

export type InboxParty = { id: string; name: string };
export type InboxSide = 'client' | 'supplier';
export type InboxMessage = { id: number; body: string; account_id: string; account_name: string; actor_name: string; mine: boolean; created_at: string };
// pinned and group: where the person keeps it in their list (Fijadas, one of their groups); the list response also carries their groups.
export type InboxConversation = { id: string; reference: string; status: DealStatus; side: InboxSide; account: InboxParty; counterpart: InboxParty; unread: number; activity: string; last_message: InboxMessage | null; pinned?: boolean; group?: string | null };
export type InboxGroup = { id: string; name: string; count: number };
// Per side, how many conversations and unread messages the person has: someone who both buys and sells sees Compras and Ventas apart.
export type InboxSides = Record<InboxSide, { conversations: number; unread: number }>;
export type InboxNotification = { id: number; kind: string; title: string; description: string; order_id: string; reference: string; status: DealStatus; side: InboxSide; account: InboxParty; counterpart: InboxParty; created_at: string; unseen: boolean };
export type InboxSummary = {
  unread_messages: number; unread_conversations: number; unseen_notifications: number;
  latest_message: (InboxMessage & { order_id: string; reference: string; side: InboxSide; account: InboxParty; counterpart: InboxParty }) | null;
  latest_notification: InboxNotification | null;
};
// What the panel needs to open a deal's document and chat without loading the conversation list first.
export type InboxTarget = { orderId: string; reference: string; side: InboxSide; account: InboxParty; counterpart: InboxParty };

export const INBOX_EVENT = 'motionpartes:inbox';
export function inboxChanged() { window.dispatchEvent(new Event(INBOX_EVENT)); }

export async function markConversationRead(orderId: string, lastMessageId: number) {
  try {
    await request(`/api/market/inbox/conversations/${orderId}/read`, { method: 'POST', body: JSON.stringify({ last_message_id: lastMessageId }) });
    inboxChanged();
  } catch { /* The next message read marks it again. */ }
}

export function targetOf(row: InboxConversation | InboxNotification): InboxTarget {
  return { orderId: 'order_id' in row ? row.order_id : row.id, reference: row.reference, side: row.side, account: row.account, counterpart: row.counterpart };
}

export function ago(value: string) {
  const minutes = Math.round((Date.now() - new Date(value).getTime()) / 60000);
  if (minutes < 1) return 'ahora';
  if (minutes < 60) return `hace ${minutes} min`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `hace ${hours} h`;
  return new Date(value).toLocaleDateString('es-PA', { timeZone: 'America/Panama', day: 'numeric', month: 'short' });
}
