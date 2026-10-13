'use client';
import { useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { type DealStatus, dealStatusLabels } from '@/lib/deal-types';
import type { SupplierRequestSummary } from '@/lib/request-types';

// One of the conversation's orders, as the Cotizaciones table lists it (GET accounts/<id>/threads/<id>/orders, mall/threads.py).
export type ConversationDocument = Omit<SupplierRequestSummary, 'client'>;

// Short enough for a narrow column; the whole status is in the cell's tooltip.
const SHORT: Record<DealStatus, string> = { pending: 'Enviada', reviewed: 'En revisión', quoted: 'Cotizada', adjustment: 'Ajuste',
  handshaked: 'Acuerdo', delivered: 'Entregada', cancelled: 'Cancelada' };
const CLOSED: DealStatus[] = ['delivered', 'cancelled'];
const FILTERS = [['all', 'Todas'], ['open', 'En curso'], ['closed', 'Cerradas']] as const;
type Filter = typeof FILTERS[number][0];
const day = (value: string) => new Date(value).toLocaleDateString('es-PA', { timeZone: 'America/Panama', day: 'numeric', month: 'short', year: 'numeric' });
const money = (value: string | number, currency = 'USD') => new Intl.NumberFormat('es-PA', { style: 'currency', currency }).format(Number(value));

// The conversations panel's right column: every order of this client and supplier with its current quotation, latest first. Choosing one
// opens its document (its items, its quotation, its activity) in the same column; current is the one the chat writes about.
export default function ConversationDocuments({ docs, failed, current, supplier, counterpart, hidden, onOpen, onRetry }: {
  docs: ConversationDocument[] | null; failed: boolean; current: string; supplier: boolean; counterpart: string; hidden?: boolean;
  onOpen: (doc: ConversationDocument) => void; onRetry: () => void;
}) {
  const [filter, setFilter] = useState<Filter>('all');
  const count = (value: Filter) => (docs || []).filter(doc => value === 'all' || CLOSED.includes(doc.status) === (value === 'closed')).length;
  const shown = (docs || []).filter(doc => filter === 'all' || CLOSED.includes(doc.status) === (filter === 'closed'));
  return <section className="inbox-docs" aria-label="Cotizaciones" hidden={hidden}>
    <div className="inbox-docs-head">
      <h3>Cotizaciones</h3>
      {docs && docs.length > 1 && <div className="inbox-docs-filter" role="group" aria-label="Filtrar cotizaciones">
        {FILTERS.map(([value, label]) => <button type="button" key={value} aria-pressed={filter === value} onClick={() => setFilter(value)}>{label} · {count(value)}</button>)}
      </div>}
      {!!docs?.length && <p>Elige una para ver sus artículos, su cotización y su actividad.</p>}
    </div>
    {failed && !docs ? <div className="notice error" role="alert"><span>No se pudieron cargar las cotizaciones.</span><button type="button" onClick={onRetry}><RefreshCw size={14}/>Reintentar</button></div>
      : !docs ? <div className="inbox-docs-loading" role="status"><span className="sr-only">Cargando cotizaciones…</span><div className="skeleton-block"/><div className="skeleton-block"/></div>
      : !docs.length ? <p className="inbox-docs-empty">{supplier ? `${counterpart} aún no te ha enviado órdenes.` : `Aún no tienes órdenes con ${counterpart}. Envíale tu cesta para pedirle una cotización.`}</p>
      : <table className="inbox-docs-table" aria-label={`Cotizaciones con ${counterpart}`}>
        <thead><tr><th scope="col">Documento</th><th scope="col">Estado</th><th scope="col" className="num items">Artículos</th><th scope="col" className="num">Total</th></tr></thead>
        <tbody>{shown.length ? shown.map(doc => <tr key={doc.id} className={doc.id === current ? 'current' : undefined} aria-current={doc.id === current ? 'true' : undefined} onClick={() => onOpen(doc)}>
          <td><button type="button" className="inbox-doc-open" aria-label={`Abrir ${doc.reference}`} onClick={event => { event.stopPropagation(); onOpen(doc); }}>{doc.reference}</button><small>{day(doc.created_at)}</small></td>
          <td><span className="inbox-doc-status" title={dealStatusLabels[doc.status]}><i className={`inbox-row-dot ${doc.status}`} aria-hidden="true"/>{SHORT[doc.status]}</span>
            {doc.shortfall_pending && <small>Faltante por decidir</small>}</td>
          <td className="num items">{doc.line_count}<small>{doc.unit_count.toLocaleString('es-PA')} u.</small></td>
          <td className="num">{doc.quotation_total ? <>{money(doc.quotation_total, doc.quotation_currency || 'USD')}<small>v{doc.quotation_revision}</small></> : <small>Sin cotizar</small>}</td>
        </tr>) : <tr className="empty"><td colSpan={4}>{filter === 'open' ? 'No hay cotizaciones en curso.' : 'No hay cotizaciones cerradas.'}</td></tr>}</tbody>
      </table>}
  </section>;
}
