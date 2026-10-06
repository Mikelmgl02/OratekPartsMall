'use client';
import { Bot, Calculator, Check, ClipboardCopy, LoaderCircle, Sparkles, X } from 'lucide-react';
import type { AssistantProposal, AssistantState } from '@/lib/pricing-types';

const termsLabels: Record<string, string> = { entrega: 'Entrega', retiro: 'Retiro', factura: 'Factura', otro: 'Otra condición' };
const priceAsks: Record<string, string> = { descuento: 'un descuento', igualar_precio: 'igualar un precio', pregunta_precio: 'información de precio' };
const units = (quantity: number | null) => `${quantity} ${quantity === 1 ? 'unidad' : 'unidades'}`;

function label(proposal: AssistantProposal) {
  switch (proposal.kind) {
    case 'quantity_change': return `${proposal.codigo}: ofrecer ${units(proposal.quantity)}`;
    case 'remove_line': return `${proposal.codigo}: no ofrecer este artículo (0 unidades)`;
    case 'line_note': return `${proposal.codigo}: nota interna «${proposal.text}»`;
    case 'terms': return `${termsLabels[proposal.detail] || 'Condición'}: ${proposal.text}`;
    case 'price_request': return `El cliente pide ${priceAsks[proposal.detail] || 'información de precio'}${proposal.codigo ? ` (${proposal.codigo})` : ''}`;
    case 'unmatched': return `Fuera de la orden${proposal.text ? `: ${proposal.text}` : ''}`;
    default: return proposal.text;
  }
}

// "Asistente de cotización (IA)", supplier-only: what the client asked for, each proposal quoting the client's own words. Applying is a draft
// save the editor performs; nothing changes until the supplier clicks, and no proposal ever carries a price.
export default function QuoteAssistantPanel({ state, busy, running, pending, error, simulatorHref, onRun, onDecide, onCopy }: {
  state: AssistantState; busy: boolean; running: boolean; pending: string; error: string; simulatorHref?: string;
  onRun: () => void; onDecide: (proposal: AssistantProposal, action: 'apply' | 'dismiss') => void; onCopy?: (text: string) => void;
}) {
  const run = state.latest_run, proposals = run?.proposals || [];
  const changes = proposals.filter(item => item.can_apply), prices = proposals.filter(item => item.kind === 'price_request');
  const outside = proposals.filter(item => item.kind === 'unmatched'), questions = proposals.filter(item => item.kind === 'question');
  const item = (proposal: AssistantProposal, extra?: React.ReactNode) => {
    const text = label(proposal);
    return <li key={proposal.id} className={`quote-assistant-item${proposal.decision ? ` ${proposal.decision}` : ''}`}>
      <div><strong>{text}</strong>{proposal.evidence && <span className="quote-assistant-evidence">«{proposal.evidence}»</span>}{extra}</div>
      {proposal.decision ? <span className="quote-assistant-decision">{proposal.decision === 'applied' ? 'Aplicada' : 'Descartada'}</span>
        : <span className="quote-assistant-actions">
          {proposal.can_apply && <button type="button" className="button primary small" disabled={busy} aria-label={`Aplicar: ${text}`} onClick={() => onDecide(proposal, 'apply')}>
            {pending === proposal.id ? <LoaderCircle size={13} className="spin"/> : <Check size={13}/>}Aplicar</button>}
          <button type="button" className="button soft small" disabled={busy} aria-label={`Descartar: ${text}`} onClick={() => onDecide(proposal, 'dismiss')}><X size={13}/>Descartar</button>
        </span>}
    </li>;
  };
  return <section className="quote-assistant" aria-label="Asistente de cotización (IA)">
    <div className="quote-assistant-heading"><h4><Bot size={16}/>Asistente de cotización (IA)</h4>
      <button type="button" className="button soft small" disabled={busy || running || !!state.unavailable || state.runs_left < 1} onClick={onRun}>
        {running ? <LoaderCircle size={14} className="spin"/> : <Sparkles size={14}/>}{running ? 'Interpretando…' : run ? 'Volver a interpretar' : 'Interpretar solicitud del cliente'}</button></div>
    <p className="quote-assistant-note">El asistente no propone precios: los precios salen solo de tus listas y reglas. Lee las notas de la orden, el motivo del ajuste y la conversación desde la última versión, y nada cambia hasta que aplicas una propuesta.</p>
    {state.unavailable ? <p className="quote-assistant-muted" role="status">{state.unavailable}</p>
      : state.runs_left < 1 && <p className="quote-assistant-muted" role="status">Ya usaste las interpretaciones de esta versión.</p>}
    {error && <div className="notice error" role="alert">{error}</div>}
    {run && <>
      {run.stale && <p className="quote-assistant-stale" role="status">Hay mensajes nuevos desde esta interpretación. Vuelve a interpretar para incluirlos.</p>}
      {run.summary && <p className="quote-assistant-summary">{run.summary}</p>}
      {changes.length > 0 && <ul className="quote-assistant-list" aria-label="Propuestas del asistente">{changes.map(proposal => item(proposal))}</ul>}
      {prices.length > 0 && <ul className="quote-assistant-list" aria-label="Solicitudes de precio del cliente">{prices.map(proposal => item(proposal,
        simulatorHref && <a className="quote-assistant-link" href={simulatorHref} target="_blank" rel="noopener"><Calculator size={12}/>Abrir simulador</a>))}</ul>}
      {outside.length > 0 && <ul className="quote-assistant-list" aria-label="Lo que el cliente menciona fuera de la orden">{outside.map(proposal => item(proposal))}</ul>}
      {questions.length > 0 && <div className="quote-assistant-questions"><h5>Preguntas sugeridas para el cliente</h5>
        <ul aria-label="Preguntas sugeridas para el cliente">{questions.map(proposal => <li key={proposal.id} className={proposal.decision ? 'dismissed' : ''}><span>{proposal.text}</span>
          {onCopy && <button type="button" className="button text small" aria-label={`Copiar a la conversación: ${proposal.text}`} onClick={() => onCopy(proposal.text)}><ClipboardCopy size={13}/>Copiar a la conversación</button>}</li>)}</ul>
        <small>Se copia al mensaje de la conversación sin enviarlo; revísalo antes de enviar.</small></div>}
      {!proposals.length && <p className="quote-assistant-muted">El asistente no encontró nada que proponer en lo que escribió el cliente.</p>}
      <p className="form-footnote">Interpretado por {run.created_by.name}{run.cached ? ' · misma solicitud, sin costo' : ''}{run.rejected_items ? ` · ${run.rejected_items} ${run.rejected_items === 1 ? 'propuesta descartada' : 'propuestas descartadas'} por mencionar importes` : ''} · {state.runs_left === 1 ? 'Queda 1 interpretación' : `Quedan ${state.runs_left} interpretaciones`} para esta versión.</p>
    </>}
  </section>;
}
