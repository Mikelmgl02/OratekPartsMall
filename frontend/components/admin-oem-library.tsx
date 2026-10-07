'use client';

import { FormEvent, useEffect, useState } from 'react';
import { ArrowLeft, Check, ExternalLink, Library, LoaderCircle, Plus, Search, Trash2 } from 'lucide-react';
import Modal from './modal';
import { UppercaseInput, UppercaseTextarea } from './uppercase-field';
import { ApiError, request } from '@/lib/types';
import { OEMManualSourceKind, OEMReferenceAction, OEMReferenceConflict, OEMReferenceDetail, OEMReferencePage, OEMReferenceSource, OEMReferenceStatus,
  OEMSourceKind, isLink, linkedViaLabels, manualSourceKinds, referenceStatusHints, referenceStatusLabels, sourceKindLabels } from '@/lib/oem-reference-types';

const base = '/api/management/oem-references';
const number = (value: number) => value.toLocaleString('es-PA');
const stamp = (value: string) => new Date(value).toLocaleString('es-PA', { timeZone: 'America/Panama' });
const statuses = Object.keys(referenceStatusLabels) as OEMReferenceStatus[];
const plural: Record<OEMReferenceStatus, string> = { verified: 'VERIFICADOS', declared: 'DECLARADOS', inferred: 'INFERIDOS', disputed: 'EN DISPUTA' };
const fieldNames: Record<string, string> = { manufacturer: 'Fabricante', code: 'Número OEM', citation: 'Cita', kind: 'Tipo de fuente', verified: 'Comprobado',
  description: 'Descripción', part_type: 'Tipo de pieza', applications: 'Aplicaciones', notes: 'Notas', note: 'Motivo', source_id: 'Fuente', source: 'Fuente' };
function flat(value: unknown): string {
  if (typeof value === 'string') return value;
  if (Array.isArray(value)) return value.map(flat).filter(Boolean).join(' ');
  if (!value || typeof value !== 'object') return '';
  return Object.entries(value).map(([key, item]) => {
    const text = flat(item);
    return text && fieldNames[key] && (typeof item === 'string' || Array.isArray(item)) ? `${fieldNames[key]}: ${text}` : text;
  }).filter(Boolean).join(' ');
}
function errorMessage(error: unknown) {
  if (error instanceof ApiError && error.body && typeof error.body === 'object' && typeof (error.body as { detail?: unknown }).detail !== 'string') return flat(error.body) || error.message;
  return error instanceof Error ? error.message : 'No se pudo completar la acción. Inténtalo de nuevo.';
}
async function create(payload: Record<string, unknown>) {
  const response = await fetch(base, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
  let data;
  try { data = await response.json(); } catch { throw new Error('No se pudo guardar el número. Inténtalo de nuevo.'); }
  if (!response.ok) throw new ApiError(typeof data.detail === 'string' ? data.detail : flat(data) || 'No se pudo guardar el número.', response.status, '', data);
  return { row: data as OEMReferenceDetail, created: response.status === 201 };
}
function sourceDetail(source: OEMReferenceSource) {
  const detail = source.detail, parts: string[] = [];
  if (source.kind === 'oem_finder' && detail.run) parts.push(`CORRIDA ${detail.run}${detail.rule ? ` · REGLA ${detail.rule}` : ''}${detail.rules_version ? ` · ${detail.rules_version}` : ''}`);
  if (detail.part_codes?.length) parts.push(`${detail.part_codes.length === 1 ? 'ALTERNO' : 'ALTERNOS'} DEL CATÁLOGO: ${detail.part_codes.map(code => code.code).join(', ')}`);
  if (source.kind === 'manual') parts.push(detail.verified ? 'COMPROBADO EN UNA FUENTE PRIMARIA' : 'SIN COMPROBAR');
  if (source.kind === 'ai_lookup' && detail.approved) parts.push('APROBADA EN EL CATÁLOGO');
  if (detail.supersedes) parts.push('REGISTRADO AL INDICAR UN REEMPLAZO');
  return parts;
}
const Status = ({ status }: { status: OEMReferenceStatus }) => <span className={`oem-ref-status ${status}`} title={referenceStatusHints[status]}>{referenceStatusLabels[status]}</span>;

type View = { kind: 'list' } | { kind: 'add' } | { kind: 'detail'; id: string; notice?: string };

export default function OEMLibrary({ onClose }: { onClose: () => void }) {
  const [view, setView] = useState<View>({ kind: 'list' });
  const [search, setSearch] = useState(''); const [query, setQuery] = useState(''); const [manufacturer, setManufacturer] = useState(''); const [status, setStatus] = useState('');
  const [sourceKind, setSourceKind] = useState(''); const [linked, setLinked] = useState(''); const [page, setPage] = useState(1); const [revision, setRevision] = useState(0);
  const [data, setData] = useState<OEMReferencePage | null>(null); const [error, setError] = useState('');
  useEffect(() => { const timer = setTimeout(() => { setQuery(search.trim()); setPage(1); }, 300); return () => clearTimeout(timer); }, [search]);
  useEffect(() => {
    if (view.kind !== 'list') return;
    let live = true;
    const params = new URLSearchParams({ page: String(page) });
    if (query) params.set('q', query); if (manufacturer) params.set('manufacturer', manufacturer); if (status) params.set('status', status);
    if (sourceKind) params.set('source_kind', sourceKind); if (linked) params.set('linked', linked);
    request<OEMReferencePage>(`${base}?${params}`).then(result => { if (live) { setData(result); setError(''); } }).catch(e => { if (live) { setData(null); setError(errorMessage(e)); } });
    return () => { live = false; };
  }, [view.kind, query, manufacturer, status, sourceKind, linked, page, revision]);
  function filter(apply: () => void) { apply(); setPage(1); setError(''); }
  function back() { setView({ kind: 'list' }); setRevision(v => v + 1); }
  const counts = data?.counts;
  const filtered = Boolean(query || manufacturer || status || sourceKind || linked);
  return <Modal title="Biblioteca OEM" wide className="suffix-modal oem-library" onClose={onClose}>
    {view.kind === 'detail' ? <ReferenceDetail key={view.id} id={view.id} initialNotice={view.notice} onBack={back} onOpen={id => setView({ kind: 'detail', id })}/>
      : view.kind === 'add' ? <AddReference onBack={back} onSaved={(id, created) => setView({ kind: 'detail', id, notice: created ? 'Número agregado a la biblioteca.' : 'El número ya estaba en la biblioteca: se unieron la forma escrita y la fuente.' })}/> : <>
        <p className="modal-description">Números OEM guardados como hechos con su procedencia, independientes de los SKU. Cada número se guarda compacto, solo letras y dígitos (17801-30070 → 1780130070), y conserva las formas impresas en sus fuentes. El estado sigue a las fuentes: verificado, declarado o inferido, salvo que lo marques en disputa.</p>
        <div className="oem-library-bar">
          {counts && <p className="suffix-counts" aria-label="Resumen de la biblioteca">BIBLIOTECA: {number(counts.total)} {counts.total === 1 ? 'NÚMERO' : 'NÚMEROS'} · {statuses.map(key => `${number(counts.status[key] ?? 0)} ${counts.status[key] === 1 ? referenceStatusLabels[key] : plural[key]}`).join(' · ')}</p>}
          <button className="button primary" onClick={() => setView({ kind: 'add' })}><Plus size={16}/>Agregar número</button>
        </div>
        <div className="suffix-filters">
          <label className="suffix-search"><Search size={16}/><span className="sr-only">Buscar número OEM</span><UppercaseInput value={search} onChange={e => { setSearch(e.target.value); setError(''); }} placeholder="NÚMERO, FORMA IMPRESA, DESCRIPCIÓN O TIPO" aria-label="Buscar número OEM"/></label>
          <label>FABRICANTE<select aria-label="Fabricante" value={manufacturer} onChange={e => filter(() => setManufacturer(e.target.value))}><option value="">TODOS</option>{Object.keys(counts?.manufacturer ?? {}).sort().map(name => <option key={name} value={name}>{name} ({number(counts!.manufacturer[name])})</option>)}</select></label>
          <label>ESTADO<select aria-label="Estado" value={status} onChange={e => filter(() => setStatus(e.target.value))}><option value="">TODOS</option>{statuses.map(key => <option key={key} value={key}>{referenceStatusLabels[key]}</option>)}</select></label>
          <label>FUENTE<select aria-label="Fuente" value={sourceKind} onChange={e => filter(() => setSourceKind(e.target.value))}><option value="">TODAS</option>{(Object.keys(sourceKindLabels) as OEMSourceKind[]).map(key => <option key={key} value={key}>{sourceKindLabels[key]}</option>)}</select></label>
          <label>SKU VINCULADOS<select aria-label="SKU vinculados" value={linked} onChange={e => filter(() => setLinked(e.target.value))}><option value="">TODOS</option><option value="true">CON SKU</option><option value="false">SIN SKU</option></select></label>
        </div>
        {error && <div className="notice error" role="alert">{error}{!data && <button onClick={() => { setError(''); setRevision(v => v + 1); }}>Intentar de nuevo</button>}</div>}
        {!data && !error && <div className="loading"><LoaderCircle className="spin" size={20}/>Cargando la biblioteca OEM…</div>}
        {data && !data.results.length && <div className="empty-state"><Library size={30}/><h3>{filtered ? 'No hay números con estos filtros.' : 'La biblioteca OEM está vacía.'}</h3><p>{filtered ? 'Prueba con otra búsqueda o quita un filtro.' : 'Los alternos OEM del catálogo aparecen aquí solos; también puedes agregar un número con su fuente.'}</p></div>}
        {!!data?.results.length && <div className="suffix-table-wrap"><table className="suffix-table oem-library-table">
          <thead><tr><th>Número OEM</th><th>Fabricante</th><th>Familia / tipo de pieza</th><th>Estado</th><th>Fuentes</th><th>SKU vinculados</th><th>Acciones</th></tr></thead>
          <tbody>{data.results.map(row => <tr key={row.id}>
            <td data-label="Número OEM"><strong>{row.code}</strong>{!!row.printed_forms.length && <span>{row.printed_forms.join(' · ')}</span>}{row.superseded_by && <span>REEMPLAZADO POR {row.superseded_by.code}</span>}</td>
            <td data-label="Fabricante">{row.manufacturer}{row.system && row.system !== row.manufacturer && <span>SISTEMA {row.system}</span>}</td>
            <td data-label="Familia / tipo de pieza">{row.family || '—'}<span>{row.part_type || 'SIN TIPO DE PIEZA'}</span></td>
            <td data-label="Estado"><Status status={row.status}/></td>
            <td data-label="Fuentes">{row.source_count ? <div className="oem-chips oem-ref-kinds">{(Object.entries(row.source_kinds) as [OEMSourceKind, number][]).map(([kind, count]) => <span key={kind}>{sourceKindLabels[kind]}{count > 1 ? ` × ${count}` : ''}</span>)}</div> : 'SIN FUENTES'}</td>
            <td data-label="SKU vinculados">{row.linked_count ? <>{row.linked_skus.slice(0, 3).map(sku => <span key={sku.id} className="oem-ref-sku">{sku.sku}</span>)}{row.linked_count > 3 && <span>+{number(row.linked_count - 3)} MÁS</span>}</> : '—'}</td>
            <td data-label="Acciones"><button className="button soft small" aria-label={`Ver ${row.manufacturer} ${row.code}`} onClick={() => setView({ kind: 'detail', id: row.id })}>Ver detalle</button></td>
          </tr>)}</tbody>
        </table></div>}
        {data && <div className="pagination"><span>{number(data.count)} {data.count === 1 ? 'resultado' : 'resultados'}</span><div><button className="button soft small" disabled={!data.previous} onClick={() => setPage(page - 1)}>Anterior</button><span>Página {page}</span><button className="button soft small" disabled={!data.next} onClick={() => setPage(page + 1)}>Siguiente</button></div></div>}
        <p className="form-footnote">Los alternos OEM del catálogo (buscador OEM, Revisión OEM, editor de inventario y búsquedas con IA aprobadas) se reflejan aquí solos; al retirar el alterno se retira su fuente.</p>
      </>}
  </Modal>;
}

function AddReference({ onBack, onSaved }: { onBack: () => void; onSaved: (id: string, created: boolean) => void }) {
  const [kind, setKind] = useState<OEMManualSourceKind>('manual'); const [verified, setVerified] = useState(false);
  const [busy, setBusy] = useState(false); const [error, setError] = useState('');
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const values = new FormData(event.currentTarget), text = (key: string) => String(values.get(key) || '').trim();
    const payload: Record<string, unknown> = { manufacturer: text('manufacturer'), code: text('code'), description: text('description'), part_type: text('part_type'), applications: text('applications') };
    if (text('citation')) payload.source = { kind, citation: text('citation'), ...(kind === 'manual' ? { verified } : {}) };
    setBusy(true); setError('');
    try { const result = await create(payload); onSaved(result.row.id, result.created); }
    catch (e) { setError(errorMessage(e)); }
    finally { setBusy(false); }
  }
  return <div className="suffix-editor oem-library-editor">
    <button className="button text small" onClick={onBack}><ArrowLeft size={15}/>Volver a la biblioteca</button>
    <div className="suffix-editor-heading"><h3>Agregar número OEM</h3></div>
    <p className="form-footnote">Escribe el número como aparece en la fuente (17801-30070, MR-968365): se guarda compacto y la forma escrita queda como evidencia. Si el número ya está en la biblioteca, se unen la forma escrita y la fuente sin duplicar nada.</p>
    {error && <div className="notice error" role="alert">{error}</div>}
    <form className="stack-form" onSubmit={submit} aria-label="Agregar número OEM">
      <fieldset disabled={busy}><legend>Número</legend>
        <div className="form-row"><label>Fabricante<UppercaseInput name="manufacturer" required maxLength={120} placeholder="TOYOTA"/></label><label>Número OEM<UppercaseInput name="code" required maxLength={120} placeholder="17801-30070"/></label></div>
        <div className="form-row"><label>Tipo de pieza<UppercaseInput name="part_type" maxLength={60} placeholder="FILTRO AIRE"/></label><label>Descripción<UppercaseInput name="description" maxLength={300}/></label></div>
        <label>Aplicaciones<UppercaseTextarea name="applications" maxLength={4000} rows={2}/></label>
      </fieldset>
      <fieldset disabled={busy}><legend>Primera fuente</legend>
        <div className="form-row"><label>Tipo de fuente<select value={kind} onChange={e => setKind(e.target.value as OEMManualSourceKind)}>{manualSourceKinds.map(key => <option key={key} value={key}>{sourceKindLabels[key]}</option>)}</select></label>
          <label>Cita<input name="citation" required={kind !== 'manual' || verified} maxLength={500} autoComplete="off" placeholder="URL, O DOCUMENTO Y PÁGINA"/></label></div>
        {kind === 'manual' && <label className="admin-checkbox"><input type="checkbox" checked={verified} onChange={e => setVerified(e.target.checked)}/>Lo comprobé en una fuente primaria (catálogo o lista de precios del fabricante)</label>}
        <p className="form-footnote">Sin cita queda un registro manual a tu nombre, en estado declarado.</p>
        <button className="button primary">{busy ? 'Guardando…' : 'Agregar a la biblioteca'}</button>
      </fieldset>
    </form>
  </div>;
}

function ReferenceDetail({ id, initialNotice = '', onBack, onOpen }: { id: string; initialNotice?: string; onBack: () => void; onOpen: (id: string) => void }) {
  const [row, setRow] = useState<OEMReferenceDetail | null>(null); const [error, setError] = useState(''); const [notice, setNotice] = useState(initialNotice);
  const [busy, setBusy] = useState(''); const [retry, setRetry] = useState(0); const [kind, setKind] = useState<OEMManualSourceKind>('manual');
  useEffect(() => { let live = true; request<OEMReferenceDetail>(`${base}/${id}`).then(result => { if (live) setRow(result); }).catch(e => { if (live) setError(errorMessage(e)); }); return () => { live = false; }; }, [id, retry]);
  async function send(action: OEMReferenceAction, payload: Record<string, unknown>, text: string) {
    if (!row) return false;
    setBusy(action); setError(''); setNotice('');
    try { setRow(await request<OEMReferenceDetail>(`${base}/${row.id}`, { method: 'PATCH', body: JSON.stringify({ action, ...payload, expected_version: row.version }) })); setNotice(text); return true; }
    catch (e) {
      const current = e instanceof ApiError && e.status === 409 ? (e.body as OEMReferenceConflict | undefined)?.reference : undefined;
      if (current) setRow(current);
      setError(current ? `${errorMessage(e)} Ya ves la versión actual.` : errorMessage(e)); return false;
    } finally { setBusy(''); }
  }
  async function submit(event: FormEvent<HTMLFormElement>, action: OEMReferenceAction, payload: (values: FormData) => Record<string, unknown>, text: string) {
    event.preventDefault(); const form = event.currentTarget;
    if (await send(action, payload(new FormData(form)), text)) form.reset();
  }
  const value = (values: FormData, key: string) => String(values.get(key) || '').trim();
  const target = row?.superseded_by;
  return <div className="suffix-editor oem-library-editor">
    <button className="button text small" onClick={onBack}><ArrowLeft size={15}/>Volver a la biblioteca</button>
    {error && <div className="notice error" role="alert">{error}{!row && <button onClick={() => { setError(''); setRetry(v => v + 1); }}>Intentar de nuevo</button>}</div>}
    {notice && <div className="notice success" role="status"><Check size={16}/>{notice}</div>}
    {!row && !error && <div className="loading"><LoaderCircle className="spin" size={20}/>Cargando el número…</div>}
    {row && <>
      <div className="suffix-editor-heading"><h3>{row.code}</h3><span className="pill">{row.manufacturer}</span><Status status={row.status}/></div>
      <dl className="oem-evidence">
        <div><dt>FORMAS IMPRESAS</dt><dd>{row.printed_forms.length ? row.printed_forms.join(' · ') : 'SOLO LA FORMA COMPACTA'}</dd></div>
        <div><dt>SISTEMA / FAMILIA</dt><dd>{row.system || 'SIN SISTEMA CONOCIDO'} · {row.family || 'SIN FAMILIA'}</dd></div>
        <div><dt>ESTADO</dt><dd>{row.status_label}<small>{referenceStatusHints[row.status]}</small></dd></div>
        <div><dt>REGISTRO</dt><dd>{row.created_by || 'SISTEMA'} · {stamp(row.created_at)}<small>VERSIÓN {row.version}{row.updated_by ? ` · ÚLTIMO CAMBIO DE ${row.updated_by}` : ''}</small></dd></div>
      </dl>
      <section className="suffix-editor-section" aria-label="Disputa"><h4>Disputa</h4>
        {row.status === 'disputed' ? <>
          <p className="oem-ref-dispute">{row.dispute_note}</p>
          <div><button className="button soft small" disabled={!!busy} onClick={() => void send('clear_dispute', {}, 'Disputa quitada. El estado vuelve a seguir a las fuentes.')}>Quitar disputa</button></div>
        </> : <form className="suffix-inline-form" onSubmit={e => submit(e, 'dispute', values => ({ note: value(values, 'note') }), 'Número marcado en disputa.')}>
          <label>Motivo de la disputa<textarea name="note" required maxLength={2000} rows={2} placeholder="Por qué el número no corresponde"/></label>
          <button className="button soft" disabled={!!busy}>Marcar en disputa</button>
        </form>}
      </section>
      <form key={row.version} className="stack-form" aria-label="Datos del número" onSubmit={e => submit(e, 'edit', values => ({ description: value(values, 'description'), part_type: value(values, 'part_type'), applications: value(values, 'applications'), notes: value(values, 'notes') }), 'Datos del número guardados.')}>
        <fieldset disabled={!!busy}><legend>Datos del número</legend>
          <div className="form-row"><label>Tipo de pieza<UppercaseInput name="part_type" maxLength={60} defaultValue={row.part_type}/></label><label>Descripción<UppercaseInput name="description" maxLength={300} defaultValue={row.description}/></label></div>
          <label>Aplicaciones<UppercaseTextarea name="applications" maxLength={4000} rows={2} defaultValue={row.applications}/></label>
          <label>Notas<textarea name="notes" maxLength={4000} rows={2} defaultValue={row.notes}/></label>
          <button className="button primary">{busy === 'edit' ? 'Guardando…' : 'Guardar datos'}</button>
        </fieldset>
      </form>
      <section className="suffix-editor-section" aria-label="Fuentes"><h4>Fuentes ({number(row.sources.length)})</h4>
        {row.sources.length ? <ul className="oem-ref-sources">{row.sources.map(source => <li key={source.id}>
          <div><strong>{source.kind_label}</strong>
            {source.citation ? isLink(source.citation) ? <a href={source.citation} target="_blank" rel="noopener noreferrer">{source.citation}<ExternalLink size={12} aria-hidden="true"/></a> : <span>{source.citation}</span> : <span>SIN CITA</span>}
            {sourceDetail(source).map(text => <small key={text}>{text}</small>)}
            <small>{source.created_by || 'SISTEMA'} · {stamp(source.created_at)}</small>
          </div>
          {source.removable && <button className="icon-button danger-text" aria-label={`Quitar fuente ${source.kind_label} ${source.citation}`} disabled={!!busy} onClick={() => void send('remove_source', { source_id: source.id }, 'Fuente quitada.')}><Trash2 size={15}/></button>}
        </li>)}</ul> : <p>Sin fuentes: sus alternos del catálogo se retiraron.</p>}
        <form className="stack-form oem-ref-source-form" aria-label="Agregar fuente" onSubmit={e => submit(e, 'add_source', values => ({ source: { kind, citation: value(values, 'citation'), ...(kind === 'manual' ? { verified: values.get('verified') === 'on' } : {}) } }), 'Fuente agregada.')}>
          <div className="form-row"><label>Tipo de fuente<select value={kind} onChange={e => setKind(e.target.value as OEMManualSourceKind)}>{manualSourceKinds.map(key => <option key={key} value={key}>{sourceKindLabels[key]}</option>)}</select></label>
            <label>Cita de la fuente<input name="citation" required maxLength={500} autoComplete="off" placeholder="URL, O DOCUMENTO Y PÁGINA"/></label></div>
          {kind === 'manual' && <label className="admin-checkbox"><input type="checkbox" name="verified"/>Lo comprobé en una fuente primaria</label>}
          <button className="button soft" disabled={!!busy}><Plus size={15}/>Agregar fuente</button>
        </form>
        <p>Las fuentes del catálogo, del buscador OEM y de las búsquedas con IA siguen a sus alternos: se retiran desde el inventario.</p>
      </section>
      <section className="suffix-editor-section" aria-label="SKU vinculados"><h4>SKU vinculados ({number(row.linked_count)})</h4>
        {row.linked_count ? <ul className="oem-ref-skus">{row.linked_skus.map(sku => <li key={sku.id}><strong>{sku.sku}</strong><span>{[linkedViaLabels[sku.via], sku.code !== sku.sku ? `COMO ${sku.code}` : '', sku.is_OEM ? 'MAIN OEM' : '', sku.active ? '' : 'INACTIVO'].filter(Boolean).join(' · ')}</span></li>)}</ul>
          : <p>Ningún SKU del catálogo lleva este número, ni como SKU principal ni como alterno.</p>}
        {row.linked_count > row.linked_skus.length && <p>Se muestran {number(row.linked_skus.length)} de {number(row.linked_count)}.</p>}
      </section>
      <section className="suffix-editor-section" aria-label="Reemplazos"><h4>Reemplazos</h4>
        {target ? <div className="suffix-scope"><span>REEMPLAZADO POR {target.manufacturer} {target.code} · {referenceStatusLabels[target.status]}</span>
          <span className="oem-ref-actions"><button className="button text small" onClick={() => onOpen(target.id)}>Ver {target.code}</button><button className="button text small danger-text" disabled={!!busy} onClick={() => void send('clear_superseded_by', {}, 'Reemplazo quitado.')}>Quitar reemplazo</button></span></div>
          : <form className="suffix-inline-form" aria-label="Indicar reemplazo" onSubmit={e => submit(e, 'set_superseded_by', values => ({ manufacturer: value(values, 'manufacturer'), code: value(values, 'code') }), 'Reemplazo registrado.')}>
            <label>Fabricante del reemplazo<UppercaseInput name="manufacturer" maxLength={120} defaultValue={row.manufacturer}/></label>
            <label>Número que lo reemplaza<UppercaseInput name="code" required maxLength={120} placeholder="17801-30080"/></label>
            <button className="button soft" disabled={!!busy}>Indicar reemplazo</button>
          </form>}
        {row.supersedes.map(item => <div className="suffix-scope" key={item.id}><span>REEMPLAZA A {item.manufacturer} {item.code} · {referenceStatusLabels[item.status]}</span><button className="button text small" onClick={() => onOpen(item.id)}>Ver {item.code}</button></div>)}
        <p>Si el número de reemplazo no está en la biblioteca, se agrega con un registro manual. Un reemplazo nunca puede formar un ciclo.</p>
      </section>
    </>}
  </div>;
}
