'use client';

import { useCallback, useEffect, useState } from 'react';
import { CircleX, LoaderCircle, Search, TriangleAlert, X } from 'lucide-react';
import Modal from './modal';
import DealStatusBadge from './deal-status';
import { UppercaseInput, UppercaseTextarea } from './uppercase-field';
import { Page, request } from '@/lib/types';
import { OPEN_AGREEMENT, cancelSides, type DealCancelSide, type DealStatus } from '@/lib/deal-types';

type DealRow = { id: string; reference: string; status: DealStatus; version: number; client: { id: string; name: string }; supplier: { id: string; name: string };
  created_at: string; handshaked_at: string | null; processing_at: string | null; in_transit_at: string | null; delivered_at: string | null; received_at: string | null;
  cancelled_at: string | null; cancelled_side: DealCancelSide | null;
  cancel_reason_label: string | null; line_count: number; unit_count: number; shortfall_count: number; shortfall_pending: boolean };
type DealDetail = DealRow & { cancel_note: string;
  lines: { id: string; codigo: string; sku: string; description: string; requested: number; agreed: number | null; missing: number; pending_missing: number; final: number | null }[];
  shortfalls: { id: string; status: 'pending' | 'accepted' | 'declined'; reason_label: string; note: string; reported_at: string; responded_at: string | null;
    lines: { codigo: string; agreed: number; missing: number }[] }[];
  events: { id: number; kind: string; created_at: string; account_name: string; description: string }[] };

const date = (value: string | null) => value ? new Date(value).toLocaleString('es-PA', { timeZone: 'America/Panama', dateStyle: 'medium', timeStyle: 'short' }) : '—';
// Abiertos: confirmed and not received or cancelled (what MotionPartes can still cancel); Por recibir: marked Entregado, waiting for the client.
const filters: [string, string][] = [['agreements', 'Acuerdos confirmados'], ['open_agreements', 'Abiertos'], ['delivered', 'Por recibir'], ['received', 'Recibidos'], ['cancelled', 'Cancelados'], ['', 'Todas las órdenes']];
const shortfallStates = { pending: 'Esperando al cliente', accepted: 'Aceptado por el cliente', declined: 'El acuerdo se canceló' };
// The reasons MotionPartes cancels for (codes of mall SupplierRequest.CANCEL_REASONS).
const adminReasons: [string, string][] = [['dispute', 'Disputa entre las partes'], ['no_stock', 'Sin existencias'], ['cannot_deliver', 'El proveedor no puede entregar'],
  ['not_needed', 'El cliente ya no lo necesita'], ['other', 'Otro motivo']];

// Acuerdos (superusers): confirmed deals with what happened after the handshake, to settle disputes. Units only, never a price (D5).
export default function AdminDeals() {
  const [status, setStatus] = useState('agreements');
  const [search, setSearch] = useState('');
  const [applied, setApplied] = useState('');
  const [page, setPage] = useState(1);
  const [data, setData] = useState<Page<DealRow> | null>(null);
  const [error, setError] = useState('');
  const [selected, setSelected] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);
  useEffect(() => { if (search.trim() === applied) return; const timer = setTimeout(() => { setApplied(search.trim()); setPage(1); }, 350); return () => clearTimeout(timer); }, [search, applied]);
  useEffect(() => {
    let live = true;
    const query = new URLSearchParams({ page: String(page), ...(status ? { status } : {}), ...(applied ? { search: applied } : {}) });
    request<Page<DealRow>>(`/api/management/deals?${query}`).then(value => { if (live) { setData(value); setError(''); } })
      .catch(reason => { if (live) setError((reason as Error).message); });
    return () => { live = false; };
  }, [status, applied, page, revision]);

  return <section className="inventory-panel admin-panel admin-deals" aria-label="Acuerdos">
    <div className="admin-toolbar"><div><h2>Acuerdos</h2><p>Los acuerdos confirmados por ambas partes y lo que pasó después: el avance que marca el proveedor (Procesando, En camino, Entregado), el recibido del cliente, faltantes y cancelaciones. Solo unidades, sin precios. Cancelar como MotionPartes resuelve una disputa y no cuenta contra el proveedor.</p></div></div>
    <div className="supplier-requests-controls"><div className="supplier-requests-search"><Search size={19}/><UppercaseInput aria-label="Buscar acuerdos" value={search} maxLength={200} placeholder="REFERENCIA, CLIENTE O PROVEEDOR…" onChange={event => setSearch(event.target.value)}/>
      {search && <button type="button" className="icon-button" aria-label="Limpiar búsqueda de acuerdos" onClick={() => setSearch('')}><X size={16}/></button>}</div>
      <div className="supplier-request-filters" role="group" aria-label="Estado de los acuerdos">{filters.map(([value, label]) => <button type="button" key={value || 'all'} aria-pressed={status === value} className={status === value ? 'selected' : ''} onClick={() => { setStatus(value); setPage(1); }}>{label}</button>)}</div></div>
    {error && <div className="notice error" role="alert">{error}</div>}
    {!data && !error && <div className="loading" role="status"><LoaderCircle className="spin" size={18}/>Cargando acuerdos…</div>}
    {data && (data.results.length ? <div className="table-scroll admin-deals-table"><table><thead><tr><th>Orden</th><th>Cliente</th><th>Proveedor</th><th>Estado</th><th>Confirmado</th><th>Después del acuerdo</th><th/></tr></thead>
      <tbody>{data.results.map(row => <tr key={row.id}><td><strong>{row.reference}</strong><small>{row.line_count} artículos · {row.unit_count.toLocaleString('es-PA')} unidades</small></td><td>{row.client.name}</td><td>{row.supplier.name}</td>
        <td><DealStatusBadge status={row.status}/></td><td>{date(row.handshaked_at)}</td>
        <td>{row.shortfall_pending ? <span className="supplier-request-alert shortfall"><TriangleAlert size={13}/>Faltante por responder</span>
          : row.cancelled_side ? <span>Cancelado por {cancelSides[row.cancelled_side]}{row.cancel_reason_label ? ` · ${row.cancel_reason_label}` : ''}</span>
          : row.status === 'received' ? <span>Recibido {date(row.received_at)}</span>
          : row.status === 'delivered' ? <span>Entregado {date(row.delivered_at)} · esperando al cliente</span>
          : row.status === 'in_transit' ? <span>En camino desde {date(row.in_transit_at)}</span>
          : row.status === 'processing' ? <span>Procesando desde {date(row.processing_at)}</span>
          : row.shortfall_count ? <span>{row.shortfall_count} {row.shortfall_count === 1 ? 'faltante' : 'faltantes'}</span> : '—'}</td>
        <td><button type="button" className="button soft small" aria-label={`Ver acuerdo ${row.reference}`} onClick={() => setSelected(row.id)}>Ver</button></td></tr>)}</tbody></table></div>
      : <p className="empty-state compact">No hay acuerdos para estos filtros.</p>)}
    {data && (data.next || data.previous) && <div className="pagination"><button className="button soft small" disabled={!data.previous} onClick={() => setPage(page - 1)}>Anterior</button><span>Página {page}</span><button className="button soft small" disabled={!data.next} onClick={() => setPage(page + 1)}>Siguiente</button></div>}
    {selected && <DealPanel id={selected} onClose={() => setSelected(null)} onChanged={() => setRevision(value => value + 1)}/>}
  </section>;
}

function DealPanel({ id, onClose, onChanged }: { id: string; onClose: () => void; onChanged: () => void }) {
  const [deal, setDeal] = useState<DealDetail | null>(null);
  const [error, setError] = useState('');
  const [cancelling, setCancelling] = useState(false);
  const [reason, setReason] = useState('');
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  // After a refused cancel the panel reloads the deal and keeps the refusal on screen (keepError).
  const load = useCallback((keepError = false) => request<DealDetail>(`/api/management/deals/${id}`).then(value => { setDeal(value); if (!keepError) setError(''); })
    .catch(reason => setError((reason as Error).message)), [id]);
  useEffect(() => { void load(); }, [load]);
  async function cancel() {
    if (!deal || busy || !reason) return;
    setBusy(true); setError('');
    try {
      setDeal(await request<DealDetail>(`/api/management/deals/${id}/cancel`, { method: 'POST', body: JSON.stringify({ expected_version: deal.version, reason_code: reason, note }) }));
      setCancelling(false); onChanged();
    } catch (reason) { setError((reason as Error).message); setCancelling(false); void load(true); }
    finally { setBusy(false); }
  }
  return <Modal title={deal ? `Acuerdo ${deal.reference}` : 'Acuerdo'} drawer wide className="side-panel" onClose={() => { if (!busy) onClose(); }}>
    {error && <div className="notice error" role="alert">{error}</div>}
    {!deal && !error && <div className="loading" role="status"><LoaderCircle className="spin" size={18}/>Cargando acuerdo…</div>}
    {deal && <div className="admin-deal-detail">
      <div className="admin-deal-heading"><div><strong>{deal.client.name}</strong><span>compra a</span><strong>{deal.supplier.name}</strong></div><DealStatusBadge status={deal.status}/></div>
      <dl className="admin-deal-dates"><div><dt>Enviada</dt><dd>{date(deal.created_at)}</dd></div><div><dt>Confirmada</dt><dd>{date(deal.handshaked_at)}</dd></div>
        <div><dt>Procesando</dt><dd>{date(deal.processing_at)}</dd></div><div><dt>En camino</dt><dd>{date(deal.in_transit_at)}</dd></div>
        <div><dt>Entregada (proveedor)</dt><dd>{date(deal.delivered_at)}</dd></div><div><dt>Recibida (cliente)</dt><dd>{date(deal.received_at)}</dd></div><div><dt>Cancelada</dt><dd>{deal.cancelled_side ? `${date(deal.cancelled_at)} · ${cancelSides[deal.cancelled_side]}` : '—'}</dd></div></dl>
      {deal.cancelled_side && <p className="notice">{deal.cancel_reason_label}{deal.cancel_note ? ` · ${deal.cancel_note}` : ''}</p>}
      <div className="table-scroll"><table><thead><tr><th>Artículo</th><th>Solicitadas</th><th>Acordadas</th><th>Faltantes</th><th>Quedan</th></tr></thead><tbody>{deal.lines.map(line => <tr key={line.id}>
        <td><strong>{line.codigo}</strong><small>{line.description}</small></td><td>{line.requested}</td><td>{line.agreed ?? '—'}</td>
        <td>{line.missing || line.pending_missing ? `${line.missing}${line.pending_missing ? ` (+${line.pending_missing} por responder)` : ''}` : '—'}</td><td>{line.final ?? '—'}</td></tr>)}</tbody></table></div>
      {!!deal.shortfalls.length && <section className="admin-deal-shortfalls" aria-label="Faltantes"><h4>Faltantes</h4>{deal.shortfalls.map(item => <article key={item.id}>
        <strong>{shortfallStates[item.status]}</strong><span>{item.reason_label} · {date(item.reported_at)}{item.note ? ` · ${item.note}` : ''}</span>
        <small>{item.lines.map(line => `${line.codigo}: faltan ${line.missing} de ${line.agreed}`).join(' · ')}</small></article>)}</section>}
      <section aria-label="Actividad del acuerdo"><h4>Actividad</h4><ol className="deal-events">{deal.events.map(event => <li key={event.id}><span>{date(event.created_at)} · {event.account_name}</span><p>{event.description}</p></li>)}</ol></section>
      {OPEN_AGREEMENT.includes(deal.status) && (cancelling ? <form className="deal-after-form cancel" aria-label="Cancelar como MotionPartes" onSubmit={event => { event.preventDefault(); void cancel(); }}>
        {/* After dispatch the goods may be gone: only the count comes back, and what apiag-cloud already discounted stays discounted. */}
        <p>{['in_transit', 'delivered'].includes(deal.status) ? 'La orden ya salió del proveedor. Las unidades que el acuerdo aún reserva vuelven a estar disponibles (el proveedor debe corregir su inventario si no regresaron); lo descontado por apiag-cloud no cambia. No cuenta contra el proveedor.'
          : 'Cancelas el acuerdo como MotionPartes: las unidades reservadas vuelven al proveedor y la cancelación no cuenta contra él.'} Ambas partes verán el motivo.</p>
        <label>Motivo<select required aria-label="Motivo de la cancelación" value={reason} disabled={busy} onChange={event => setReason(event.target.value)}><option value="">Elige un motivo</option>{adminReasons.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
        <label>Nota para las partes (opcional)<UppercaseTextarea maxLength={2000} value={note} disabled={busy} onChange={event => setNote(event.target.value)}/></label>
        <div><button type="submit" className="button primary danger" disabled={busy || !reason}>{busy ? <LoaderCircle size={15} className="spin"/> : <CircleX size={15}/>}Cancelar el acuerdo</button>
          <button type="button" className="button soft" disabled={busy} onClick={() => setCancelling(false)}>Volver</button></div></form>
        : <button type="button" className="button soft danger" onClick={() => setCancelling(true)}><CircleX size={15}/>Cancelar como MotionPartes</button>)}
    </div>}
  </Modal>;
}
