'use client';
import { useCallback, useEffect, useState } from 'react';
import { LoaderCircle, PackageSearch, ShoppingBag } from 'lucide-react';
import Modal from './modal';
import { ApiError, request } from '@/lib/types';
import { cestaChanged } from '@/lib/cesta';

// Pedir lo que falta a otro proveedor (mall/reorder.py): each item the order will not bring, with the other suppliers that have its SKU in
// stock (in_stock only, as the catalog shows them), one of them suggested. What the person keeps goes into the account's cesta, from where
// the solicitud is sent as usual.
type MissingOffer = { supplier_id: string; supplier_name: string; known: boolean; items: { id: string; codigo: string; brand: string; description: string }[] };
type MissingLine = { order_line_id: string; part_id: string; sku: string; codigo: string; brand: string; description: string; requested: number; missing: number;
  in_basket: number; suggested: string | null; offers: MissingOffer[] };
type Missing = { order: { id: string; reference: string; status: string; supplier_name: string }; lines: MissingLine[]; added_at: string | null };
type Choice = { include: boolean; item: string; quantity: string };

const date = (value: string) => new Date(value).toLocaleString('es-PA', { timeZone: 'America/Panama', dateStyle: 'medium', timeStyle: 'short' });
const units = (count: number) => `${count} ${count === 1 ? 'unidad' : 'unidades'}`;
const failure = (error: unknown) => error instanceof TypeError ? 'Se perdió la conexión. Inténtalo de nuevo.' : error instanceof Error ? error.message : 'No se pudo completar.';
const valid = (value: string) => /^\d+$/.test(value) && Number(value) >= 1 && Number(value) <= 9999;

// Each line starts chosen when another supplier has it and the cesta does not hold the missing units of its SKU already.
function choicesFrom(data: Missing): Record<string, Choice> {
  return Object.fromEntries(data.lines.map(line => [line.order_line_id, { include: !!line.suggested && line.in_basket < line.missing,
    item: line.suggested ?? '', quantity: String(line.missing) }]));
}

export default function ReorderMissing({ accountId, orderId, reference, onClose }: { accountId: string; orderId: string; reference: string; onClose: () => void }) {
  const path = `/api/market/accounts/${accountId}/sent-requests/${orderId}/missing`;
  const [data, setData] = useState<Missing | null>(null);
  const [choices, setChoices] = useState<Record<string, Choice>>({});
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [added, setAdded] = useState<number | null>(null);
  const load = useCallback(async () => {
    try { const value = await request<Missing>(path); setData(value); setChoices(choicesFrom(value)); setError(''); }
    catch (caught) { setError(failure(caught)); }
  }, [path]);
  useEffect(() => { void load(); }, [load]);
  const chosen = (data?.lines || []).filter(line => choices[line.order_line_id]?.include && choices[line.order_line_id].item);
  const ready = chosen.length > 0 && chosen.every(line => valid(choices[line.order_line_id].quantity));
  const change = (id: string, next: Partial<Choice>) => setChoices(current => ({ ...current, [id]: { ...current[id], ...next } }));
  async function submit() {
    if (!ready || busy) return;
    setBusy(true); setError('');
    try {
      await request(path, { method: 'POST', body: JSON.stringify({ lines: chosen.map(line => ({ order_line_id: line.order_line_id,
        supplier_item_id: choices[line.order_line_id].item, quantity: Number(choices[line.order_line_id].quantity) })) }) });
      setAdded(chosen.length);
      cestaChanged(accountId);  // the header's cesta counts them now
    } catch (caught) {
      setError(failure(caught));
      if (caught instanceof ApiError && caught.status === 409) void load();  // a supplier ran out meanwhile: the list as it is now
    } finally { setBusy(false); }
  }
  const total = (data?.lines || []).reduce((sum, line) => sum + line.missing, 0);
  return <Modal wide title={`Pedir lo que falta · ${reference}`} className="reorder-modal" onClose={onClose}>
    {added !== null ? <div className="reorder-done">
      <div className="notice success" role="status">{added === 1 ? 'Se agregó 1 artículo a tu cesta.' : `Se agregaron ${added} artículos a tu cesta.`} Desde ahí envías la solicitud a cada proveedor.</div>
      <div className="reorder-actions"><button type="button" className="button primary" onClick={() => { onClose(); cestaChanged(accountId, true); }}><ShoppingBag size={16}/>Ver cesta</button>
        <button type="button" className="button soft" onClick={onClose}>Cerrar</button></div>
    </div> : !data ? error ? <div className="notice error" role="alert"><span>{error}</span><button type="button" onClick={() => void load()}>Reintentar</button></div>
      : <div className="loading" role="status"><LoaderCircle className="spin"/>Buscando otros proveedores…</div>
    : !data.lines.length ? <p className="reorder-intro">A esta orden ya no le falta nada.</p> : <>
      <p className="reorder-intro">{data.order.status === 'cancelled' ? `La orden se canceló: le faltan ${units(total)}.` : `${data.order.supplier_name} no traerá ${units(total)} de esta orden.`} Elige a qué proveedor
        pedírselas: se agregan a tu cesta y desde ahí envías la solicitud. Solo aparecen proveedores con existencias.</p>
      {data.added_at && <p className="notice reorder-again">Ya agregaste lo que faltaba de esta orden a tu cesta el {date(data.added_at)}. Si lo agregas otra vez, se suman las unidades.</p>}
      {error && <div className="notice error" role="alert">{error}</div>}
      <ul className="reorder-lines" aria-label="Lo que falta">{data.lines.map(line => {
        const choice = choices[line.order_line_id], none = !line.offers.length;
        return <li key={line.order_line_id} className={none ? 'none' : choice?.include ? 'chosen' : undefined}>
          <label className="reorder-item"><input type="checkbox" checked={!!choice?.include} disabled={none || busy} aria-label={`Pedir ${line.codigo}`}
            onChange={event => change(line.order_line_id, { include: event.target.checked })}/>
            <span><strong>{line.codigo}</strong><small>{line.brand ? `${line.brand} · ` : ''}{line.description}</small>
              <em>Faltan {line.missing} de {line.requested}{line.in_basket > 0 ? ` · ya tienes ${line.in_basket} en tu cesta` : ''}</em></span></label>
          {none ? <p className="reorder-none"><PackageSearch size={15}/>Ningún otro proveedor lo tiene en existencia ahora.</p> : <div className="reorder-choice">
            <select aria-label={`Proveedor para ${line.codigo}`} value={choice?.item ?? ''} disabled={busy} onChange={event => change(line.order_line_id, { item: event.target.value, include: true })}>
              {line.offers.map(offer => <optgroup key={offer.supplier_id} label={offer.known ? `${offer.supplier_name} · ya le compras` : offer.supplier_name}>
                {offer.items.map(item => <option key={item.id} value={item.id}>{offer.supplier_name} — {item.codigo} ({item.brand || 'sin marca'})</option>)}</optgroup>)}
            </select>
            <input type="number" min={1} max={9999} inputMode="numeric" aria-label={`Unidades de ${line.codigo}`} value={choice?.quantity ?? ''} disabled={busy}
              aria-invalid={choice?.include && !valid(choice.quantity) ? true : undefined} onChange={event => change(line.order_line_id, { quantity: event.target.value })}/>
            <small aria-hidden="true">unidades</small>
          </div>}
        </li>;
      })}</ul>
      <div className="reorder-actions">
        <button type="button" className="button primary" disabled={!ready || busy} onClick={() => void submit()}>{busy ? <LoaderCircle size={16} className="spin"/> : <ShoppingBag size={16}/>}
          Agregar a mi cesta{chosen.length ? ` (${chosen.length})` : ''}</button>
        <button type="button" className="button soft" disabled={busy} onClick={onClose}>Cancelar</button>
      </div>
    </>}
  </Modal>;
}
