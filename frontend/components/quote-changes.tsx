'use client';
import type { ReactNode } from 'react';
import type { Deal, Quotation } from '@/lib/deal-types';

// What each quotation version changed against the one before, line by line, then the total (deal-detail.tsx): the latest version's changes
// on top, the earlier ones folded under it, each able to show its whole document. The same kind of change on more than 3 items reads as one
// line ("40 artículos: precio cambiado"), its items folded under it.
type Change = { code: string | null; kind: string; text: string };
const money = (value: string | number, currency = 'USD') => new Intl.NumberFormat('es-PA', { style: 'currency', currency }).format(Number(value));
const day = (value: string) => new Date(value).toLocaleDateString('es-PA', { timeZone: 'America/Panama', dateStyle: 'medium' });
const units = (value: number) => `${value} ${value === 1 ? 'unidad' : 'unidades'}`;

export function quoteChanges(before: Quotation, after: Quotation, lines: Deal['lines']): Change[] {
  const changes: Change[] = [], next = new Map(after.lines.map(line => [line.order_line_id, line]));
  const codeOf = (id: string) => lines.find(line => line.id === id)?.codigo ?? before.lines.find(line => line.order_line_id === id)?.codigo ?? '';
  for (const a of before.lines) {
    const b = next.get(a.order_line_id), code = codeOf(a.order_line_id);
    if (!b) { changes.push({ code, kind: 'quitado', text: `quitado de la cotización (${units(a.quantity)})` }); continue; }
    if (b.quantity !== a.quantity) changes.push({ code, kind: 'ofrecidas', text: b.quantity === 0 ? `ya no se ofrece (antes ${a.quantity})` : a.quantity === 0 ? `ahora se ofrece (${b.quantity})` : `ofrecidas ${a.quantity} → ${b.quantity}` });
    if (Number(b.unit_price) !== Number(a.unit_price) && b.quantity > 0) changes.push({ code, kind: 'precio', text: `precio ${money(a.unit_price, before.currency)} → ${money(b.unit_price, after.currency)}` });
  }
  for (const b of after.lines) if (!before.lines.some(a => a.order_line_id === b.order_line_id)) changes.push({ code: codeOf(b.order_line_id), kind: 'agregado', text: `agregado (${b.quantity})` });
  if (before.terms.trim() !== after.terms.trim()) changes.push({ code: null, kind: 'condiciones', text: 'Condiciones actualizadas.' });
  if (Number(before.total) !== Number(after.total)) changes.push({ code: null, kind: 'total', text: `Total: ${money(before.total, before.currency)} → ${money(after.total, after.currency)}.` });
  return changes;
}

function ChangeList({ changes }: { changes: Change[] }) {
  if (!changes.length) return <ul><li>Sin cambios en los artículos.</li></ul>;
  const groups = new Map<string, Change[]>();
  for (const change of changes) groups.set(change.code ? change.kind : `${change.kind}:${change.text}`, [...(groups.get(change.code ? change.kind : `${change.kind}:${change.text}`) || []), change]);
  const items: ReactNode[] = [];
  for (const [key, group] of groups) {
    if (group.length <= 3) { for (const change of group) items.push(<li key={`${key}:${change.code}:${change.text}`}>{change.code && <strong>{change.code}: </strong>}{change.text}{change.code ? '.' : ''}</li>); continue; }
    const same = group.every(change => change.text === group[0].text);
    items.push(<li key={key}><details><summary>{group.length} artículos: {same ? group[0].text : `${group[0].kind} cambiado`}</summary>
      {same ? <p>{group.map(change => change.code).join(' · ')}</p> : <ul>{group.map(change => <li key={change.code}><strong>{change.code}: </strong>{change.text}.</li>)}</ul>}</details></li>);
  }
  return <ul>{items}</ul>;
}

export default function QuoteChanges({ deal, renderVersion }: { deal: Deal; renderVersion: (quote: Quotation) => ReactNode }) {
  const quotes = deal.quotations || [];
  if (quotes.length < 2) return null;
  const versions = quotes.slice(1).map((quote, index) => ({ quote, changes: quoteChanges(quotes[index], quote, deal.lines) })).reverse();
  const [latest, ...older] = versions;
  return <section className="deal-quote-changes" aria-label="Cambios entre versiones">
    <h4>Cambios en v{latest.quote.revision} <small>· por el proveedor · {day(latest.quote.created_at)}</small></h4>
    <ChangeList changes={latest.changes}/>
    <details className="deal-quote-changes-older"><summary>Versiones anteriores ({quotes.length - 1})</summary>
      {older.map(({ quote, changes }) => <div key={quote.id} className="deal-quote-changes-version"><p><strong>v{quote.revision}</strong> · por el proveedor · {day(quote.created_at)}</p><ChangeList changes={changes}/></div>)}
      <div className="deal-quote-changes-version"><p><strong>v{quotes[0].revision}</strong> · por el proveedor · {day(quotes[0].created_at)} · cotización inicial</p></div>
      {quotes.slice(0, -1).map(quote => <details key={quote.id} className="deal-quote-changes-doc"><summary>Ver documento v{quote.revision}</summary>{renderVersion(quote)}</details>)}
    </details>
  </section>;
}
