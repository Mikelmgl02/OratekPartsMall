// Productos nuevos (mall.sku_proposal_views): new catalog SKUs proposed from supplier items that match nothing in the catalog.
export type ProposalStatus = 'pending' | 'approved' | 'linked' | 'rejected' | 'resolved';
export type FieldSource = { source: 'supplier' | 'oem_library' | 'admin' | 'ai'; by?: string; at?: string; confidence?: number };
export type ProposalItem = { id: string; supplier: string; supplier_name: string; supplier_invent_id: string; codigo: string; brand: string;
  description: string; references: { brand: string; code: string; ref_type?: string }[]; source: 'upload' | 'apiag'; available: number; matching_status: string };
export type Proposal = { id: string; status: ProposalStatus; status_label: string; version: number; sku: string; description: string; brand: string;
  oem: { code: string; brand: string } | null; part_type: { id: string; category: string; name: string } | null;
  references: { brand: string; code: string; ref_type?: string }[]; field_sources: Partial<Record<'sku' | 'description' | 'oem' | 'references' | 'part_type', FieldSource>>;
  item_count: number; supplier_count: number; stock: number; items: ProposalItem[]; part: { id: string; sku: string } | null;
  decided_by: string | null; decided_at: string | null; reject_reason: string; reject_reason_label: string; reject_note: string; created_at: string; updated_at: string;
  earlier: { id: string; sku: string } | null;   // the same product approved or linked before: its items go to that SKU
  catalog_match?: { id: string; sku: string; code: string; active: boolean }[] };   // the detail only: catalog SKUs its code or OEM number already belongs to
export type ProposalSummary = Record<ProposalStatus, number>;
export type BulkResult = { approved: number; results: { id: string; ok: boolean; sku?: string; message?: string }[] };

export const proposalStatusLabels: Record<ProposalStatus, string> = { pending: 'Por revisar', approved: 'Aprobados', linked: 'Vinculados', rejected: 'Rechazados', resolved: 'Resueltos' };
export const rejectReasons = { duplicate: 'Ya existe en el catálogo', not_a_part: 'No es un repuesto', bad_data: 'Datos del proveedor incorrectos', other: 'Otro motivo' };
export const sourceLabels: Record<FieldSource['source'], string> = { supplier: 'del proveedor', oem_library: 'de la biblioteca OEM', admin: 'editado', ai: 'propuesto por IA' };
