'use client';
import { useCallback, useEffect, useState } from 'react';
import { Bot, LoaderCircle, LockKeyhole, Save, ShieldCheck } from 'lucide-react';
import { Account, ApiError, type MembershipPermission, request } from '@/lib/types';
import type { PricingSettings, PricingSettingsUpdate, PricingSettingsValues } from '@/lib/pricing-types';

const fields = ['default_currency', 'usd_pab_parity', 'config_min_permission', 'publish_min_permission', 'over_request_policy', 'over_stock_policy',
  'accept_shortfall_policy', 'prefill_quantity', 'assistant_enabled'] as const;
type Field = typeof fields[number];
const ownerOnly = new Set<Field>(['config_min_permission', 'publish_min_permission', 'assistant_enabled']);
const permissionOptions: [MembershipPermission, string][] = [['staff', 'Cualquier miembro de tu equipo'], ['manager', 'Administradores y propietario'], ['owner', 'Solo el propietario']];
const policyOptions = [['confirm', 'Pedir confirmación'], ['block', 'Bloquear']] as const;
const failure = (error: unknown, fallback: string) => error instanceof TypeError ? 'Se perdió la conexión. Tus cambios siguen en pantalla; vuelve a guardarlos.' : error instanceof Error ? error.message : fallback;
const date = (value: string) => new Date(value).toLocaleString('es-PA', { timeZone: 'America/Panama', dateStyle: 'medium', timeStyle: 'short' });
const valuesFrom = (settings: PricingSettings) => Object.fromEntries(fields.map(key => [key, settings[key]])) as PricingSettingsValues;

// "Precios › Configuración": every private pricing setting of the supplier. Members below the configuration permission read it only; the permission
// fields and the AI assistant change only by the owner. A save sends only the fields that changed, so a stale form never re-sends another member's values.
export default function PricingSettingsPanel({ account }: { account: Account }) {
  const path = `/api/market/accounts/${account.id}/pricing/settings`;
  const [settings, setSettings] = useState<PricingSettings | null>(null);
  const [form, setForm] = useState<PricingSettingsValues | null>(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const load = useCallback(async () => {
    try { const value = await request<PricingSettings>(path); setSettings(value); setForm(valuesFrom(value)); setError(''); }
    catch (caught) { setError(failure(caught, 'No se pudo cargar la configuración de precios.')); }
  }, [path]);
  useEffect(() => { void load(); }, [load]);
  const set = <K extends Field>(key: K, value: PricingSettingsValues[K]) => { setForm(current => current && { ...current, [key]: value }); setNotice(''); };
  const changed = form && settings ? fields.filter(key => form[key] !== settings[key]) : [];
  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!form || !settings) return;
    if (!changed.length) { setNotice('No hay cambios por guardar.'); return; }
    const body = { expected_version: settings.version, ...Object.fromEntries(changed.map(key => [key, form[key]])) } as PricingSettingsUpdate;
    setBusy(true); setError(''); setNotice('');
    try {
      // Values already in place are a no-op on the server, so retrying the same body after a lost response is safe.
      const saved = await request<PricingSettings>(path, { method: 'POST', body: JSON.stringify(body) });
      setSettings(saved); setForm(valuesFrom(saved)); setNotice('Configuración guardada.');
    } catch (caught) {
      // 409 carries the current values; a 403 means this member's permission changed meanwhile, so reload what is still editable.
      const refused = caught instanceof ApiError && caught.status === 403;
      const current = refused ? await request<PricingSettings>(path).catch(() => undefined)
        : caught instanceof ApiError && caught.status === 409 ? (caught.body as { settings?: PricingSettings } | null)?.settings : undefined;
      if (current) {
        // Fields another member changed, or that this member can no longer change, take the server's value; the rest of the edits stay on screen.
        const keep = (key: Field) => current[key] === settings[key] && (ownerOnly.has(key) ? current.can_manage_permissions : current.can_configure);
        setForm(Object.fromEntries(fields.map(key => [key, keep(key) ? form[key] : current[key]])) as PricingSettingsValues);
        setSettings(current);
      }
      setError(current && !refused ? 'Otro miembro de tu equipo cambió esta configuración. Se cargaron sus valores; revisa tus cambios y vuelve a guardar.'
        : failure(caught, 'No se pudo guardar la configuración.'));
    } finally { setBusy(false); }
  }
  const editable = !!settings?.can_configure && !busy, owner = !!settings?.can_manage_permissions && editable;
  const locked = (key: Field) => !(ownerOnly.has(key) ? owner : editable);
  return <>
    <div className="supplier-pricing-heading"><div><h2>Configuración</h2><p>Cómo prepara tu equipo las cotizaciones: quién configura precios y quién las envía, qué alertas bloquean y qué se sugiere en cada borrador.</p></div></div>
    {error && <div className="notice error" role="alert"><span>{error}</span>{!settings && <button type="button" onClick={() => void load()}>Intentar de nuevo</button>}</div>}
    {notice && <div className="notice success" role="status">{notice}</div>}
    {!settings || !form ? !error && <div className="loading"><LoaderCircle className="spin"/>Cargando la configuración…</div> : <form className="stack-form pricing-settings-form" autoComplete="off" onSubmit={submit}>
      {!settings.can_configure && <p className="supplier-pricing-readonly">Pide a un administrador de tu cuenta que cambie esta configuración.</p>}
      <fieldset>
        <legend><ShieldCheck size={15}/>Permisos</legend>
        {settings.can_configure && !settings.can_manage_permissions && <p className="pricing-settings-owner"><LockKeyhole size={13}/>Solo el propietario de la cuenta cambia los permisos y el asistente de IA.</p>}
        <div className="form-row">
          <label>Quién puede configurar precios<select value={form.config_min_permission} disabled={locked('config_min_permission')} onChange={event => set('config_min_permission', event.target.value as MembershipPermission)}>
            {permissionOptions.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select>
            <small>Listas, importaciones, perfiles de clientes, reglas y esta configuración.</small></label>
          <label>Quién puede enviar cotizaciones<select value={form.publish_min_permission} disabled={locked('publish_min_permission')} onChange={event => set('publish_min_permission', event.target.value as MembershipPermission)}>
            {permissionOptions.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select>
            <small>Quien no tenga este permiso prepara el borrador y usa «Solicitar aprobación»; la cotización la envía un miembro con permiso.</small></label>
        </div>
      </fieldset>
      <fieldset>
        <legend>Cotizaciones</legend>
        <div className="form-row">
          <label>Ofrecer más de lo disponible<select value={form.over_stock_policy} disabled={locked('over_stock_policy')} onChange={event => set('over_stock_policy', event.target.value as PricingSettingsValues['over_stock_policy'])}>
            {policyOptions.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
          <label>Ofrecer más de lo solicitado<select value={form.over_request_policy} disabled={locked('over_request_policy')} onChange={event => set('over_request_policy', event.target.value as PricingSettingsValues['over_request_policy'])}>
            {policyOptions.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
        </div>
        <div className="form-row">
          <label>Cantidad sugerida<select value={form.prefill_quantity} disabled={locked('prefill_quantity')} onChange={event => set('prefill_quantity', event.target.value as PricingSettingsValues['prefill_quantity'])}>
            <option value="requested">Solicitadas</option><option value="available">Hasta tus existencias</option></select>
            <small>Lo que propone cada borrador nuevo; siempre puedes cambiarlo antes de enviar.</small></label>
          <label>Moneda predeterminada<select value={form.default_currency} disabled={locked('default_currency')} onChange={event => set('default_currency', event.target.value as PricingSettingsValues['default_currency'])}>
            <option value="USD">USD</option><option value="PAB">PAB</option></select>
            <small>Para la primera cotización de un cliente sin moneda preferida.</small></label>
        </div>
        <label className="admin-checkbox"><input type="checkbox" checked={form.usd_pab_parity} disabled={locked('usd_pab_parity')} onChange={event => set('usd_pab_parity', event.target.checked)}/>Tratar USD y PAB como equivalentes</label>
      </fieldset>
      <fieldset>
        <legend>Confirmación del cliente</legend>
        <label>Si el cliente confirma y faltan existencias<select value={form.accept_shortfall_policy} disabled={locked('accept_shortfall_policy')} onChange={event => set('accept_shortfall_policy', event.target.value as PricingSettingsValues['accept_shortfall_policy'])}>
          <option value="block">Bloquear la confirmación</option><option value="allow">Permitir y avisarme</option></select>
          <small>Solo cuentan las unidades que bajaron desde que enviaste la cotización.</small></label>
      </fieldset>
      <fieldset>
        <legend><Bot size={15}/>Asistente de IA</legend>
        <label className="admin-checkbox"><input type="checkbox" checked={form.assistant_enabled} disabled={locked('assistant_enabled')} onChange={event => set('assistant_enabled', event.target.checked)}/>Usar el asistente de cotización</label>
        <p className="form-footnote">El texto de la orden y de la conversación se envía a Google Gemini para interpretarlo. Nunca se envían precios ni existencias. El asistente aparece en tus órdenes cuando la plataforma lo tenga disponible.</p>
      </fieldset>
      {settings.updated_by && settings.updated_at && <p className="form-footnote">Última actualización por {settings.updated_by.name} el {date(settings.updated_at)}.</p>}
      {settings.can_configure && <button className="button primary" disabled={busy}>{busy ? <LoaderCircle className="spin" size={16}/> : <Save size={16}/>}{changed.length ? `Guardar configuración (${changed.length})` : 'Guardar configuración'}</button>}
    </form>}
  </>;
}
