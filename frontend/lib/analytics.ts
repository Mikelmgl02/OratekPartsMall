import type { SessionUser } from './types';

type Usage = { kind: 'visit' } | { kind: 'search'; term: string } | { kind: 'part_view'; part_id: string }
  | { kind: 'basket_add'; part_id: string; supplier_item_id: string; quantity: number };
type Visit = { id: string; lastSeen: number };
const visits = new Map<number, Visit>();
const idleLimit = 30 * 60 * 1000;

function visitId(userId: number) {
  const key = `motionpartes:usage-visit:${userId}`;
  const now = Date.now();
  let visit = visits.get(userId);
  try {
    const saved = JSON.parse(localStorage.getItem(key) || 'null');
    if (saved && typeof saved.id === 'string' && /^[a-f0-9-]{36}$/.test(saved.id) && Number.isFinite(saved.lastSeen)) visit = saved;
  } catch { /* Session tracking also works when browser storage is unavailable. */ }
  if (!visit || now - visit.lastSeen >= idleLimit || visit.lastSeen > now) visit = { id: crypto.randomUUID(), lastSeen: now };
  visit.lastSeen = now;
  visits.set(userId, visit);
  try { localStorage.setItem(key, JSON.stringify(visit)); } catch { /* Keep the visit in memory. */ }
  return visit.id;
}

export function trackUsage(user: SessionUser | null, accountId: string, event: Usage) {
  if (!user || user.is_superuser || !accountId) return;
  try {
    const body = JSON.stringify({ ...event, event_id: crypto.randomUUID(), visit_id: visitId(user.id), account_id: accountId });
    const send = (retry: boolean) => fetch('/api/market/analytics/events', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body, keepalive: true,
    }).then(response => { if (retry && response.status >= 500) setTimeout(() => { void send(false); }, 600); })
      .catch(() => { if (retry) setTimeout(() => { void send(false); }, 600); });
    void send(true);
  } catch { /* Analytics must never interrupt browsing or submitting a request. */ }
}
