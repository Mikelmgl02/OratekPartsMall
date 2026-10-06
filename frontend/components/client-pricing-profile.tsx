'use client';
import { useCallback, useEffect, useRef, useState } from 'react';
import { ArrowLeft, LoaderCircle, LockKeyhole, Save } from 'lucide-react';
import { ClientRules } from './pricing-rules';
import { UppercaseInput, UppercaseTextarea } from './uppercase-field';
import { Account, ApiError, request } from '@/lib/types';
import type { ClientProfile, ClientProfileUpdate, Currency, PriceListsEnvelope } from '@/lib/pricing-types';

type Form = { price_list_id: string; fallback_to_default: boolean; discount_percent: string; preferred_currency: '' | Currency; customer_code: string; default_terms: string; internal_notes: string };
const failure = (error: unknown, fallback: string) => error instanceof TypeError ? 'Se perdió la conexión. Tus cambios siguen en pantalla; vuelve a guardarlos.' : error instanceof Error ? error.message : fallback;
const date = (value: string) => new Date(value).toLocaleString('es-PA', { timeZone: 'America/Panama', dateStyle: 'medium', timeStyle: 'short' });
// Percentages go to the server as text: 0 to 99.99 with at most two decimals (a comma is accepted).
const percentText = (value: string) => { const normalized = value.trim().replace(',', '.'); return /^\d{1,2}(?:\.\d{1,2})?$/.test(normalized) ? normalized : null; };
const samePercent = (a: string, b: string) => Number(a) === Number(b);
function formFrom(profile: ClientProfile): Form {
  return { price_list_id: profile.price_list_id || '', fallback_to_default: profile.fallback_to_default, discount_percent: profile.discount_percent.replace(/\.00$/, ''),
    preferred_currency: profile.preferred_currency, customer_code: profile.customer_code, default_terms: profile.default_terms, internal_notes: profile.internal_notes };
}

// "Perfil comercial · {CLIENTE}": the private profile of one supplier–client pair. The client never sees it; it only receives the prices quoted.
export default function ClientPricingProfile({ account, clientId, embedded = false, onBack, onSaved }: { account: Account; clientId: string; embedded?: boolean;
  onBack?: () => void; onSaved?: (profile: ClientProfile) => void }) {
  const base = `/api/market/accounts/${account.id}`;
  const [profile, setProfile] = useState<ClientProfile | null>(null);
  const [lists, setLists] = useState<PriceListsEnvelope | null>(null);
  const [form, setForm] = useState<Form | null>(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const retry = useRef<{ key: string; id: string } | null>(null);
  const onSavedRef = useRef(onSaved); onSavedRef.current = onSaved;
  // keepForm: a first client rule created the profile with default values, so unsaved edits in the form still apply on top of it.
  const load = useCallback(async (keepForm = false) => {
    try {
      const [value, envelope] = await Promise.all([request<ClientProfile>(`${base}/clients/${clientId}/profile`), request<PriceListsEnvelope>(`${base}/price-lists`)]);
      setProfile(value); setLists(envelope); setForm(current => keepForm && current ? current : formFrom(value)); setError('');
    } catch (caught) { setError(failure(caught, 'No se pudo cargar el perfil comercial.')); }
  }, [base, clientId]);
  useEffect(() => { void load(); }, [load]);
  const set = <K extends keyof Form>(key: K, value: Form[K]) => { setForm(current => current && { ...current, [key]: value }); setNotice(''); };
  function changes(current: Form, saved: ClientProfile) {
    const body: Omit<ClientProfileUpdate, 'operation_id' | 'expected_version'> = {}, original = formFrom(saved);
    if (current.price_list_id !== original.price_list_id) body.price_list_id = current.price_list_id || null;
    if (current.fallback_to_default !== original.fallback_to_default) body.fallback_to_default = current.fallback_to_default;
    const discount = percentText(current.discount_percent || '0');
    if (discount !== null && !samePercent(discount, saved.discount_percent)) body.discount_percent = discount;
    if (current.preferred_currency !== original.preferred_currency) body.preferred_currency = current.preferred_currency;
    if (current.customer_code.trim() !== original.customer_code) body.customer_code = current.customer_code.trim();
    for (const key of ['default_terms', 'internal_notes'] as const) if (current[key].trim().toUpperCase() !== original[key]) body[key] = current[key].trim().toUpperCase();
    return body;
  }
  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!form || !profile) return;
    if (percentText(form.discount_percent || '0') === null) { setError('Escribe un descuento general entre 0 y 99,99, con hasta dos decimales.'); return; }
    const body = changes(form, profile);
    // Only changed fields travel; the first save creates the profile (expected_version 0).
    if (!Object.keys(body).length && profile.exists) { setNotice('No hay cambios por guardar.'); return; }
    const payload = { expected_version: profile.version, ...body }, key = JSON.stringify(payload);
    if (retry.current?.key !== key) retry.current = { key, id: crypto.randomUUID() };
    setBusy(true); setError(''); setNotice('');
    try {
      const saved = await request<ClientProfile>(`${base}/clients/${clientId}/profile`, { method: 'POST', body: JSON.stringify({ ...payload, operation_id: retry.current.id }) });
      retry.current = null; setProfile(saved); setForm(formFrom(saved)); setNotice('Perfil guardado. Los precios sugeridos de este cliente ya usan estos valores.');
      onSavedRef.current?.(saved);
    } catch (caught) {
      const current = caught instanceof ApiError && caught.status === 409 ? (caught.body as { profile?: ClientProfile } | null)?.profile : undefined;
      if (current) { setProfile(current); setForm(formFrom(current)); retry.current = null; }
      setError(current ? 'Otro usuario actualizó este perfil. Se cargaron sus valores actuales; revísalos y vuelve a guardar.' : failure(caught, 'No se pudo guardar el perfil.'));
    } finally { setBusy(false); }
  }
  const readOnly = !profile?.can_configure || busy;
  const active = lists?.results.filter(list => list.active || list.id === form?.price_list_id) || [];
  const defaultList = lists?.results.find(list => list.is_default);
  return <section className={`client-profile ${embedded ? 'embedded' : ''}`} aria-label={profile ? `Perfil comercial · ${profile.client.name}` : 'Perfil comercial'}>
    {!embedded && <div className="supplier-pricing-heading"><div>{onBack && <button type="button" className="button text small" onClick={onBack}><ArrowLeft size={14}/>Volver a clientes</button>}
      <h2>{profile ? `Perfil comercial · ${profile.client.name}` : 'Perfil comercial'}</h2>
      {profile && <p>{profile.order_count} {profile.order_count === 1 ? 'orden' : 'órdenes'} · última el {date(profile.last_order_at)}{profile.updated_by && profile.updated_at ? ` · perfil actualizado por ${profile.updated_by.name} el ${date(profile.updated_at)}` : ''}</p>}</div></div>}
    <div className="supplier-pricing-private" role="note"><LockKeyhole size={17}/><p>Privado entre tu empresa y este cliente. El cliente no ve esta configuración; solo recibe los precios que cotizas.</p></div>
    {error && <div className="notice error" role="alert"><span>{error}</span>{!profile && <button type="button" onClick={() => void load()}>Intentar de nuevo</button>}</div>}
    {notice && <div className="notice success" role="status">{notice}</div>}
    {!profile || !form ? !error && <div className="loading"><LoaderCircle className="spin"/>Cargando el perfil comercial…</div> : <>
      {!profile.can_configure && <p className="supplier-pricing-readonly">Pide a un administrador de tu cuenta que cambie esta configuración.</p>}
      {!profile.exists && <p className="pricing-empty-note">Este cliente aún no tiene perfil comercial: sus precios sugeridos usan tu lista predeterminada{defaultList ? ` ${defaultList.code}` : ''} sin descuento. Guarda el perfil para crearlo.</p>}
      <form className="stack-form client-profile-form" autoComplete="off" onSubmit={submit}>
        <div className="form-row">
          <label>Lista de precios<select value={form.price_list_id} disabled={readOnly} onChange={event => set('price_list_id', event.target.value)}>
            <option value="">{defaultList ? `Tu lista predeterminada (${defaultList.code})` : 'Tu lista predeterminada'}</option>
            {active.filter(list => !list.is_default || list.id === form.price_list_id).map(list => <option key={list.id} value={list.id}>{list.code} · {list.name} ({list.currency}){list.active ? '' : ' · archivada'}</option>)}</select>
            {profile.price_list && !profile.price_list.active && <small className="pricing-field-error">La lista {profile.price_list.code} está archivada: se usa {profile.effective_list ? `la lista ${profile.effective_list.code}` : 'tu lista predeterminada'}.</small>}</label>
          <label>Descuento general (%)<input inputMode="decimal" value={form.discount_percent} disabled={readOnly} placeholder="0" onChange={event => set('discount_percent', event.target.value)}/>
            <small>Se aplica cuando ninguna regla más específica gana; nunca se suma a otra regla.</small></label>
        </div>
        <label className="admin-checkbox"><input type="checkbox" checked={form.fallback_to_default} disabled={readOnly || !form.price_list_id} onChange={event => set('fallback_to_default', event.target.checked)}/>Usar la lista predeterminada si falta un precio</label>
        <div className="form-row">
          <label>Moneda preferida<select value={form.preferred_currency} disabled={readOnly} onChange={event => set('preferred_currency', event.target.value as '' | Currency)}>
            <option value="">Tu moneda predeterminada</option><option value="USD">USD</option><option value="PAB">PAB</option></select><small>La usa su primera cotización; un ajuste conserva la moneda de la versión anterior.</small></label>
          <label>Código del cliente en tu sistema<input maxLength={40} value={form.customer_code} disabled={readOnly} placeholder="EJ.: C-00123" onChange={event => set('customer_code', event.target.value)}/></label>
        </div>
        <label>Condiciones predeterminadas para sus cotizaciones<UppercaseTextarea maxLength={5000} value={form.default_terms} disabled={readOnly} placeholder="EJ.: PRECIOS NO INCLUYEN ITBMS · ENTREGA EN 48 HORAS"
          onChange={event => set('default_terms', event.target.value)}/><small>Se proponen en la primera cotización de cada orden; las revisas antes de enviarlas y el cliente solo ve lo que publiques.</small></label>
        <label>Notas internas (solo tu equipo)<UppercaseTextarea maxLength={4000} value={form.internal_notes} disabled={readOnly} placeholder="EJ.: PAGA A 30 DÍAS · LLAMAR ANTES DE ENTREGAR" onChange={event => set('internal_notes', event.target.value)}/></label>
        {profile.can_configure && <button className="button primary" disabled={busy}>{busy ? <LoaderCircle className="spin" size={16}/> : <Save size={16}/>}{profile.exists ? 'Guardar perfil' : 'Crear perfil'}</button>}
      </form>
      {/* A first client rule creates the profile: read it again so the form saves on top of it. */}
      <ClientRules account={account} client={profile.client} canConfigure={profile.can_configure} onChanged={() => { if (!profile.exists) void load(true); onSavedRef.current?.(profile); }}/>
    </>}
  </section>;
}
