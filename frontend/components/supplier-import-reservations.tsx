'use client';

import { useId } from 'react';
import { CheckCircle2, PackageCheck } from 'lucide-react';
import { SupplierImportDecision, SupplierImportReservation } from '@/lib/types';

export type ReservationDecisions = Record<string, SupplierImportDecision>;

const units = (value: number) => value.toLocaleString('es-PA');
const plural = (count: number, one: string, many: string) => `${units(count)} ${count === 1 ? one : many}`;
const day = (value: string) => new Date(value).toLocaleDateString('es-PA', { dateStyle: 'medium', timeZone: 'America/Panama' });

// The confirmed orders still waiting for an answer: those of rows not imported yet (an imported row keeps the answer it was given).
export function undecidedOrders(rows: SupplierImportReservation[], decisions: ReservationDecisions) {
  return rows.filter(row => !row.applied).flatMap(row => row.orders).filter(order => !decisions[order.id]).length;
}

// The heads-up of a stock upload: items of the file that confirmed deals hold units of. Those units left the available stock at the
// handshake; the supplier says, order by order, whether the file's count already discounts it (its reservation is settled) or still
// includes it (it keeps holding its units), so a sale is never subtracted twice.
export default function SupplierImportReservations({ rows, decisions, disabled, onChange }: {
  rows: SupplierImportReservation[]; decisions: ReservationDecisions; disabled: boolean; onChange: (next: ReservationDecisions) => void;
}) {
  const heading = useId();
  const open = rows.filter(row => !row.applied).flatMap(row => row.orders.map(order => order.id));
  const answer = (ids: string[], decision: SupplierImportDecision) => onChange({ ...decisions, ...Object.fromEntries(ids.map(id => [id, decision])) });
  return <section className="supplier-import-reservations" aria-labelledby={heading}>
    <div className="supplier-import-reservations-heading">
      <PackageCheck size={20}/>
      <div><h4 id={heading}>Pedidos confirmados en este archivo</h4>
        <p>{plural(rows.length, 'artículo de este archivo tiene', 'artículos de este archivo tienen')} unidades reservadas por acuerdos confirmados: desde la confirmación ya no figuran como disponibles. Indica si el saldo de tu archivo ya descuenta cada pedido, para no restarlo dos veces.</p></div>
    </div>
    {!!open.length && <div className="supplier-import-reservations-actions">
      <button type="button" className="button soft small" disabled={disabled} onClick={() => answer(open, 'net')}>Mi archivo ya los descuenta todos</button>
      <button type="button" className="button soft small" disabled={disabled} onClick={() => answer(open, 'gross')}>Mi archivo aún los incluye todos</button>
    </div>}
    <ul className="supplier-import-reservation-list">{rows.map(row => {
      const choice = (id: string, given?: SupplierImportDecision | '') => row.applied ? given || '' : decisions[id] || '';
      const answered = row.orders.every(order => choice(order.id, order.decision));
      // Orders the file already discounts stop holding units; the rest of what the item holds stays reserved.
      const settled = row.orders.filter(order => choice(order.id, order.decision) === 'net').reduce((sum, order) => sum + order.quantity, 0);
      const held = Math.max(0, row.reserved_quantity - settled);
      return <li key={row.row} className={`supplier-import-reservation ${row.applied ? 'applied' : ''}`} aria-label={`Fila ${row.row} · ${row.codigo}`}>
        <div className="supplier-import-reservation-item"><span>Fila {row.row} · {row.supplier_invent_id}</span><strong>{row.codigo}</strong>{row.description && <small>{row.description}</small>}</div>
        <dl className="supplier-import-reservation-numbers">
          <div><dt>Saldo actual</dt><dd>{units(row.current_quantity)}</dd></div>
          <div><dt>Saldo en Excel</dt><dd>{units(row.uploaded_quantity)}</dd></div>
          <div><dt>Reservadas</dt><dd>{units(row.reserved_quantity)}</dd></div>
          <div className="result"><dt>{row.applied ? 'Disponibles' : 'Disponibles al importar'}</dt><dd>{answered ? units(Math.max(0, row.uploaded_quantity - held)) : '—'}</dd></div>
        </dl>
        <ul className="supplier-import-orders">{row.orders.map(order => {
          const value = choice(order.id, order.decision);
          return <li key={order.id}>
            <div><strong>{order.reference}</strong><span>{order.client_name} · {plural(order.quantity, 'unidad', 'unidades')} · confirmado el {day(order.confirmed_at)}{order.delivered_at ? ` · entregado el ${day(order.delivered_at)}` : ''}</span></div>
            {row.applied ? <span className="supplier-import-order-answer"><CheckCircle2 size={14}/>{value === 'net' ? 'Ya estaba descontado: la reserva se liquidó' : 'Seguía incluido: sigue reservado'}</span>
              : <fieldset className="supplier-import-order-choice" disabled={disabled} aria-label={`¿El saldo de la fila ${row.row} ya descuenta el pedido ${order.reference}?`}>
                <label className={value === 'net' ? 'selected' : ''}><input type="radio" name={`reservation-${order.id}`} checked={value === 'net'} onChange={() => answer([order.id], 'net')}/>Ya lo descuenta</label>
                <label className={value === 'gross' ? 'selected' : ''}><input type="radio" name={`reservation-${order.id}`} checked={value === 'gross'} onChange={() => answer([order.id], 'gross')}/>Aún lo incluye</label>
              </fieldset>}
          </li>;
        })}</ul>
        {answered && held > row.uploaded_quantity && <small className="supplier-import-reservation-warning">El saldo del archivo es menor que las {plural(held, 'unidad que sigue reservada', 'unidades que siguen reservadas')}: este artículo quedará sin disponibilidad.</small>}
      </li>;
    })}</ul>
  </section>;
}
