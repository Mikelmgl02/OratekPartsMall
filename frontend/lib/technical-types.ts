export type PartType = { id: string; category: string; name: string; part_count: number; field_count?: number };
export type TechnicalField = { key: string; label: string; section: string; kind: 'number' | 'integer' | 'boolean' | 'choice' | 'text'; unit: string; options: string[]; required: boolean; position: number };
export type TechnicalTemplate = { part_type: string; revision: number; fields: TechnicalField[] };
export type TechnicalFilterTemplate = Pick<PartType, 'id' | 'category' | 'name'> & { fields: TechnicalField[] };
export type TechnicalOperator = 'eq' | 'between' | 'gt' | 'lt' | 'gte' | 'lte' | 'contains';
export type TechnicalFilterRule = { key: string; operator: TechnicalOperator; value: string; value_to?: string; label: string; unit: string };
export type CatalogTechnicalFilter = { part_type: string; label: string; rules: TechnicalFilterRule[] };
export const technicalOperators: Record<TechnicalOperator, string> = { eq: 'Exacto', between: 'Entre', gt: 'Mayor que', lt: 'Menor que', gte: 'Mayor o igual', lte: 'Menor o igual', contains: 'Contiene' };
export function technicalRuleLabel(rule: TechnicalFilterRule) {
  const symbols = { eq: '=', between: 'entre', gt: '>', lt: '<', gte: '≥', lte: '≤', contains: 'contiene' };
  const value = rule.value === 'true' ? 'Sí' : rule.value === 'false' ? 'No' : rule.value;
  return `${rule.label} ${symbols[rule.operator]} ${value}${rule.operator === 'between' ? ` y ${rule.value_to}` : ''}${rule.unit ? ` ${rule.unit}` : ''}`;
}
export type VehicleApplication = { id: string; make: string; model: string; generation: string; year_from: number; year_to: number; engine: string; trim: string; transmission: string; market: string };
export type ApplicationLink = { application: VehicleApplication; position: string; notes: string; source?: string; verified?: boolean };
export type SpecificationValue = { value: string | number | boolean | null; unit?: string; source?: string };
export type TechnicalSheet = { part: string; part_type: PartType | null; revision: number; template: TechnicalTemplate | null; values: Record<string, SpecificationValue>; applications: ApplicationLink[]; missing_required: string[] };
export const units = ['', 'mm', 'cm', 'm', 'in', 'g', 'kg', 'lb', 'bar', 'kPa', 'psi', 'V', 'W', 'Nm', 'ml', 'l', '°C'];
export const kindLabels = { number: 'MEDIDA / DECIMAL', integer: 'NÚMERO ENTERO', boolean: 'SÍ / NO', choice: 'SELECCIÓN', text: 'TEXTO' };
export const applicationLabel = (a: VehicleApplication) => [a.make, a.model, a.generation, `${a.year_from}–${a.year_to}`, a.engine, a.trim, a.transmission, a.market].filter(Boolean).join(' · ');
