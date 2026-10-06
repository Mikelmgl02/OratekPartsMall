'use client';
import { useCallback, useEffect, useId, useRef, useState } from 'react';
import { Archive, ArchiveRestore, LoaderCircle, PackageSearch, Pencil, Plus, Scale, Search, X } from 'lucide-react';
import Modal from './modal';
import { UppercaseInput } from './uppercase-field';
import { money, percent } from './price-explanation';
import { Account, ApiError, Page, request } from '@/lib/types';
import { decimal, priceCents } from '@/lib/money';
import type { AccountRef, Currency, PriceRow, PricingClientPage, PricingRule, PricingRuleInput, PricingRulePage, RuleItem, RuleKind, RuleScope, RuleTarget } from '@/lib/pricing-types';

// Supplier-only: commercial rules never reach a client screen; clients only receive the net prices of the quotations they get.
export const precedenceHelp = 'Gana la regla más específica: cliente › todos; artículo › línea › marca › todos; mayor cantidad mínima. Solo se aplica una regla por artículo.';
const failure = (error: unknown, fallback: string) => error instanceof TypeError ? 'Se perdió la conexión. Inténtalo de nuevo.' : error instanceof Error ? error.message : fallback;
const day = (value: string) => new Date(`${value}T12:00:00-05:00`).toLocaleDateString('es-PA', { timeZone: 'America/Panama', dateStyle: 'medium' });
const validityLabels: Record<PricingRule['validity'], string> = { active: 'Vigente', scheduled: 'Programada', expired: 'Vencida', archived: 'Archivada' };
const statusFilters = [{ value: 'active', label: 'Activas' }, { value: 'archived', label: 'Archivadas' }, { value: '', label: 'Todas' }] as const;

export function ruleTarget(rule: Pick<PricingRule, 'target' | 'target_value' | 'item'>) {
  return { all: 'Todos los artículos', line: `Línea ${rule.target_value}`, brand: `Marca ${rule.target_value}`, item: `Artículo ${rule.item?.codigo ?? ''}` }[rule.target];
}
export function ruleAction(rule: Pick<PricingRule, 'kind' | 'value' | 'currency'>) {
  return rule.kind === 'discount' ? `Descuento ${percent(rule.value)}` : `Precio neto ${money(rule.value, rule.currency || 'USD')}`;
}
export function ruleWindow(rule: Pick<PricingRule, 'valid_from' | 'valid_until'>) {
  if (rule.valid_from && rule.valid_until) return `${day(rule.valid_from)} – ${day(rule.valid_until)}`;
  return rule.valid_from ? `Desde el ${day(rule.valid_from)}` : rule.valid_until ? `Hasta el ${day(rule.valid_until)}` : 'Siempre';
}

export function RuleTable({ rules, canConfigure, showClient = true, busy = false, onEdit, onArchive, onRestore, label }: {
  rules: PricingRule[]; canConfigure: boolean; showClient?: boolean; busy?: boolean; label: string;
  onEdit?: (rule: PricingRule) => void; onArchive?: (rule: PricingRule) => void; onRestore?: (rule: PricingRule) => void;
}) {
  const [confirming, setConfirming] = useState<string | null>(null);
  const actions = canConfigure && (onEdit || onArchive || onRestore);
  return <div className="table-scroll"><table className="pricing-rule-table" aria-label={label}><thead><tr><th>Nombre</th>{showClient && <th>Aplica a</th>}<th>Artículos</th><th>Acción</th>
    <th>Desde</th><th>Vigencia</th><th>Estado</th>{actions && <th><span className="sr-only">Acciones</span></th>}</tr></thead>
    <tbody>{rules.map(rule => <tr key={rule.id} className={rule.active ? '' : 'archived'}>
      <td><strong>{rule.name}</strong>{rule.note && <small>{rule.note}</small>}</td>
      {showClient && <td>{rule.client ? rule.client.name : 'Todos tus clientes'}</td>}
      <td>{ruleTarget(rule)}{rule.item && <small>{rule.item.supplier_invent_id}{rule.item.brand ? ` · ${rule.item.brand}` : ''}</small>}</td>
      <td>{ruleAction(rule)}</td><td className="number-cell">{rule.min_quantity === 1 ? '1 unidad' : `${rule.min_quantity} unidades`}</td><td>{ruleWindow(rule)}</td>
      <td><span className={`pricing-rule-state ${rule.validity}`}>{validityLabels[rule.validity]}</span></td>
      {actions && <td className="pricing-rule-actions">{confirming === rule.id ? <span className="pricing-rule-confirm">¿Archivar?<button type="button" disabled={busy} onClick={() => { setConfirming(null); onArchive?.(rule); }}>Sí, archivar</button>
        <button type="button" onClick={() => setConfirming(null)}>No</button></span> : <>
        {rule.active && onEdit && <button type="button" className="icon-button" aria-label={`Editar regla ${rule.name}`} disabled={busy} onClick={() => onEdit(rule)}><Pencil size={15}/></button>}
        {rule.active && onArchive && <button type="button" className="icon-button" aria-label={`Archivar regla ${rule.name}`} disabled={busy} onClick={() => setConfirming(rule.id)}><Archive size={15}/></button>}
        {!rule.active && onRestore && <button type="button" className="icon-button" aria-label={`Reactivar regla ${rule.name}`} disabled={busy} onClick={() => onRestore(rule)}><ArchiveRestore size={15}/></button>}</>}</td>}
    </tr>)}</tbody></table></div>;
}

// Archive and reactivate share the compare-and-set of rule edits: a stale revision reloads the list instead of overwriting.
async function changeState(account: Account, rule: PricingRule, active: boolean) {
  const path = `/api/market/accounts/${account.id}/pricing-rules/${rule.id}${active ? '' : '/archive'}`;
  return request<PricingRule>(path, { method: 'POST', body: JSON.stringify(active ? { expected_version: rule.revision, active: true } : { expected_version: rule.revision }) });
}

// "Reglas": every rule of the supplier, active first, with filters, search and the rule form.
export default function PricingRules({ account }: { account: Account }) {
  const [status, setStatus] = useState<'active' | 'archived' | ''>('active');
  const [search, setSearch] = useState('');
  const [applied, setApplied] = useState('');
  const [page, setPage] = useState(1);
  const [data, setData] = useState<PricingRulePage | null>(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState<PricingRule | 'new' | null>(null);
  const [revision, setRevision] = useState(0);
  useEffect(() => { const timer = setTimeout(() => { if (search.trim() !== applied) { setApplied(search.trim()); setPage(1); } }, 350); return () => clearTimeout(timer); }, [search, applied]);
  useEffect(() => {
    let cancelled = false; setError('');
    const query = new URLSearchParams({ page: String(page), ...(status ? { status } : {}), ...(applied ? { search: applied } : {}) });
    request<PricingRulePage>(`/api/market/accounts/${account.id}/pricing-rules?${query}`).then(value => { if (!cancelled) setData(value); })
      .catch(caught => { if (!cancelled) setError(failure(caught, 'No se pudieron cargar tus reglas.')); });
    return () => { cancelled = true; };
  }, [account.id, status, applied, page, revision]);
  async function setActive(rule: PricingRule, active: boolean) {
    setBusy(true); setError(''); setNotice('');
    try { await changeState(account, rule, active); setNotice(active ? `Regla ${rule.name} reactivada.` : `Regla ${rule.name} archivada.`); }
    catch (caught) { setError(failure(caught, 'No se pudo actualizar la regla.')); }
    finally { setBusy(false); setRevision(value => value + 1); }
  }
  return <>
    <div className="supplier-pricing-heading"><div><h2>Reglas comerciales</h2><p>Descuentos por marca, por tu línea o por artículo, precios netos, escalas por volumen y vigencias. {precedenceHelp}</p></div>
      {data?.can_configure && <button type="button" className="button soft small" onClick={() => setEditing('new')}><Plus size={15}/>Nueva regla</button>}</div>
    {data && !data.can_configure && <p className="supplier-pricing-readonly">Pide a un administrador de tu cuenta que cambie esta configuración.</p>}
    <div className="price-grid-controls">
      <div className="supplier-requests-search price-grid-search"><Search size={18}/><input aria-label="Buscar reglas" value={search} maxLength={200} placeholder="Nombre, línea, marca, artículo o cliente…" autoComplete="off" onChange={event => setSearch(event.target.value)}/>{search && <button type="button" className="icon-button" aria-label="Limpiar búsqueda de reglas" onClick={() => setSearch('')}><X size={16}/></button>}</div>
      <div className="supplier-request-filters" role="group" aria-label="Estado de las reglas">{statusFilters.map(item => <button type="button" key={item.value} aria-pressed={status === item.value} className={status === item.value ? 'selected' : ''} onClick={() => { setStatus(item.value); setPage(1); }}>{item.label}</button>)}</div>
    </div>
    {notice && <div className="notice success" role="status">{notice}</div>}
    {error && <div className="notice error" role="alert"><span>{error}</span><button type="button" onClick={() => setRevision(value => value + 1)}>Intentar de nuevo</button></div>}
    {!data && !error ? <div className="loading"><LoaderCircle className="spin"/>Cargando tus reglas…</div> : data && (!data.results.length
      ? <div className="empty-state compact"><Scale size={30}/><h3>{applied || status !== 'active' ? 'No hay reglas para estos filtros.' : 'Todavía no tienes reglas comerciales.'}</h3><p>Sin reglas, tus precios sugeridos salen de la lista de cada cliente y de su descuento general.</p></div>
      : <RuleTable label="Reglas comerciales" rules={data.results} canConfigure={data.can_configure} busy={busy} onEdit={setEditing} onArchive={rule => void setActive(rule, false)} onRestore={rule => void setActive(rule, true)}/>)}
    {data && data.count > data.results.length && <div className="pagination"><span>{data.count} reglas</span><div><button type="button" className="button soft small" disabled={!data.previous} onClick={() => setPage(value => value - 1)}>Anterior</button><span>Página {page}</span><button type="button" className="button soft small" disabled={!data.next} onClick={() => setPage(value => value + 1)}>Siguiente</button></div></div>}
    {editing && <RuleForm account={account} rule={editing === 'new' ? null : editing} onClose={() => setEditing(null)}
      onSaved={(saved, created) => { setEditing(null); setNotice(created ? `Regla ${saved.name} creada.` : `Regla ${saved.name} actualizada.`); setRevision(value => value + 1); }}/>}
  </>;
}

// "Precios especiales y descuentos de este cliente" plus the general rules that also reach the client, read-only.
export function ClientRules({ account, client, canConfigure, onChanged }: { account: Account; client: AccountRef; canConfigure: boolean; onChanged?: () => void }) {
  const [own, setOwn] = useState<PricingRule[] | null>(null);
  const [general, setGeneral] = useState<PricingRule[] | null>(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState<{ rule: PricingRule | null; kind: RuleKind } | null>(null);
  const base = `/api/market/accounts/${account.id}/pricing-rules`;
  const load = useCallback(async () => {
    try {
      const [mine, everyone] = await Promise.all([request<PricingRulePage>(`${base}?client=${client.id}&status=active`), request<PricingRulePage>(`${base}?scope=all&status=active`)]);
      setOwn(mine.results); setGeneral(everyone.results); setError('');
    } catch (caught) { setError(failure(caught, 'No se pudieron cargar las reglas de este cliente.')); }
  }, [base, client.id]);
  useEffect(() => { void load(); }, [load]);
  async function archive(rule: PricingRule) {
    setBusy(true); setError(''); setNotice('');
    try { await changeState(account, rule, false); setNotice(`Regla ${rule.name} archivada.`); onChanged?.(); }
    catch (caught) { setError(failure(caught, 'No se pudo archivar la regla.')); }
    finally { setBusy(false); void load(); }
  }
  return <section className="client-profile-rules" aria-label="Precios especiales y descuentos de este cliente">
    <div className="supplier-pricing-heading"><div><h3>Precios especiales y descuentos de este cliente</h3><p>Solo se aplican a este cliente y ganan a tus reglas generales.</p></div>
      {canConfigure && <div className="supplier-workspace-actions"><button type="button" className="button soft small" onClick={() => setEditing({ rule: null, kind: 'net_price' })}><Plus size={15}/>Agregar precio neto</button>
        <button type="button" className="button soft small" onClick={() => setEditing({ rule: null, kind: 'discount' })}><Plus size={15}/>Agregar descuento</button></div>}</div>
    {notice && <div className="notice success" role="status">{notice}</div>}
    {error && <div className="notice error" role="alert"><span>{error}</span><button type="button" onClick={() => void load()}>Intentar de nuevo</button></div>}
    {!own && !error ? <div className="loading"><LoaderCircle className="spin"/>Cargando reglas…</div> : own && (own.length
      ? <RuleTable label="Reglas de este cliente" rules={own} showClient={false} canConfigure={canConfigure} busy={busy} onEdit={rule => setEditing({ rule, kind: rule.kind })} onArchive={rule => void archive(rule)}/>
      : <p className="pricing-empty-note">Este cliente no tiene precios especiales ni descuentos propios.</p>)}
    {!!general?.length && <details className="client-profile-general"><summary>Reglas generales que también aplican ({general.length})</summary>
      <p className="form-footnote">{precedenceHelp} El descuento general de este cliente y sus reglas propias ganan a cualquier regla para todos tus clientes.</p>
      <RuleTable label="Reglas generales" rules={general} canConfigure={false}/></details>}
    {editing && <RuleForm account={account} rule={editing.rule} preset={{ scope: 'client', client, kind: editing.kind, target: editing.kind === 'net_price' ? 'item' : 'all' }}
      onClose={() => setEditing(null)} onSaved={(saved, created) => { setEditing(null); setNotice(created ? `Regla ${saved.name} creada.` : `Regla ${saved.name} actualizada.`); onChanged?.(); void load(); }}/>}
  </section>;
}

type RuleValues = { name: string; scope: RuleScope; client: AccountRef | null; target: RuleTarget; target_value: string; item: RuleItem | null; kind: RuleKind;
  value: string; currency: '' | Currency; min_quantity: string; valid_from: string; valid_until: string; note: string };
function valuesFrom(rule: PricingRule | null, preset?: { scope?: RuleScope; client?: AccountRef; kind?: RuleKind; target?: RuleTarget }): RuleValues {
  if (rule) return { name: rule.name, scope: rule.scope, client: rule.client, target: rule.target, target_value: rule.target_value, item: rule.item, kind: rule.kind, value: rule.value,
    currency: rule.currency, min_quantity: String(rule.min_quantity), valid_from: rule.valid_from || '', valid_until: rule.valid_until || '', note: rule.note };
  return { name: '', scope: preset?.scope || 'all', client: preset?.client || null, target: preset?.target || 'all', target_value: '', item: null, kind: preset?.kind || 'discount',
    value: '', currency: '', min_quantity: '1', valid_from: '', valid_until: '', note: '' };
}

// The rule form: one rule is a discount or a net price for all clients or one, on all items, a LINEA, a brand or one item.
export function RuleForm({ account, rule, preset, onClose, onSaved }: { account: Account; rule: PricingRule | null;
  preset?: { scope?: RuleScope; client?: AccountRef; kind?: RuleKind; target?: RuleTarget }; onClose: () => void; onSaved: (rule: PricingRule, created: boolean) => void }) {
  const [values, setValues] = useState<RuleValues>(() => valuesFrom(rule, preset));
  const [revision, setRevision] = useState(rule?.revision || 0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const retry = useRef<{ key: string; id: string } | null>(null);
  const fixedClient = !rule && !!preset?.client, clientLocked = fixedClient || !!rule?.client;
  const set = <K extends keyof RuleValues>(key: K, value: RuleValues[K]) => setValues(current => ({ ...current, [key]: value }));
  const cents = priceCents(values.value);
  const example = cents === null ? '' : values.kind === 'discount' ? cents > BigInt(0) && cents < BigInt(10000) ? `Ejemplo: un precio de lista de ${money('100.00', 'USD')} queda en ${money(decimal(BigInt(10000) - cents), 'USD')}.` : ''
    : `${values.scope === 'client' ? 'Este cliente pagará' : 'Tus clientes pagarán'} ${money(decimal(cents), values.currency || 'USD')} por unidad${Number(values.min_quantity) > 1 ? ` desde ${values.min_quantity} unidades` : ''}, sin importar la lista.`;
  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault(); setError('');
    if (cents === null || cents === BigInt(0)) { setError(values.kind === 'discount' ? 'Escribe un descuento mayor que 0 y menor que 100, con hasta dos decimales.' : 'Escribe un precio neto mayor que 0, con hasta dos decimales.'); return; }
    if (values.kind === 'discount' && cents >= BigInt(10000)) { setError('El descuento debe ser menor que 100.'); return; }
    if (values.target === 'item' && !values.item) { setError('Elige el artículo de la regla.'); return; }
    if (values.scope === 'client' && !values.client) { setError('Elige el cliente de la regla.'); return; }
    const body: PricingRuleInput = { name: values.name, scope: values.scope, client_id: values.scope === 'client' ? values.client?.id ?? null : null, target: values.target,
      target_value: ['line', 'brand'].includes(values.target) ? values.target_value.trim().toUpperCase() : '', item_id: values.target === 'item' ? values.item?.id ?? null : null,
      kind: values.kind, value: decimal(cents), currency: values.kind === 'net_price' ? values.currency : '', min_quantity: Number(values.min_quantity) || 1,
      valid_from: values.valid_from || null, valid_until: values.valid_until || null, note: values.note };
    const payload = rule ? { ...body, expected_version: revision } : body, key = JSON.stringify(payload);
    // A retry of the same creation reuses its operation_id, so a lost response never creates the rule twice.
    if (!rule && retry.current?.key !== key) retry.current = { key, id: crypto.randomUUID() };
    setBusy(true);
    try {
      const saved = await request<PricingRule>(`/api/market/accounts/${account.id}/pricing-rules${rule ? `/${rule.id}` : ''}`, { method: 'POST',
        body: JSON.stringify(rule ? payload : { ...payload, operation_id: retry.current!.id }) });
      onSaved(saved, !rule);
    } catch (caught) {
      const current = caught instanceof ApiError && caught.status === 409 ? (caught.body as { rule?: PricingRule } | null)?.rule : undefined;
      if (current) { setValues(valuesFrom(current)); setRevision(current.revision); }
      setError(current ? 'Otro usuario actualizó esta regla. Se cargaron sus valores actuales; revísalos y vuelve a guardar.' : failure(caught, 'No se pudo guardar la regla.'));
    } finally { setBusy(false); }
  }
  return <Modal wide title={rule ? `Regla · ${rule.name}` : fixedClient ? preset?.kind === 'net_price' ? 'Agregar precio neto' : 'Agregar descuento' : 'Nueva regla'} className="pricing-rule-modal" onClose={() => { if (!busy) onClose(); }}>
    <form className="stack-form pricing-rule-form" autoComplete="off" onSubmit={submit}>
      <label>Nombre<UppercaseInput required maxLength={120} value={values.name} disabled={busy} onChange={event => set('name', event.target.value)} placeholder="EJ.: FILTROS TALLER CENTRAL"/></label>
      <div className="form-row">
        <label>Aplica a<select value={values.scope} disabled={busy || fixedClient} onChange={event => set('scope', event.target.value as RuleScope)}><option value="all">Todos tus clientes</option><option value="client">Un cliente</option></select></label>
        {values.scope === 'client' && (clientLocked ? <label>Cliente<input value={values.client?.name || ''} disabled readOnly/></label>
          : <ClientSelect account={account} value={values.client} disabled={busy} onChange={value => set('client', value)}/>)}
      </div>
      <div className="form-row">
        <label>Artículos<select value={values.target} disabled={busy} onChange={event => set('target', event.target.value as RuleTarget)}>
          <option value="all" disabled={values.kind === 'net_price'}>Todos</option><option value="line" disabled={values.kind === 'net_price'}>Una línea</option>
          <option value="brand" disabled={values.kind === 'net_price'}>Una marca</option><option value="item">Un artículo</option></select></label>
        {values.target === 'line' && <label>Línea<UppercaseInput required maxLength={120} value={values.target_value} disabled={busy} onChange={event => set('target_value', event.target.value)} placeholder="TU LINEA, EJ.: FILTROS"/></label>}
        {values.target === 'brand' && <label>Marca<UppercaseInput required maxLength={120} value={values.target_value} disabled={busy} onChange={event => set('target_value', event.target.value)} placeholder="EJ.: KSM"/></label>}
      </div>
      {values.target === 'item' && (values.item ? <div className="pricing-picked-item"><span><strong>{values.item.codigo}</strong><small>{values.item.supplier_invent_id}{values.item.brand ? ` · ${values.item.brand}` : ''}{values.item.description ? ` · ${values.item.description}` : ''}</small></span>
        <button type="button" className="button text small" disabled={busy} onClick={() => set('item', null)}>Cambiar artículo</button></div>
        : <ItemSearch account={account} label="Artículo" onPick={item => set('item', item)}/>)}
      <div className="form-row">
        <label>Tipo<select value={values.kind} disabled={busy} onChange={event => { const kind = event.target.value as RuleKind; setValues(current => ({ ...current, kind, target: kind === 'net_price' ? 'item' : current.target })); }}>
          <option value="discount">Descuento %</option><option value="net_price">Precio neto</option></select></label>
        <label>{values.kind === 'discount' ? 'Descuento (%)' : 'Precio neto'}<input required inputMode="decimal" value={values.value} disabled={busy} onChange={event => set('value', event.target.value)} placeholder={values.kind === 'discount' ? '10' : '16.80'}/></label>
      </div>
      {values.kind === 'net_price' && <label>Moneda<select value={values.currency} disabled={busy} onChange={event => set('currency', event.target.value as '' | Currency)}><option value="">Tu moneda predeterminada</option><option value="USD">USD</option><option value="PAB">PAB</option></select></label>}
      <div className="form-row three">
        <label>Desde (unidades)<input type="number" min={1} max={9999} step={1} required value={values.min_quantity} disabled={busy} onChange={event => set('min_quantity', event.target.value)}/></label>
        <label>Vigente desde<input type="date" value={values.valid_from} disabled={busy} onChange={event => set('valid_from', event.target.value)}/></label>
        <label>Vigente hasta<input type="date" value={values.valid_until} min={values.valid_from || undefined} disabled={busy} onChange={event => set('valid_until', event.target.value)}/></label>
      </div>
      <label>Nota interna<UppercaseInput maxLength={200} value={values.note} disabled={busy} onChange={event => set('note', event.target.value)} placeholder="SOLO PARA TU EQUIPO"/></label>
      {example && <p className="pricing-rule-example" role="status">{example}</p>}
      <p className="form-footnote">{precedenceHelp} Las fechas se cuentan en hora de Panamá, incluido el último día.</p>
      {error && <div className="notice error" role="alert">{error}</div>}
      <button className="button primary full" disabled={busy}>{busy && <LoaderCircle className="spin" size={18}/>}{rule ? 'Guardar regla' : 'Crear regla'}</button>
    </form>
  </Modal>;
}

// Clients that ordered from the supplier (the only ones a rule or the simulator may name), searchable by name or code.
export function ClientSelect({ account, value, disabled, onChange, allowNone = false }: { account: Account; value: AccountRef | null; disabled?: boolean;
  onChange: (value: AccountRef | null) => void; allowNone?: boolean }) {
  const [search, setSearch] = useState('');
  const [clients, setClients] = useState<AccountRef[]>([]);
  const [error, setError] = useState('');
  const id = useId();
  useEffect(() => {
    let cancelled = false;
    const timer = setTimeout(() => {
      request<PricingClientPage>(`/api/market/accounts/${account.id}/clients${search.trim() ? `?${new URLSearchParams({ search: search.trim() })}` : ''}`)
        .then(page => { if (!cancelled) { setClients(page.results.map(({ id, name }) => ({ id, name }))); setError(''); } })
        .catch(caught => { if (!cancelled) setError(failure(caught, 'No se pudieron cargar tus clientes.')); });
    }, search ? 300 : 0);
    return () => { cancelled = true; clearTimeout(timer); };
  }, [account.id, search]);
  const options = value && !clients.some(client => client.id === value.id) ? [value, ...clients] : clients;
  return <div className="pricing-client-select">
    <label htmlFor={`${id}-client`}>Cliente</label>
    <div><input aria-label="Buscar cliente" value={search} maxLength={200} disabled={disabled} placeholder="Buscar por nombre o código…" onChange={event => setSearch(event.target.value)}/>
      <select id={`${id}-client`} value={value?.id || ''} disabled={disabled} onChange={event => onChange(options.find(client => client.id === event.target.value) || null)}>
        <option value="">{allowNone ? 'Sin cliente: solo reglas generales' : 'Elige un cliente'}</option>
        {options.map(client => <option key={client.id} value={client.id}>{client.name}</option>)}</select></div>
    {error && <small className="pricing-field-error">{error}</small>}
  </div>;
}

// Finds items of the supplier's inventory (matched or not) by its ID, code, brand, description or LINEA.
export function ItemSearch({ account, label, onPick, exclude = [] }: { account: Account; label: string; onPick: (item: RuleItem) => void; exclude?: string[] }) {
  const [search, setSearch] = useState('');
  const [results, setResults] = useState<PriceRow[] | null>(null);
  const [error, setError] = useState('');
  const id = useId();
  useEffect(() => {
    if (search.trim().length < 2) { setResults(null); return; }
    let cancelled = false;
    const timer = setTimeout(() => {
      request<Page<PriceRow>>(`/api/market/accounts/${account.id}/prices?${new URLSearchParams({ search: search.trim() })}`)
        .then(page => { if (!cancelled) { setResults(page.results); setError(''); } }).catch(caught => { if (!cancelled) setError(failure(caught, 'No se pudo buscar en tu inventario.')); });
    }, 300);
    return () => { cancelled = true; clearTimeout(timer); };
  }, [account.id, search]);
  const shown = (results || []).filter(row => !exclude.includes(row.supplier_item_id)).slice(0, 8);
  return <div className="pricing-item-search">
    <label htmlFor={`${id}-search`}>{label}</label>
    <div className="supplier-requests-search"><PackageSearch size={17}/><input id={`${id}-search`} value={search} maxLength={200} placeholder="Tu ID de inventario, código, marca o línea…" autoComplete="off" onChange={event => setSearch(event.target.value)}/></div>
    {error && <small className="pricing-field-error">{error}</small>}
    {results && (shown.length ? <ul className="pricing-item-results" aria-label="Artículos encontrados">{shown.map(row => <li key={row.supplier_item_id}>
      <button type="button" onClick={() => { onPick({ id: row.supplier_item_id, supplier_invent_id: row.supplier_invent_id, codigo: row.codigo, brand: row.brand, description: row.description }); setSearch(''); setResults(null); }}>
        <strong>{row.codigo}</strong><small>{row.supplier_invent_id}{row.brand ? ` · ${row.brand}` : ''}{row.discount_group ? ` · Línea ${row.discount_group}` : ''}{row.description ? ` · ${row.description}` : ''}</small></button></li>)}</ul>
      : <small className="pricing-empty-note">No encontramos artículos con esa búsqueda.</small>)}
  </div>;
}
