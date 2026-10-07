export type OEMReferenceStatus = 'verified' | 'declared' | 'inferred' | 'disputed';
export type OEMSourceKind = 'price_list' | 'manufacturer_catalog' | 'aftermarket_catalog' | 'ai_lookup' | 'supplier_declaration' | 'catalog_approval' | 'oem_finder' | 'manual';
export type OEMManualSourceKind = 'manual' | 'price_list' | 'manufacturer_catalog' | 'aftermarket_catalog' | 'supplier_declaration';
export type OEMReferenceLink = { id: string; manufacturer: string; code: string; status: OEMReferenceStatus };
export type OEMLinkedSKU = { id: string; sku: string; is_OEM: boolean; active: boolean; via: 'sku' | 'oem' | 'company' | 'unknown'; code: string };
export type OEMSourceDetail = { run?: number; rule?: string; rules_version?: string; verified?: boolean; approved?: boolean; supersedes?: string;
  part_codes?: { id: number; part: string; code: string }[] };
export type OEMReferenceSource = {
  id: number; kind: OEMSourceKind; kind_label: string; citation: string; detail: OEMSourceDetail; created_by: string | null; created_at: string; removable: boolean;
};
export type OEMReference = {
  id: string; manufacturer: string; code: string; printed_forms: string[]; system: string; family: string; part_type: string; description: string;
  applications: string; status: OEMReferenceStatus; status_label: string; dispute_note: string; superseded_by: OEMReferenceLink | null; notes: string;
  source_count: number; source_kinds: Partial<Record<OEMSourceKind, number>>; linked_skus: OEMLinkedSKU[]; linked_count: number;
  created_by: string | null; updated_by: string | null; created_at: string; updated_at: string; version: number;
};
export type OEMReferenceDetail = OEMReference & { sources: OEMReferenceSource[]; supersedes: OEMReferenceLink[] };
export type OEMReferencePage = {
  count: number; next: string | null; previous: string | null; results: OEMReference[];
  counts: { status: Partial<Record<OEMReferenceStatus, number>>; manufacturer: Record<string, number>; total: number };
};
export type OEMReferenceAction = 'edit' | 'add_source' | 'remove_source' | 'dispute' | 'clear_dispute' | 'set_superseded_by' | 'clear_superseded_by';
export type OEMSourceInput = { kind: OEMManualSourceKind; citation: string; verified?: boolean };
export type OEMReferenceConflict = { detail: string; reference: OEMReferenceDetail };

export const referenceStatusLabels: Record<OEMReferenceStatus, string> = { verified: 'VERIFICADO', declared: 'DECLARADO', inferred: 'INFERIDO', disputed: 'EN DISPUTA' };
export const referenceStatusHints: Record<OEMReferenceStatus, string> = {
  verified: 'Una lista de precios, el catálogo del fabricante, un registro comprobado o una búsqueda con IA aprobada en el catálogo.',
  declared: 'Un catálogo de marca de repuesto, un proveedor, una aprobación en el catálogo o un registro manual sin comprobar.',
  inferred: 'Solo el buscador OEM lo propone.', disputed: 'Alguien lo marcó en disputa; las fuentes nuevas no cambian el estado hasta quitar la disputa.',
};
export const sourceKindLabels: Record<OEMSourceKind, string> = {
  price_list: 'LISTA DE PRECIOS', manufacturer_catalog: 'CATÁLOGO DEL FABRICANTE', aftermarket_catalog: 'CATÁLOGO DE REPUESTO', ai_lookup: 'BÚSQUEDA CON IA',
  supplier_declaration: 'DECLARACIÓN DE PROVEEDOR', catalog_approval: 'APROBACIÓN EN EL CATÁLOGO', oem_finder: 'BUSCADOR OEM', manual: 'REGISTRO MANUAL',
};
export const manualSourceKinds: OEMManualSourceKind[] = ['manual', 'price_list', 'manufacturer_catalog', 'aftermarket_catalog', 'supplier_declaration'];
export const linkedViaLabels: Record<OEMLinkedSKU['via'], string> = { sku: 'SKU PRINCIPAL', oem: 'ALTERNO OEM', company: 'ALTERNO DE EMPRESA', unknown: 'ALTERNO' };
export const isLink = (citation: string) => /^https?:\/\//i.test(citation);
