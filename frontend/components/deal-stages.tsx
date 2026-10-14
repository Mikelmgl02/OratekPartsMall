'use client';
import { useState } from 'react';
import { Check, CircleHelp, LockKeyhole, Minus } from 'lucide-react';
import Modal from './modal';
import type { Deal, DealStatus } from '@/lib/deal-types';

// The order's stages, as a stepper at the top of its document (deal-detail.tsx): the stage the order is in, those it went through (each one
// opens read-only, as the order was then) and those still ahead, locked until it gets there. The ? opens what each stage means, and the
// note under it says what it takes to reach the next one, for the side reading it.
export type Stage = { label: string; statuses: DealStatus[]; who: '' | 'cliente' | 'proveedor'; help: string };
export const STAGES: Stage[] = [
  { label: 'Sin cotizar', statuses: ['pending', 'reviewed'], who: 'proveedor',
    help: 'El cliente envió su pedido. El proveedor lo abre (queda bloqueado para su revisión), revisa existencias y prepara la cotización.' },
  { label: 'Cotizada', statuses: ['quoted', 'adjustment'], who: 'cliente',
    help: 'El proveedor envió una cotización con cantidades, precios y condiciones; sus unidades quedan reservadas unos días. El cliente la confirma o pide un ajuste, y el proveedor responde con una nueva versión.' },
  { label: 'Acordada', statuses: ['handshaked'], who: 'proveedor', help: 'Las dos partes confirmaron la misma versión: es el acuerdo. Las unidades quedan apartadas para esta orden.' },
  { label: 'Procesando', statuses: ['processing'], who: 'proveedor', help: 'El proveedor prepara el pedido.' },
  { label: 'En camino', statuses: ['in_transit'], who: 'proveedor', help: 'El pedido salió hacia el cliente. Si el cliente lo retira, esta etapa se omite.' },
  { label: 'Entregada', statuses: ['delivered'], who: 'cliente', help: 'El proveedor la marcó entregada. Falta que el cliente confirme que la recibió; se le recuerda si tarda.' },
  { label: 'Recibida', statuses: ['received'], who: '', help: 'El cliente confirmó que la recibió. La orden queda cerrada.' },
];

// The furthest stage the order reached. A cancelled order stops at the last stage its dates reached.
export function reachedStage(deal: Deal) {
  if (deal.status !== 'cancelled') return Math.max(0, STAGES.findIndex(stage => stage.statuses.includes(deal.status)));
  return deal.delivered_at ? 5 : deal.in_transit_at ? 4 : deal.processing_at ? 3 : deal.handshaked_at ? 2 : deal.quotation ? 1 : 0;
}
// When the order entered each stage (null: not yet, or skipped).
export function stageDates(deal: Deal): (string | null)[] {
  return [deal.created_at, deal.quotations?.[0]?.created_at ?? deal.quotation?.created_at ?? null, deal.handshaked_at, deal.processing_at ?? null,
    deal.in_transit_at ?? null, deal.delivered_at ?? null, deal.received_at ?? null];
}
// Procesando and En camino can be passed without being marked (a pickup skips En camino; older orders skip both).
export const skippedStage = (deal: Deal, index: number) => index < reachedStage(deal) && ((index === 3 && !deal.processing_at) || (index === 4 && !deal.in_transit_at));

const when = (value: string | null) => value ? new Date(value).toLocaleString('es-PA', { timeZone: 'America/Panama', dateStyle: 'medium', timeStyle: 'short' }) : '';

export default function DealStages({ deal, viewing, onView }: { deal: Deal; viewing: number; onView: (index: number) => void }) {
  const [help, setHelp] = useState(false);
  const reached = reachedStage(deal), dates = stageDates(deal), cancelled = deal.status === 'cancelled', closed = deal.status === 'received';
  return <>
    <nav className={`deal-stages ${cancelled ? 'cancelled' : ''}`} aria-label="Etapas de la orden">
      <ol>{STAGES.map((stage, index) => {
        const locked = index > reached, skipped = skippedStage(deal, index), done = (index < reached && !skipped) || (closed && index === reached);
        const title = locked ? `${stage.label} · se desbloquea cuando la orden llegue a esta etapa` : skipped ? `${stage.label} · omitida`
          : [stage.label, dates[index] && when(dates[index]), index === reached && !closed && !cancelled && stage.who && `le toca al ${stage.who}`].filter(Boolean).join(' · ');
        return <li key={stage.label}>
          <button type="button" className={['deal-stage', done && 'done', index === viewing && 'viewing', index === reached && 'current', locked && 'locked', skipped && 'skipped'].filter(Boolean).join(' ')}
            disabled={locked || skipped} aria-current={index === viewing ? 'step' : undefined} title={title} onClick={() => onView(index)}>
            <span className="deal-stage-dot" aria-hidden="true">{skipped ? <Minus size={11}/> : done ? <Check size={11}/> : locked ? <LockKeyhole size={10}/> : index + 1}</span>
            <span className="deal-stage-label">{stage.label}</span>
            {(locked || skipped) && <span className="sr-only">{locked ? ', bloqueada' : ', omitida'}</span>}
          </button>
        </li>;
      })}</ol>
      {cancelled && <span className="deal-stage-cancelled">Cancelada</span>}
      <button type="button" className="deal-stages-help" aria-label="¿Qué significa cada etapa?" title="¿Qué significa cada etapa?" onClick={() => setHelp(true)}><CircleHelp size={16}/></button>
    </nav>
    {help && <Modal title="Etapas de la orden" className="deal-stages-modal" onClose={() => setHelp(false)}>
      <ol className="deal-stages-list">{STAGES.map((stage, index) => {
        const locked = index > reached, skipped = skippedStage(deal, index);
        return <li key={stage.label} className={[index < reached && !skipped && 'done', index === reached && 'current', locked && 'locked'].filter(Boolean).join(' ')}>
          <span className="deal-stage-dot" aria-hidden="true">{index + 1}</span>
          <div><strong>{stage.label}{index === reached && <em> · {cancelled ? 'aquí se canceló' : 'etapa actual'}</em>}{locked && <span> · bloqueada</span>}{skipped && <span> · omitida</span>}</strong>
            {stage.who && <small>Le toca: {stage.who === 'cliente' ? 'Cliente' : 'Proveedor'}</small>}<p>{stage.help}</p></div>
        </li>;
      })}</ol>
    </Modal>}
  </>;
}

// What it takes to reach the next stage, for the side reading it: your move, the other party's, or nothing left.
export function nextStep(deal: Deal, supplier: boolean, stockWarnings = 0): { who: 'you' | 'them' | 'done'; text: string } {
  const version = `la cotización v${deal.quotation?.revision ?? 1}`;
  const steps: Record<DealStatus, [boolean, string, string]> = {  // [supplier's move, supplier's text, client's text]
    pending: [true, 'Abre la orden para revisarla: quedará bloqueada para el cliente y podrás preparar la cotización.',
      'Puedes agregar artículos enviando otra cesta a este proveedor. Al abrir la orden, el proveedor la bloqueará para revisión.'],
    reviewed: [true, `Los artículos de esta orden están bloqueados. Revisa las existencias${stockWarnings ? ` (${stockWarnings} ${stockWarnings === 1 ? 'artículo por confirmar' : 'artículos por confirmar'})` : ''}, fija los precios y envía la cotización.`,
      'Los artículos de esta orden están bloqueados. El proveedor está preparando la cotización.'],
    quoted: [false, `Enviaste ${version}. Mientras el cliente no la confirme, puedes editarla y enviarle una nueva versión.`, `Confirma ${version} o solicita un ajuste.`],
    adjustment: [true, 'El cliente solicitó un ajuste. Envía una nueva versión o devuelve la misma cotización.', 'Solicitaste un ajuste. El proveedor enviará una nueva versión o devolverá la misma cotización.'],
    handshaked: [true, 'Ambas partes confirmaron la cotización. Márcala como Procesando cuando empieces a prepararla.', 'Ambas partes confirmaron la cotización. El proveedor te avisará cuando empiece a prepararla.'],
    processing: [true, 'Estás preparando la orden. Márcala En camino cuando salga, o como entregada si el cliente la retira.', 'El proveedor está preparando tu orden.'],
    in_transit: [true, 'La orden va en camino. Márcala como entregada cuando llegue.', 'Tu orden va en camino.'],
    delivered: [false, 'Marcaste la orden como entregada. Falta que el cliente confirme que la recibió.', 'El proveedor marcó tu orden como entregada. Confirma que la recibiste cuando la tengas.'],
    received: [false, 'Orden entregada y recibida. Sus condiciones quedan guardadas.', 'Orden entregada y recibida. Sus condiciones quedan guardadas.'],
    cancelled: [false, 'El acuerdo se canceló. Sus condiciones y su actividad quedan guardadas.', 'El acuerdo se canceló. Sus condiciones y su actividad quedan guardadas.'],
  };
  const [supplierMove, supplierText, clientText] = steps[deal.status];
  if (deal.status === 'received' || deal.status === 'cancelled') return { who: 'done', text: supplierText };
  return { who: supplierMove === supplier ? 'you' : 'them', text: supplier ? supplierText : clientText };
}

export function NextStepNote({ deal, supplier, stockWarnings }: { deal: Deal; supplier: boolean; stockWarnings?: number }) {
  const { who, text } = nextStep(deal, supplier, stockWarnings), next = STAGES[reachedStage(deal) + 1];
  const lead = who === 'you' ? 'Te toca' : who === 'them' ? 'En espera' : deal.status === 'cancelled' ? 'Cancelada' : 'Listo';
  return <p className={`deal-next-step ${who} ${deal.status}`} role="note" aria-label="Siguiente paso">
    <strong>{lead}</strong>{who !== 'done' && next && <> · para pasar a <em>{next.label}</em></>}: {text}
  </p>;
}
