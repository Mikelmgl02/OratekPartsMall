'use client';
import { useEffect, useState } from 'react';
import dynamic from 'next/dynamic';
import { BadgeCheck, CircleX, LoaderCircle, PackageCheck, PackageOpen, TriangleAlert, Truck } from 'lucide-react';
import { UppercaseTextarea } from './uppercase-field';
import { ApiError } from '@/lib/types';
import { ACTIVE_AGREEMENT, GOODS_HELD, cancelReasons, cancelSides, shortfallReasons, type Deal, type DealAgreement } from '@/lib/deal-types';

const AgreementGrid = dynamic(() => import('./agreement-grid'), { ssr: false, loading: () => <div className="quotation-grid-loading" role="status"><LoaderCircle size={18} className="spin"/>Cargando acuerdo…</div> });
const date = (value: string) => new Date(value).toLocaleString('es-PA', { timeZone: 'America/Panama', dateStyle: 'medium', timeStyle: 'short' });
// A date that ends a sentence: its own "a. m." closes it.
const dateEnd = (value: string) => date(value).replace(/\.?$/, '.');
const money = (value: string | number, currency = 'USD') => new Intl.NumberFormat('es-PA', { style: 'currency', currency }).format(Number(value));
const units = (count: number) => `${count.toLocaleString('es-PA')} ${count === 1 ? 'unidad' : 'unidades'}`;
type Act = (action: string, extra?: Record<string, unknown>) => Promise<true | ApiError | false>;
// The open confirmation or form. DealDetail holds it, so its Decisiones (in the conversations panel) can open a step's confirmation here.
export type AfterMode = '' | 'process' | 'ship' | 'deliver' | 'receive' | 'shortfall' | 'cancel' | 'accept';
type Step = 'process' | 'ship' | 'deliver';

// The supplier's next steps (mall.deal_lifecycle): Procesando from a confirmed agreement; then En camino, or Entregado at once when the client
// picks it up; Entregado once it is on its way. A shortfall waiting for the client holds a prepared order where it is.
export function supplierSteps(deal: Deal): Step[] {
  const pending = deal.shortfalls?.some(item => item.status === 'pending');
  return deal.status === 'handshaked' ? ['process'] : deal.status === 'processing' ? pending ? [] : ['ship', 'deliver'] : deal.status === 'in_transit' ? ['deliver'] : [];
}
export const stepLabels: Record<Step | 'receive', string> = { process: 'Marcar como procesando', ship: 'Marcar en camino', deliver: 'Marcar como entregado', receive: 'Confirmar recibido' };
export const stepIcons = { process: PackageOpen, ship: Truck, deliver: PackageCheck, receive: BadgeCheck };

// After the handshake (mall.deal_lifecycle): the supplier marks Procesando, En camino and Entregado, or reports units it could not find while
// the goods are still held; the client accepts that shortfall or cancels, and confirms Recibido once the supplier marked Entregado. Once the
// order left (En camino) neither party cancels: MotionPartes can. stepButtons: the step buttons show here (in the conversations panel the
// document's Decisiones offer them instead). The parent keys this by the deal's version, so an open form closes when the deal changes.
export default function DealAfterHandshake({ deal, supplier, busy, act, canDecide = true, inventoryHref, mode, onMode, stepButtons = true }: {
  deal: Deal; supplier: boolean; busy: boolean; act: Act; canDecide?: boolean; inventoryHref?: string; mode: AfterMode; onMode: (mode: AfterMode) => void; stepButtons?: boolean;
}) {
  const [error, setError] = useState('');
  const [missing, setMissing] = useState<Record<string, string>>({});
  const [reason, setReason] = useState('');
  const [note, setNote] = useState('');
  const quotationId = deal.agreement?.quotation_id || deal.quotation?.id;
  const currency = deal.quotation?.currency || 'USD';
  const pending = deal.shortfalls?.find(item => item.status === 'pending');
  const agreement = deal.agreement;
  // A shortfall and a party's cancellation only while the goods are still held (Acuerdo confirmado or Procesando).
  const goodsHeld = GOODS_HELD.includes(deal.status) && !!quotationId;
  const decides = !supplier || canDecide;
  const steps = supplier && decides && quotationId ? supplierSteps(deal) : [];
  const canReceive = !supplier && deal.status === 'delivered' && !!quotationId;
  // Deals confirmed before reservations existed hold no stock: nothing moves when they are delivered, short or cancelled.
  const reserved = deal.stock_reserved !== false;
  // What the supplier's stock did, when its payload says so (older results only know whether the deal ever reserved units).
  const stockUnits = deal.stock_units;
  const stillHeld = stockUnits ? stockUnits.held > 0 : reserved;
  const heldSentence = stockUnits && stockUnits.held > 0 ? `Sus ${stockUnits.held} ${stockUnits.held === 1 ? 'unidad sigue reservada' : 'unidades siguen reservadas'} hasta que tu próxima carga de existencias las descuente.`
    : 'Sus unidades siguen reservadas hasta que tu próxima carga de existencias las descuente.';
  const changed = agreement?.lines.some(line => line.missing || line.pending_missing);
  const reported = Object.entries(missing).map(([order_line_id, value]) => ({ order_line_id, missing: Number(value) })).filter(line => line.missing > 0);
  const projected = agreement ? agreement.lines.reduce((sum, line) => sum + (line.final - line.pending_missing), 0) : 0;
  const projectedTotal = agreement ? agreement.lines.reduce((sum, line) => sum + Number(line.unit_price) * (line.final - line.pending_missing), 0) : 0;
  const agreed = units(agreement?.units ?? 0);
  // A form opened elsewhere (the panel's Decisiones) starts empty too.
  useEffect(() => { setReason(''); setNote(''); setMissing({}); setError(''); }, [mode]);
  const choose = (next: AfterMode) => onMode(mode === next ? '' : next);
  async function send(action: string, extra: Record<string, unknown> = {}) {
    const result = await act(action, { quotation_id: quotationId, ...extra });
    if (result === true) onMode('');
    else if (result instanceof ApiError) setError(result.message);  // shown next to the form too, not only atop the deal
  }
  const stepButton = (step: Step, primary: boolean) => {
    const Icon = stepIcons[step];
    return <button key={step} type="button" className={`button ${mode === step || primary ? 'primary' : 'soft'}`} disabled={busy} aria-expanded={mode === step} onClick={() => choose(step)}><Icon size={15}/>{stepLabels[step]}</button>;
  };
  const back = <button type="button" className="button soft" disabled={busy} onClick={() => onMode('')}>Volver</button>;
  const spinner = (Icon: typeof PackageCheck) => busy ? <LoaderCircle size={15} className="spin"/> : <Icon size={15}/>;
  const shipped = !!(deal.in_transit_at || deal.delivered_at);
  const received = ([deal.delivered_at && `Entregada el ${date(deal.delivered_at)}`, deal.received_at && `recibida el ${date(deal.received_at)}`].filter(Boolean).join(' · ')
    || 'Orden entregada y recibida').replace(/\.?$/, '.');

  return <section className="deal-after" aria-label="Después del acuerdo">
    {error && <div className="notice error" role="alert">{error}</div>}
    {deal.status === 'in_transit' && deal.in_transit_at && <div className="deal-outcome in_transit"><Truck size={22}/><div>
      <strong>En camino</strong><p>{supplier ? `La marcaste en camino el ${dateEnd(deal.in_transit_at)}` : `El proveedor la envió el ${dateEnd(deal.in_transit_at)}`}</p></div></div>}
    {deal.status === 'delivered' && deal.delivered_at && <div className="deal-outcome delivered awaiting"><PackageCheck size={22}/><div>
      {supplier ? <>
        <strong>Entregado · esperando al cliente</strong>
        <p>Marcaste la orden como entregada el {dateEnd(deal.delivered_at)} Falta que el cliente confirme que la recibió.</p>
        {stillHeld && <p>{heldSentence}</p>}
      </> : <>
        <strong>Entregado · falta tu confirmación</strong>
        <p>El proveedor la marcó como entregada el {dateEnd(deal.delivered_at)} ¿Ya la recibiste?</p>
        {canReceive && stepButtons && <button type="button" className="button primary" disabled={busy} aria-expanded={mode === 'receive'}
          onClick={() => choose('receive')}><stepIcons.receive size={15}/>{stepLabels.receive}</button>}
        <small>Si no la recibiste, escríbele al proveedor en la conversación.</small>
      </>}</div></div>}
    {deal.status === 'received' && <div className="deal-outcome received"><BadgeCheck size={22}/><div>
      <strong>Recibido</strong>
      {/* Neutral on both sides: orders delivered before Recibido existed were closed without the client's confirmation. */}
      <p>{received}</p>
      {supplier && stillHeld && <p>{heldSentence}</p>}</div></div>}
    {deal.status === 'cancelled' && deal.cancellation && <div className="deal-outcome cancelled"><CircleX size={22}/><div>
      <strong>Acuerdo cancelado por {cancelSides[deal.cancellation.side]}</strong>
      <p>{deal.cancellation.reason_label} · {date(deal.cancellation.cancelled_at)}</p>{deal.cancellation.note && <p>{deal.cancellation.note}</p>}
      {/* MotionPartes may cancel after the order left: the units come back to the count, the goods may not have. */}
      {supplier && reserved && (!shipped ? <p>Las unidades que el acuerdo aún reservaba volvieron a estar disponibles.</p>
        : !stockUnits || stockUnits.released_after_dispatch > 0 ? <p>Las unidades que el acuerdo aún reservaba volvieron a estar disponibles; si no regresaron a tu bodega, corrige su saldo en {inventoryHref ? <a href={inventoryHref}>Inventario</a> : 'Inventario'}.</p>
        : stockUnits.settled > 0 ? <p>Sus unidades ya se habían descontado de tus existencias; si la mercancía regresa, súmalas a tu inventario.</p> : null)}</div></div>}
    {agreement && changed && <AgreementTable agreement={agreement} currency={currency}/>}
    {pending && <div className="deal-shortfall-pending" role="region" aria-label="Faltante reportado"><TriangleAlert size={20}/><div>
      <strong>{supplier ? 'Faltante reportado · esperando la respuesta del cliente' : 'El proveedor no tiene todas las unidades'}</strong>
      <ul>{pending.lines.map(line => <li key={line.order_line_id}><strong>{line.codigo}</strong>: faltan {line.missing} de {line.agreed}</li>)}</ul>
      <p>{pending.reason_label}{pending.note ? ` · ${pending.note}` : ''} · {date(pending.reported_at)}</p>
      {supplier ? <p>{reserved ? 'Las unidades faltantes ya salieron de tus existencias.' : 'Este acuerdo no reservaba unidades, así que tus existencias no cambiaron.'} Si el saldo real de estos artículos es distinto, corrígelo en {inventoryHref ? <a href={inventoryHref}>Inventario</a> : 'Inventario'}. El cliente puede aceptar el acuerdo reducido o cancelarlo.</p>
        : projected ? <p>Puedes aceptar el acuerdo con las unidades disponibles ({units(projected)} por {money(projectedTotal, currency)}) o cancelarlo sin costo para ti.</p>
        : <p>No queda ninguna unidad: al aceptar el faltante, el acuerdo se cancela sin costo para ti.</p>}
    </div></div>}

    {supplier && !canDecide && ACTIVE_AGREEMENT.includes(deal.status) && <p className="deal-draft-warning">Solo un miembro con permiso para decidir sobre acuerdos puede marcar el avance de la orden (Procesando, En camino, Entregado), reportar un faltante o cancelar.</p>}
    {supplier && deal.status === 'in_transit' && <p className="deal-after-note" role="note">Ya va en camino: si no puedes entregarla, escríbele al cliente; MotionPartes puede cancelar el acuerdo si hay una disputa.</p>}
    {!supplier && deal.status === 'in_transit' && <p className="deal-after-note" role="note">Tu orden va en camino. Si algo no está bien, escríbele al proveedor en la conversación; MotionPartes puede intervenir si no se ponen de acuerdo.</p>}
    {decides && (goodsHeld || (stepButtons && steps.length > 0)) && <div className="deal-after-actions">
      {stepButtons && steps.map((step, index) => stepButton(step, index === 0))}
      {supplier && goodsHeld && !pending && <button type="button" className="button soft" disabled={busy} aria-expanded={mode === 'shortfall'} onClick={() => choose('shortfall')}><TriangleAlert size={15}/>Reportar faltante</button>}
      {!supplier && goodsHeld && pending && <button type="button" className="button primary" disabled={busy} aria-expanded={mode === 'accept'} onClick={() => choose('accept')}>{projected ? 'Aceptar el acuerdo reducido' : 'Aceptar el faltante'}</button>}
      {goodsHeld && <button type="button" className="button soft danger" disabled={busy} aria-expanded={mode === 'cancel'} onClick={() => choose('cancel')}><CircleX size={15}/>Cancelar acuerdo</button>}
    </div>}

    {mode === 'process' && steps.includes('process') && <div className="notice deal-confirmation"><strong>¿Marcar la orden como Procesando?</strong>
      <p>Avisaremos al cliente que la estás preparando. Tus existencias no cambian.</p>
      <div><button type="button" className="button primary" disabled={busy} onClick={() => void send('process')}>{spinner(PackageOpen)}Sí, marcar procesando</button>{back}</div></div>}

    {mode === 'ship' && steps.includes('ship') && <div className="notice deal-confirmation"><strong>¿Marcar la orden En camino?</strong>
      <p>Confirmas que despachaste {agreed}. Después ya no podrás reportar faltantes ni cancelarla; avisaremos al cliente.</p>
      <div><button type="button" className="button primary" disabled={busy} onClick={() => void send('ship')}>{spinner(Truck)}Sí, va en camino</button>{back}</div></div>}

    {mode === 'deliver' && steps.includes('deliver') && <div className="notice deal-confirmation"><strong>¿Marcar la orden como entregada?</strong>
      <p>Confirmas que entregaste {agreed} al cliente (también si la retiró en tu local). El cliente deberá confirmar que la recibió. {reserved ? 'Tus existencias no cambian ahora: las unidades siguen reservadas hasta que tu próxima carga las descuente.' : 'Tus existencias no cambian.'}</p>
      <div><button type="button" className="button primary" disabled={busy} onClick={() => void send('deliver')}>{spinner(PackageCheck)}Sí, marcar entregado</button>{back}</div></div>}

    {mode === 'receive' && canReceive && <div className="notice deal-confirmation"><strong>¿Confirmas que recibiste la orden {deal.reference}?</strong>
      <p>Recibiste {agreed} de {deal.supplier.name}. La orden queda cerrada y el proveedor verá tu confirmación. Si falta algo, escríbele al proveedor antes de confirmar.</p>
      <div><button type="button" className="button primary" disabled={busy} onClick={() => void send('receive')}>{spinner(BadgeCheck)}Sí, la recibí</button>{back}</div></div>}

    {goodsHeld && mode === 'accept' && pending && !supplier && <div className="notice deal-confirmation"><strong>{projected ? '¿Aceptar el acuerdo reducido?' : '¿Aceptar el faltante y cancelar el acuerdo?'}</strong>
      <p>{projected ? `El acuerdo quedará en ${units(projected)} por ${money(projectedTotal, currency)}. La cotización original se conserva en el historial.`
        : 'El proveedor no tiene ninguna de las unidades: el acuerdo queda cancelado por el proveedor, sin costo para ti.'}</p>
      <div><button type="button" className="button primary" disabled={busy} onClick={() => void send('accept_shortfall')}>{busy ? <LoaderCircle size={15} className="spin"/> : null}Sí, aceptar</button>{back}</div></div>}

    {goodsHeld && decides && supplier && !pending && mode === 'shortfall' && agreement && <form className="deal-after-form" aria-label="Reportar faltante" onSubmit={event => { event.preventDefault(); void send('report_shortfall', { shortfall: reported, reason_code: reason, reason: note }); }}>
      <p>Indica cuántas unidades no encontraste de cada artículo. {reserved ? 'Salen de tus existencias ahora mismo, para que nadie más las pida; ' : 'Este acuerdo no reservó unidades, así que tus existencias no cambian; '}el acuerdo cambia cuando el cliente lo acepte, y también puede cancelarlo.</p>
      <div className="table-scroll"><table><thead><tr><th>Artículo</th><th>Acordadas</th><th>Faltan</th></tr></thead><tbody>{agreement.lines.filter(line => line.final > 0).map(line => <tr key={line.order_line_id}>
        <td><strong>{line.codigo}</strong><small>{line.description}</small></td><td>{line.final}</td>
        <td><input type="number" inputMode="numeric" min={0} max={line.final} step={1} aria-label={`Faltan de ${line.codigo}`} value={missing[line.order_line_id] ?? ''} placeholder="0" disabled={busy}
          onChange={event => setMissing(values => ({ ...values, [line.order_line_id]: event.target.value }))}/></td></tr>)}</tbody></table></div>
      <label>Motivo<select required aria-label="Motivo del faltante" value={reason} disabled={busy} onChange={event => setReason(event.target.value)}><option value="">Elige un motivo</option>{shortfallReasons.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
      <label>Nota para el cliente (opcional)<UppercaseTextarea maxLength={4000} value={note} disabled={busy} onChange={event => setNote(event.target.value)} placeholder="POR EJEMPLO: SE DAÑÓ EN BODEGA; LLEGA MÁS MERCADERÍA EL LUNES"/></label>
      <div><button type="submit" className="button primary" disabled={busy || !reason || !reported.length || reported.some(line => !Number.isInteger(line.missing) || line.missing > (agreement.lines.find(item => item.order_line_id === line.order_line_id)?.final ?? 0))}>
        {busy ? <LoaderCircle size={15} className="spin"/> : <TriangleAlert size={15}/>}Reportar faltante</button>{back}</div>
    </form>}

    {goodsHeld && decides && mode === 'cancel' && <form className="deal-after-form cancel" aria-label="Cancelar acuerdo" onSubmit={event => { event.preventDefault(); void send('cancel', { reason_code: reason, reason: note, side: supplier ? 'supplier' : 'client' }); }}>
      <p>{supplier ? `La cancelación queda registrada como incumplimiento del proveedor.${reserved ? ' Las unidades que el acuerdo reserva vuelven a estar disponibles.' : ''}`
        : `El proveedor verá el motivo.${reserved ? ' Las unidades que el acuerdo reserva vuelven al proveedor.' : ''}`}</p>
      <label>Motivo<select required aria-label="Motivo de la cancelación" value={reason} disabled={busy} onChange={event => setReason(event.target.value)}><option value="">Elige un motivo</option>{cancelReasons[supplier ? 'supplier' : 'client'].map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
      <label>Nota (opcional)<UppercaseTextarea maxLength={4000} value={note} disabled={busy} onChange={event => setNote(event.target.value)} placeholder="CUÉNTALE A LA OTRA PARTE QUÉ PASÓ"/></label>
      <div><button type="submit" className="button primary danger" disabled={busy || !reason}>{busy ? <LoaderCircle size={15} className="spin"/> : <CircleX size={15}/>}Cancelar el acuerdo</button>{back}</div>
    </form>}
  </section>;
}

function AgreementTable({ agreement, currency }: { agreement: DealAgreement; currency: string }) {
  return <section className="deal-agreement" aria-label="Acuerdo vigente"><div className="deal-quote-heading"><div><h3>Acuerdo vigente</h3><small>Las unidades acordadas menos los faltantes aceptados</small></div>
    <strong>{money(agreement.total, currency)}</strong></div>
    <AgreementGrid agreement={agreement} currency={currency}/></section>;
}
