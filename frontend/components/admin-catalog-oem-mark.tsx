'use client';

import { useEffect, useState } from 'react';
import { LoaderCircle } from 'lucide-react';
import { ManagedPart, request } from '@/lib/types';
import Modal from './modal';
import { UppercaseInput } from './uppercase-field';

type PlanRow = { id: string; sku: string; description: string; is_OEM: boolean; makes: string[]; suggested: string; warnings: string[]; blocked: string;
  marked_alterno: boolean; oem_brands: string[] };
type Plan = { rows: PlanRow[]; manufacturers: string[] };
export type OEMMarkMode = 'mark' | 'unmark';
const plural = (count: number, one: string, many: string) => `${count} ${count === 1 ? one : many}`;

// Marcar como OEM, from the grid's right click: the SKU code is the OEM number of a make. Marking files it as the SKU's OEM alterno
// (the make, the code, a citation naming who marked it), makes the SKU MAIN OEM and so puts the number in Biblioteca OEM. Quitar marca
// OEM takes the flag off and can retire that alterno, which takes the number out of the library when nothing else backs it.
export default function CatalogMarkOEM({ mode, parts, onClose, onSaved }: { mode: OEMMarkMode; parts: ManagedPart[]; onClose: () => void; onSaved: (text: string) => void }) {
  const [plan, setPlan] = useState<Plan | null>(null); const [error, setError] = useState('');
  const [makes, setMakes] = useState<Record<string, string>>({});
  const [chosen, setChosen] = useState<Set<string>>(new Set());
  const [retire, setRetire] = useState<Record<string, boolean>>({});
  const [citation, setCitation] = useState(''); const [reviewed, setReviewed] = useState(false); const [busy, setBusy] = useState(false);
  useEffect(() => {
    let live = true;
    request<Plan>(`/api/management/catalog/mark-oem?ids=${parts.map(part => part.id).join(',')}`).then(result => {
      if (!live) return;
      setPlan(result);
      setMakes(Object.fromEntries(result.rows.map(row => [row.id, row.suggested])));
      setChosen(new Set(result.rows.filter(row => mode === 'mark' ? !row.blocked && !row.is_OEM : row.is_OEM).map(row => row.id)));
      setRetire(Object.fromEntries(result.rows.map(row => [row.id, row.marked_alterno])));
    }).catch(reason => { if (live) setError((reason as Error).message); });
    return () => { live = false; };
  }, [parts, mode]);
  const rows = plan?.rows ?? [];
  const picked = rows.filter(row => chosen.has(row.id));
  const missing = mode === 'mark' && picked.some(row => !makes[row.id]?.trim());
  function toggle(id: string, on: boolean) { setChosen(current => { const next = new Set(current); if (on) next.add(id); else next.delete(id); return next; }); setReviewed(false); }
  async function save() {
    if (busy || !picked.length || missing || !reviewed) return;
    setBusy(true); setError('');
    try {
      if (mode === 'mark') {
        await request('/api/management/catalog/mark-oem', { method: 'POST', body: JSON.stringify({ rows: picked.map(row => ({ id: row.id, manufacturer: makes[row.id].trim(), citation })) }) });
        onSaved(`${plural(picked.length, 'SKU marcado', 'SKU marcados')} como OEM: ${picked.map(row => `${makes[row.id].trim()} ${row.sku}`).join(', ')}. Ya están en la Biblioteca OEM.`);
      } else {
        await request('/api/management/catalog/unmark-oem', { method: 'POST', body: JSON.stringify({ rows: picked.map(row => ({ id: row.id, retire_number: !!retire[row.id] })) }) });
        onSaved(`${plural(picked.length, 'SKU ya no es MAIN OEM', 'SKU ya no son MAIN OEM')}: ${picked.map(row => row.sku).join(', ')}.`);
      }
    } catch (reason) { setError((reason as Error).message); }
    finally { setBusy(false); }
  }

  return <Modal title={mode === 'mark' ? 'Marcar como OEM' : 'Quitar marca OEM'} wide onClose={() => { if (!busy) onClose(); }}>
    <p className="modal-description">{mode === 'mark'
      ? 'El código del SKU es el número OEM de un fabricante: el SKU queda como MAIN OEM y el número entra a la Biblioteca OEM, con tu nombre como fuente.'
      : 'El SKU deja de ser MAIN OEM. Si el número no era correcto, retíralo también: sale de sus alternos y de la Biblioteca OEM si nada más lo respalda.'}</p>
    {error && <p className="notice error" role="alert">{error}</p>}
    {!plan && !error && <div className="loading"><LoaderCircle className="spin" size={20}/>Revisando los SKU…</div>}
    {plan && <section className="merge-editor" aria-label={mode === 'mark' ? 'SKU por marcar como OEM' : 'SKU por desmarcar'}>
      <div className="grouping-source-list">{rows.map(row => {
        const off = mode === 'mark' ? !!row.blocked || row.is_OEM : !row.is_OEM;
        return <article key={row.id} className={`grouping-source oem-mark-row ${chosen.has(row.id) ? 'selected' : ''}`}>
          <label className="grouping-select"><input type="checkbox" aria-label={`${mode === 'mark' ? 'Marcar' : 'Desmarcar'} ${row.sku}`} checked={chosen.has(row.id)} disabled={off || busy} onChange={event => toggle(row.id, event.target.checked)}/>
            <span>{row.sku}</span></label>
          <p className="oem-mark-description">{row.description || 'Sin descripción'}</p>
          {mode === 'mark' && !off && <label className="oem-mark-make">Fabricante del número
            <UppercaseInput list="oem-mark-makes" aria-label={`Fabricante de ${row.sku}`} value={makes[row.id] ?? ''} maxLength={120} placeholder="TOYOTA, NISSAN…" disabled={busy}
              onChange={event => { setMakes(current => ({ ...current, [row.id]: event.target.value })); setReviewed(false); }}/></label>}
          {mode === 'unmark' && !off && <label className="admin-checkbox"><input type="checkbox" checked={!!retire[row.id]} disabled={busy} onChange={event => setRetire(current => ({ ...current, [row.id]: event.target.checked }))}/>
            <span>Retirar también el alterno OEM {row.oem_brands.join(' / ') || ''} {row.sku}</span></label>}
          {row.is_OEM && mode === 'mark' && <small className="oem-mark-note">Ya es MAIN OEM.</small>}
          {row.blocked && mode === 'mark' && <small className="oem-mark-blocked">{row.blocked}</small>}
          {mode === 'mark' && row.warnings.map(text => <small key={text} className="oem-mark-warning">{text}</small>)}
        </article>;
      })}</div>
      <datalist id="oem-mark-makes">{plan.manufacturers.map(name => <option key={name} value={name}/>)}</datalist>
      {mode === 'mark' && <label className="oem-mark-citation">Fuente (opcional)<input value={citation} maxLength={300} placeholder="CATÁLOGO, EPC O PÁGINA QUE LO CONFIRMA" disabled={busy} onChange={event => setCitation(event.target.value)}/></label>}
      {!!picked.length && <div className="grouping-confirmation">
        <label className="admin-checkbox"><input type="checkbox" checked={reviewed} disabled={busy} onChange={event => setReviewed(event.target.checked)}/>
          <span>{mode === 'mark' ? 'Revisé que cada código es el número OEM de su fabricante.' : 'Revisé qué SKU dejan de ser MAIN OEM.'}</span></label>
      </div>}
      <button type="button" className="button primary full" disabled={busy || !picked.length || missing || !reviewed} onClick={() => void save()}>
        {busy ? <><LoaderCircle size={16} className="spin"/>Guardando…</> : !picked.length ? 'Elige al menos un SKU' : missing ? 'Indica el fabricante de cada SKU'
          : mode === 'mark' ? `Marcar ${picked.length} SKU como OEM` : `Quitar marca OEM a ${picked.length} SKU`}</button>
    </section>}
  </Modal>;
}
