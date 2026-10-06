'use client';
import { useState } from 'react';
import { Calculator, LoaderCircle, Trash2 } from 'lucide-react';
import { ClientSelect, ItemSearch, precedenceHelp } from './pricing-rules';
import { PriceCalculation, money, percent } from './price-explanation';
import { Account, request } from '@/lib/types';
import type { AccountRef, Currency, RuleItem, SimulationResult } from '@/lib/pricing-types';

const failure = (error: unknown, fallback: string) => error instanceof TypeError ? 'Se perdió la conexión. Inténtalo de nuevo.' : error instanceof Error ? error.message : fallback;
type SimulatedItem = RuleItem & { quantity: string };

// "Simulador": the engine's price for a client, items and quantities, with every step explained. Read-only: nothing is saved.
export default function PricingSimulator({ account }: { account: Account }) {
  const [client, setClient] = useState<AccountRef | null>(null);
  const [items, setItems] = useState<SimulatedItem[]>([]);
  const [currency, setCurrency] = useState<'' | Currency>('');
  const [onDate, setOnDate] = useState('');
  const [result, setResult] = useState<SimulationResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const valid = items.length > 0 && items.every(item => /^\d{1,4}$/.test(item.quantity.trim()) && Number(item.quantity) > 0);
  async function calculate(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!valid) { setError(items.length ? 'Indica una cantidad entre 1 y 9.999 para cada artículo.' : 'Agrega al menos un artículo.'); return; }
    setBusy(true); setError('');
    try {
      setResult(await request<SimulationResult>(`/api/market/accounts/${account.id}/pricing/simulate`, { method: 'POST', body: JSON.stringify({
        client_id: client?.id ?? null, ...(currency ? { currency } : {}), ...(onDate ? { on_date: onDate } : {}),
        items: items.map(item => ({ supplier_item_id: item.id, quantity: Number(item.quantity) })) }) }));
    } catch (caught) { setError(failure(caught, 'No se pudo calcular el precio.')); }
    finally { setBusy(false); }
  }
  return <>
    <div className="supplier-pricing-heading"><div><h2>Simulador</h2><p>Prueba qué precio sugeriría una cotización para un cliente y una cantidad, y por qué. No guarda nada. {precedenceHelp}</p></div></div>
    <form className="stack-form pricing-simulator-form" autoComplete="off" onSubmit={calculate}>
      <div className="form-row">
        <ClientSelect account={account} value={client} allowNone disabled={busy} onChange={value => { setClient(value); setResult(null); }}/>
        <div className="form-row">
          <label>Moneda<select value={currency} disabled={busy} onChange={event => { setCurrency(event.target.value as '' | Currency); setResult(null); }}><option value="">La del cliente</option><option value="USD">USD</option><option value="PAB">PAB</option></select></label>
          <label>Fecha<input type="date" value={onDate} disabled={busy} onChange={event => { setOnDate(event.target.value); setResult(null); }}/></label>
        </div>
      </div>
      {items.length < 50 && <ItemSearch account={account} label="Artículo(s)" exclude={items.map(item => item.id)} onPick={item => { setItems(current => [...current, { ...item, quantity: '1' }]); setResult(null); }}/>}
      {!!items.length && <ul className="pricing-simulator-items" aria-label="Artículos para simular">{items.map(item => <li key={item.id}>
        <span><strong>{item.codigo}</strong><small>{item.supplier_invent_id}{item.brand ? ` · ${item.brand}` : ''}</small></span>
        <label>Cantidad<input inputMode="numeric" value={item.quantity} disabled={busy} aria-label={`Cantidad de ${item.codigo}`} onChange={event => { const quantity = event.target.value; setItems(current => current.map(value => value.id === item.id ? { ...value, quantity } : value)); setResult(null); }}/></label>
        <button type="button" className="icon-button" aria-label={`Quitar ${item.codigo}`} disabled={busy} onClick={() => { setItems(current => current.filter(value => value.id !== item.id)); setResult(null); }}><Trash2 size={15}/></button></li>)}</ul>}
      {error && <div className="notice error" role="alert">{error}</div>}
      <button className="button primary" disabled={busy || !items.length}>{busy ? <LoaderCircle className="spin" size={16}/> : <Calculator size={16}/>}Calcular precio</button>
    </form>
    {result && <section className="pricing-simulation" aria-label="Resultado de la simulación">
      <p className="pricing-simulation-context">{result.client ? `${result.client.name} · ${result.profile?.exists ? `perfil v${result.profile.version}${Number(result.profile.discount_percent) > 0 ? ` · descuento general ${percent(result.profile.discount_percent)}` : ''}` : 'sin perfil comercial'}` : 'Sin cliente: solo reglas generales'}
        {' · '}{result.price_list ? `lista ${result.price_list.code}` : 'sin lista'} · {result.currency} · {new Date(`${result.on_date}T12:00:00-05:00`).toLocaleDateString('es-PA', { timeZone: 'America/Panama', dateStyle: 'medium' })}</p>
      {result.lines.map(line => <article key={line.supplier_item_id} className="pricing-simulation-line">
        <header><div><strong>{line.codigo}</strong><small>{line.supplier_invent_id}{line.brand ? ` · ${line.brand}` : ''}{line.discount_group ? ` · Línea ${line.discount_group}` : ''}</small></div>
          <div><strong>{line.unit_price ? money(line.unit_price, result.currency) : 'Sin precio sugerido'}</strong><small>{line.quantity} {line.quantity === 1 ? 'unidad' : 'unidades'}{line.line_total ? ` · importe ${money(line.line_total, result.currency)}` : ''}</small></div></header>
        <h4>Cómo se calculó</h4>
        <PriceCalculation explanation={line.explanation} currency={result.currency}/>
      </article>)}
    </section>}
  </>;
}
