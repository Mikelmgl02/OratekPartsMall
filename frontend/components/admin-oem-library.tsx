'use client';

import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { CellValueChangedEvent, ColDef, GridApi, SuppressKeyboardEventParams } from 'ag-grid-community';
import type { CustomCellRendererProps } from 'ag-grid-react';
import ServerGrid from './server-grid';
import { Check, ExternalLink, Library, LoaderCircle, PencilLine, Plus, Search, Trash2 } from 'lucide-react';
import Modal from './modal';
import { UppercaseInput, UppercaseTextarea } from './uppercase-field';
import { ApiError, request } from '@/lib/types';
import { useGridEdits } from '@/lib/use-grid-edits';
import { OEMAlternosEditor, OEMGridContext, OEMStatusEditor } from './admin-oem-cell-editors';
import { OEMManualSourceKind, OEMReference, OEMReferenceAction, OEMReferenceConflict, OEMReferenceDetail, OEMReferencePage, OEMReferenceSource, OEMReferenceStatus,
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

// The side panel over the library: the grid underneath stays mounted, so its scroll position and rows are where they were.
type Panel = { kind: 'add' } | { kind: 'detail'; id: string; notice?: string };
type Cell = CustomCellRendererProps<OEMReference, unknown, OEMGridContext>;
// A row field and the column that shows it: what flashes when a fresh row comes in.
const oemFieldColumns: Record<string, string> = { description: 'description', part_type: 'family', family: 'family', status: 'status', status_label: 'status',
  dispute_note: 'status', cross_reference_count: 'cross', cross_reference_preview: 'cross', linked_count: 'linked', linked_skus: 'linked', source_count: 'sources',
  source_kinds: 'sources', manufacturer: 'manufacturer', system: 'manufacturer', code: 'code', printed_forms: 'code', superseded_by: 'code' };
// Enter and Tab belong to a popup's own fields while it is open (the grid would otherwise close it and move to another cell).
const keepKeys = ({ editing, event }: SuppressKeyboardEventParams<OEMReference>) => editing && (event.key === 'Enter' || event.key === 'Tab');

function CodeCell({ data }: Cell) {
  return data ? <div className="admin-grid-stack"><strong>{data.code}</strong>{!!data.printed_forms.length && <small>{data.printed_forms.join(' · ')}</small>}{data.superseded_by && <small>REEMPLAZADO POR {data.superseded_by.code}</small>}</div> : null;
}
function MakerCell({ data }: Cell) {
  return data ? <div className="admin-grid-stack"><strong>{data.manufacturer}</strong>{data.system && data.system !== data.manufacturer && <small>SISTEMA {data.system}</small>}</div> : null;
}
function FamilyCell({ data }: Cell) {
  return data ? <div className="admin-grid-stack"><strong>{data.family || '—'}</strong><small>{data.part_type || 'SIN TIPO DE PIEZA'}</small></div> : null;
}
function SourcesCell({ data }: Cell) {
  if (!data) return null;
  return data.source_count ? <div className="oem-chips oem-ref-kinds">{(Object.entries(data.source_kinds) as [OEMSourceKind, number][]).map(([kind, count]) => <span key={kind}>{sourceKindLabels[kind]}{count > 1 ? ` × ${count}` : ''}</span>)}</div> : <span className="admin-grid-muted">SIN FUENTES</span>;
}
function LinkedCell({ data }: Cell) {
  if (!data) return null;
  return data.linked_count ? <div className="admin-grid-chips oem-linked">{data.linked_skus.slice(0, 3).map(sku => <span key={sku.id} className="oem-ref-sku">{sku.sku}</span>)}{data.linked_count > 3 && <span className="more">+{number(data.linked_count - 3)} MÁS</span>}</div> : <span className="admin-grid-muted">—</span>;
}
function CrossCell({ data }: Cell) {
  if (!data) return null;
  const hidden = data.cross_reference_count - data.cross_reference_preview.length;
  return data.cross_reference_count ? <div className="admin-grid-chips">{data.cross_reference_preview.map(code => <span key={code.id} className="company">{code.brand} {code.code}</span>)}{hidden > 0 && <span className="more">+{number(hidden)} MÁS</span>}</div> : <span className="admin-grid-muted">Sin alternos</span>;
}
function OpenCell({ data, context }: Cell) {
  return data ? <div className="admin-grid-actions"><button className="button soft small" aria-label={`Ver ${data.manufacturer} ${data.code}`} onClick={() => context.open(data.id)}>Ver detalle</button></div> : null;
}
const libraryOrdering: Record<string, string> = { code: 'code', manufacturer: 'manufacturer', family: 'family', status: 'status', description: 'description' };
// edit: Editar celdas is on (the library's part types, and whether a batch is being saved); null keeps the grid read-only. Part type and
// description save in batches (lib/use-grid-edits), status and alternos in their popups.
type OEMEdit = { partTypes: string[]; isSaving: () => boolean };
function libraryColumns(wide: boolean, edit: OEMEdit | null = null): ColDef<OEMReference>[] {
  const editing = edit !== null;
  const editable = () => editing && !edit.isSaving();
  const cue = { 'editable-cell-active': editable };
  const title = (text: string) => editing ? `${text} ✎` : text;
  return [
    { colId: 'code', headerName: 'Número OEM', width: 210, minWidth: 160, pinned: wide ? 'left' : undefined, cellRenderer: CodeCell, tooltipValueGetter: ({ data }) => data?.printed_forms.join(' · ') },
    { colId: 'manufacturer', headerName: 'Fabricante', width: 150, cellRenderer: MakerCell },
    // The number's aftermarket alternos and the SKUs that carry it come first, where they show without scrolling on a laptop.
    // Always opens (read-only without Editar celdas) so every code can be read and copied.
    { colId: 'cross', headerName: title('Alternos'), headerTooltip: 'Códigos de posventa que los catálogos de marca de repuesto imprimen para este número', flex: 1, minWidth: 230,
      cellRenderer: CrossCell, tooltipValueGetter: ({ data }) => data?.cross_reference_preview.map(code => `${code.brand} ${code.code}`).join(' · '),
      valueGetter: ({ data }) => data?.cross_reference_count ?? 0, valueSetter: () => false, cellClassRules: cue, editable: true, cellEditor: OEMAlternosEditor,
      cellEditorPopup: true, cellEditorPopupPosition: 'under', cellEditorParams: { readOnly: !editing }, suppressKeyboardEvent: keepKeys },
    { colId: 'linked', headerName: 'SKU vinculados', flex: 1, minWidth: 200, cellRenderer: LinkedCell, tooltipValueGetter: ({ data }) => data?.linked_skus.map(sku => sku.sku).join(' · ') },
    // The part type, one of the library's (a paste of anything else is ignored; a new type is written in the number's detail), or blank.
    { colId: 'family', headerName: title('Familia / tipo de pieza'), flex: 1, minWidth: 190, cellRenderer: FamilyCell, valueGetter: ({ data }) => data?.part_type ?? '',
      cellDataType: false, cellClassRules: cue, editable,
      valueSetter: ({ data, newValue }) => {
        const value = String(newValue ?? '').trim().toUpperCase();
        if ((value && !edit?.partTypes.includes(value)) || data.part_type === value) return false;
        data.part_type = value;
        return true;
      },
      cellEditor: 'agRichSelectCellEditor', cellEditorPopup: true,
      cellEditorParams: { values: ['', ...(edit?.partTypes ?? [])], allowTyping: true, filterList: true, searchType: 'matchAny', highlightMatch: true, valueListMaxHeight: 320,
                          formatValue: (value: string) => value || 'SIN TIPO DE PIEZA' } },
    { colId: 'description', headerName: title('Descripción'), field: 'description', flex: 1.2, minWidth: 220, cellClass: 'admin-grid-text', cellClassRules: cue,
      valueFormatter: ({ value }) => value || '—', tooltipField: 'description', cellDataType: false, editable, cellEditor: 'agTextCellEditor' },
    { colId: 'status', headerName: title('Estado'), width: 130, cellRenderer: ({ data }: Cell) => data ? <Status status={data.status}/> : null,
      valueGetter: ({ data }) => data?.status ?? '', valueSetter: () => false, cellClassRules: cue, editable, cellEditor: OEMStatusEditor, cellEditorPopup: true,
      cellEditorPopupPosition: 'under', suppressKeyboardEvent: keepKeys },
    { colId: 'sources', headerName: 'Fuentes', flex: 1, minWidth: 220, cellRenderer: SourcesCell },
    { colId: 'open', headerName: 'Acciones', width: 140, pinned: wide ? 'right' : undefined, cellRenderer: OpenCell, suppressHeaderMenuButton: true, resizable: false },
  ];
}

export default function OEMLibrary({ onClose }: { onClose: () => void }) {
  return <Modal title="Biblioteca OEM" wide className="suffix-modal oem-library" onClose={onClose}><OEMLibraryPanel/></Modal>;
}

// The library body: the Inventario toolbar shows it in a modal, the OEM admin section inline.
export function OEMLibraryPanel() {
  const [panel, setPanel] = useState<Panel | null>(null);
  const [revision, setRevision] = useState(0);
  const [search, setSearch] = useState(''); const [query, setQuery] = useState(''); const [manufacturer, setManufacturer] = useState(''); const [status, setStatus] = useState('');
  const [sourceKind, setSourceKind] = useState(''); const [linked, setLinked] = useState('');
  const [counts, setCounts] = useState<OEMReferencePage['counts'] | null>(null);
  const [wide] = useState(() => typeof window === 'undefined' || window.innerWidth >= 900);
  // Editar celdas, as in Inventario (lib/use-grid-edits): part types and descriptions save together under the version each row showed,
  // the saved cells flash, a refused batch puts every cell back, and the rows on screen are re-read so changes made elsewhere arrive.
  const [editing, setEditing] = useState(false);
  const [partTypes, setPartTypes] = useState<string[] | null>(null);
  const grid = useRef<GridApi<OEMReference> | null>(null);
  const reread = useCallback((ids: string[]) => request<OEMReferencePage>(`${base}?ids=${ids.join(',')}`).then(page => page.results), []);
  const save = useCallback((rows: Record<string, unknown>[]) => request<{ updated: OEMReference[] }>(`${base}/bulk-edit`, { method: 'POST', body: JSON.stringify({ rows }) })
    .then(result => result.updated), []);
  const { status: cellStatus, setStatus: setCellStatus, isSaving, refreshRows, queue, flush } = useGridEdits<OEMReference>({ grid, fieldColumns: oemFieldColumns,
    reread, save, cueColumns: ['cross', 'family', 'description', 'status'], poll: true, noun: { one: 'número', many: 'números' } });
  useEffect(() => { const timer = setTimeout(() => setQuery(search.trim()), 300); return () => clearTimeout(timer); }, [search]);
  useEffect(() => {
    if (!editing || partTypes) return;
    let live = true;
    request<{ results: string[] }>(`${base}/part-types`).then(result => { if (live) setPartTypes(result.results); })
      .catch(error => { if (live) { setEditing(false); setCellStatus({ kind: 'error', text: errorMessage(error) }); } });
    return () => { live = false; };
  }, [editing, partTypes, setCellStatus]);
  const params = useMemo(() => ({ q: query, manufacturer, status, source_kind: sourceKind, linked }), [query, manufacturer, status, sourceKind, linked]);
  const edit = useMemo(() => editing && partTypes ? { partTypes, isSaving } : null, [editing, partTypes, isSaving]);
  const columns = useMemo(() => libraryColumns(wide, edit), [wide, edit]);
  const open = useMemo<OEMGridContext>(() => ({ open: (id: string) => setPanel({ kind: 'detail', id }),
    onRowChanged: (id: string) => { refreshRows([id]).catch(error => setCellStatus({ kind: 'error', text: errorMessage(error) })); } }), [refreshRows, setCellStatus]);
  const onCellValueChanged = useCallback((event: CellValueChangedEvent<OEMReference>) => {
    if (!['edit', 'paste', 'undo', 'redo'].includes(String(event.source)) || !event.data) return;  // grid patches never save back
    const row = event.data, column = event.column.getColId();
    if (column === 'family') queue(row.id, { expected_version: row.version, part_type: row.part_type }, { part_type: event.oldValue ?? '' });
    else if (column === 'description') queue(row.id, { expected_version: row.version, description: row.description }, { description: event.oldValue ?? '' });
  }, [queue]);
  const gridProps = useMemo(() => ({ onCellValueChanged, stopEditingWhenCellsLoseFocus: true, undoRedoCellEditing: true, undoRedoCellEditingLimit: 20,
    onGridPreDestroyed: () => { grid.current = null; } }), [onCellValueChanged]);
  function toggleEditing() {
    grid.current?.stopEditing();
    void flush();
    setEditing(value => !value); setCellStatus(null);
  }
  function filter(apply: () => void) { apply(); }
  const rowChanged = useCallback((id: string) => { refreshRows([id]).catch(() => 0); }, [refreshRows]);
  const filtered = Boolean(query || manufacturer || status || sourceKind || linked);
  return <div className="oem-panel">
        <p className="modal-description">Números OEM guardados como hechos con su procedencia, independientes de los SKU. Cada número se guarda compacto, solo letras y dígitos (17801-30070 → 1780130070), y conserva las formas impresas en sus fuentes. El estado sigue a las fuentes: verificado, declarado o inferido, salvo que lo marques en disputa.</p>
        <div className="oem-library-bar">
          {counts && <p className="suffix-counts" aria-label="Resumen de la biblioteca">BIBLIOTECA: {number(counts.total)} {counts.total === 1 ? 'NÚMERO' : 'NÚMEROS'} · {statuses.map(key => `${number(counts.status[key] ?? 0)} ${counts.status[key] === 1 ? referenceStatusLabels[key] : plural[key]}`).join(' · ')}</p>}
          <button className="button primary" onClick={() => setPanel({ kind: 'add' })}><Plus size={16}/>Agregar número</button>
        </div>
        <div className="suffix-filters">
          <label className="suffix-search"><Search size={16}/><span className="sr-only">Buscar número OEM</span><UppercaseInput value={search} onChange={e => setSearch(e.target.value)} placeholder="NÚMERO, FORMA IMPRESA, DESCRIPCIÓN, TIPO O CÓDIGO DE REPUESTO" aria-label="Buscar número OEM"/></label>
          <label>FABRICANTE<select aria-label="Fabricante" value={manufacturer} onChange={e => filter(() => setManufacturer(e.target.value))}><option value="">TODOS</option>{Object.keys(counts?.manufacturer ?? {}).sort().map(name => <option key={name} value={name}>{name} ({number(counts!.manufacturer[name])})</option>)}</select></label>
          <label>ESTADO<select aria-label="Estado" value={status} onChange={e => filter(() => setStatus(e.target.value))}><option value="">TODOS</option>{statuses.map(key => <option key={key} value={key}>{referenceStatusLabels[key]}</option>)}</select></label>
          <label>FUENTE<select aria-label="Fuente" value={sourceKind} onChange={e => filter(() => setSourceKind(e.target.value))}><option value="">TODAS</option>{(Object.keys(sourceKindLabels) as OEMSourceKind[]).map(key => <option key={key} value={key}>{sourceKindLabels[key]}</option>)}</select></label>
          <label>SKU VINCULADOS<select aria-label="SKU vinculados" value={linked} onChange={e => filter(() => setLinked(e.target.value))}><option value="">TODOS</option><option value="true">CON SKU</option><option value="false">SIN SKU</option></select></label>
          <button type="button" className={`inventory-edit-toggle${editing ? ' selected' : ''}`} aria-pressed={editing} onClick={toggleEditing}><PencilLine size={15} aria-hidden="true"/>{editing ? 'Editando celdas' : 'Editar celdas'}</button>
        </div>
        {editing && <p className="inventory-edit-hint">{partTypes ? 'Escribe sobre una celda punteada o haz doble clic; también puedes pegar desde Excel. Tipo de pieza y descripción se guardan juntos al soltar y las celdas que cambian destellan; si otra persona cambió el número, se recarga sin guardar. Esc cancela, Ctrl+Z deshace. Doble clic en Alternos para agregar o retirar códigos, y en Estado para marcar o quitar una disputa.' : 'Cargando tipos de pieza…'}
          {cellStatus && cellStatus.kind !== 'error' && <span className={`inventory-edit-status ${cellStatus.kind}`} role="status">{cellStatus.text}</span>}</p>}
        {cellStatus?.kind === 'error' && <div className="notice error" role="alert">{cellStatus.text}<button type="button" onClick={() => setCellStatus(null)}>Cerrar</button></div>}
        {/* A new storage key: a layout saved before Alternos moved forward would keep it off screen. */}
        <ServerGrid<OEMReference> storageKey="admin-oem-library-2" label="Números OEM" path={base} params={params} columns={columns} rowId={row => row.id}
          ordering={libraryOrdering} revision={revision} rowHeight={66} context={open} onPage={page => setCounts((page as OEMReferencePage).counts)} className="suffix-table-wrap oem-library-table"
          gridProps={gridProps} onReady={api => { grid.current = api; }}
          empty={<div className="empty-state"><Library size={30}/><h3>{filtered ? 'No hay números con estos filtros.' : 'La biblioteca OEM está vacía.'}</h3><p>{filtered ? 'Prueba con otra búsqueda o quita un filtro.' : 'Los alternos OEM del catálogo aparecen aquí solos; también puedes agregar un número con su fuente.'}</p></div>}/>
        <p className="form-footnote">Los alternos OEM del catálogo (buscador OEM, Revisión OEM, editor de inventario y búsquedas con IA aprobadas) se reflejan aquí solos; al retirar el alterno se retira su fuente. Los alternos de posventa vienen de los catálogos de marca de repuesto y pertenecen al número: cada SKU vinculado los muestra.</p>
    {panel && <Modal title={panel.kind === 'add' ? 'Agregar número OEM' : 'Número OEM'} drawer className="side-panel" onClose={() => setPanel(null)}>
      {panel.kind === 'detail' ? <ReferenceDetail key={panel.id} id={panel.id} initialNotice={panel.notice} onOpen={id => setPanel({ kind: 'detail', id })} onChanged={rowChanged}/>
        : <AddReference onSaved={(id, created) => { setRevision(value => value + 1); setPanel({ kind: 'detail', id, notice: created ? 'Número agregado a la biblioteca.' : 'El número ya estaba en la biblioteca: se unieron la forma escrita y la fuente.' }); }}/>}
    </Modal>}
  </div>;
}

function AddReference({ onSaved }: { onSaved: (id: string, created: boolean) => void }) {
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

// One number in the side panel; every change it saves refreshes the number's row in the grid underneath (onChanged).
function ReferenceDetail({ id, initialNotice = '', onOpen, onChanged }: { id: string; initialNotice?: string; onOpen: (id: string) => void; onChanged: (id: string) => void }) {
  const [row, setRow] = useState<OEMReferenceDetail | null>(null); const [error, setError] = useState(''); const [notice, setNotice] = useState(initialNotice);
  const [busy, setBusy] = useState(''); const [retry, setRetry] = useState(0); const [kind, setKind] = useState<OEMManualSourceKind>('manual');
  useEffect(() => { let live = true; request<OEMReferenceDetail>(`${base}/${id}`).then(result => { if (live) setRow(result); }).catch(e => { if (live) setError(errorMessage(e)); }); return () => { live = false; }; }, [id, retry]);
  async function send(action: OEMReferenceAction, payload: Record<string, unknown>, text: string) {
    if (!row) return false;
    setBusy(action); setError(''); setNotice('');
    try { setRow(await request<OEMReferenceDetail>(`${base}/${row.id}`, { method: 'PATCH', body: JSON.stringify({ action, ...payload, expected_version: row.version }) })); setNotice(text); onChanged(row.id); return true; }
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
          : <p>Ningún SKU del catálogo lleva este número: ni como SKU principal, ni como alterno, ni por su nombre o un catálogo.</p>}
        {row.linked_count > row.linked_skus.length && <p>Se muestran {number(row.linked_skus.length)} de {number(row.linked_count)}.</p>}
      </section>
      <section className="suffix-editor-section" aria-label="Alternos de posventa"><h4>Alternos de posventa ({number(row.cross_references.length)})</h4>
        {row.cross_references.length ? <ul className="oem-ref-skus oem-ref-cross">{row.cross_references.map(code => <li key={code.id}><strong>{code.brand} {code.code}</strong>{code.citations.map(text => <span key={text}>{text}</span>)}</li>)}</ul>
          : <p>Ningún catálogo de marca de repuesto imprime alternos de este número.</p>}
        <p>Códigos de marcas de repuesto que los catálogos imprimen como equivalentes de este número. Pertenecen al número, no a un SKU: cada SKU vinculado los muestra, también los SKU duplicados.</p>
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
