'use client';
import { ArrowRight, Search } from 'lucide-react';
import { PartPicker } from './admin-catalog-editors';
import { batchLabels, CASE_KINDS, caseKindLabels, caseStatusLabel } from '@/lib/line-types';
import type { CaseAction, CaseDecision, CaseRow, MatchingCase } from '@/lib/line-types';

// One card per case kind of matching on References (plan S6.3, moved here in S8 so the Matching Cases queue and Agrupación inteligente
// share them). Variants waiting on a code change or a variant case are out of stock, so each one is decidable on its card.
type Code = { brand: string; code: string };
export const guardLabels: Record<string, string> = { prefix: 'El prefijo del código no coincide.', noun: 'La descripción no coincide.', side: 'El lado no coincide (izquierdo / derecho, delantero / trasero).',
  make: 'La marca del vehículo no coincide.', subgrupo: 'Las líneas OEM tienen subgrupos distintos.', spec_conflict: 'Las especificaciones no coinciden con las de la línea OEM.',
  separated: 'Estas líneas OEM se mantuvieron separadas antes.', disputa: 'La referencia está en disputa.',
  lineas: 'El código está en varias líneas OEM.', catalogo: 'Un catálogo no revisado contra el PDF lo vincula.',
  sin_codigo: 'El artículo no tiene código: corrígelo con el proveedor o elige su línea OEM.', codigo_largo: 'El código supera 120 caracteres.',
  conflicto: 'El código choca con el de otra línea OEM.', en_catalogo: 'El código ya es de una línea OEM del catálogo.',
  unificada: 'La línea OEM del código se unificó en otra.', colocacion: 'No se pudo crear su producto nuevo.',
  sufijo: 'El sufijo del código no es una marca conocida (origen, genuino, comercial).' };
const estadoLabels: Record<string, string> = { preparacion: 'EN PREPARACIÓN', referencia: 'REFERENCIA', rechazada: 'RECHAZADA', retirada: 'RETIRADA', unificada: 'UNIFICADA' };
const evidenceLabels: Record<string, string> = { numbers: 'NÚMEROS OEM', codes: 'CÓDIGOS', citations: 'FUENTES' };
export const decisionDone: Record<string, string> = {
  'code_change:approve': 'Cambio de código aprobado.',
  'code_change:dismiss': 'Cambio de código rechazado. El artículo conserva su código actual.',
  'reference:approve': 'Referencia aprobada en la línea OEM.',
  'reference:dismiss': 'Referencia rechazada. No se repetirá mientras sus datos sean iguales.',
  approve: 'Artículo vinculado a la línea OEM elegida.',
  dismiss: 'Caso descartado. No se repetirá mientras sus datos sean iguales.',
  separar_referencia: 'Referencia separada: el código queda como interno de este proveedor y el artículo pasa a Productos nuevos.',
  merge: 'Líneas OEM unificadas. Puedes separarlas después con Separar SKU.',
  keep_apart: 'Las líneas OEM se mantendrán separadas y no se volverán a proponer juntas.',
};
export const refList = (refs: Code[]) => refs.map(ref => [ref.brand, ref.code].filter(Boolean).join(': ')).join(' · ');
export const isOpen = (item: MatchingCase) => item.status === 'review';
export const isKnownKind = (item: MatchingCase) => (CASE_KINDS as string[]).includes(item.kind);
// The lines a case decides between: its own lines (S6), else the candidates (and, for a merge, the sources too), once each.
export function caseLines(item: MatchingCase): CaseRow[] {
  const rows: CaseRow[] = item.lines?.length ? item.lines : item.kind === 'merge' ? [...item.sources, ...item.candidates] : item.candidates;
  return rows.filter((row, index) => row.id && rows.findIndex(other => other.id === row.id) === index);
}
// The card's accessible name: its kind and what it decides (the item's code, or the lines a merge joins).
export const caseTitle = (item: MatchingCase) => `${caseKindLabels[item.kind] ?? item.kind.toUpperCase()} ${item.kind === 'merge' ? caseLines(item).map(row => row.sku).join(' + ') : item.sources[0]?.sku ?? ''}`.trim();
// Evidence values may be codes, numbers or citations: strings, or objects with a code and a brand.
export function evidenceText(value: unknown): string {
  if (typeof value === 'string' || typeof value === 'number') return String(value);
  if (Array.isArray(value)) return value.map(evidenceText).filter(Boolean).join(' · ');
  if (value && typeof value === 'object') {
    const row = value as Record<string, unknown>;
    return [row.brand ?? row.manufacturer, row.code ?? row.number, row.catalog ?? row.citation].filter(part => typeof part === 'string' && part).join(': ');
  }
  return '';
}
export const decisionMessage = (item: MatchingCase, action: CaseAction) => decisionDone[`${item.kind}:${action}`] ?? decisionDone[action];

// The card's frame: who it is about, its kind, its lote and its status.
export function CaseArticle({ item, children }: { item: MatchingCase; children: React.ReactNode }) {
  return <article className={`inventory-ai-proposal ${item.status}`} aria-label={caseTitle(item)}>
    <div className="inventory-ai-proposal-heading"><strong>{item.supplier_name || (item.kind === 'merge' ? 'LÍNEAS DEL CATÁLOGO' : 'FAMILIA DEL CATÁLOGO')}</strong>
      <span className="matching-case-pills">{isKnownKind(item) && <span className="pill">{caseKindLabels[item.kind]}</span>}{item.route === 'lote' && <span className="pill">LOTE · {batchLabels[item.batch_key || ''] ?? item.batch_key}</span>}<span className="pill">{caseStatusLabel[item.status] ?? item.status.toUpperCase()}</span></span></div>
    {children}
  </article>;
}

type CardProps = { item: MatchingCase; busy: boolean; line: string; onLine: (value: string) => void; searching: boolean; onSearch: () => void; picking: boolean; onPick: () => void;
  onDecide: (decision: CaseDecision, message: string) => void };

export function CaseCard({ item, busy, line, onLine, searching, onSearch, picking, onPick, onDecide }: CardProps) {
  const choices = caseLines(item);
  const proposed = choices.find(row => row.sku === item.target_sku)?.id ?? '';
  const aiLine = item.ai.target_sku ? choices.find(row => row.sku === item.ai.target_sku)?.id : undefined;
  const source = item.sources[0];
  const evidence = Object.entries(evidenceLabels).map(([field, label]) => [label, evidenceText(item.evidence?.[field])] as const).filter(([, text]) => text);
  const lineChoices = (legend: string, selected: string, searchable: boolean) => <fieldset className="matching-lines"><legend>{legend}</legend>
    {choices.map(row => <label key={row.id} className={`matching-line${selected === row.id ? ' selected' : ''}`}>
      <input type="radio" name={`linea-${item.id}`} value={row.id} checked={selected === row.id} disabled={!isOpen(item) || busy} onChange={() => onLine(row.id)}/>
      <span><strong>{row.sku}{row.estado && row.estado !== 'publicada' && <em className="pill">{estadoLabels[row.estado] ?? row.estado.toUpperCase()}</em>}{row.id === aiLine && <em className="pill">PROPUESTA IA</em>}</strong>
        <small>{row.description || (row.name !== row.sku ? row.name : '') || 'SIN DESCRIPCIÓN'}</small>{!!row.codes?.length && <small>REFERENCIAS: {refList(row.codes)}</small>}</span>
    </label>)}
    {searchable && isOpen(item) && (searching
      ? <PartPicker label="línea OEM" value={choices.some(row => row.id === selected) ? '' : selected} onChange={onLine}/>
      : <button type="button" className="button text small" disabled={busy} onClick={onSearch}><Search size={14} aria-hidden="true"/>Buscar otra línea OEM</button>)}
  </fieldset>;
  const itemSummary = source && <p><strong>{source.sku}</strong>{source.brand && <> · {source.brand}</>}{source.supplier_invent_id && <small> · ID {source.supplier_invent_id}</small>}<br/>{source.description || 'SIN DESCRIPCIÓN'}
    {!!source.references?.length && item.kind !== 'reference' && <small><br/>REFERENCIAS DECLARADAS: {refList(source.references)}</small>}
    {item.reference?.line_sku && <small><br/>LÍNEA OEM DEL ARTÍCULO: {item.reference.line_sku}</small>}</p>;
  const why = <>{item.reason && <p>{item.reason}</p>}{item.guard && <p className="matching-guard">{guardLabels[item.guard] ?? item.guard}</p>}
    {evidence.map(([label, text]) => <p key={label} className="matching-evidence"><small>{label}: {text}</small></p>)}
    {item.ai.reason && <div className="notice"><strong>PROPUESTA IA{item.ai.target_sku ? `: ${item.ai.target_sku}` : ''}</strong><p>{item.ai.reason}</p></div>}
    {item.ai.error && <div className="notice error">{item.ai.error}</div>}</>;
  const hold = <p className="matching-hold">Mientras se decide, este artículo no cuenta en las existencias.</p>;
  const say = (action: CaseAction) => decisionMessage(item, action);

  if (item.kind === 'code_change') {
    const fresh = item.new_code || source?.codigo_pendiente || '';
    const freshBrand = item.new_brand || source?.brand_pendiente || source?.brand || '';
    return <>
      {itemSummary}
      <div className="matching-code-change" role="group" aria-label="Cambio de código">
        <p><small>CÓDIGO ACTUAL</small><strong>{source?.sku}</strong><span>{source?.brand || 'SIN MARCA'}</span></p>
        <ArrowRight size={18} aria-hidden="true"/>
        <p><small>CÓDIGO NUEVO</small><strong>{fresh}</strong><span>{freshBrand || 'SIN MARCA'}</span></p>
      </div>
      {!!choices.length && <p className="matching-evidence"><small>EL CÓDIGO NUEVO YA ESTÁ EN: {choices.map(row => row.sku).join(' · ')}</small></p>}
      {why}{isOpen(item) && <>{hold}<div className="admin-toolbar-actions">
        <button className="button primary small" disabled={busy} onClick={() => onDecide({ action: 'approve' }, say('approve'))}>Aprobar</button>
        <button className="button soft small" disabled={busy} onClick={() => onDecide({ action: 'dismiss' }, say('dismiss'))}>Rechazar</button>
      </div></>}
    </>;
  }
  if (item.kind === 'reference') {
    const chosen = picking ? line : '';
    return <>
      {itemSummary}
      <p className="matching-code"><small>REFERENCIA DECLARADA</small><strong>{item.new_code}</strong><span>{item.new_brand || 'TODAS LAS MARCAS'}</span></p>
      {why}
      {isOpen(item) && <>
        {picking && lineChoices('Línea OEM que recibe la referencia', chosen, true)}
        <div className="admin-toolbar-actions">
          <button className="button primary small" disabled={busy || (picking && !chosen)} onClick={() => onDecide(chosen ? { action: 'approve', line: chosen } : { action: 'approve' }, say('approve'))}>{picking ? 'Aprobar en la línea OEM elegida' : 'Aprobar'}</button>
          <button className="button soft small" disabled={busy} aria-pressed={picking} onClick={onPick}>{picking ? 'Cancelar elección' : 'Elegir línea OEM'}</button>
          <button className="button soft small" disabled={busy} onClick={() => onDecide({ action: 'dismiss' }, say('dismiss'))}>Rechazar</button>
        </div>
      </>}
    </>;
  }
  if (item.kind === 'merge') {
    const keep = isOpen(item) ? line || proposed || choices[0]?.id || '' : proposed;  // decided: only the line it was proposed to keep
    return <>
      {why}
      {lineChoices(isOpen(item) ? 'Línea OEM que se conserva' : 'Líneas OEM', keep, false)}
      {isOpen(item) && <div className="admin-toolbar-actions">
        <button className="button primary small" disabled={busy || choices.length < 2 || !keep} onClick={() => onDecide({ action: 'merge', line: keep }, say('merge'))}>Unificar</button>
        <button className="button soft small" disabled={busy} onClick={() => onDecide({ action: 'keep_apart' }, say('keep_apart'))}>Mantener separadas</button>
      </div>}
    </>;
  }
  // variant and line: the item waits for a line, chosen by id (Vincular), or keeps its code as its own (Separar referencia).
  const chosen = line || proposed;
  return <>
    {itemSummary}{why}
    {lineChoices(isOpen(item) ? 'Línea OEM de destino' : 'Líneas OEM propuestas', chosen, true)}
    {isOpen(item) && <>{hold}<div className="admin-toolbar-actions">
      <button className="button primary small" disabled={busy || !chosen} onClick={() => onDecide({ action: 'approve', line: chosen }, say('approve'))}>Vincular</button>
      <button className="button soft small" disabled={busy} onClick={() => onDecide({ action: 'separar_referencia' }, say('separar_referencia'))}>Separar referencia</button>
    </div></>}
  </>;
}

