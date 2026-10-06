'use client';

import { FormEvent, useEffect, useState } from 'react';
import { Check, FileWarning, LoaderCircle } from 'lucide-react';
import Modal from './modal';
import { UppercaseInput, UppercaseTextarea } from './uppercase-field';
import { CatalogImportIssue, CatalogImportIssuePage, CatalogImportIssueResult, CatalogImportIssueValues, request } from '@/lib/types';

const issueUrl = (id: string) => `/api/management/catalog/import/issues/${encodeURIComponent(id)}`;

export default function ImportIssues({ onClose, onSaved }: { onClose: () => void; onSaved: () => void }) {
  const [data, setData] = useState<CatalogImportIssuePage | null>(null);
  const [status, setStatus] = useState<'pending' | 'resolved'>('pending');
  const [page, setPage] = useState(1);
  const [revision, setRevision] = useState(0);
  const [editing, setEditing] = useState<CatalogImportIssue | null>(null);
  const [loadingId, setLoadingId] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState('');
  const [error, setError] = useState('');

  useEffect(() => {
    let cancelled = false;
    setData(null); setError('');
    request<CatalogImportIssuePage>(`/api/management/catalog/import/issues?status=${status}&page=${page}`).then(result => {
      if (cancelled) return;
      if (!result.results.length && page > 1) { setPage(value => value - 1); return; }
      setData(result);
    }).catch(error => { if (!cancelled) setError(error instanceof Error ? error.message : 'No se pudieron cargar los errores de importación.'); });
    return () => { cancelled = true; };
  }, [status, page, revision]);

  async function edit(issue: CatalogImportIssue) {
    if (loadingId || saving) return;
    setLoadingId(issue.id); setError(''); setNotice('');
    try { setEditing(await request<CatalogImportIssue>(issueUrl(issue.id))); }
    catch (error) { setError(error instanceof Error ? error.message : 'No se pudo abrir esta fila.'); }
    finally { setLoadingId(null); }
  }

  return <Modal title="Errores de importación" wide onClose={() => { if (!saving && !loadingId) onClose(); }}>
    <p className="modal-description">Las filas con errores se guardan aquí para corregirlas después. Puedes recuperar y reintentar cada fila sin volver a cargar el archivo Excel.</p>
    {notice && <div className="notice success" role="status"><Check size={16}/>{notice}</div>}
    {error && <div className="notice error" role="alert">{error}<button type="button" onClick={() => setRevision(value => value + 1)}>Intentar de nuevo</button></div>}
    {editing ? <IssueEditor key={editing.id} issue={editing} onBack={() => setEditing(null)} onBusyChange={setSaving} onSaved={result => {
      setEditing(null); setRevision(value => value + 1); setNotice(`Fila ${result.issue.row} corregida y guardada en el inventario interno.`); onSaved();
    }}/> : <>
      <div className="import-issues-toolbar">
        <div className="admin-inventory-tabs" role="group" aria-label="Estado de errores de importación"><button className={status === 'pending' ? 'selected' : ''} aria-pressed={status === 'pending'} onClick={() => { setStatus('pending'); setPage(1); }}>Pendientes</button><button className={status === 'resolved' ? 'selected' : ''} aria-pressed={status === 'resolved'} onClick={() => { setStatus('resolved'); setPage(1); }}>Corregidos</button></div>
        {data && <p role="status">{data.pending_count} {data.pending_count === 1 ? 'fila pendiente' : 'filas pendientes'} de corregir</p>}
      </div>
      {!data && !error && <div className="loading"><LoaderCircle size={20} className="spin"/>Cargando errores de importación…</div>}
      {data && !data.results.length && <div className="empty-state"><FileWarning size={30}/><h3>{status === 'pending' ? 'No hay filas pendientes de corregir.' : 'Todavía no hay filas corregidas.'}</h3><p>{status === 'pending' ? 'Los errores de futuras importaciones aparecerán aquí.' : 'Las filas que guardes después de corregir aparecerán aquí.'}</p></div>}
      {!!data?.results.length && <div className="table-scroll"><table className="admin-catalog-table import-issues-table"><thead><tr><th>Archivo / fila</th><th>SKU / descripción</th><th>Errores</th><th>Acciones</th></tr></thead><tbody>{data.results.map(issue => <tr key={issue.id}>
        <td><strong>{issue.filename}</strong><span>Fila de Excel {issue.row}</span></td>
        <td><strong>{issue.values.sku || 'SKU por corregir'}</strong><span>{issue.values.description || issue.values.name || 'Sin descripción'}</span></td>
        <td>{issue.status === 'resolved' ? <span className="status matched">Corregida</span> : <div className="import-issue-errors">{issue.errors.map((item, index) => <p key={index}><strong>{item.column}</strong> {item.message}</p>)}</div>}</td>
        <td><button className="button soft small" type="button" aria-label={`${issue.status === 'resolved' ? 'Ver' : 'Corregir'} fila ${issue.row}`} disabled={!!loadingId} onClick={() => edit(issue)}>{loadingId === issue.id ? <><LoaderCircle size={15} className="spin"/>Cargando…</> : issue.status === 'resolved' ? 'Ver fila' : 'Corregir'}</button></td>
      </tr>)}</tbody></table></div>}
      {data && <div className="pagination"><span>{data.count} {data.count === 1 ? 'fila' : 'filas'}</span><div><button className="button soft small" disabled={!data.previous || !!loadingId} onClick={() => setPage(value => value - 1)}>Anterior</button><span>Página {page}</span><button className="button soft small" disabled={!data.next || !!loadingId} onClick={() => setPage(value => value + 1)}>Siguiente</button></div></div>}
    </>}
  </Modal>;
}

function IssueEditor({ issue, onBack, onBusyChange, onSaved }: { issue: CatalogImportIssue; onBack: () => void; onBusyChange: (busy: boolean) => void; onSaved: (result: CatalogImportIssueResult) => void }) {
  const [values, setValues] = useState<CatalogImportIssueValues>(() => {
    const initial = Object.fromEntries(Object.entries(issue.values).map(([key, value]) => [key, String(value ?? '')])) as CatalogImportIssueValues;
    if (['TRUE', '1'].includes(initial.active)) initial.active = 'SI';
    if (['FALSE', '0'].includes(initial.active)) initial.active = 'NO';
    return initial;
  });
  const [errors, setErrors] = useState(issue.status === 'pending' ? issue.errors : []);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const resolved = issue.status === 'resolved';
  const awaitingImport = issue.can_repair == null ? issue.job_status === 'ready' || issue.job_status === 'importing' : !issue.can_repair;
  function update(field: keyof CatalogImportIssueValues, value: string) { setValues(current => ({ ...current, [field]: value })); }
  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy || resolved) return;
    setBusy(true); onBusyChange(true); setError('');
    try {
      const result = await request<CatalogImportIssueResult>(issueUrl(issue.id), { method: 'POST', body: JSON.stringify({ values }) });
      if (result.valid) onSaved(result);
      else { setErrors(result.errors || result.issue.errors); setError('La fila sigue pendiente. Revisa los errores y vuelve a guardar.'); }
    } catch (error) { setError(error instanceof Error ? error.message : 'No se pudo guardar esta fila. Inténtalo de nuevo.'); }
    finally { setBusy(false); onBusyChange(false); }
  }
  return <section className="import-issue-editor catalog-editor" aria-label={`Corrección de fila ${issue.row}`}>
    <div className="import-issue-heading"><div><h3>Fila {issue.row}</h3><p>{issue.filename}</p></div><button className="button soft small" type="button" disabled={busy} onClick={onBack}>Volver a errores</button></div>
    {resolved && <div className="notice success" role="status">Esta fila ya fue corregida y guardada.</div>}
    {awaitingImport && <div className="notice" role="status">Termina la importación de las filas válidas de este archivo para guardar esta corrección.</div>}
    {error && <div className="notice error" role="alert">{error}</div>}
    {errors.length > 0 && <div className="notice import-issue-errors">{errors.map((item, index) => <p key={index}><strong>{item.column}:</strong> {item.message}</p>)}</div>}
    <form className="stack-form" autoComplete="off" onSubmit={save}>
      <div className="form-grid">
        <label>SKU interno<UppercaseInput name={`import-issue-${issue.id}-sku`} required maxLength={200} value={values.sku} disabled={busy || resolved} onChange={event => update('sku', event.target.value)}/></label>
        <label>Nombre del repuesto<UppercaseInput name={`import-issue-${issue.id}-name`} maxLength={200} value={values.name} disabled={busy || resolved} onChange={event => update('name', event.target.value)}/></label>
      </div>
      <label>Descripción<UppercaseTextarea name={`import-issue-${issue.id}-description`} rows={3} maxLength={10000} value={values.description} disabled={busy || resolved} onChange={event => update('description', event.target.value)}/></label>
      {issue.recovery?.length > 0 && <div className="import-issue-suggestions"><p>Sugerencias de códigos recuperados de Excel</p>{issue.recovery.map(item => <div key={`${item.row}:${item.column}`}><span>{item.column === 'SKU' ? 'SKU interno' : 'Código alterno'}:</span>{item.candidates.map(candidate => <button className="button soft small" type="button" key={candidate} disabled={busy || resolved} onClick={() => update(item.column === 'SKU' ? 'sku' : 'code', candidate)}>{candidate}</button>)}<small>{item.reason}</small></div>)}</div>}
      <div className="form-grid">
        <label>Código alterno<UppercaseInput name={`import-issue-${issue.id}-code`} maxLength={120} value={values.code} disabled={busy || resolved} onChange={event => update('code', event.target.value)}/></label>
        <label>Marca del código alterno<UppercaseInput name={`import-issue-${issue.id}-brand`} maxLength={120} value={values.brand} disabled={busy || resolved} onChange={event => update('brand', event.target.value)}/></label>
        <label>Categoría<UppercaseInput name={`import-issue-${issue.id}-category`} maxLength={120} value={values.category} disabled={busy || resolved} onChange={event => update('category', event.target.value)}/></label>
        <label>Subcategoría<UppercaseInput name={`import-issue-${issue.id}-subcategory`} maxLength={120} value={values.subcategory} disabled={busy || resolved} onChange={event => update('subcategory', event.target.value)}/></label>
      </div>
      <label>Estado del repuesto<select name={`import-issue-${issue.id}-active`} value={values.active} disabled={busy || resolved} autoComplete="off" onChange={event => update('active', event.target.value)}><option value="">Conservar estado actual (nuevos: activo)</option><option value="SI">ACTIVO</option><option value="NO">INACTIVO</option>{!['', 'SI', 'NO'].includes(values.active) && <option value={values.active}>REVISAR: {values.active}</option>}</select></label>
      <details className="import-issue-original"><summary>Ver datos originales de Excel</summary><dl>{Object.entries(issue.original).map(([key, value]) => {
        const cell = value && typeof value === 'object' ? value as { value?: unknown; number_format?: string } : null;
        const original = cell ? cell.value : value;
        return <div key={key}><dt>{{ sku: 'SKU', name: 'Nombre', description: 'Descripción', code: 'Código alterno', brand: 'Marca del código', active: 'Activo', category: 'Categoría', subcategory: 'Subcategoría' }[key] || key}</dt><dd>{original == null || original === '' ? 'Vacío' : String(original)}{cell?.number_format && <small>Formato de Excel: {cell.number_format}</small>}</dd></div>;
      })}</dl></details>
      {!resolved && <><p className="form-footnote">La fila se guardará en el inventario interno cuando todos sus datos sean válidos. Las filas ya importadas permanecen registradas.</p><button className="button primary full" disabled={busy || awaitingImport || !values.sku.trim()}>{busy ? <><LoaderCircle size={16} className="spin"/>Guardando…</> : 'Guardar y reintentar'}</button></>}
    </form>
  </section>;
}
