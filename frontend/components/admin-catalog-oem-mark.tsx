'use client';

import { useEffect, useState } from 'react';
import { LoaderCircle } from 'lucide-react';
import { ManagedPart, request } from '@/lib/types';
import Modal from './modal';
import { UppercaseInput } from './uppercase-field';

type ListedItem = { id: string; manufacturer: string; code: string; status: string; status_label: string; description: string };
type PlanRow = { id: string; sku: string; description: string; is_OEM: boolean; makes: string[]; suggested: string; listed: ListedItem[];
  description_for_list: string; part_type_for_list: string; warnings: string[]; blocked: string; marked_alterno: boolean; oem_brands: string[] };
type Plan = { rows: PlanRow[]; manufacturers: string[] };
export type OEMMarkMode = 'mark' | 'create' | 'unmark';
const plural = (count: number, one: string, many: string) => `${count} ${count === 1 ? one : many}`;
const titles: Record<OEMMarkMode, string> = { mark: 'Marcar como OEM', create: 'Crear OEM desde SKU', unmark: 'Quitar marca OEM' };
const descriptions: Record<OEMMarkMode, string> = {
  mark: 'El código del SKU es un número de la lista OEM: el SKU queda vinculado a ese ítem y marcado como OEM, con tu nombre como fuente.',
  create: 'El código del SKU es un número OEM que la lista todavía no tiene: se agrega a la Biblioteca OEM con el fabricante que elijas y el SKU queda marcado como OEM.',
  unmark: 'Solo para SKU cuyo código no está en la lista OEM. Un SKU con su código en la lista es OEM: si el número no es correcto, ponlo en disputa en la Biblioteca OEM.',
};

// The grid's right click. Marcar como OEM ties a SKU to the item the OEM list already has with its code; Crear OEM desde SKU adds the
// item to the list first; Quitar marca OEM unmarks a SKU whose code is not in the list. A SKU is OEM when its code is in the OEM list.
export default function CatalogMarkOEM({ mode, parts, onClose, onSaved, onSwitch }: { mode: OEMMarkMode; parts: ManagedPart[]; onClose: () => void;
  onSaved: (text: string) => void; onSwitch: (parts: ManagedPart[], mode: OEMMarkMode) => void }) {
  const [plan, setPlan] = useState<Plan | null>(null); const [error, setError] = useState('');
  const [makes, setMakes] = useState<Record<string, string>>({});
  const [details, setDetails] = useState<Record<string, { description: string; part_type: string }>>({});
  const [chosen, setChosen] = useState<Set<string>>(new Set());
  const [retire, setRetire] = useState<Record<string, boolean>>({});
  const [citation, setCitation] = useState(''); const [reviewed, setReviewed] = useState(false); const [busy, setBusy] = useState(false);
  useEffect(() => {
    let live = true;
    request<Plan>(`/api/management/catalog/${mode === 'create' ? 'create-oem' : 'mark-oem'}?ids=${parts.map(part => part.id).join(',')}`).then(result => {
      if (!live) return;
      setPlan(result);
      setMakes(Object.fromEntries(result.rows.map(row => [row.id, row.suggested])));
      setDetails(Object.fromEntries(result.rows.map(row => [row.id, { description: row.description_for_list, part_type: row.part_type_for_list }])));
      setChosen(new Set(result.rows.filter(row => usable(row)).map(row => row.id)));
      setRetire(Object.fromEntries(result.rows.map(row => [row.id, row.marked_alterno])));
    }).catch(reason => { if (live) setError((reason as Error).message); });
    return () => { live = false; };
    function usable(row: PlanRow) { return mode === 'unmark' ? row.is_OEM && !row.listed.length : !row.blocked && !row.is_OEM; }
  }, [parts, mode]);
  const rows = plan?.rows ?? [];
  const off = (row: PlanRow) => mode === 'unmark' ? !row.is_OEM || !!row.listed.length : !!row.blocked || row.is_OEM;
  const picked = rows.filter(row => chosen.has(row.id) && !off(row));
  const missing = mode !== 'unmark' && picked.some(row => !makes[row.id]?.trim());
  const notListed = mode === 'mark' ? rows.filter(row => !row.is_OEM && !row.listed.length && row.blocked.includes('Crear OEM')) : [];
  function toggle(id: string, on: boolean) { setChosen(current => { const next = new Set(current); if (on) next.add(id); else next.delete(id); return next; }); setReviewed(false); }
  async function save() {
    if (busy || !picked.length || missing || !reviewed) return;
    setBusy(true); setError('');
    try {
      if (mode === 'unmark') {
        await request('/api/management/catalog/unmark-oem', { method: 'POST', body: JSON.stringify({ rows: picked.map(row => ({ id: row.id, retire_number: !!retire[row.id] })) }) });
        onSaved(`${plural(picked.length, 'SKU ya no es OEM', 'SKU ya no son OEM')}: ${picked.map(row => row.sku).join(', ')}.`);
      } else {
        const body = picked.map(row => ({ id: row.id, manufacturer: makes[row.id].trim(), citation, ...(mode === 'create' ? details[row.id] : {}) }));
        await request(`/api/management/catalog/${mode === 'create' ? 'create-oem' : 'mark-oem'}`, { method: 'POST', body: JSON.stringify({ rows: body }) });
        const named = picked.map(row => `${makes[row.id].trim()} ${row.sku}`).join(', ');
        onSaved(mode === 'create' ? `${picked.length === 1 ? 'Agregado a la Biblioteca OEM y marcado' : `${picked.length} números agregados a la Biblioteca OEM y marcados`} como OEM: ${named}.`
          : `${plural(picked.length, 'SKU marcado', 'SKU marcados')} como OEM: ${named}.`);
      }
    } catch (reason) { setError((reason as Error).message); }
    finally { setBusy(false); }
  }
  const verb = mode === 'mark' ? 'Marcar' : mode === 'create' ? 'Crear OEM de' : 'Desmarcar';

  return <Modal title={titles[mode]} wide onClose={() => { if (!busy) onClose(); }}>
    <p className="modal-description">{descriptions[mode]}</p>
    {error && <p className="notice error" role="alert">{error}</p>}
    {!plan && !error && <div className="loading"><LoaderCircle className="spin" size={20}/>Revisando los SKU…</div>}
    {plan && <section className="merge-editor" aria-label={mode === 'unmark' ? 'SKU por desmarcar' : mode === 'create' ? 'SKU para crear su OEM' : 'SKU por marcar como OEM'}>
      <div className="grouping-source-list">{rows.map(row => {
        const disabled = off(row), editable = !disabled && mode !== 'unmark';
        return <article key={row.id} className={`grouping-source oem-mark-row ${chosen.has(row.id) && !disabled ? 'selected' : ''}`}>
          <label className="grouping-select"><input type="checkbox" aria-label={`${verb} ${row.sku}`} checked={chosen.has(row.id) && !disabled} disabled={disabled || busy} onChange={event => toggle(row.id, event.target.checked)}/>
            <span>{row.sku}</span></label>
          <p className="oem-mark-description">{row.description || 'Sin descripción'}</p>
          {editable && mode === 'mark' && row.listed.length > 1 && <label className="oem-mark-make">Ítem de la lista OEM
            <select aria-label={`Fabricante de ${row.sku}`} value={makes[row.id] ?? ''} disabled={busy}
              onChange={event => { setMakes(current => ({ ...current, [row.id]: event.target.value })); setReviewed(false); }}>
              {row.listed.map(item => <option key={item.id} value={item.manufacturer}>{item.manufacturer} {item.code} · {item.status_label}</option>)}</select></label>}
          {editable && mode === 'mark' && row.listed.length === 1 && <small className="oem-mark-note">En la lista OEM: {row.listed[0].manufacturer} {row.listed[0].code} · {row.listed[0].status_label}{row.listed[0].description ? ` · ${row.listed[0].description}` : ''}</small>}
          {editable && mode === 'create' && <div className="oem-mark-create">
            <label className="oem-mark-make">Fabricante del número
              <UppercaseInput list="oem-mark-makes" aria-label={`Fabricante de ${row.sku}`} value={makes[row.id] ?? ''} maxLength={120} placeholder="TOYOTA, NISSAN…" disabled={busy}
                onChange={event => { setMakes(current => ({ ...current, [row.id]: event.target.value })); setReviewed(false); }}/></label>
            <label>Descripción en la lista<UppercaseInput aria-label={`Descripción OEM de ${row.sku}`} value={details[row.id]?.description ?? ''} maxLength={300} disabled={busy}
              onChange={event => setDetails(current => ({ ...current, [row.id]: { ...current[row.id], description: event.target.value } }))}/></label>
            <label>Tipo de pieza<UppercaseInput aria-label={`Tipo de pieza de ${row.sku}`} value={details[row.id]?.part_type ?? ''} maxLength={60} placeholder="BOMBA DE AGUA" disabled={busy}
              onChange={event => setDetails(current => ({ ...current, [row.id]: { ...current[row.id], part_type: event.target.value } }))}/></label></div>}
          {mode === 'unmark' && !disabled && <label className="admin-checkbox"><input type="checkbox" checked={!!retire[row.id]} disabled={busy} onChange={event => setRetire(current => ({ ...current, [row.id]: event.target.checked }))}/>
            <span>Retirar también su alterno OEM {row.oem_brands.join(' / ') || ''} {row.sku}</span></label>}
          {mode === 'unmark' && row.is_OEM && !!row.listed.length && <small className="oem-mark-blocked">Su código es {row.listed[0].manufacturer} {row.listed[0].code} de la lista OEM, así que es OEM: si el número no es correcto, ponlo en disputa en la Biblioteca OEM.</small>}
          {row.is_OEM && mode !== 'unmark' && <small className="oem-mark-note">Ya es OEM.</small>}
          {row.blocked && mode !== 'unmark' && !row.is_OEM && <small className="oem-mark-blocked">{row.blocked}</small>}
          {mode !== 'unmark' && !row.is_OEM && row.warnings.map(text => <small key={text} className="oem-mark-warning">{text}</small>)}
        </article>;
      })}</div>
      {notListed.length > 0 && <div className="notice oem-mark-switch">{plural(notListed.length, 'SKU no tiene', 'SKU no tienen')} su número en la lista OEM.
        <button type="button" className="button soft small" disabled={busy} onClick={() => onSwitch(parts.filter(part => notListed.some(row => row.id === part.id)), 'create')}>Crear OEM desde SKU</button></div>}
      <datalist id="oem-mark-makes">{plan.manufacturers.map(name => <option key={name} value={name}/>)}</datalist>
      {mode !== 'unmark' && <label className="oem-mark-citation">Fuente (opcional)<input value={citation} maxLength={300} placeholder="CATÁLOGO, EPC O PÁGINA QUE LO CONFIRMA" disabled={busy} onChange={event => setCitation(event.target.value)}/></label>}
      {!!picked.length && <div className="grouping-confirmation">
        <label className="admin-checkbox"><input type="checkbox" checked={reviewed} disabled={busy} onChange={event => setReviewed(event.target.checked)}/>
          <span>{mode === 'unmark' ? 'Revisé qué SKU dejan de ser OEM.' : 'Revisé que cada código es el número OEM de su fabricante.'}</span></label>
      </div>}
      <button type="button" className="button primary full" disabled={busy || !picked.length || missing || !reviewed} onClick={() => void save()}>
        {busy ? <><LoaderCircle size={16} className="spin"/>Guardando…</> : !picked.length ? 'Elige al menos un SKU' : missing ? 'Indica el fabricante de cada SKU'
          : mode === 'mark' ? `Marcar ${picked.length} SKU como OEM` : mode === 'create' ? `Crear ${plural(picked.length, 'OEM', 'OEM')} y marcar` : `Quitar marca OEM a ${picked.length} SKU`}</button>
    </section>}
  </Modal>;
}
