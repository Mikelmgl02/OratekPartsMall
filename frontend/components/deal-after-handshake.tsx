'use client';
import { useState } from 'react';
import dynamic from 'next/dynamic';
import { CircleX, LoaderCircle, PackageCheck, TriangleAlert } from 'lucide-react';
import { UppercaseTextarea } from './uppercase-field';
import { ApiError } from '@/lib/types';
import { cancelReasons, cancelSides, shortfallReasons, type Deal, type DealAgreement } from '@/lib/deal-types';

const AgreementGrid = dynamic(() => import('./agreement-grid'), { ssr: false, loading: () => <div className="quotation-grid-loading" role="status"><LoaderCircle size={18} className="spin"/>Cargando acuerdo…</div> });
const date = (value: string) => new Date(value).toLocaleString('es-PA', { timeZone: 'America/Panama', dateStyle: 'medium', timeStyle: 'short' });
const money = (value: string | number, currency = 'USD') => new Intl.NumberFormat('es-PA', { style: 'currency', currency }).format(Number(value));
const units = (count: number) => `${count.toLocaleString('es-PA')} ${count === 1 ? 'unidad' : 'unidades'}`;
type Act = (action: string, extra?: Record<string, unknown>) => Promise<true | ApiError | false>;

// After the handshake (mall.deal_lifecycle): the supplier marks the deal delivered or reports units it could not find, the client accepts
// that shortfall or cancels, either party cancels. The parent keys this by the deal's version, so an open form closes when the deal changes.
export default function DealAfterHandshake({ deal, supplier, busy, act, canDecide = true, inventoryHref }: {
  deal: Deal; supplier: boolean; busy: boolean; act: Act; canDecide?: boolean; inventoryHref?: string;
}) {
  const [mode, setMode] = useState<'' | 'deliver' | 'shortfall' | 'cancel' | 'accept'>('');
  const [error, setError] = useState('');
  const [missing, setMissing] = useState<Record<string, string>>({});
  const [reason, setReason] = useState('');
  const [note, setNote] = useState('');
  const quotationId = deal.agreement?.quotation_id || deal.quotation?.id;
  const currency = deal.quotation?.currency || 'USD';
  const pending = deal.shortfalls?.find(item => item.status === 'pending');
  const agreement = deal.agreement;
  const open = deal.status === 'handshaked' && !!quotationId;
  // Deals confirmed before reservations existed hold no stock: nothing moves when they are delivered, short or cancelled.
  const reserved = deal.stock_reserved !== false;
  const changed = agreement?.lines.some(line => line.missing || line.pending_missing);
  const reported = Object.entries(missing).map(([order_line_id, value]) => ({ order_line_id, missing: Number(value) })).filter(line => line.missing > 0);
  const projected = agreement ? agreement.lines.reduce((sum, line) => sum + (line.final - line.pending_missing), 0) : 0;
  const projectedTotal = agreement ? agreement.lines.reduce((sum, line) => sum + Number(line.unit_price) * (line.final - line.pending_missing), 0) : 0;
  function choose(next: typeof mode) { setMode(value => value === next ? '' : next); setReason(''); setNote(''); setMissing({}); setError(''); }
  async function send(action: string, extra: Record<string, unknown> = {}) {
    const result = await act(action, { quotation_id: quotationId, ...extra });
    if (result === true) choose('');
    else if (result instanceof ApiError) setError(result.message);  // shown next to the form too, not only atop the deal
  }

  return <section className="deal-after" aria-label="Después del acuerdo">
    {error && <div className="notice error" role="alert">{error}</div>}
    {deal.status === 'delivered' && deal.delivered_at && <div className="deal-outcome delivered"><PackageCheck size={22}/><div>
      <strong>Entregado</strong><p>{supplier ? 'Marcaste' : 'El proveedor marcó'} el acuerdo como entregado el {date(deal.delivered_at)}.</p>
      {supplier && reserved && <p>Sus unidades siguen reservadas hasta que tu próxima carga de existencias las descuente.</p>}</div></div>}
    {deal.status === 'cancelled' && deal.cancellation && <div className="deal-outcome cancelled"><CircleX size={22}/><div>
      <strong>Acuerdo cancelado por {cancelSides[deal.cancellation.side]}</strong>
      <p>{deal.cancellation.reason_label} · {date(deal.cancellation.cancelled_at)}</p>{deal.cancellation.note && <p>{deal.cancellation.note}</p>}
      {supplier && reserved && <p>Las unidades que el acuerdo aún reservaba volvieron a estar disponibles.</p>}</div></div>}
    {agreement && changed && <AgreementTable agreement={agreement} currency={currency}/>}
    {pending && <div className="deal-shortfall-pending" role="region" aria-label="Faltante reportado"><TriangleAlert size={20}/><div>
      <strong>{supplier ? 'Faltante reportado · esperando la respuesta del cliente' : 'El proveedor no tiene todas las unidades'}</strong>
      <ul>{pending.lines.map(line => <li key={line.order_line_id}><strong>{line.codigo}</strong>: faltan {line.missing} de {line.agreed}</li>)}</ul>
      <p>{pending.reason_label}{pending.note ? ` · ${pending.note}` : ''} · {date(pending.reported_at)}</p>
      {supplier ? <p>{reserved ? 'Las unidades faltantes ya salieron de tus existencias.' : 'Este acuerdo no reservaba unidades, así que tus existencias no cambiaron.'} Si el saldo real de estos artículos es distinto, corrígelo en {inventoryHref ? <a href={inventoryHref}>Inventario</a> : 'Inventario'}. El cliente puede aceptar el acuerdo reducido o cancelarlo.</p>
        : projected ? <p>Puedes aceptar el acuerdo con las unidades disponibles ({units(projected)} por {money(projectedTotal, currency)}) o cancelarlo sin costo para ti.</p>
        : <p>No queda ninguna unidad: al aceptar el faltante, el acuerdo se cancela sin costo para ti.</p>}
    </div></div>}

    {open && supplier && !canDecide && <p className="deal-draft-warning">Solo un miembro con permiso para decidir sobre acuerdos puede marcar la entrega, reportar un faltante o cancelar.</p>}
    {open && (!supplier || canDecide) && <div className="deal-after-actions">
      {supplier && !pending && <>
        <button type="button" className={`button ${mode === 'deliver' ? 'primary' : 'soft'}`} disabled={busy} aria-expanded={mode === 'deliver'} onClick={() => choose('deliver')}><PackageCheck size={15}/>Marcar como entregado</button>
        <button type="button" className="button soft" disabled={busy} aria-expanded={mode === 'shortfall'} onClick={() => choose('shortfall')}><TriangleAlert size={15}/>Reportar faltante</button>
      </>}
      {!supplier && pending && <button type="button" className="button primary" disabled={busy} aria-expanded={mode === 'accept'} onClick={() => choose('accept')}>{projected ? 'Aceptar el acuerdo reducido' : 'Aceptar el faltante'}</button>}
      <button type="button" className="button soft danger" disabled={busy} aria-expanded={mode === 'cancel'} onClick={() => choose('cancel')}><CircleX size={15}/>Cancelar acuerdo</button>
    </div>}

    {open && mode === 'deliver' && <div className="notice deal-confirmation"><strong>¿Marcar el acuerdo como entregado?</strong>
      <p>Confirmas que entregaste {units(agreement?.units ?? 0)} al cliente. {reserved ? 'Tus existencias no cambian ahora: las unidades siguen reservadas hasta que tu próxima carga las descuente.' : 'Tus existencias no cambian.'}</p>
      <div><button type="button" className="button primary" disabled={busy} onClick={() => void send('deliver')}>{busy ? <LoaderCircle size={15} className="spin"/> : <PackageCheck size={15}/>}Sí, marcar entregado</button>
        <button type="button" className="button soft" disabled={busy} onClick={() => choose('')}>Volver</button></div></div>}

    {open && mode === 'accept' && <div className="notice deal-confirmation"><strong>{projected ? '¿Aceptar el acuerdo reducido?' : '¿Aceptar el faltante y cancelar el acuerdo?'}</strong>
      <p>{projected ? `El acuerdo quedará en ${units(projected)} por ${money(projectedTotal, currency)}. La cotización original se conserva en el historial.`
        : 'El proveedor no tiene ninguna de las unidades: el acuerdo queda cancelado por el proveedor, sin costo para ti.'}</p>
      <div><button type="button" className="button primary" disabled={busy} onClick={() => void send('accept_shortfall')}>{busy ? <LoaderCircle size={15} className="spin"/> : null}Sí, aceptar</button>
        <button type="button" className="button soft" disabled={busy} onClick={() => choose('')}>Volver</button></div></div>}

    {open && mode === 'shortfall' && agreement && <form className="deal-after-form" aria-label="Reportar faltante" onSubmit={event => { event.preventDefault(); void send('report_shortfall', { shortfall: reported, reason_code: reason, reason: note }); }}>
      <p>Indica cuántas unidades no encontraste de cada artículo. {reserved ? 'Salen de tus existencias ahora mismo, para que nadie más las pida; ' : 'Este acuerdo no reservó unidades, así que tus existencias no cambian; '}el acuerdo cambia cuando el cliente lo acepte, y también puede cancelarlo.</p>
      <div className="table-scroll"><table><thead><tr><th>Artículo</th><th>Acordadas</th><th>Faltan</th></tr></thead><tbody>{agreement.lines.filter(line => line.final > 0).map(line => <tr key={line.order_line_id}>
        <td><strong>{line.codigo}</strong><small>{line.description}</small></td><td>{line.final}</td>
        <td><input type="number" inputMode="numeric" min={0} max={line.final} step={1} aria-label={`Faltan de ${line.codigo}`} value={missing[line.order_line_id] ?? ''} placeholder="0" disabled={busy}
          onChange={event => setMissing(values => ({ ...values, [line.order_line_id]: event.target.value }))}/></td></tr>)}</tbody></table></div>
      <label>Motivo<select required aria-label="Motivo del faltante" value={reason} disabled={busy} onChange={event => setReason(event.target.value)}><option value="">Elige un motivo</option>{shortfallReasons.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
      <label>Nota para el cliente (opcional)<UppercaseTextarea maxLength={4000} value={note} disabled={busy} onChange={event => setNote(event.target.value)} placeholder="POR EJEMPLO: SE DAÑÓ EN BODEGA; LLEGA MÁS MERCADERÍA EL LUNES"/></label>
      <div><button type="submit" className="button primary" disabled={busy || !reason || !reported.length || reported.some(line => !Number.isInteger(line.missing) || line.missing > (agreement.lines.find(item => item.order_line_id === line.order_line_id)?.final ?? 0))}>
        {busy ? <LoaderCircle size={15} className="spin"/> : <TriangleAlert size={15}/>}Reportar faltante</button><button type="button" className="button soft" disabled={busy} onClick={() => choose('')}>Volver</button></div>
    </form>}

    {open && mode === 'cancel' && <form className="deal-after-form cancel" aria-label="Cancelar acuerdo" onSubmit={event => { event.preventDefault(); void send('cancel', { reason_code: reason, reason: note, side: supplier ? 'supplier' : 'client' }); }}>
      <p>{supplier ? `La cancelación queda registrada como incumplimiento del proveedor.${reserved ? ' Las unidades que el acuerdo reserva vuelven a estar disponibles.' : ''}`
        : `El proveedor verá el motivo.${reserved ? ' Las unidades que el acuerdo reserva vuelven al proveedor.' : ''}`}</p>
      <label>Motivo<select required aria-label="Motivo de la cancelación" value={reason} disabled={busy} onChange={event => setReason(event.target.value)}><option value="">Elige un motivo</option>{cancelReasons[supplier ? 'supplier' : 'client'].map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
      <label>Nota (opcional)<UppercaseTextarea maxLength={4000} value={note} disabled={busy} onChange={event => setNote(event.target.value)} placeholder="CUÉNTALE A LA OTRA PARTE QUÉ PASÓ"/></label>
      <div><button type="submit" className="button primary danger" disabled={busy || !reason}>{busy ? <LoaderCircle size={15} className="spin"/> : <CircleX size={15}/>}Cancelar el acuerdo</button>
        <button type="button" className="button soft" disabled={busy} onClick={() => choose('')}>Volver</button></div>
    </form>}
  </section>;
}

function AgreementTable({ agreement, currency }: { agreement: DealAgreement; currency: string }) {
  return <section className="deal-agreement" aria-label="Acuerdo vigente"><div className="deal-quote-heading"><div><h3>Acuerdo vigente</h3><small>Las unidades acordadas menos los faltantes aceptados</small></div>
    <strong>{money(agreement.total, currency)}</strong></div>
    <AgreementGrid agreement={agreement} currency={currency}/></section>;
}
