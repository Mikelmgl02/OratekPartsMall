'use client';
import type { ReactNode } from 'react';
import type { Deal, Quotation } from '@/lib/deal-types';
import type { SupplierRequestLine } from '@/lib/request-types';

// The order's items as a plain table, the main part of its document (deal-detail.tsx DealDocument): what the client asked for and, once
// quoted, what the supplier offers of each item, at what price and for how much, with the total. Before the handshake the supplier also
// sees its available units, with the reported and reserved ones when some are held, and, with price lists, the suggested price. Ampliar
// shows every column (SKU, inventory ID) in the full grid. Narrow (the conversations panel's column, a phone) the availability goes under
// the item, a short offer under the ordered units and the unit price under the amount, so the item keeps its room.
type Line = Deal['lines'][number];
const money = (value: string | number, currency = 'USD') => new Intl.NumberFormat('es-PA', { style: 'currency', currency }).format(Number(value));
const units = (value: number) => value.toLocaleString('es-PA');
const stockOf = (line: Line) => 'stock' in line ? (line as SupplierRequestLine).stock : undefined;

// The supplier's available units for one line: in their own column, or on one line under the item when narrow.
function Availability({ stock, inline = false }: { stock: SupplierRequestLine['stock'] | undefined; inline?: boolean }) {
  if (!stock) return <><span className="deal-line-short">Revisar artículo</span>
    <div className="deal-item-note deal-item-detail">Su código cambió: confirma la equivalencia en Inventario.</div></>;
  const enough = stock.shortfall ? <span className="deal-line-short">faltan {units(stock.shortfall)}</span> : 'suficientes';
  return <>
    {inline ? <>Disponibles: {units(stock.available_quantity)} · {enough}</> : <>{units(stock.available_quantity)}<div className="deal-item-note">{enough}</div></>}
    {stock.reserved_quantity > 0 && <div className="deal-item-note deal-item-detail">{units(stock.reported_quantity)} reportadas · {units(stock.reserved_quantity)} reservadas
      {stock.held_for_deal ? ` (${units(stock.held_for_deal)} para esta orden)` : ''}</div>}
  </>;
}

export default function DealItemsTable({ lines, quote, showStock, price }: {
  lines: Deal['lines']; quote: Quotation | null; showStock: boolean; price?: (line: Line) => ReactNode;
}) {
  const offered = new Map((quote?.lines || []).map(line => [line.order_line_id, line]));
  const currency = quote?.currency || 'USD';
  return <div className="deal-items-frame">
    <table className="deal-items" aria-label={quote ? `Líneas de la cotización v${quote.revision}` : 'Artículos de la orden'}>
      <thead><tr>
        <th scope="col">Artículo</th>
        <th scope="col" className="num">Pedidas</th>
        {showStock && <th scope="col" className="num deal-items-wide">Disponibles</th>}
        {price && !quote && <th scope="col" className="num">Precio sugerido</th>}
        {quote && <><th scope="col" className="num deal-items-wide">Ofrecidas</th><th scope="col" className="num deal-items-wide">Precio</th><th scope="col" className="num">Importe</th></>}
      </tr></thead>
      <tbody>{lines.map(line => {
        const offer = offered.get(line.id), stock = showStock ? stockOf(line) : undefined;
        const short = !!offer && offer.quantity < line.quantity, sold = !!offer && offer.quantity > 0;
        return <tr key={line.id}>
          <td><div className="deal-item-code">{line.codigo}</div><div className="deal-item-description">{line.brand ? `${line.brand} · ` : ''}{line.description}</div>
            {showStock && <div className="deal-item-note deal-item-stock deal-items-narrow"><Availability stock={stock} inline/></div>}</td>
          <td className="num">{units(line.quantity)}
            {short && <div className="deal-item-note deal-line-short deal-items-narrow">{sold ? `ofrece ${units(offer.quantity)}` : 'No disponible'}</div>}</td>
          {showStock && <td className="num deal-items-wide"><Availability stock={stock}/></td>}
          {price && !quote && <td className="num deal-item-suggested">{price(line)}</td>}
          {quote && <>
            <td className="num deal-items-wide">{!offer ? '—' : offer.quantity === 0 ? <span className="deal-line-short">No disponible</span>
              : <span className={short ? 'deal-line-short' : undefined}>{units(offer.quantity)}</span>}</td>
            <td className="num deal-items-wide">{sold ? money(offer.unit_price, currency) : '—'}</td>
            <td className="num"><strong className="deal-item-amount">{sold ? money(offer.total, currency) : '—'}</strong>
              {sold && <div className="deal-item-note deal-items-narrow">{units(offer.quantity)} × {money(offer.unit_price, currency)}</div>}</td>
          </>}
        </tr>;
      })}</tbody>
      {/* One cell per column (hidden with it when narrow), so the total stays under Importe in both layouts. */}
      {quote && <tfoot><tr>
        <th scope="row">Total</th><td/>{showStock && <td className="deal-items-wide"/>}<td className="deal-items-wide"/><td className="deal-items-wide"/>
        <td className="num"><strong className="deal-item-amount">{money(quote.total, currency)}</strong></td>
      </tr></tfoot>}
    </table>
  </div>;
}
