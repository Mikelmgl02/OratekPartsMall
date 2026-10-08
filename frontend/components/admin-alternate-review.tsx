'use client';

import { useEffect, useMemo, useState } from 'react';
import type { ColDef } from 'ag-grid-community';
import type { CustomCellRendererProps } from 'ag-grid-react';
import { ArrowLeft, Check, ClipboardCheck, LoaderCircle } from 'lucide-react';
import ClientGrid from './client-grid';
import { ApiError, request } from '@/lib/types';
import { AlternateReviewAction, AlternateReviewDetail, AlternateReviewList, AlternateReviewRow, NumberEvidence, ReviewAlterno, partNumberViaLabels,
  referenceStatusLabels } from '@/lib/oem-reference-types';

const base = '/api/management/alternate-review';
const message = (error: unknown) => error instanceof Error ? error.message : 'No se pudo completar la acción. Inténtalo de nuevo.';
type Cell = CustomCellRendererProps<AlternateReviewRow, unknown, { open: (id: string) => void }>;

function SkuCell({ data }: Cell) {
  return data ? <div className="admin-grid-stack"><strong>{data.part.sku}</strong><small>{data.part.description || 'SIN DESCRIPCIÓN'}</small></div> : null;
}
function OpenCell({ data, context }: Cell) {
  return data ? <div className="admin-grid-actions"><button className="button soft small" aria-label={`Revisar alternos de ${data.part.sku}`} onClick={() => context.open(data.part.id)}>Revisar</button></div> : null;
}
const columns: ColDef<AlternateReviewRow>[] = [
  { colId: 'sku', headerName: 'SKU', flex: 1, minWidth: 240, valueGetter: ({ data }) => data?.part.sku, cellRenderer: SkuCell, tooltipValueGetter: ({ data }) => data?.part.description },
  { colId: 'undecided', headerName: 'Por decidir', field: 'undecided', width: 130, type: 'rightAligned', cellClass: ['ag-right-aligned-cell', 'admin-grid-number'] },
  { colId: 'copies', headerName: 'Copias', field: 'copies', width: 110, type: 'rightAligned', cellClass: ['ag-right-aligned-cell'] },
  { colId: 'open', headerName: 'Acciones', width: 140, cellRenderer: OpenCell, sortable: false, resizable: false },
];

// Catalog alternos no OEM number of their SKU carries: the copy may come from an item matched through a number the catalog prints
// for several parts. A person keeps or removes each one with the evidence in view.
export default function AlternateReview({ onCount, onChanged }: { onCount: (count: number) => void; onChanged: () => void }) {
  const [open, setOpen] = useState<string | null>(null);
  const [rows, setRows] = useState<AlternateReviewRow[] | null>(null); const [error, setError] = useState(''); const [revision, setRevision] = useState(0);
  useEffect(() => {
    if (open) return;
    let live = true; setError('');
    request<AlternateReviewList>(base).then(result => { if (live) { setRows(result.results); onCount(result.count); } }).catch(e => { if (live) setError(message(e)); });
    return () => { live = false; };
  }, [open, revision, onCount]);
  const context = useMemo(() => ({ open: (id: string) => setOpen(id) }), []);
  if (open) return <ReviewSku id={open} onBack={() => setOpen(null)} onChanged={onChanged}/>;
  return <div className="alt-review">
    <p className="modal-description">SKU con alternos que copió un catálogo de repuesto y que ninguno de sus números OEM lleva. La copia pudo venir de un artículo vinculado por un número que el catálogo imprime para varias piezas. Compara las aplicaciones y decide cada código.</p>
    {error && <div className="notice error" role="alert">{error}<button onClick={() => setRevision(value => value + 1)}>Intentar de nuevo</button></div>}
    {!rows && !error && <div className="loading"><LoaderCircle className="spin" size={18}/>Cargando la revisión…</div>}
    {rows && <ClientGrid<AlternateReviewRow> label="SKU por revisar" rows={rows} columns={columns} rowId={row => row.part.id} rowHeight={58} maxRows={14} context={context}
      empty={<div className="empty-state"><ClipboardCheck size={30}/><h3>No hay alternos de catálogo por revisar.</h3><p>Cada alterno que copió un catálogo ya llega por un número OEM de su SKU o quedó confirmado.</p></div>}/>}
  </div>;
}

function Evidence({ items, total }: { items: NumberEvidence[]; total: number }) {
  return <ul className="alt-review-filed">{items.map(item => <li key={item.reference.id}><strong>{item.reference.manufacturer} {item.reference.code}</strong>{(item.applications || item.description) && <span>{item.applications || item.description}</span>}</li>)}
    {total > items.length && <li className="more">Y {total - items.length} {total - items.length === 1 ? 'número más' : 'números más'}</li>}</ul>;
}

function ReviewSku({ id, onBack, onChanged }: { id: string; onBack: () => void; onChanged: () => void }) {
  const [data, setData] = useState<AlternateReviewDetail | null>(null); const [error, setError] = useState(''); const [retry, setRetry] = useState(0);
  const [busy, setBusy] = useState(''); const [confirm, setConfirm] = useState(''); const [notice, setNotice] = useState('');
  useEffect(() => {
    let live = true; setError('');
    request<AlternateReviewDetail>(`${base}/${id}`).then(result => { if (live) setData(result); }).catch(e => { if (live) setError(message(e)); });
    return () => { live = false; };
  }, [id, retry]);
  async function decide(action: AlternateReviewAction, code?: ReviewAlterno) {
    const key = code ? `${action}:${code.id}` : action;
    setBusy(key); setError(''); setNotice(''); setConfirm('');
    try {
      setData(await request<AlternateReviewDetail>(`${base}/${id}`, { method: 'POST', body: JSON.stringify({ action, ...(code ? { alternate: code.id } : {}) }) }));
      setNotice(action === 'keep' ? `${code!.brand} ${code!.code} confirmado como alterno del SKU.` : action === 'remove' ? `${code!.brand} ${code!.code} retirado del SKU.` : 'Copias retiradas: los clientes siguen viendo esos códigos por número OEM.');
      onChanged();
    } catch (e) {
      const current = e instanceof ApiError && e.status === 409 ? (e.body as { review?: AlternateReviewDetail } | undefined)?.review : undefined;
      if (current) setData(current);
      setError(message(e));
    } finally { setBusy(''); }
  }
  const part = data?.part;
  const done = data && !data.undecided.length && !data.copies.length;
  return <div className="suffix-editor alt-review-detail">
    <button className="button text small" onClick={onBack}><ArrowLeft size={15}/>Volver a la lista</button>
    {error && <div className="notice error" role="alert">{error}{!data && <button onClick={() => setRetry(value => value + 1)}>Intentar de nuevo</button>}</div>}
    {notice && <div className="notice success" role="status"><Check size={16}/>{notice}</div>}
    {!data && !error && <div className="loading"><LoaderCircle className="spin" size={18}/>Cargando el SKU…</div>}
    {data && part && <>
      <div className="suffix-editor-heading"><h3>{part.sku}</h3>{part.is_OEM && <span className="pill">MAIN OEM</span>}{!part.active && <span className="pill">INACTIVO</span>}</div>
      <p className="alt-review-description">{part.description || 'SIN DESCRIPCIÓN'}<small>{[part.category, part.subcategory].filter(Boolean).join(' / ') || 'SIN CLASIFICAR'}</small></p>
      <section className="suffix-editor-section" aria-label="Números OEM del SKU"><h4>Números OEM del SKU ({data.numbers.length})</h4>
        {data.numbers.length ? <ul className="alt-review-numbers">{data.numbers.map(entry => <li key={entry.reference.id}>
          <strong>{entry.reference.manufacturer} {entry.reference.code}</strong>
          <span>{entry.via.map(via => partNumberViaLabels[via]).join(' · ')} · {referenceStatusLabels[entry.reference.status]}</span>
          {(entry.applications || entry.description) && <small>{entry.applications || entry.description}</small>}
          {entry.printed_for.map(printed => <small key={printed.catalog} className={printed.brand_codes.length > 1 ? 'shared' : ''}>{printed.catalog} lo imprime para {printed.brand_codes.join(', ') || 'ningún artículo propio'}{printed.brand_codes.length > 1 ? ' · VARIAS PIEZAS' : ''}</small>)}
        </li>)}</ul> : <p>Este SKU no llega a ningún número OEM.</p>}
      </section>
      <section className="suffix-editor-section" aria-label="Códigos por decidir"><h4>Códigos por decidir ({data.undecided.length})</h4>
        {data.undecided.length ? <ul className="alt-review-codes">{data.undecided.map(code => <li key={code.id}>
          <div><strong>{code.brand} {code.code}</strong><small>{code.reference_source}</small>
            {code.filed_count ? <><small>Un catálogo lo archiva en estos números, que el SKU no alcanza:</small><Evidence items={code.filed_under} total={code.filed_count}/></> : <small>Ningún número OEM lleva este código.</small>}</div>
          <div className="alt-review-actions">{confirm === `remove:${code.id}` ? <>
            <button className="button soft small danger-text" disabled={!!busy} onClick={() => void decide('remove', code)}>Confirmar retiro</button>
            <button className="button text small" onClick={() => setConfirm('')}>Cancelar</button>
          </> : <>
            <button className="button soft small" disabled={!!busy} aria-label={`Conservar ${code.brand} ${code.code}`} onClick={() => void decide('keep', code)}>{busy === `keep:${code.id}` ? 'Guardando…' : 'Conservar'}</button>
            <button className="button text small danger-text" disabled={!!busy} aria-label={`Retirar ${code.brand} ${code.code}`} onClick={() => setConfirm(`remove:${code.id}`)}>Retirar</button>
          </>}</div>
        </li>)}</ul> : <p>No quedan códigos por decidir.</p>}
      </section>
      <section className="suffix-editor-section" aria-label="Copias"><h4>Copias que ya llegan por número OEM ({data.copies.length})</h4>
        {data.copies.length ? <>
          <ul className="part-oem-codes">{data.copies.map(code => <li key={code.id} title={code.reference_source}>{code.brand} {code.code}</li>)}</ul>
          <div className="alt-review-actions">{confirm === 'copies' ? <>
            <button className="button soft small danger-text" disabled={!!busy} onClick={() => void decide('remove_copies')}>Confirmar retiro de {data.copies.length} {data.copies.length === 1 ? 'copia' : 'copias'}</button>
            <button className="button text small" onClick={() => setConfirm('')}>Cancelar</button>
          </> : <button className="button soft small" disabled={!!busy} onClick={() => setConfirm('copies')}>Retirar {data.copies.length === 1 ? 'la copia' : `las ${data.copies.length} copias`}</button>}</div>
          <p>Los clientes siguen viendo estos códigos por los números OEM del SKU.</p>
        </> : <p>Sin copias.</p>}
      </section>
      {done && <p className="notice success alt-review-done"><Check size={16}/>Revisión completa: este SKU ya no tiene alternos de catálogo por revisar.</p>}
    </>}
  </div>;
}
