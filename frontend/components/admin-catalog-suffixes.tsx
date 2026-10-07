'use client';

import { FormEvent, useEffect, useState } from 'react';
import { ArrowLeft, Check, LoaderCircle, Plus, Search, Tags, Trash2 } from 'lucide-react';
import Modal from './modal';
import { UppercaseInput } from './uppercase-field';
import { request } from '@/lib/types';
import { CatalogSuffix, CatalogSuffixDetail, CatalogSuffixPage, SuffixAction, SuffixClass, actionLabels, classLabels, companyKinds, kindLabel,
  statusLabels, tagKindLabels, variantKindLabels } from '@/lib/suffix-types';

const base = '/api/management/catalog/suffixes';
const detailUrl = (token: string) => `${base}/${encodeURIComponent(token)}`;
const number = (value: number) => value.toLocaleString('es-PA');
const errorMessage = (error: unknown) => error instanceof Error ? error.message : 'No se pudo guardar. Inténtalo de nuevo.';
const done: Record<string, string> = { confirm_tag: 'confirmado como etiqueta', set_variant: 'marcado como variante', set_unknown: 'marcado como desconocido' };

function impact(row: CatalogSuffix) {
  const run = row.stats.run, parts: string[] = [];
  if (run?.unknown_unlock_if_labeled_TAG) parts.push(`BLOQUEA ${number(run.unknown_unlock_if_labeled_TAG.skus_blocked_all)} / DESBLOQUEA ${number(run.unknown_unlock_if_labeled_TAG.unlock_alone_all)}`);
  if (run?.owner_confirmation_effect) parts.push(`AUTO SI CONFIRMA ${number(run.owner_confirmation_effect.auto_if_all_listed_confirmed_all)}`);
  if (run?.chain_rows_all !== undefined) parts.push(`${number(run.chain_rows_all)} EN EL ÚLTIMO ANÁLISIS OEM`);
  return parts.length ? parts : ['SIN ANÁLISIS OEM'];
}

export default function CatalogSuffixes({ onClose, initialSearch = '' }: { onClose: () => void; initialSearch?: string }) {
  const [cls, setCls] = useState(''); const [status, setStatus] = useState(''); const [unlock, setUnlock] = useState(false);
  const [search, setSearch] = useState(initialSearch); const [query, setQuery] = useState(initialSearch); const [page, setPage] = useState(1);
  const [data, setData] = useState<CatalogSuffixPage | null>(null); const [error, setError] = useState(''); const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(''); const [revision, setRevision] = useState(0); const [editing, setEditing] = useState<string | null>(null);
  useEffect(() => { const timer = setTimeout(() => { setQuery(search.trim()); setPage(1); }, 300); return () => clearTimeout(timer); }, [search]);
  useEffect(() => {
    let live = true;
    const params = new URLSearchParams({ page: String(page) });
    if (cls) params.set('class', cls); if (status) params.set('status', status); if (unlock) params.set('has_unlock', 'true'); if (query) params.set('search', query);
    request<CatalogSuffixPage>(`${base}?${params}`).then(result => { if (live) setData(result); }).catch(e => { if (live) { setData(null); setError(errorMessage(e)); } });
    return () => { live = false; };
  }, [cls, status, unlock, query, page, revision]);
  function filter(apply: () => void) { apply(); setPage(1); setError(''); setNotice(''); }
  async function label(row: CatalogSuffix, action: SuffixAction) {
    setBusy(`${row.token}:${action}`); setError(''); setNotice('');
    try { await request(detailUrl(row.token), { method: 'PATCH', body: JSON.stringify({ action, version: row.version }) }); setNotice(`${row.token} ${done[action]}. La próxima revisión OEM usará este cambio.`); setRevision(v => v + 1); }
    catch (e) { setError(errorMessage(e)); setRevision(v => v + 1); }
    finally { setBusy(''); }
  }
  const counts = data?.counts;
  return <Modal title="Sufijos de códigos" wide className="suffix-modal" onClose={onClose}>
    {editing ? <SuffixEditor token={editing} onBack={() => { setEditing(null); setRevision(v => v + 1); }}/> : <>
      <p className="modal-description">Cada sufijo al final de un SKU es una etiqueta (marca, origen o marcador que no cambia la pieza), una variante (cambia o puede cambiar la pieza) o un sufijo desconocido que bloquea la búsqueda del OEM hasta que lo etiquetes.</p>
      {counts && <p className="suffix-counts" aria-label="Resumen de la tabla">TABLA: {number(counts.cls.TAG ?? 0)} ETIQUETAS · {number(counts.cls.VARIANT ?? 0)} VARIANTES · {number(counts.cls.UNKNOWN ?? 0)} DESCONOCIDOS · {number(counts.status.needs_owner_label ?? 0)} POR ETIQUETAR · VERSIÓN {data.version}</p>}
      <div className="suffix-filters">
        <label className="suffix-search"><Search size={16}/><span className="sr-only">Buscar sufijo</span><UppercaseInput value={search} onChange={e => { setSearch(e.target.value); setError(''); }} placeholder="SUFIJO, MARCA O PROPUESTA" aria-label="Buscar sufijo"/></label>
        <label>CLASE<select aria-label="Clase" value={cls} onChange={e => filter(() => setCls(e.target.value))}><option value="">TODAS</option>{(Object.keys(classLabels) as SuffixClass[]).map(key => <option key={key} value={key}>{classLabels[key]}</option>)}</select></label>
        <label>ESTADO<select aria-label="Estado" value={status} onChange={e => filter(() => setStatus(e.target.value))}><option value="">TODOS</option>{Object.entries(statusLabels).map(([key, text]) => <option key={key} value={key}>{text}</option>)}</select></label>
        <label className="admin-checkbox"><input type="checkbox" checked={unlock} onChange={e => filter(() => setUnlock(e.target.checked))}/>SOLO LOS QUE DESBLOQUEAN SKU{counts ? ` (${number(counts.has_unlock)})` : ''}</label>
      </div>
      {data && Object.keys(data.company_suffixes).length > 0 && <p className="form-footnote">Crean referencias de empresa: {Object.entries(data.company_suffixes).map(([token, brand]) => `-${token} → ${brand}`).join(' · ')}.</p>}
      {error && <div className="notice error" role="alert">{error}{!data && <button onClick={() => { setError(''); setRevision(v => v + 1); }}>Intentar de nuevo</button>}</div>}
      {notice && <div className="notice success" role="status"><Check size={16}/>{notice}</div>}
      {!data && !error && <div className="loading"><LoaderCircle className="spin" size={20}/>Cargando sufijos…</div>}
      {data && !data.results.length && <div className="empty-state"><Tags size={30}/><h3>No hay sufijos con estos filtros.</h3><p>Prueba con otra búsqueda o quita un filtro.</p></div>}
      {!!data?.results.length && <div className="suffix-table-wrap"><table className="suffix-table">
        <thead><tr><th>Sufijo</th><th>Clase / tipo</th><th>Atribución</th><th>Estado</th><th>SKU</th><th>Ejemplos</th><th>Impacto</th><th>Acciones</th></tr></thead>
        <tbody>{data.results.map(row => <tr key={row.token}>
          <td data-label="Sufijo"><strong>{row.token}</strong>{row.alias_of && <span>ALIAS DE {row.alias_of}</span>}{!!row.aliases.length && <span>ALIAS: {row.aliases.join(', ')}</span>}{row.match === 'shape' && <span>PATRÓN</span>}</td>
          <td data-label="Clase / tipo"><span className={`suffix-class ${row.cls.toLowerCase()}`}>{classLabels[row.cls]}</span><span>{kindLabel(row)}</span>{!!row.scope_overrides.length && <span>{row.scope_overrides.length} {row.scope_overrides.length === 1 ? 'ALCANCE' : 'ALCANCES'} POR PIEZA</span>}</td>
          <td data-label="Atribución">{row.attribution || '—'}{row.creates_company_reference && row.cls === 'TAG' && <span>CREA REFERENCIA DE EMPRESA</span>}</td>
          <td data-label="Estado"><span className={`status ${row.owner_confirmed || row.status === 'seed_confirmed' ? 'matched' : 'review'}`}>{statusLabels[row.status]}</span>{row.auto_eligible && <span>AUTOMÁTICO</span>}</td>
          <td data-label="SKU" className="number-cell">{number(row.stats.count_all ?? 0)}<span>{number(row.stats.count_in_stock ?? 0)} CON EXISTENCIAS</span></td>
          <td data-label="Ejemplos"><ul className="suffix-examples">{(row.stats.examples ?? []).slice(0, 2).map(example => <li key={example}>{example}</li>)}</ul>{!row.stats.examples?.length && '—'}</td>
          <td data-label="Impacto">{impact(row).map(text => <span key={text}>{text}</span>)}</td>
          <td data-label="Acciones"><div className="suffix-actions">
            <button className="button soft small" aria-label={`Confirmar TAG ${row.token}`} disabled={!!busy || (row.cls === 'TAG' && row.owner_confirmed)} onClick={() => label(row, 'confirm_tag')}>{busy === `${row.token}:confirm_tag` ? <LoaderCircle size={14} className="spin"/> : null}Confirmar TAG</button>
            <button className="button soft small" aria-label={`Es VARIANTE ${row.token}`} disabled={!!busy || (row.cls === 'VARIANT' && row.owner_confirmed)} onClick={() => label(row, 'set_variant')}>Es VARIANTE</button>
            <button className="button text small" aria-label={`Desconocido ${row.token}`} disabled={!!busy || row.cls === 'UNKNOWN'} onClick={() => label(row, 'set_unknown')}>Desconocido</button>
            <button className="button text small" aria-label={`Editar sufijo ${row.token}`} disabled={!!busy} onClick={() => setEditing(row.token)}>Editar</button>
          </div></td>
        </tr>)}</tbody>
      </table></div>}
      {data && <div className="pagination"><span>{number(data.count)} {data.count === 1 ? 'resultado' : 'resultados'}</span><div><button className="button soft small" disabled={!data.previous} onClick={() => setPage(page - 1)}>Anterior</button><span>Página {page}</span><button className="button soft small" disabled={!data.next} onClick={() => setPage(page + 1)}>Siguiente</button></div></div>}
      <p className="form-footnote">Confirmar una etiqueta permite quitarla en los renombrados automáticos a OEM. Cada cambio queda auditado y sube la versión de la tabla.</p>
    </>}
  </Modal>;
}

function SuffixEditor({ token, onBack }: { token: string; onBack: () => void }) {
  const [row, setRow] = useState<CatalogSuffixDetail | null>(null); const [error, setError] = useState(''); const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false); const [retry, setRetry] = useState(0);
  const [cls, setCls] = useState<SuffixClass>('TAG'); const [kind, setKind] = useState(''); const [attribution, setAttribution] = useState(''); const [company, setCompany] = useState(false);
  useEffect(() => { let live = true; request<CatalogSuffixDetail>(detailUrl(token)).then(result => { if (live) loaded(result); }).catch(e => { if (live) setError(errorMessage(e)); }); return () => { live = false; }; }, [token, retry]);
  function loaded(result: CatalogSuffixDetail) { setRow(result); setCls(result.cls); setKind(result.tag_kind || result.variant_kind); setAttribution(result.attribution); setCompany(result.creates_company_reference); }
  async function send(payload: Record<string, unknown>, text: string) {
    if (!row) return false;
    setBusy(true); setError(''); setNotice('');
    try { loaded(await request<CatalogSuffixDetail>(detailUrl(row.token), { method: 'PATCH', body: JSON.stringify({ ...payload, version: row.version }) })); setNotice(text); return true; }
    catch (e) { setError(errorMessage(e)); return false; }
    finally { setBusy(false); }
  }
  function classify(event: FormEvent) {
    event.preventDefault();
    const action = { TAG: 'confirm_tag', VARIANT: 'set_variant', UNKNOWN: 'set_unknown' }[cls];
    const payload: Record<string, unknown> = { action, attribution };
    if (cls === 'TAG') Object.assign(payload, { tag_kind: kind, creates_company_reference: company && companyKinds.includes(kind) });
    if (cls === 'VARIANT') payload.variant_kind = kind;
    void send(payload, 'Clasificación guardada.');
  }
  async function alias(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const form = event.currentTarget;
    if (await send({ action: 'add_alias', alias: new FormData(form).get('alias') }, 'Alias agregado.')) form.reset();
  }
  async function scope(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const form = event.currentTarget, values = new FormData(form);
    const heads = String(values.get('heads') || '').split(',').map(head => head.trim()).filter(Boolean);
    if (await send({ action: 'add_scope_override', heads, cls: values.get('scope_cls'), kind: values.get('scope_kind') || '', attribution: values.get('scope_attribution') || '' }, 'Alcance agregado.')) form.reset();
  }
  const kinds = cls === 'TAG' ? tagKindLabels : cls === 'VARIANT' ? variantKindLabels : {};
  return <div className="suffix-editor">
    <button className="button text small" onClick={onBack}><ArrowLeft size={15}/>Volver a la tabla</button>
    {error && <div className="notice error" role="alert">{error}{!row && <button onClick={() => setRetry(v => v + 1)}>Intentar de nuevo</button>}</div>}
    {notice && <div className="notice success" role="status"><Check size={16}/>{notice}</div>}
    {!row && !error && <div className="loading"><LoaderCircle className="spin" size={20}/>Cargando sufijo…</div>}
    {row && <>
      <div className="suffix-editor-heading"><h3>{row.token}</h3><span className={`suffix-class ${row.cls.toLowerCase()}`}>{classLabels[row.cls]}</span><span className={`status ${row.owner_confirmed || row.status === 'seed_confirmed' ? 'matched' : 'review'}`}>{statusLabels[row.status]}</span></div>
      {row.notes && <p className="form-footnote">{row.notes}</p>}
      {row.proposed_label && <p className="notice">PROPUESTA: {row.proposed_label}</p>}
      <form className="stack-form" onSubmit={classify} aria-label="Clasificación del sufijo"><fieldset disabled={busy}><legend>Clasificación</legend>
        <div className="form-row three"><label>Clase<select value={cls} onChange={e => { const next = e.target.value as SuffixClass; setCls(next); setKind(next === 'TAG' ? 'brand' : next === 'VARIANT' ? 'spec' : ''); }}>{(Object.keys(classLabels) as SuffixClass[]).map(key => <option key={key} value={key}>{classLabels[key]}</option>)}</select></label>
          {cls !== 'UNKNOWN' && <label>Tipo<select value={kind} onChange={e => setKind(e.target.value)}>{Object.entries(kinds).map(([key, text]) => <option key={key} value={key}>{text}</option>)}</select></label>}
          <label>Atribución<input autoComplete="off" maxLength={200} value={attribution} onChange={e => setAttribution(e.target.value)} placeholder="brand: MARCA"/></label></div>
        {cls === 'TAG' && companyKinds.includes(kind) && <label className="admin-checkbox"><input type="checkbox" checked={company} onChange={e => setCompany(e.target.checked)}/>Crea referencia de empresa{kind === 'company_code' ? ': la biblioteca de referencias registra el código sin el sufijo como código de esta empresa' : ': se usará al proponer referencias de empresa en la revisión OEM'}</label>}
        <button className="button primary">{busy ? 'Guardando…' : 'Guardar clasificación'}</button></fieldset></form>
      <section className="suffix-editor-section" aria-label="Alias"><h4>Alias</h4>
        <p>{row.alias_of ? `Este sufijo es un alias de ${row.alias_of}.` : row.aliases.length ? `Otras escrituras: ${row.aliases.join(', ')}.` : 'Sin otras escrituras registradas.'}</p>
        {!row.alias_of && row.match === 'exact' && <p>Un alias es otra escritura del mismo sufijo (letras y números, A/B o dos palabras) y sigue siempre su clasificación.</p>}
        {row.match === 'exact' && <form className="suffix-inline-form" onSubmit={alias}><label>Nuevo alias<UppercaseInput name="alias" required maxLength={60} placeholder="EJ.: HELMM"/></label><button className="button soft" disabled={busy}><Plus size={15}/>Agregar alias</button></form>}
      </section>
      <section className="suffix-editor-section" aria-label="Alcances por tipo de pieza"><h4>Alcances por tipo de pieza</h4>
        <p>El mismo sufijo puede significar otra cosa según el sustantivo inicial de la descripción (por ejemplo, DAI en CASQ o IN en BATERIA). Los sustantivos se guardan con la abreviatura del catálogo (BOBINA → COIL).</p>
        {row.scope_overrides.map((item, index) => <div className="suffix-scope" key={index}><span>{item.heads.join(', ')} → {classLabels[item.cls]}{item.kind ? ` · ${tagKindLabels[item.kind] || variantKindLabels[item.kind] || item.kind}` : ''}{item.attribution ? ` · ${item.attribution}` : ''}</span><button className="icon-button danger-text" aria-label={`Quitar alcance ${item.heads.join(', ')}`} disabled={busy} onClick={() => void send({ action: 'remove_scope_override', index }, 'Alcance quitado.')}><Trash2 size={15}/></button></div>)}
        {row.match === 'exact' && <ScopeForm busy={busy} onSubmit={scope}/>}
      </section>
      <section className="suffix-editor-section" aria-label="Historial"><h4>Historial</h4>
        {row.changes.length ? <ul className="suffix-history">{row.changes.map((change, index) => <li key={index}><strong>{actionLabels[change.action] || change.action}</strong><span>{change.actor || 'SISTEMA'} · {new Date(change.created_at).toLocaleString('es-PA', { timeZone: 'America/Panama' })}{change.note ? ` · ${change.note}` : ''}</span></li>)}</ul> : <p>Sin cambios registrados.</p>}
      </section>
    </>}
  </div>;
}

function ScopeForm({ busy, onSubmit }: { busy: boolean; onSubmit: (event: FormEvent<HTMLFormElement>) => void }) {
  const [cls, setCls] = useState<SuffixClass>('TAG');
  const kinds = cls === 'TAG' ? tagKindLabels : cls === 'VARIANT' ? variantKindLabels : {};
  return <form className="stack-form suffix-scope-form" onSubmit={onSubmit}>
    <label>Sustantivos (separados por comas)<UppercaseInput name="heads" required maxLength={300} placeholder="BATERIA, BUJE BIELA"/></label>
    <div className="form-row three"><label>Clase en el alcance<select name="scope_cls" value={cls} onChange={e => setCls(e.target.value as SuffixClass)}>{(Object.keys(classLabels) as SuffixClass[]).map(key => <option key={key} value={key}>{classLabels[key]}</option>)}</select></label>
      {cls !== 'UNKNOWN' && <label>Tipo en el alcance<select name="scope_kind" key={cls}>{Object.entries(kinds).map(([key, text]) => <option key={key} value={key}>{text}</option>)}</select></label>}
      <label>Atribución en el alcance<input name="scope_attribution" autoComplete="off" maxLength={200}/></label></div>
    <button className="button soft" disabled={busy}><Plus size={15}/>Agregar alcance</button>
  </form>;
}
