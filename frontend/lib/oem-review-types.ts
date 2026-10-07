import type { CatalogGroupingCandidate } from './types';
import type { OEMRun } from './oem-finder-types';

export type OEMReviewTier = 'STRONG_PENDING_OWNER_TAGS' | 'CURRENT_REVIEW' | 'PROBABLE_BASE' | 'CURRENT_LIKELY_OEM' | 'LOCAL_XREF' | 'MULTI_OEM' | 'CONFLICT'
  | 'VARIANT_REVIEW' | 'UNKNOWN_SUFFIX' | 'WEAK' | 'NEEDS_AI';
export type OEMReviewStatus = 'review' | 'applied' | 'dismissed' | 'resolved';
export type OEMReviewAction = 'approve' | 'choose_oem' | 'send_to_merge' | 'dismiss' | 'reopen';
export type OEMDismissReason = 'not_oem' | 'other_part' | 'keep_code' | 'data_error' | 'other';
export type OEMReviewFilters = { status: OEMReviewStatus; tier: string; blocker: string; chain: string; make: string; system: string; in_stock: string; search: string };
export type OEMChainToken = { sep: string; tok: string; token: string; class: 'TAG' | 'VARIANT' | 'UNKNOWN'; kind: string; status: string; auto_eligible: boolean };
export type OEMChoice = { key: string; code: string; written: string; system: string; grade: number; brand: string };
export type OEMReviewEvidence = {
  lexicon: { result: string | null; share: number | null; n_other_keys: number | null; top_heads: [string, number][] | null; side: string | null };
  units: string[]; attributions: string[]; n_suppliers: number; source_counts: Record<string, number>; genuine_tokens: string[]; flags: string[];
  contradictions: string[]; verified_oem_alterno: boolean; second_unit: boolean; siblings: string[]; conflicts: { text: string; kind: string }[];
  conflict_kind: string; shared_with: string[]; family_incompatible: string[]; siblings_to_merge_into_this: string[]; inconsistent_siblings: string[];
  warnings: string[]; secondary_company_refs: { code: string; brand: string; segment: string }[]; pending_owner_tags: string[]; blocking_unknowns: string[];
  reasons: string[]; makes: string[]; make_source: string; head: string; aftermarket_family: string[]; apply_conflict: { detail: string; run: number | null } | null;
};
export type OEMReviewCase = {
  id: number; part_id: string; sku: string; description: string; evaluated_sku: string; evaluated_description: string; tier: OEMReviewTier; tier_label: string;
  underlying_tier: string; status: OEMReviewStatus; fingerprint: string; candidate: string; written_form: string; brand: string; system: string; grade: string;
  chain: string; chain_tokens: OEMChainToken[]; blockers: string[]; in_stock: boolean; available_quantity: number; method: 'flag' | 'rename';
  evidence: OEMReviewEvidence; choices: OEMChoice[]; codes: { code: string; brand: string; ref_type: string }[];
  supplier_codes: { codigo: string; brand: string; supplier: string; available: number }[]; stale: string; stale_label: string; actions: OEMReviewAction[];
  decision: { action?: string; run?: number; change?: number; code?: string; brand?: string; method?: string; label?: string; note?: string; target_sku?: string; previous_sku?: string; bulk?: boolean; detail?: string };
  decided_by: string | null; decided_at: string | null;
};
export type OEMLastRun = { id: number; finished_at: string; suffix_table_version: string; tiers: Record<string, [number, number]>; scenario: { tokens?: string[]; tiers?: Record<string, number> }; cases: Record<string, number> };
export type OEMReviewPage = {
  count: number; next: string | null; previous: string | null; results: OEMReviewCase[]; tiers: Partial<Record<OEMReviewTier, number>>;
  statuses: Partial<Record<OEMReviewStatus, number>>; facets: { blockers: [string, number][]; chains: [string, number][]; makes: [string, number][]; systems: [string, number][] };
  last_run: OEMLastRun | null;
};
export type OEMReviewSummary = { statuses: Partial<Record<OEMReviewStatus, number>>; review: number };
export type OEMDecisionResult = { case: OEMReviewCase; run: number | null; change: number | null; family: (CatalogGroupingCandidate & { oem: { code: string; brand: string } }) | null };
export type OEMBulkSample = { case: number; sku: string; description: string; tier: string; code: string; brand: string; method: 'flag' | 'rename'; outcome: 'applied' | 'conflict' | 'skipped'; detail: string };
export type OEMBulkPreview = {
  count: number; selected: number; limit: number; batch_size: number; excluded: Record<string, number>; tiers: Record<string, number>; methods: Partial<Record<'flag' | 'rename', number>>;
  sample: OEMBulkSample[]; selection: string; halt: { reason: string; created_at: string } | null; running: number | null;
};
export type OEMBulkProgress = { run: OEMRun; finished: boolean; waiting: string };

export const reviewTiers: OEMReviewTier[] = ['STRONG_PENDING_OWNER_TAGS', 'CURRENT_REVIEW', 'PROBABLE_BASE', 'CURRENT_LIKELY_OEM', 'LOCAL_XREF', 'MULTI_OEM', 'CONFLICT',
  'VARIANT_REVIEW', 'UNKNOWN_SUFFIX', 'WEAK', 'NEEDS_AI'];
export const reviewTierLabels: Record<OEMReviewTier, string> = {
  STRONG_PENDING_OWNER_TAGS: 'PENDIENTE DE ETIQUETAS', CURRENT_REVIEW: 'ACTUAL (REVISAR)', PROBABLE_BASE: 'BASE PROBABLE', CURRENT_LIKELY_OEM: 'ACTUAL PROBABLE',
  LOCAL_XREF: 'REFERENCIA LOCAL', MULTI_OEM: 'VARIOS OEM', CONFLICT: 'CONFLICTO (AGRUPAR)', VARIANT_REVIEW: 'VARIANTE', UNKNOWN_SUFFIX: 'SUFIJO DESCONOCIDO',
  WEAK: 'DÉBIL', NEEDS_AI: 'REQUIERE IA',
};
export const bulkTiers: OEMReviewTier[] = ['STRONG_PENDING_OWNER_TAGS', 'CURRENT_REVIEW', 'PROBABLE_BASE', 'CURRENT_LIKELY_OEM', 'LOCAL_XREF'];
export const reviewStatusLabels: Record<OEMReviewStatus, string> = { review: 'POR REVISAR', applied: 'APROBADOS', dismissed: 'DESCARTADOS', resolved: 'RESUELTOS' };
export const dismissReasons: Record<OEMDismissReason, string> = {
  not_oem: 'EL NÚMERO NO ES EL OEM DE ESTA PIEZA', other_part: 'EL OEM ES DE OTRA PIEZA, LADO O VARIANTE', keep_code: 'CONSERVAR EL CÓDIGO ACTUAL COMO MAIN',
  data_error: 'EL SKU O LA DESCRIPCIÓN TIENEN UN ERROR', other: 'OTRO MOTIVO',
};
export const excludedLabels: Record<string, string> = {
  tier: 'en niveles que no se aprueban en lote (varios OEM, conflicto, variante, sufijo desconocido, débil o IA)', stale: 'cambiaron desde el análisis (el SKU o un sufijo de su cadena)',
  previously_reverted: 'se revirtieron antes con el mismo OEM', over_limit: 'quedan para otra ejecución (máximo por ejecución)',
};
export const reviewSkipLabels: Record<string, string> = {
  case_changed: 'EL CASO CAMBIÓ', snapshot_changed: 'EL SKU CAMBIÓ', suffix_changed: 'UN SUFIJO SE RECLASIFICÓ', new_claimant: 'OTRO SKU RECLAMA EL NÚMERO', retired: 'RETIRADO O AGRUPADO',
  missing: 'YA NO EXISTE',
};
const conflictKinds: Record<string, string> = {
  shared_base_family: 'FAMILIA COMPARTIDA', base_is_existing_active_part: 'EL OEM YA ES OTRO SKU ACTIVO', base_is_retired_part_sku: 'EL OEM ES UN SKU RETIRADO',
  base_is_alterno_of_other_part: 'EL OEM ES ALTERNO DE OTRO SKU', base_is_retired_sku_already_merged_into_this_part: 'EL OEM ES UN SKU YA AGRUPADO AQUÍ',
};
const flagLabels: Record<string, string> = {
  platform_partner: 'MARCA ASOCIADA (PLATAFORMA)', make_resolved_by_lexicon: 'MARCA DEDUCIDA POR LÉXICO', make_resolved_by_exclusive_shape: 'MARCA DEDUCIDA POR FORMA',
  make_ambiguous: 'MARCA AMBIGUA', make_from_format: 'MARCA DEDUCIDA DEL FORMATO', o_as_zero: 'LETRA O EN LUGAR DE 0', packed: 'NÚMERO SIN GUIONES',
  glued_suffix: 'SUFIJO PEGADO', prefix_stripped: 'PREFIJO QUITADO', ford_typed_as_mazda: 'FORD ESCRITO COMO MAZDA', isuzu_no_revision_digit: 'ISUZU SIN DÍGITO DE REVISIÓN',
};
const blockerLabels: Record<string, string> = {
  no_second_unit: 'SIN SEGUNDA EVIDENCIA INDEPENDIENTE', component_conflict: 'COMPONENTE EN UNA CLASE DE CONJUNTO', other_oem_candidates: 'HAY OTROS CANDIDATOS OEM',
  both_sides_on_side_specific_class: 'AMBOS LADOS EN UNA CLASE CON LADO', sibling_family_inconsistent: 'HERMANOS INCOMPATIBLES', extra_sku_segment: 'SEGMENTO EXTRA EN EL SKU',
  redundant_variant: 'VARIANTE REDUNDANTE EN EL SKU', side_not_confirmed_by_class: 'LADO NO CONFIRMADO POR LA CLASE', family_incompatible: 'FAMILIA INCOMPATIBLE',
  apply_conflict: 'EL CATÁLOGO RECHAZÓ EL CAMBIO', contradiction: 'CONTRADICCIÓN', unconfirmed_tag: 'ETIQUETA SIN CONFIRMAR', unknown: 'SUFIJO DESCONOCIDO',
  conflict: 'CONFLICTO', system: 'SISTEMA SIN APLICACIÓN AUTOMÁTICA', lexicon_not_strong: 'LÉXICO NO FUERTE', desc_variant: 'DESCRIPCIÓN DE VARIANTE O PAQUETE',
  make_source: 'MARCA NO EXPLÍCITA', make_groups: 'VARIAS MARCAS', flag: 'BANDERA', assembly_or_kit_head: 'CONJUNTO O KIT',
};
const lexiconLabels: Record<string, string> = { agree_strong: 'COINCIDE FUERTE', agree: 'SOLO COINCIDE', disagree: 'NO COINCIDE', weak: 'DÉBIL', no_data: 'SIN DATOS', 'n/a': 'NO APLICA', None: 'SIN DATOS' };
export const lexiconLabel = (result: string | null) => lexiconLabels[result || 'no_data'] || (result || '').toUpperCase();

export function blockerLabel(blocker: string) {
  const [head, ...rest] = blocker.split(':');
  const detail = rest.join(':');
  if (!detail) return blockerLabels[head] || blocker.toUpperCase();
  if (head === 'conflict') return `CONFLICTO: ${conflictKinds[detail] || detail.toUpperCase()}`;
  if (head === 'flag') return flagLabels[detail] || `BANDERA ${detail.toUpperCase()}`;
  if (head === 'lexicon_not_strong') return `LÉXICO ${lexiconLabel(detail)}`;
  if (head === 'system') return `SISTEMA ${detail} SIN APLICACIÓN AUTOMÁTICA`;
  if (head === 'make_source') return `MARCA POR ${({ hint: 'MODELO', shape: 'FORMA DEL CÓDIGO', none: 'NINGUNA', format: 'FORMATO' } as Record<string, string>)[detail] || detail.toUpperCase()}`;
  if (head === 'make_groups') return `${detail} GRUPOS DE MARCA`;
  if (head === 'desc_variant') return `DESCRIPCIÓN CON «${detail}»`;
  if (head === 'unconfirmed_tag') return `ETIQUETA ${detail} SIN CONFIRMAR`;
  if (head === 'unknown') return `SUFIJO DESCONOCIDO ${detail}`;
  if (head === 'assembly_or_kit_head') return `CONJUNTO O KIT (${detail})`;
  if (head === 'contradiction') return `CONTRADICCIÓN: ${detail.split(',').map(item => item.replace(/_/g, ' ').toUpperCase()).join(', ')}`;
  return `${blockerLabels[head] || head.toUpperCase()}: ${detail}`;
}
export const conflictKindLabel = (kind: string) => conflictKinds[kind] || kind.toUpperCase();
export const unitLabels: Record<string, string> = { lexicon_agree: 'LÉXICO', corroborated: 'CORROBORADO', genuine_claim: 'ETIQUETA GENUINA' };
