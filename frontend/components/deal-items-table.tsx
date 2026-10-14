'use client';
import { Fragment, type ReactNode } from 'react';
import type { Deal, DealAgreement, Quotation } from '@/lib/deal-types';
import type { SupplierRequestLine } from '@/lib/request-types';

// The order's items, the main part of its document (deal-detail.tsx DealDocument), as a dense grid: Código · Descripción · Disp (the
// supplier's available units, before the handshake) · Pedido · Cant. (what the quotation offers) · Precio · Subtotal, with the total. Before a
// quotation, the supplier with price lists sees the suggested price instead. Items go by grupo, subgrupo and description, under a header row
// per grupo › subgrupo. Narrow (the conversations panel's column) the code goes above the description and Pedido under Cant.; on a phone the
// total moves next to its label and Subtotal goes (drawn from data attributes, so each value is in the page once). After the handshake, when
// shortfalls changed the agreement, Cant. and Subtotal are what remains, with a row under each item saying what is missing. Ampliar shows every column (SKU, inventory ID) in the full grid.
type Line = Deal['lines'][number];
const money = (value: string | number, currency = 'USD') => new Intl.NumberFormat('es-PA', { style: 'currency', currency }).format(Number(value));
const units = (value: number) => value.toLocaleString('es-PA');
const stockOf = (line: Line) => 'stock' in line ? (line as SupplierRequestLine).stock : undefined;
const collate = (a = '', b = '') => (a ? b ? a.localeCompare(b, 'es') : -1 : b ? 1 : 0);  // the ungrouped last

export function sortedLines(lines: Deal['lines']) {
  return [...lines].sort((a, b) => collate(a.grupo, b.grupo) || collate(a.subgrupo, b.subgrupo) || a.description.localeCompare(b.description, 'es') || a.codigo.localeCompare(b.codigo));
}

export default function DealItemsTable({ lines, quote, showStock, price, agreement }: {
  lines: Deal['lines']; quote: Quotation | null; showStock: boolean; price?: (line: Line) => ReactNode; agreement?: DealAgreement | null;
}) {
  const offered = new Map((quote?.lines || []).map(line => [line.order_line_id, line]));
  const reduced = agreement?.lines.some(line => line.missing || line.pending_missing) ? new Map(agreement.lines.map(line => [line.order_line_id, line])) : null;
  const currency = quote?.currency || 'USD';
  const sorted = sortedLines(lines), grouped = sorted.some(line => line.grupo);
  const groupOf = (line: Line) => line.grupo ? `${line.grupo}${line.subgrupo ? ` › ${line.subgrupo}` : ''}` : 'Sin grupo';
  const counts = sorted.reduce((map, line) => map.set(groupOf(line), (map.get(groupOf(line)) || 0) + 1), new Map<string, number>());
  const suggested = !!price && !quote;
  const columns = 3 + (showStock ? 1 : 0) + (quote ? 3 : suggested ? 1 : 0);
  return <div className="deal-items-frame">
    <table className={`deal-items ${quote ? 'quoted' : ''}`} aria-label={quote ? `Líneas de la cotización v${quote.revision}` : 'Artículos de la orden'}>
      <thead><tr>
        <th scope="col" className="deal-items-wide">Código</th>
        <th scope="col">Descripción</th>
        {showStock && <th scope="col" className="num">Disp</th>}
        <th scope="col" className={`num ${quote ? 'deal-items-wide' : ''}`}>Pedido</th>
        {suggested && <th scope="col" className="num">Precio sugerido</th>}
        {quote && <><th scope="col" className="num">Cant.</th><th scope="col" className="num">Precio</th><th scope="col" className="num deal-items-subtotal">Subtotal</th></>}
      </tr></thead>
      <tbody>{sorted.map((line, index) => {
        const offer = offered.get(line.id), kept = reduced?.get(line.id), stock = showStock ? stockOf(line) : undefined, group = groupOf(line);
        const given = kept ? kept.final : offer?.quantity ?? 0, sold = !!offer && given > 0, short = !!offer && given < line.quantity;
        const disp = !stock ? 'unknown' : stock.available_quantity >= line.quantity ? 'enough' : stock.available_quantity > 0 ? 'short' : 'none';
        return <Fragment key={line.id}>
          {grouped && (index === 0 || groupOf(sorted[index - 1]) !== group) && <tr className="deal-items-group"><th scope="colgroup" colSpan={columns}>{group} <span>{counts.get(group)}</span></th></tr>}
          <tr className={quote && !sold ? 'deal-items-off' : undefined}>
            <td className="deal-items-wide deal-item-code">{line.codigo}</td>
            <td className="deal-item-description" data-code={line.codigo}>{line.description}{line.brand && <span className="deal-item-brand"> · {line.brand}</span>}</td>
            {showStock && <td className={`num deal-item-disp ${disp}`} title={stock ? `Reportadas: ${units(stock.reported_quantity)} · Reservadas: ${units(stock.reserved_quantity)}${stock.held_for_deal ? ` (${units(stock.held_for_deal)} para esta orden)` : ''}`
              : 'El código o su vínculo con el SKU cambió: confirma la equivalencia en Inventario.'}>
              {stock ? units(stock.available_quantity) : '?'}
              {stock ? stock.shortfall > 0 && <div className="deal-item-note">faltan {units(stock.shortfall)}</div> : <div className="deal-item-note">revisar</div>}</td>}
            <td className={`num deal-item-ordered ${quote ? 'deal-items-wide' : ''}`}>{units(line.quantity)}</td>
            {suggested && <td className="num deal-item-suggested">{price!(line)}</td>}
            {quote && <>
              <td className={`num deal-item-offered ${short ? 'deal-line-short' : ''}`} data-ordered={`de ${units(line.quantity)}`}>{!offer ? '—' : given === 0 ? <span className="deal-line-short">{kept ? '0' : 'No disponible'}</span> : units(given)}
                {kept && kept.final !== kept.agreed && <div className="deal-item-note">acordadas {units(kept.agreed)}</div>}</td>
              <td className="num">{sold ? money(offer.unit_price, currency) : '—'}</td>
              <td className="num deal-items-subtotal"><strong className="deal-item-amount">{sold ? money(kept ? kept.total : offer.total, currency) : '—'}</strong></td>
            </>}
          </tr>
          {kept && (kept.missing > 0 || kept.pending_missing > 0) && <tr className="deal-items-inline"><td colSpan={columns}><div className={kept.pending_missing ? 'pending' : 'accepted'}>
            {kept.missing > 0 && <span>{units(kept.missing)} {kept.missing === 1 ? 'faltante aceptado' : 'faltantes aceptados'} · quedan {units(kept.final)}</span>}
            {kept.pending_missing > 0 && <span>Faltante reportado: {units(kept.pending_missing)} por responder</span>}</div></td></tr>}
        </Fragment>;
      })}</tbody>
      {/* One cell per column (hidden with it when narrow), so the total stays under Subtotal; on a phone it sits next to its label. */}
      {quote && <tfoot><tr>
        <td className="deal-items-wide"/><th scope="row" data-total={money(reduced ? agreement!.total : quote.total, currency)}>Total</th>
        {showStock && <td/>}<td className="deal-items-wide"/><td/><td/>
        <td className="num deal-items-subtotal"><strong className="deal-item-amount">{money(reduced ? agreement!.total : quote.total, currency)}</strong></td>
      </tr></tfoot>}
    </table>
  </div>;
}
