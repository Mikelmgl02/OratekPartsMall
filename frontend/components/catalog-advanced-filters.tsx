'use client';

import { FormEvent, useEffect, useState } from 'react';
import { FileText, LoaderCircle, Plus, Search, Trash2 } from 'lucide-react';
import { request } from '@/lib/types';
import { CatalogTechnicalFilter, TechnicalField, TechnicalFilterRule, TechnicalFilterTemplate, TechnicalOperator, technicalOperators } from '@/lib/technical-types';
import Modal from './modal';

const emptyRule = (): TechnicalFilterRule => ({ key: '', operator: 'eq', value: '', label: '', unit: '' });
function operators(field?: TechnicalField): TechnicalOperator[] {
  return field?.kind === 'number' || field?.kind === 'integer' ? ['eq', 'between', 'gt', 'lt', 'gte', 'lte'] : field?.kind === 'text' ? ['contains', 'eq'] : ['eq'];
}

export default function CatalogAdvancedFilters({ accountId, initial, onApply, onClose }: {
  accountId: string; initial?: CatalogTechnicalFilter; onApply: (filter?: CatalogTechnicalFilter) => void; onClose: () => void;
}) {
  const [templates, setTemplates] = useState<TechnicalFilterTemplate[] | null>(null);
  const [templateId, setTemplateId] = useState(initial?.part_type || '');
  const [rules, setRules] = useState<TechnicalFilterRule[]>(initial?.rules || []);
  const [search, setSearch] = useState('');
  const [error, setError] = useState('');
  const [validation, setValidation] = useState('');
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    let live = true; setError('');
    request<{ part_types: TechnicalFilterTemplate[] }>(`/api/market/catalog/technical-filters?${new URLSearchParams({ account: accountId })}`)
      .then(data => { if (live) setTemplates(data.part_types); })
      .catch(cause => { if (live) setError(cause instanceof Error ? cause.message : 'No se pudieron cargar las plantillas.'); });
    return () => { live = false; };
  }, [accountId, retry]);
  const template = templates?.find(item => item.id === templateId);
  const visibleTemplates = templates?.filter(item => item.id === templateId || `${item.category} ${item.name}`.toLocaleLowerCase('es').includes(search.toLocaleLowerCase('es'))) || [];
  function patch(index: number, value: Partial<TechnicalFilterRule>) {
    setValidation(''); setRules(current => current.map((rule, i) => i === index ? { ...rule, ...value } : rule));
  }
  function submit(event: FormEvent) {
    event.preventDefault(); setValidation('');
    if (!template) { setValidation('Selecciona una plantilla técnica.'); return; }
    for (const rule of rules) {
      const field = template.fields.find(item => item.key === rule.key);
      if (!field || !operators(field).includes(rule.operator) || !rule.value.trim()) { setValidation('Completa cada condición antes de aplicar los filtros.'); return; }
      if (field.kind === 'number' || field.kind === 'integer') {
        if (!Number.isFinite(Number(rule.value)) || (rule.operator === 'between' && (!rule.value_to || !Number.isFinite(Number(rule.value_to)) || Number(rule.value) > Number(rule.value_to)))) {
          setValidation(`${field.label}: indica un rango válido, con el mínimo antes del máximo.`); return;
        }
      }
    }
    onApply({ part_type: template.id, label: `${template.category} / ${template.name}`, rules: rules.map(rule => ({ ...rule, value: rule.value.trim() })) });
  }
  return <Modal title="Filtros avanzados" wide className="technical-filter-modal" onClose={onClose}>
    <form className="technical-filter-form" onSubmit={submit}>
      <div className="technical-filter-intro"><span><FileText size={22} aria-hidden="true"/></span><div><h3>Buscar por ficha técnica</h3><p>Elige una plantilla y filtra sus medidas o características.</p></div></div>
      {error ? <div className="notice error" role="alert">{error}<button type="button" className="button soft small" onClick={() => setRetry(value => value + 1)}>Reintentar</button></div> : !templates ? <p className="loading" role="status"><LoaderCircle className="spin" size={18}/>Cargando plantillas…</p> : <>
        <div className="technical-template-picker">
          {templates.length > 8 && <label className="technical-template-search"><Search size={16} aria-hidden="true"/><input aria-label="Buscar plantilla técnica" placeholder="Buscar plantilla…" value={search} onChange={event => setSearch(event.target.value)}/></label>}
          <label>Plantilla técnica<select required value={templateId} onChange={event => { setTemplateId(event.target.value); setRules([]); setValidation(''); }}>
            <option value="">Selecciona una plantilla</option>
            {Array.from(new Set(visibleTemplates.map(item => item.category))).map(group => <optgroup label={group} key={group}>{visibleTemplates.filter(item => item.category === group).map(item => <option value={item.id} key={item.id}>{item.name}</option>)}</optgroup>)}
          </select></label>
          {!templates.length && <p>No hay plantillas con campos en tu catálogo todavía.</p>}
          {templates.length > 0 && !visibleTemplates.length && <p>No hay plantillas para esa búsqueda.</p>}
        </div>
        {template && <section className="technical-filter-conditions" aria-label="Condiciones técnicas">
          <div className="technical-filter-heading"><h3>Condiciones</h3><span>Coincidir con todas</span></div>
          {!rules.length && <p className="technical-filter-empty">Añade un campo para buscar por sus valores. Sin condiciones, verás todos los repuestos de esta plantilla.</p>}
          {rules.map((rule, index) => {
            const field = template.fields.find(item => item.key === rule.key);
            const numeric = field?.kind === 'number' || field?.kind === 'integer';
            return <div className="technical-filter-rule" key={index}>
              <label className="technical-rule-field">Campo<select aria-label={`Campo ${index + 1}`} required value={rule.key} onChange={event => {
                const next = template.fields.find(item => item.key === event.target.value);
                patch(index, { key: event.target.value, label: next?.label || '', unit: next?.unit || '', operator: operators(next)[0], value: '', value_to: '' });
              }}><option value="">Selecciona un campo</option>{Array.from(new Set(template.fields.map(item => item.section))).map(section => <optgroup label={section} key={section}>{template.fields.filter(item => item.section === section).map(item => <option value={item.key} key={item.key} disabled={rules.some((other, i) => i !== index && other.key === item.key)}>{item.label}{item.unit ? ` (${item.unit})` : ''}</option>)}</optgroup>)}</select></label>
              <label>Comparación<select aria-label={`Comparación ${index + 1}`} value={rule.operator} disabled={!field || operators(field).length === 1} onChange={event => patch(index, { operator: event.target.value as TechnicalOperator })}>{operators(field).map(operator => <option value={operator} key={operator}>{technicalOperators[operator]}</option>)}</select></label>
              <div className="technical-rule-values">{field?.kind === 'boolean' || field?.kind === 'choice' ? <label>Valor<select aria-label={`Valor ${index + 1}`} required value={rule.value} onChange={event => patch(index, { value: event.target.value })}><option value="">Selecciona</option>{field.kind === 'boolean' ? <><option value="true">Sí</option><option value="false">No</option></> : field.options.map(value => <option key={value}>{value}</option>)}</select></label> : <>
                <label>{rule.operator === 'between' ? 'Desde' : 'Valor'}{field?.unit && <span className="technical-unit"> · {field.unit}</span>}<input aria-label={`${rule.operator === 'between' ? 'Desde' : 'Valor'} ${index + 1}`} required disabled={!field} type={numeric ? 'number' : 'text'} step="any" maxLength={120} value={rule.value} onChange={event => patch(index, { value: event.target.value })} placeholder={numeric ? '0' : 'Escribe un valor'}/></label>
                {rule.operator === 'between' && <label>Hasta{field?.unit && <span className="technical-unit"> · {field.unit}</span>}<input aria-label={`Hasta ${index + 1}`} type="number" required step="any" value={rule.value_to || ''} onChange={event => patch(index, { value_to: event.target.value })} placeholder="0"/></label>}
              </>}</div>
              <button type="button" className="icon-button technical-rule-remove" aria-label={`Quitar condición ${index + 1}`} onClick={() => { setRules(current => current.filter((_, i) => i !== index)); setValidation(''); }}><Trash2 size={16}/></button>
            </div>;
          })}
          <button type="button" className="button soft small" disabled={rules.length >= Math.min(8, template.fields.length)} onClick={() => setRules(current => [...current, emptyRule()])}><Plus size={15}/>Añadir condición</button>
        </section>}
      </>}
      {validation && <div className="notice error" role="alert">{validation}</div>}
      <p className="technical-filter-note">Se combina con tu búsqueda y los demás filtros. Los rangos incluyen ambos límites; un dato sin completar no coincide con una condición.</p>
      <div className="technical-filter-footer"><button type="button" className="button text" disabled={!initial} onClick={() => onApply(undefined)}>Quitar filtro técnico</button><div><button type="button" className="button soft" onClick={onClose}>Cancelar</button><button className="button primary" disabled={!template || !!error}>Aplicar filtros<Search size={16}/></button></div></div>
    </form>
  </Modal>;
}
