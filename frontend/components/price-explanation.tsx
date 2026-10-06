// "Cómo se calculó": the supplier-only explanation of an engine price, step by step (quotation drawer and simulator).
import type { PriceExplanation } from '@/lib/pricing-types';
import { moneyFormatter, priceCents } from '@/lib/money';

export function money(value: string | null, currency: string) {
  const cents = value === null ? null : priceCents(value);
  return cents === null ? '—' : moneyFormatter(currency)(cents);
}
export const percent = (value: string) => `${value.replace(/\.00$/, '').replace('.', ',')} %`;
const panama = (value: string) => new Date(value).toLocaleDateString('es-PA', { timeZone: 'America/Panama', dateStyle: 'medium' });
export const discardReasons: Record<string, string> = { menos_especifica: 'menos específica', moneda_distinta: 'moneda distinta', precio_menor: 'precio menor',
  empate: 'mismo resultado; gana la más antigua', sin_precio_base: 'sin precio de lista' };
const CLIENT_DISCOUNT = 'synthetic:client_discount';

export function statusNote(explanation: PriceExplanation) {
  return {
    missing: `Sin precio en la lista ${explanation.price_list?.code}.`, identity_changed: 'El artículo de tu inventario cambió desde la solicitud: no hay precio sugerido.',
    currency_mismatch: `Tu lista está en ${explanation.price_list?.currency} y la cotización en ${explanation.currency}: sin precio sugerido. Activa la paridad USD/PAB o cambia la moneda.`,
    out_of_range: 'El precio calculado supera el máximo permitido.', no_price_list: 'Sin lista de precios ni regla para este artículo.', priced: '' }[explanation.status];
}

// One line per step, as the engine applied them: list price, the single rule, parity, rounding; then what it discarded and the next breaks.
export function PriceCalculation({ explanation, currency }: { explanation: PriceExplanation; currency: string }) {
  const base = explanation.base, status = statusNote(explanation);
  return <>
    <ol className="price-drawer-steps">
      {base && <li>Lista {base.price_list_code}{base.fallback ? ' (respaldo)' : ''}: <strong>{money(base.unit_price, base.currency)}</strong><small>Revisión {base.entry_revision}{base.updated_at ? ` · actualizada el ${panama(base.updated_at)}` : ''}</small></li>}
      {explanation.profile?.archived_list && <li>La lista {explanation.profile.archived_list} del cliente está archivada; se usa {explanation.price_list?.code ? `la lista ${explanation.price_list.code}` : 'tu lista predeterminada'}.</li>}
      {status && <li>{status}</li>}
      {explanation.steps.map((step, index) => <li key={index}>{step.kind === 'parity' ? `Paridad ${step.from}/${step.to} 1:1`
        : step.rule_id === CLIENT_DISCOUNT ? `Descuento general del cliente: −${percent(step.value)} → ${money(step.after, currency)}`
        : `${step.action === 'net_price' ? 'Precio neto especial' : 'Regla'} ${step.name}: ${step.action === 'discount' ? `−${percent(step.value)}` : money(step.value, currency)} → ${money(step.after, currency)}`}
        {step.kind === 'rule' && step.rule_id !== CLIENT_DISCOUNT && <small>{step.scope === 'client' ? 'Acuerdo de este cliente' : 'Para todos tus clientes'}{step.min_quantity > 1 ? ` · desde ${step.min_quantity} unidades` : ''}</small>}</li>)}
      {explanation.unit_price && <li>Redondeo a centavos: <strong>{money(explanation.unit_price, currency)}</strong></li>}
    </ol>
    {!!explanation.discarded.length && <p className="price-drawer-note">No aplicadas: {explanation.discarded.map(item => `${item.rule_id === CLIENT_DISCOUNT ? 'Descuento general del cliente' : item.name} (${discardReasons[item.reason] || item.reason})`).join(' · ')}</p>}
    {explanation.next_breaks.map(item => <p key={item.min_quantity} className="price-drawer-note">A partir de {item.min_quantity} unidades: {money(item.unit_price, currency)}</p>)}
    {explanation.floor_price && <p className="price-drawer-note">Tu precio mínimo: {money(explanation.floor_price, currency)}</p>}
  </>;
}
