'use client';

import { useEffect, useState } from 'react';
import { PackageCheck, X } from 'lucide-react';
import { useInbox } from './inbox';
import { shortReference } from '@/lib/inbox';

// Recibido is never automatic: while orders the supplier marked Entregado wait for the person's confirmation, the storefront reminds them
// (the inbox summary's awaiting_receipt; this asks the server nothing of its own). Confirmar recibido opens the oldest order in the
// conversations panel, where the person confirms it themselves: the reminder never sends it. It hides while the panel is open. Closing it
// snoozes it for 24 h on this device (by person), unless a newer Entregado arrives meanwhile; the browser may keep nothing (a private
// window), and then the snooze lasts the visit.
const SNOOZE = 24 * 60 * 60 * 1000;
type Snooze = { latest_at: string | null; until: number };
const snoozeKey = (userId: number) => `motionpartes:receipt-reminder:${userId}`;
const time = (value: string | null | undefined) => { const parsed = value ? Date.parse(value) : NaN; return Number.isFinite(parsed) ? parsed : 0; };
const kept = new Map<number, Snooze>();

function readSnooze(userId: number): Snooze | null {
  try {
    const saved = JSON.parse(localStorage.getItem(snoozeKey(userId)) || 'null');
    if (saved && typeof saved.until === 'number') return { latest_at: typeof saved.latest_at === 'string' ? saved.latest_at : null, until: saved.until };
  } catch { /* the snooze kept in this page, if any */ }
  return kept.get(userId) ?? null;
}

export default function ReceiptReminderSnackbar({ userId }: { userId: number }) {
  const inbox = useInbox();
  const summary = inbox?.summary ?? null;
  const awaiting = summary?.awaiting_receipt;
  const [snooze, setSnooze] = useState(() => readSnooze(userId));
  // Rendered again when the snooze runs out, even between the inbox's polls.
  const [, setTick] = useState(0);
  const count = awaiting?.count ?? 0;
  const snoozed = !!snooze && Date.now() < snooze.until && time(snooze.latest_at) >= time(awaiting?.latest_at);
  useEffect(() => {
    if (!snoozed || !snooze) return;
    const timer = setTimeout(() => setTick(value => value + 1), Math.min(Math.max(0, snooze.until - Date.now()) + 50, 2 ** 31 - 1));
    return () => clearTimeout(timer);
  }, [snoozed, snooze]);
  const oldest = awaiting?.orders[0];
  const visible = count > 0 && inbox?.showing === null && !snoozed;
  function close() {
    const next = { latest_at: awaiting?.latest_at ?? null, until: Date.now() + SNOOZE };
    setSnooze(next); kept.set(userId, next);
    try { localStorage.setItem(snoozeKey(userId), JSON.stringify(next)); } catch { /* kept for this visit only */ }
  }
  // Which of their accounts the first order is for matters only when the waiting orders are for more than one.
  const severalAccounts = new Set(awaiting?.orders.map(order => order.account.id)).size > 1;
  const named = oldest && `${shortReference(oldest.reference)} de ${oldest.counterpart.name}${severalAccounts ? ` · ${oldest.account.name}` : ''}`;
  // The live region stays in the page so screen readers announce the reminder when it appears.
  return <div className="receipt-snackbar-region" role="status" aria-live="polite" data-loaded={summary ? '' : undefined}>
    {visible && inbox && oldest && <div className="receipt-snackbar">
      <PackageCheck size={18} aria-hidden="true"/>
      <span className="receipt-snackbar-text" title={oldest.reference}>{count === 1
        ? `${oldest.counterpart.name} marcó tu orden ${shortReference(oldest.reference)} como entregada. ¿Ya la recibiste?`
        : `Tienes ${count.toLocaleString('es-PA')} órdenes marcadas como entregadas. ¿Ya las recibiste? La primera: ${named}.`}</span>
      <span className="receipt-snackbar-actions">
        <button type="button" className="receipt-snackbar-confirm" onClick={() => inbox.open({ threadId: oldest.thread_id, orderId: oldest.id, reference: oldest.reference,
          side: 'client', account: oldest.account, counterpart: oldest.counterpart })}>Confirmar recibido</button>
        {count > 1 && <a href={`/?vista=solicitudes&cuenta=${encodeURIComponent(oldest.account.id)}&estado=por-recibir`}>Ver todas</a>}
      </span>
      <button type="button" className="receipt-snackbar-close" aria-label="Cerrar recordatorio de recibido" onClick={close}><X size={16}/></button>
    </div>}
  </div>;
}
