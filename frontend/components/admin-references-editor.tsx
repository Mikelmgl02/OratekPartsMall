'use client';

import { FormEvent, useCallback, useEffect, useId, useState } from 'react';
import { LoaderCircle, Plus, Star, Trash2 } from 'lucide-react';
import { ApiError, Page, request } from '@/lib/types';
import { referenceEstadoLabels, referenceEstadoTones, referenceTipoLabels, sourceKindLabels, sourceMarkLabels, asManagedPart } from '@/lib/line-types';
import type { Brand, LineConflict, OEMLine, Reference, ReferenceAdded, ReferenceEstado, ReferenceTipo } from '@/lib/line-types';
import Modal from './modal';
import OEMReferenceLookup from './oem-reference-lookup';
import { UppercaseInput } from './uppercase-field';

const base = '/api/management';
const message = (error: unknown) => error instanceof Error ? error.message : 'No se pudo guardar la referencia. Inténtalo de nuevo.';
const EDIT_TIPOS: ReferenceTipo[] = ['oem', 'aftermarket', 'otro'];
const EDIT_ESTADOS: Exclude<ReferenceEstado, 'retirada'>[] = ['verificado', 'declarado', 'inferido', 'disputa'];
type Draft = { code: string; brand: string; tipo: ReferenceTipo; estado: ReferenceEstado; dispute_note: string; notes: string };
const draftOf = (ref: Reference): Draft => ({ code: ref.code, brand: ref.brand, tipo: ref.tipo, estado: ref.estado, dispute_note: ref.dispute_note, notes: ref.notes });

// A brand typed as any of its spellings: the suggestions list each brand's nombre and its aliases (TOY → TOYOTA), and the backend keeps
// the nombre (mall.brands.resolve). Blank means the code is the same for every brand.
export function BrandInput({ value, onChange, label, name }: { value: string; onChange: (value: string) => void; label: string; name?: string }) {
  const list = useId();
  const [brands, setBrands] = useState<Brand[]>([]);
  useEffect(() => {
    const text = value.trim();
    if (!text) { setBrands([]); return; }
    let live = true;
    const timer = setTimeout(() => request<Page<Brand>>(`${base}/brands?search=${encodeURIComponent(text)}&page=1`).then(page => { if (live) setBrands(page.results.slice(0, 20)); }).catch(() => { if (live) setBrands([]); }), 250);
    return () => { live = false; clearTimeout(timer); };
  }, [value]);
  return <><UppercaseInput aria-label={label} name={name} list={list} value={value} maxLength={120} placeholder="TODAS LAS MARCAS" onChange={event => onChange(event.target.value)}/>
    <datalist id={list}>{brands.flatMap(brand => [<option key={brand.id} value={brand.nombre}>{brand.familia ? `FAMILIA ${brand.familia.nombre}` : ''}</option>,
      ...brand.aliases.map(alias => <option key={`${brand.id}:${alias.id}`} value={alias.alias}>{`→ ${brand.nombre}`}</option>)])}</datalist></>;
}

function Sources({ reference: row }: { reference: Reference }) {
  if (!row.sources.length) return <span className="admin-grid-muted">Sin fuentes registradas</span>;
  return <ul className="reference-sources">{row.sources.map((source, index) => <li key={`${source.kind}:${source.citation}:${index}`} className={source.retirada ? 'retired' : ''}>
    <strong>{sourceKindLabels[source.kind] ?? source.kind.toUpperCase()}</strong>{source.citation && <span> · {source.citation}</span>}
    {source.marks.map(mark => <em key={mark} className="pill">{sourceMarkLabels[mark] ?? mark}</em>)}{source.retirada && <em className="pill">RETIRADA</em>}
  </li>)}</ul>;
}

// The line's References (plan S8.3), replacing the Alternos tab, "Equivalencias por número OEM" and "EL SKU PRINCIPAL ES OEM": every
// Reference with its tipo, estado, brand and sources, written one row at a time (management/references/<id> with the version it was
// read at; a retired one is never deleted), the main chosen by its radio (management/lines/<id>/main: the SKU follows its code, so the
// line's tipo follows the main's), and new ones added to the line (management/lines/<id>/references).
export default function ReferencesEditor({ line, onLine }: { line: OEMLine; onLine: (line: OEMLine) => void }) {
  const [refs, setRefs] = useState<Reference[] | null>(null);
  const [loadError, setLoadError] = useState(''); const [retry, setRetry] = useState(0);
  const [error, setError] = useState(''); const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState('');
  const [editing, setEditing] = useState<number | null>(null); const [draft, setDraft] = useState<Draft | null>(null);
  const [retiring, setRetiring] = useState<number | null>(null);
  const [adding, setAdding] = useState({ code: '', brand: '', tipo: 'otro' as ReferenceTipo, estado: 'declarado' as ReferenceEstado, source: '' });
  const locked = line.estado === 'unificada';
  const load = useCallback(async () => {
    const rows = await request<Reference[]>(`${base}/lines/${line.id}/references`);
    setRefs(rows);
    return rows;
  }, [line.id]);
  useEffect(() => { let live = true; setLoadError(''); load().catch(caught => { if (live) setLoadError(message(caught)); }); return () => { live = false; }; }, [load, retry]);
  // After a write: the References again, and the line (its SKU, tipo, version and counts follow the main and the codes).
  async function refresh() {
    await load();
    onLine(await request<OEMLine>(`${base}/lines/${line.id}`));
  }
  async function run(key: string, action: () => Promise<string | void>) {
    setBusy(key); setError(''); setNotice('');
    try { const text = await action(); if (text) setNotice(text); }
    catch (caught) {
      setError(message(caught));
      if (caught instanceof ApiError && caught.status === 409) await refresh().catch(() => undefined);  // someone changed it: show what it is now
    } finally { setBusy(''); }
  }
  const makeMain = (ref: Reference) => run(`main:${ref.id}`, async () => {
    try {
      onLine(await request<OEMLine>(`${base}/lines/${line.id}/main`, { method: 'POST', body: JSON.stringify({ reference: ref.id, expected_version: line.version }) }));
    } catch (caught) {
      const holder = caught instanceof ApiError ? (caught.body as LineConflict)?.line : null;
      if (holder) throw new Error(`${caught instanceof Error ? caught.message : ''} (${holder.sku})`.trim());
      throw caught;
    }
    await load();
    return `${ref.code} es ahora la referencia principal: el SKU de la línea OEM pasa a ser ${ref.code}.`;
  });
  const save = (ref: Reference) => run(`save:${ref.id}`, async () => {
    if (!draft) return;
    const changes = Object.fromEntries(Object.entries(draft).filter(([key, value]) => value !== draftOf(ref)[key as keyof Draft]));
    if (!Object.keys(changes).length) { setEditing(null); return; }
    await request<Reference>(`${base}/references/${ref.id}`, { method: 'PATCH', body: JSON.stringify({ ...changes, expected_version: ref.version }) });
    setEditing(null); setDraft(null);
    await refresh();
    return `Referencia ${draft.code} guardada.`;
  });
  const retire = (ref: Reference) => run(`retire:${ref.id}`, async () => {
    await request<Reference>(`${base}/references/${ref.id}`, { method: 'DELETE' });
    setRetiring(null);
    await refresh();
    return `Referencia ${ref.code} retirada. Se conserva en el historial de la línea OEM.`;
  });
  const add = (rows: { code: string; brand: string; tipo: ReferenceTipo; estado?: ReferenceEstado; source?: string }[]) => run('add', async () => {
    const texts: string[] = [];
    for (const row of rows) {
      const result = await request<ReferenceAdded>(`${base}/lines/${line.id}/references`, { method: 'POST',
        body: JSON.stringify({ code: row.code.trim(), brand: row.brand.trim(), tipo: row.tipo, ...(row.estado ? { estado: row.estado } : {}), ...(row.source?.trim() ? { source: row.source.trim() } : {}) }) });
      texts.push(result.added ? `${result.reference.code} agregada.` : `${result.reference.code} ya era una referencia de esta línea OEM.`);
    }
    setAdding({ code: '', brand: '', tipo: 'otro', estado: 'declarado', source: '' });
    await refresh();
    return texts.join(' ');
  });
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (adding.code.trim()) void add([adding]);
  }
  if (loadError) return <div className="notice error" role="alert">{loadError}<button type="button" onClick={() => setRetry(value => value + 1)}>Intentar de nuevo</button></div>;
  if (!refs) return <div className="loading"><LoaderCircle className="spin" size={18}/>Cargando referencias…</div>;
  const live = refs.filter(ref => ref.estado !== 'retirada');
  const retired = refs.filter(ref => ref.estado === 'retirada');
  return <div className="references-editor">
    <p className="form-footnote">El SKU es el código de la referencia principal, y el tipo de la línea OEM sigue al de esa referencia (OEM, aftermarket u otro). Cada referencia se guarda por separado; una retirada se conserva en el historial y su código queda reservado para esta línea.</p>
    {error && <div className="notice error" role="alert">{error}</div>}
    {notice && <div className="notice success" role="status">{notice}</div>}
    <ul className="reference-rows" aria-label="Referencias de la línea OEM">{live.map(ref => {
      const open = editing === ref.id && draft;
      return <li key={ref.id} className={`reference-row${ref.is_main ? ' main' : ''}`} aria-label={`Referencia ${ref.code}`}>
        <div className="reference-row-head">
          <label className="reference-main" title={ref.is_main ? 'Referencia principal: su código es el SKU' : 'Hacerla principal'}>
            <input type="radio" name={`principal-${line.id}`} checked={ref.is_main} disabled={locked || !!busy || ref.is_main || ref.tipo === 'interno'} onChange={() => void makeMain(ref)}
              aria-label={`Referencia principal ${ref.code}`}/>{ref.is_main ? <><Star size={13} aria-hidden="true"/>PRINCIPAL</> : 'Principal'}</label>
          <strong className="reference-code">{ref.code}</strong>
          <span className={`reference-tipo ${ref.tipo}`}>{referenceTipoLabels[ref.tipo]}{ref.brand ? ` · ${ref.brand}` : ref.tipo === 'interno' ? '' : ' · TODAS LAS MARCAS'}</span>
          <span className={`reference-estado ${referenceEstadoTones[ref.estado]}`}>{referenceEstadoLabels[ref.estado]}</span>
          {ref.supplier && <span className="pill">INTERNO DE {ref.supplier.name}</span>}
          <small className="reference-variants">{ref.variant_count} {ref.variant_count === 1 ? 'artículo' : 'artículos'}</small>
          {busy.endsWith(`:${ref.id}`) && <LoaderCircle className="spin" size={14}/>}
        </div>
        {ref.estado === 'disputa' && ref.dispute_note && <p className="reference-note">EN DISPUTA: {ref.dispute_note}</p>}
        {ref.notes && <p className="reference-note">{ref.notes}</p>}
        {ref.printed_forms.length > 0 && <p className="reference-note">TAMBIÉN IMPRESO COMO: {ref.printed_forms.join(' · ')}</p>}
        <Sources reference={ref}/>
        {open ? <div className="reference-edit" role="group" aria-label={`Editar referencia ${ref.code}`}>
          <div className="form-row"><label>Código<UppercaseInput aria-label={`Código de ${ref.code}`} value={draft.code} maxLength={120} required onChange={event => setDraft({ ...draft, code: event.target.value })}/></label>
            <label>Marca<BrandInput label={`Marca de ${ref.code}`} value={draft.brand} onChange={brand => setDraft({ ...draft, brand })}/></label></div>
          <div className="form-row"><label>Tipo<select aria-label={`Tipo de ${ref.code}`} value={draft.tipo} disabled={ref.tipo === 'interno'} onChange={event => setDraft({ ...draft, tipo: event.target.value as ReferenceTipo })}>
              {(ref.tipo === 'interno' ? ['interno' as const] : EDIT_TIPOS).map(value => <option key={value} value={value}>{referenceTipoLabels[value]}</option>)}</select></label>
            <label>Estado<select aria-label={`Estado de ${ref.code}`} value={draft.estado} onChange={event => setDraft({ ...draft, estado: event.target.value as ReferenceEstado })}>
              {EDIT_ESTADOS.map(value => <option key={value} value={value}>{referenceEstadoLabels[value]}</option>)}</select></label></div>
          {draft.estado === 'disputa' && <label>Motivo de la disputa<input aria-label={`Motivo de la disputa de ${ref.code}`} value={draft.dispute_note} required onChange={event => setDraft({ ...draft, dispute_note: event.target.value })}/></label>}
          <label>Notas<input aria-label={`Notas de ${ref.code}`} value={draft.notes} onChange={event => setDraft({ ...draft, notes: event.target.value })}/></label>
          <div className="admin-toolbar-actions"><button type="button" className="button primary small" disabled={!!busy || !draft.code.trim() || (draft.estado === 'disputa' && !draft.dispute_note.trim())} onClick={() => void save(ref)}>Guardar referencia</button>
            <button type="button" className="button soft small" disabled={!!busy} onClick={() => { setEditing(null); setDraft(null); }}>Cancelar</button></div>
        </div> : !locked && <div className="admin-toolbar-actions reference-actions">
          <button type="button" className="button text small" disabled={!!busy} aria-label={`Editar referencia ${ref.code}`} onClick={() => { setEditing(ref.id); setDraft(draftOf(ref)); setRetiring(null); }}>Editar</button>
          {!ref.is_main && (retiring === ref.id
            ? <><span className="reference-confirm">¿Retirar {ref.code}{ref.variant_count ? ` (tiene ${ref.variant_count} ${ref.variant_count === 1 ? 'artículo' : 'artículos'})` : ''}?</span>
                <button type="button" className="button dark small" disabled={!!busy} onClick={() => void retire(ref)}>Confirmar retiro</button>
                <button type="button" className="button soft small" disabled={!!busy} onClick={() => setRetiring(null)}>Cancelar</button></>
            : <button type="button" className="button text small danger-text" disabled={!!busy} aria-label={`Retirar referencia ${ref.code}`} onClick={() => setRetiring(ref.id)}><Trash2 size={13} aria-hidden="true"/>Retirar</button>)}
        </div>}
      </li>;
    })}</ul>
    {retired.length > 0 && <details className="reference-retired"><summary>Retiradas ({retired.length})</summary><ul className="reference-rows">{retired.map(ref =>
      <li key={ref.id} className="reference-row retired" aria-label={`Referencia retirada ${ref.code}`}><div className="reference-row-head"><strong className="reference-code">{ref.code}</strong>
        <span className={`reference-tipo ${ref.tipo}`}>{referenceTipoLabels[ref.tipo]}{ref.brand ? ` · ${ref.brand}` : ''}</span><span className="reference-estado off">RETIRADA</span></div><Sources reference={ref}/></li>)}</ul></details>}
    {!locked && <>
      <form className="stack-form reference-add" autoComplete="off" onSubmit={submit} aria-label="Agregar referencia">
        <strong>Agregar referencia</strong>
        <div className="form-row"><label>Código<UppercaseInput aria-label="Código de la nueva referencia" value={adding.code} maxLength={120} required onChange={event => setAdding({ ...adding, code: event.target.value })} placeholder="EJ.: 51712-1R000"/></label>
          <label>Marca (opcional)<BrandInput label="Marca de la nueva referencia" value={adding.brand} onChange={brand => setAdding({ ...adding, brand })}/></label></div>
        <div className="form-row"><label>Tipo<select aria-label="Tipo de la nueva referencia" value={adding.tipo} onChange={event => setAdding({ ...adding, tipo: event.target.value as ReferenceTipo })}>{EDIT_TIPOS.map(value => <option key={value} value={value}>{referenceTipoLabels[value]}</option>)}</select></label>
          <label>Estado<select aria-label="Estado de la nueva referencia" value={adding.estado} onChange={event => setAdding({ ...adding, estado: event.target.value as ReferenceEstado })}>{EDIT_ESTADOS.filter(value => value !== 'disputa').map(value => <option key={value} value={value}>{referenceEstadoLabels[value]}</option>)}</select></label></div>
        <label>Fuente (opcional)<input aria-label="Fuente de la nueva referencia" value={adding.source} maxLength={500} onChange={event => setAdding({ ...adding, source: event.target.value })} placeholder="Catálogo y página, ficha técnica o enlace"/></label>
        <button className="button soft small" disabled={!!busy || !adding.code.trim()}>{busy === 'add' ? <LoaderCircle className="spin" size={14}/> : <Plus size={14}/>}Agregar referencia</button>
        <p className="form-footnote">Registra solo la misma pieza: una aplicación parecida, una variante o un kit no bastan. Si el código ya es de otra línea OEM, unifícalas en Casos de coincidencia o con Agrupar con….</p>
      </form>
      <OEMReferenceLookup part={asManagedPart(line)} onAdd={rows => void add(rows.map(row => ({ code: row.code, brand: row.brand, tipo: 'oem' as const, estado: 'declarado' as const, source: row.reference_source || '' })))}/>
    </>}
    {locked && <p className="notice">Esta línea OEM está unificada en otra: sus referencias se leen aquí y se cambian en la línea que la recibió.</p>}
  </div>;
}

// Marcar referencia principal (the Inventario's right click): the line's live References as a radio list; the chosen one becomes the
// main (management/lines/<id>/main, with the version the line was read at) and the SKU follows its code.
export function MainReferenceDialog({ line, onClose, onSaved }: { line: OEMLine; onClose: () => void; onSaved: (line: OEMLine, text: string) => void }) {
  const [refs, setRefs] = useState<Reference[] | null>(null); const [chosen, setChosen] = useState<number | null>(null);
  const [error, setError] = useState(''); const [busy, setBusy] = useState(false);
  useEffect(() => {
    let live = true;
    request<Reference[]>(`${base}/lines/${line.id}/references`).then(rows => { if (live) { setRefs(rows.filter(ref => ref.estado !== 'retirada' && ref.tipo !== 'interno')); setChosen(rows.find(ref => ref.is_main)?.id ?? null); } })
      .catch(caught => { if (live) setError(message(caught)); });
    return () => { live = false; };
  }, [line.id]);
  const current = refs?.find(ref => ref.is_main);
  async function save() {
    const ref = refs?.find(row => row.id === chosen);
    if (!ref || ref.is_main) return;
    setBusy(true); setError('');
    try {
      const saved = await request<OEMLine>(`${base}/lines/${line.id}/main`, { method: 'POST', body: JSON.stringify({ reference: ref.id, expected_version: line.version }) });
      onSaved(saved, `${ref.code} es ahora la referencia principal de la línea OEM: su SKU pasó de ${line.sku} a ${saved.sku}.`);
    } catch (caught) {
      const holder = caught instanceof ApiError ? (caught.body as LineConflict)?.line : null;
      setError(`${message(caught)}${holder ? ` (${holder.sku})` : ''}`);
    } finally { setBusy(false); }
  }
  return <Modal title={`Referencia principal · ${line.sku}`} onClose={() => { if (!busy) onClose(); }}>
    <p className="modal-description">La referencia principal da su código al SKU y su tipo a la línea OEM. La anterior queda como una referencia más de la línea.</p>
    {!refs && !error && <div className="loading"><LoaderCircle className="spin" size={18}/>Cargando referencias…</div>}
    {refs && <fieldset className="matching-lines"><legend>Referencias vivas</legend>{refs.map(ref => <label key={ref.id} className={`matching-line${chosen === ref.id ? ' selected' : ''}`}>
      <input type="radio" name={`main-${line.id}`} checked={chosen === ref.id} disabled={busy} onChange={() => setChosen(ref.id)}/>
      <span><strong>{ref.code}{ref.is_main && <em className="pill">ACTUAL</em>}</strong><small>{referenceTipoLabels[ref.tipo]}{ref.brand ? ` · ${ref.brand}` : ''} · {referenceEstadoLabels[ref.estado]}</small></span></label>)}</fieldset>}
    {error && <div className="notice error" role="alert">{error}</div>}
    <button type="button" className="button primary full" disabled={busy || !chosen || chosen === current?.id} onClick={() => void save()}>{busy && <LoaderCircle size={16} className="spin"/>}Hacer principal</button>
  </Modal>;
}
