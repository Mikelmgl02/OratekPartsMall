import type { Page } from '@playwright/test';
import type { BasketLine, Offer, Part } from '../lib/types';

// The server's cesta (mall/basket.py) for mocked specs: lines per client account, answered as the API does. An article goes in only when
// the spec knows it (a part's offers, or a line the spec seeded: a cesta an old browser kept, which moves in once) and is not the
// account's own; added again under the SKU it now has, its line takes that article as it is now.
export type BasketChange = { supplier_item_id: string; quantity: number; mode?: 'add' | 'set' | 'max'; expected_part_id?: string };
export type FakeBasket = {
  lines: Record<string, BasketLine[]>;
  changes: { account: string; changes: BasketChange[] }[];
  // A solicitud the spec's own mock accepted: its units leave the cesta, as the server does in the same transaction.
  settle: (account: string, sent: { supplier_item_id: string; quantity: number }[]) => void;
};

export async function fakeServerBasket(page: Page, { parts = [], offers = {}, known = [], server = {} }: {
  parts?: Part[]; offers?: Record<string, Offer[]>; known?: BasketLine[]; server?: Record<string, BasketLine[]>;
} = {}): Promise<FakeBasket> {
  const templates = new Map<string, BasketLine[]>();
  const remember = (line: BasketLine) => templates.set(line.offer.selected_item!.id, [...(templates.get(line.offer.selected_item!.id) || []), line]);
  for (const line of [...known, ...Object.values(server).flat()]) if (line.offer.selected_item) remember(line);
  for (const part of parts) for (const offer of offers[part.id] || []) for (const item of offer.items || []) {
    const { items: _, ...supplier } = offer;
    remember({ key: `${part.id}:${offer.supplier_id}:${item.id}`, part, offer: { ...supplier, selected_item: item }, quantity: 1 });
  }
  // The article under the SKU it was chosen under, or else as the spec knows it first.
  const template = (change: BasketChange) => (templates.get(change.supplier_item_id) || []).find(line => !change.expected_part_id || line.part.id === change.expected_part_id)
    ?? templates.get(change.supplier_item_id)?.[0];
  const state: FakeBasket = {
    lines: Object.fromEntries(Object.entries(server).map(([account, lines]) => [account, lines.map(line => ({ ...line }))])),
    changes: [],
    settle(account, sent) {
      const lines = state.lines[account] ??= [];
      for (const item of sent) {
        const index = lines.findIndex(line => line.offer.selected_item?.id === item.supplier_item_id);
        if (index < 0) continue;
        if (lines[index].quantity <= item.quantity) lines.splice(index, 1); else lines[index] = { ...lines[index], quantity: lines[index].quantity - item.quantity };
      }
    },
  };
  await page.route(/\/api\/market\/accounts\/([^/?]+)\/basket$/, route => {
    const account = new URL(route.request().url()).pathname.split('/').at(-2)!;
    const lines = state.lines[account] ??= [];
    if (route.request().method() === 'GET') return route.fulfill({ json: { lines, count: lines.length } });
    const { changes } = route.request().postDataJSON() as { changes: BasketChange[] };
    state.changes.push({ account, changes });
    const next = lines.map(line => ({ ...line }));
    for (const change of changes) {
      const index = next.findIndex(line => line.offer.selected_item?.id === change.supplier_item_id);
      if (index >= 0) {
        const fresh = change.mode === 'add' && change.expected_part_id ? template(change) : undefined;
        if (fresh && fresh.part.id === change.expected_part_id) next[index] = { ...fresh, quantity: next[index].quantity };
        const current = next[index].quantity;
        const total = change.mode === 'set' ? change.quantity : change.mode === 'max' ? Math.max(current, change.quantity) : current + change.quantity;
        if (total <= 0) next.splice(index, 1); else next[index] = { ...next[index], quantity: Math.min(9999, total) };
        continue;
      }
      if (change.quantity <= 0) continue;
      const found = template(change);
      if (!found) return route.fulfill({ status: 400, json: { changes: ['Un artículo ya no está disponible para solicitar una cotización.'] } });  // all or none
      if (found.offer.supplier_id === account) return route.fulfill({ status: 400, json: { changes: [`${found.offer.selected_item!.codigo} es de tu propia cuenta: no puedes pedirte una cotización a ti mismo.`] } });
      next.push({ ...found, quantity: Math.min(9999, change.quantity) });
    }
    state.lines[account] = next;
    return route.fulfill({ json: { lines: next, count: next.length } });
  });
  return state;
}
