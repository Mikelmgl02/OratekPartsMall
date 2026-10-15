// Productos nuevos (mall.sku_proposal_views): new catalog SKUs proposed from supplier items that match nothing in the catalog.
export type ProposalStatus = 'pending' | 'approved' | 'linked' | 'rejected' | 'resolved';
export type FieldSource = { source: 'supplier' | 'oem_library' | 'admin' | 'ai'; by?: string; at?: string; confidence?: number; note?: string; run?: string };
export type ProposalItem = { id: string; supplier: string; supplier_name: string; supplier_invent_id: string; codigo: string; brand: string;
  description: string; references: { brand: string; code: string; ref_type?: string }[]; source: 'upload' | 'apiag'; available: number; matching_status: string };
export type Proposal = { id: string; status: ProposalStatus; status_label: string; version: number; sku: string; description: string; brand: string;
  oem: { code: string; brand: string } | null; part_type: { id: string; category: string; name: string } | null;
  references: { brand: string; code: string; ref_type?: string }[]; field_sources: Partial<Record<'sku' | 'description' | 'oem' | 'references' | 'part_type' | 'internal_code', FieldSource>>;
  internal_code: boolean;   // a supplier's own code (merchandise, tools): approved without filing it as an OEM number
  ai_review: AiReview | null;   // the latest Revisión IA verdict (pending proposals only)
  item_count: number; supplier_count: number; stock: number; items: ProposalItem[]; part: { id: string; sku: string } | null;
  decided_by: string | null; decided_at: string | null; reject_reason: string; reject_reason_label: string; reject_note: string; created_at: string; updated_at: string;
  earlier: { id: string; sku: string } | null;   // the same product approved or linked before: its items go to that SKU
  catalog_match?: { id: string; sku: string; code: string; active: boolean }[] };   // the detail only: catalog SKUs its code or OEM number already belongs to
export type ProposalSummary = Record<ProposalStatus, number>;
export type BulkResult = { action?: 'approve' | 'reject' | 'link'; done?: number; approved: number; results: { id: string; ok: boolean; sku?: string; message?: string }[] };

// Revisión IA (mall.sku_review_views): good = approve as is, bad = reject (or link to the SKU the catalog already has), unsure = a person decides.
export type Verdict = 'good' | 'bad' | 'unsure';
export type ReviewTab = Verdict | 'stale' | 'decided';
export type ReviewSuggestions = { part_type?: { category: string; name: string; confidence: number }; description?: { value: string; confidence: number; drafted?: boolean };
  internal_code?: { confidence: number }; duplicate_of?: { kind: 'proposal' | 'part' | 'line'; id: string; sku: string; descripcion: string } };   // part and line: an OEM line (S8)
export type AiReview = { verdict: Verdict; confidence: number; reasons: string[]; reason: string; suggestions: ReviewSuggestions; stale: boolean; run: string;
  link_part: { id: string; sku: string } | null; created_at: string };
export type ReviewRunSummary = { id: string; search: string; only_unreviewed: boolean; created_by: string; created_at: string; finished_at: string | null; model: string;
  metrics: Record<string, number>; total_items: number; batch_size: number; completed_batches: number; total_batches: number; status: 'ready' | 'running' | 'completed' };
export type ReviewVerdictRow = { id: string; proposal: string; state: 'fresh' | 'stale' | 'superseded' | 'decided'; method: 'rule' | 'ai'; verdict: Verdict; reason: string; reason_label: string;
  confidence: number; reasons: string[]; suggestions: ReviewSuggestions; applied: { written?: string[]; kept?: string[]; skipped?: string }; reviewed_version: number;
  link_part: { id: string; sku: string } | null; supplier_description: string; stock: number; signals: string[];
  current: { version: number; status: ProposalStatus; status_label: string; sku: string; description: string; brand: string; internal_code: boolean; oem: { code: string; brand: string } | null;
    part_type: { id: string; category: string; name: string } | null; field_sources: Proposal['field_sources']; part: { id: string; sku: string } | null } };
export type ReviewCounts = Record<ReviewTab | 'link' | 'reject', number>;
export type ReviewReport = ReviewRunSummary & { counts: ReviewCounts; verdict: ReviewTab; offset: number; count: number; next_offset: number | null; results: ReviewVerdictRow[] };
export type ReviewOverview = { configured: boolean; cap_micro_usd: number; month_spend_micro_usd: number; eligible_count: number; unreviewed_count: number; runs: ReviewRunSummary[] };
export type ActionableRow = { id: string; version: number; sku: string; description: string; reason: string; note: string; part: string | null;
  internal_code: boolean; oem: { code: string; brand: string } | null };

export const verdictLabels: Record<Verdict | 'stale', string> = { good: 'BUENA', bad: 'MALA', unsure: 'DUDOSA', stale: 'CAMBIÓ' };

// The tabs by the line's estado (S6: a product is a line in preparación; approving publishes it, linking unifies it into another).
export const proposalStatusLabels: Record<ProposalStatus, string> = { pending: 'En preparación', approved: 'Publicados', linked: 'Unificados', rejected: 'Rechazados', resolved: 'Resueltos' };
export const rejectReasons = { duplicate: 'Ya existe en el catálogo', not_a_part: 'No es un repuesto', bad_data: 'Datos del proveedor incorrectos', other: 'Otro motivo' };
export const sourceLabels: Record<FieldSource['source'], string> = { supplier: 'del proveedor', oem_library: 'de la biblioteca OEM', admin: 'editado', ai: 'propuesto por IA' };
