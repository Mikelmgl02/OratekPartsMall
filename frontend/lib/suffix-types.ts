export type SuffixClass = 'TAG' | 'VARIANT' | 'UNKNOWN';
export type SuffixStatus = 'seed_confirmed' | 'needs_owner_confirmation' | 'needs_owner_label' | 'owner_confirmed';
export type SuffixScope = { heads: string[]; cls: SuffixClass; kind: string; attribution: string; confidence: string };
export type SuffixRunStats = {
  chain_rows_all?: number; chain_rows_in_stock?: number; by_tier?: Record<string, number>;
  unknown_unlock_if_labeled_TAG?: { skus_blocked_all: number; skus_blocked_in_stock?: number; unlock_alone_all: number; unlock_alone_in_stock?: number };
  owner_confirmation_effect?: { auto_if_alone_all?: number; auto_if_all_listed_confirmed_all: number; rows_in_chain_all?: number };
};
export type SuffixStats = { count_all?: number; count_in_stock?: number; examples?: string[]; run?: SuffixRunStats };
export type CatalogSuffix = {
  token: string; match: 'exact' | 'shape'; alias_of: string | null; aliases: string[]; cls: SuffixClass; tag_kind: string; variant_kind: string;
  attribution: string; creates_company_reference: boolean; scope_overrides: SuffixScope[]; confidence: 'low' | 'medium' | 'high';
  status: SuffixStatus; owner_confirmed: boolean; confirmed_by: string | null; confirmed_at: string | null; auto_eligible: boolean;
  proposed_label: string; legacy_company_registry: boolean; notes: string; stats: SuffixStats; updated_at: string; version: number;
};
export type SuffixChange = { action: string; actor: string | null; before: Record<string, unknown>; after: Record<string, unknown>; note: string; version: number; created_at: string };
export type CatalogSuffixDetail = CatalogSuffix & { changes: SuffixChange[] };
export type CatalogSuffixPage = {
  count: number; next: string | null; previous: string | null; results: CatalogSuffix[]; version: number;
  counts: { cls: Partial<Record<SuffixClass, number>>; status: Partial<Record<SuffixStatus, number>>; has_unlock: number };
  company_suffixes: Record<string, string>;
};
export type SuffixAction = 'confirm_tag' | 'set_variant' | 'set_unknown' | 'note' | 'add_alias' | 'add_scope_override' | 'remove_scope_override';
export type CompanySuffixes = { suffixes: Record<string, string>; registry: 'settings' | 'table' | 'fallback' };

export const classLabels: Record<SuffixClass, string> = { TAG: 'ETIQUETA', VARIANT: 'VARIANTE', UNKNOWN: 'DESCONOCIDO' };
export const statusLabels: Record<SuffixStatus, string> = {
  owner_confirmed: 'CONFIRMADO POR EL PROPIETARIO', seed_confirmed: 'CONFIRMADO EN LA SEMILLA',
  needs_owner_confirmation: 'POR CONFIRMAR', needs_owner_label: 'POR ETIQUETAR',
};
export const tagKindLabels: Record<string, string> = {
  company_code: 'CÓDIGO DE EMPRESA', brand: 'MARCA', house_brand: 'MARCA PROPIA', origin: 'ORIGEN', genuine: 'GENUINO',
  supplier_marker: 'MARCADOR DE PROVEEDOR', commercial: 'COMERCIAL', lifecycle: 'CICLO DE VIDA',
};
export const variantKindLabels: Record<string, string> = {
  position: 'POSICIÓN', size: 'MEDIDA', kit: 'KIT', material: 'MATERIAL', spec: 'ESPECIFICACIÓN', version: 'VERSIÓN', color: 'COLOR',
  package: 'EMPAQUE', descriptor: 'DESCRIPTOR', market: 'MERCADO', alt_oem_tail: 'OEM ALTERNO', compound: 'COMPUESTO',
};
export const actionLabels: Record<string, string> = {
  seed_confirm: 'Confirmado por el propietario en la semilla', confirm_tag: 'Etiqueta confirmada', set_variant: 'Marcado como variante',
  set_unknown: 'Marcado como desconocido', note: 'Notas actualizadas', add_alias: 'Alias agregado', set_alias: 'Convertido en alias',
  add_scope_override: 'Alcance agregado', remove_scope_override: 'Alcance quitado',
};
export const companyKinds = ['brand', 'company_code', 'house_brand'];
// Same rule as catalog_identity.company_reference: the token after the last '-' names the company.
export const LEGACY_COMPANY_SUFFIXES: Record<string, string> = { FEB: 'FEBEST', FEBEST: 'FEBEST' };
export function companyCode(code: string, suffixes: Record<string, string>) {
  const value = code.trim().toUpperCase(), cut = value.lastIndexOf('-');
  const suffix = cut > 0 ? value.slice(cut + 1) : '';
  return cut > 0 && Object.hasOwn(suffixes, suffix) ? { code: value.slice(0, cut), brand: suffixes[suffix].trim().toUpperCase() } : null;
}
export const kindLabel = (row: Pick<CatalogSuffix, 'tag_kind' | 'variant_kind'>) => tagKindLabels[row.tag_kind] || variantKindLabels[row.variant_kind] || 'SIN TIPO';
