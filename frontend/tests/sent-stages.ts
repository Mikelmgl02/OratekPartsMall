import type { Page } from '@playwright/test';

// The client's orders by stage (GET sent-requests/summary), which the Órdenes page reads for its stage cards: counts per status, the rest
// empty unless a spec says otherwise.
const statuses = ['pending', 'reviewed', 'quoted', 'adjustment', 'handshaked', 'processing', 'in_transit', 'delivered', 'received', 'cancelled'];
export function sentStages(counts: Partial<Record<string, number>> = {}, extra: Record<string, unknown> = {}) {
  const byStatus = Object.fromEntries(statuses.map(status => [status, counts[status] ?? 0]));
  return { total: Object.values(byStatus).reduce((sum, value) => sum + value, 0), statuses: byStatus, oldest: {}, to_confirm: [], in_progress: [], to_receive: [],
    shortfall_pending: 0, month_start: '2026-10-01T05:00:00Z', sent_this_month: 0, received_this_month: 0, updated_at: '2026-10-14T22:52:00Z', ...extra };
}
export const sentStagesRoute = /\/api\/market\/accounts\/[^/]+\/sent-requests\/summary$/;
export const mockSentStages = (page: Page, data = sentStages()) => page.route(sentStagesRoute, route => route.fulfill({ json: data }));
