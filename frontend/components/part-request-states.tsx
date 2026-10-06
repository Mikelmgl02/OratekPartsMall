import { Check, CheckCheck, Clock3, ShoppingBag, FileText, Handshake, RotateCcw } from 'lucide-react';
import type { RequestedQuantities } from '@/lib/request-types';

export function ItemRequestStates({ draftQuantity, history }: { draftQuantity: number; history?: RequestedQuantities }) {
  return <div className="item-request-states" aria-label="Estado de tus unidades">
    {draftQuantity > 0 && <span className="in-basket"><Check size={12}/>{draftQuantity.toLocaleString('es-PA')} EN CESTA</span>}
    {!!history?.pending_quantity && <span className="pending"><Clock3 size={12}/>{history.pending_quantity.toLocaleString('es-PA')} ENVIADAS · POR REVISAR</span>}
    {!!history?.quoted_quantity && <span className="pending"><FileText size={12}/>{history.quoted_quantity.toLocaleString('es-PA')} COTIZADAS · POR CONFIRMAR</span>}
    {!!history?.adjustment_quantity && <span className="pending"><RotateCcw size={12}/>{history.adjustment_quantity.toLocaleString('es-PA')} EN AJUSTE</span>}
    {!!history?.handshaked_quantity && <span className="reviewed"><Handshake size={12}/>{history.handshaked_quantity.toLocaleString('es-PA')} EN ACUERDOS</span>}
    {!!history?.reviewed_quantity && <span className="reviewed"><CheckCheck size={12}/>{history.reviewed_quantity.toLocaleString('es-PA')} EN REVISIÓN</span>}
  </div>;
}

export default function PartRequestStates({ draftQuantity, history, loading, preview, error, onRetry }: {
  draftQuantity: number; history?: RequestedQuantities; loading: boolean; preview: boolean; error: string; onRetry: () => void;
}) {
  const cards = [
    {label:'EN CESTA', value:draftQuantity, Icon:ShoppingBag, kind:'draft'},
    {label:'ENVIADAS · POR REVISAR', value:history?.pending_quantity, Icon:Clock3, kind:'pending'},
    {label:'EN REVISIÓN', value:history?.reviewed_quantity, Icon:CheckCheck, kind:'reviewed'},
    ...(history?.quoted_quantity ? [{label:'POR CONFIRMAR', value:history.quoted_quantity, Icon:FileText, kind:'quoted'}] : []),
    ...(history?.adjustment_quantity ? [{label:'EN AJUSTE', value:history.adjustment_quantity, Icon:RotateCcw, kind:'adjustment'}] : []),
    ...(history?.handshaked_quantity ? [{label:'EN ACUERDOS', value:history.handshaked_quantity, Icon:Handshake, kind:'handshaked'}] : []),
  ];
  return <section className="part-request-summary" aria-label="Tus cantidades para este SKU">
    <h3>Tus cantidades para este SKU</h3>
    <div className="part-request-counts">{cards.map(({label,value,Icon,kind}) => <div className={kind} key={label}><span><Icon size={14}/>{label}</span><strong>{value === undefined ? loading ? <span className="skeleton-block" aria-label="Cargando cantidades"/> : '—' : value.toLocaleString('es-PA')}</strong><small>UNIDADES</small></div>)}</div>
    <p>{preview ? 'Vista de ejemplo. Estas cantidades de la cesta se guardan únicamente en este navegador.' : 'Las unidades solicitadas aparecen una sola vez, según el estado de su orden. La cotización puede ofrecer una cantidad diferente. Enviar una solicitud no reserva existencias.'}</p>
    {error && <div className="notice error" role="alert">{error}<button onClick={onRetry}>Actualizar estados</button></div>}
  </section>;
}
