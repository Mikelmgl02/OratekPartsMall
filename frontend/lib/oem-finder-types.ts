export type OEMApplyTier = 'AUTO_FLAG_CURRENT' | 'AUTO_RENAME_BASE' | 'STRONG_PENDING_OWNER_TAGS';
export type OEMRunMode = 'dry_run' | 'apply_auto' | 'ai';
export type OEMBatch = { index: number; applied: number; conflicts: number; skipped: number; errors: number };
export type OEMApplySummary = {
  applied?: number; conflicts?: number; skipped?: Record<string, number>; errors?: number; tiers?: Partial<Record<string, number>>;
  batches?: OEMBatch[]; stopped?: 'halted' | 'catalog_import' | 'errors' | 'cancelled' | null; excluded?: Record<string, number>; selected?: number; spot_check?: number[];
  done?: number; total?: number;
};
export type OEMRun = {
  id: number; mode: OEMRunMode; status: 'running' | 'completed' | 'failed'; rules_version: string; suffix_table_version: string;
  scope: { tier?: string | null; limit?: number | null; canary?: number | null; in_stock_first?: boolean; batch_size?: number; stage?: string;
    action?: 'approve' | 'choose_oem' | 'approve_batch'; sku?: string; case?: number; filters?: Record<string, string>; operation_id?: string };
  tiers: Record<string, [number, number]>; applied: OEMApplySummary; errors: { error: string; detail: string; sku?: string }[];
  actor: string | null; started_at: string; finished_at: string | null; changes: number; reverted: number;
};
export type OEMHalt = { reason: string; run: number | null; created_by: string | null; created_at: string };
export type OEMRunPage = { count: number; next: string | null; previous: string | null; results: OEMRun[]; halt: OEMHalt | null };
export type OEMSpotRow = {
  run: number; change: number; batch: number; tier: OEMApplyTier; method: string; part_id: string; previous_sku: string; sku: string; current_sku: string;
  code: string; brand: string; description: string; system: string; grade: string; lexicon: string; lexicon_share: number | null; lexicon_keys: number | null;
  class_heads: string; units: string; attributions: string; suppliers: number; available_quantity: number; reference_source: string; reasons: string;
  reverted_at: string; verdict: string; evidence: Record<string, unknown>;
};
export type OEMSpotCheck = { run: number; columns: string[]; rows: OEMSpotRow[]; csv: string };
export type OEMRevertResult = { run: number; reverted: number; already_reverted: number; skipped: { change: number; sku: string; reason: string; label: string; detail: string }[] };

// Pendientes de aplicación automática (mall.oem_auto): AUTO rows the next apply run would take, per finder tier (tab) and apply tier.
export type OEMAutoTier = 'AUTO_FLAG_CURRENT' | 'AUTO_RENAME_BASE';
export type OEMPendingStatus = 'pending' | 'applied' | 'sent_to_review' | 'excluded' | 'stale';
export type OEMPendingMode = 'canary' | 'batch';
export type OEMPendingFilters = { chain: string; make: string; in_stock: string; search: string };
export type OEMPendingToken = { sep: string; tok: string; token: string; class: 'TAG' | 'VARIANT' | 'UNKNOWN'; kind: string; status: string; auto_eligible: boolean };
export type OEMPendingRow = {
  id: number; part_id: string; sku: string; evaluated_sku: string; description: string; tier: OEMAutoTier; apply_tier: OEMApplyTier; candidate: string;
  written_form: string; brand: string; makes: string[]; system: string; grade: string; chain: string; chain_tokens: OEMPendingToken[]; owner_tags: string[];
  method: 'flag' | 'rename'; in_stock: boolean; available_quantity: number;
  evidence: { lexicon: { result: string | null; share: number | null; n_other_keys: number | null; top_heads: [string, number][] }; units: string[];
    attributions: string[]; n_suppliers: number; verified_oem_alterno: boolean; second_unit: boolean; genuine_tokens: string[]; siblings: string[];
    make_source: string; head: string; reasons: string[]; warnings: string[] };
  status: OEMPendingStatus; reason: string; reason_label: string; stale: string; stale_label: string; note: string; decided_by: string | null;
  decided_at: string | null; updated_at: string; run: number | null;
};
export type OEMApplyTierState = { pending: number; in_stock: number; canary_done: boolean };
export type OEMPendingPage = {
  count: number; next: string | null; previous: string | null; results: OEMPendingRow[]; tiers: Partial<Record<OEMAutoTier, number>>;
  apply_tiers: Partial<Record<OEMApplyTier, number>>; statuses: Partial<Record<OEMPendingStatus, number>>;
  facets: { chains: [string, number][]; makes: [string, number][]; systems: [string, number][] }; apply: Record<OEMApplyTier, OEMApplyTierState>;
  // rules_version: optional while an API older than oem-finder-3 may still answer (the canary credit is per rules version)
  sizes: Record<OEMPendingMode, number>; rules_version?: string; halt: OEMHalt | null; busy: { lock: boolean; running: number | null };
  last_refresh: { id: number; mode: OEMRunMode; stage: string; finished_at: string; rules_version?: string } | null;
};
export type OEMPendingPreviewRow = {
  part_id: string; sku: string; description: string; oem: string; brand: string; method: 'flag' | 'rename'; outcome: 'applied' | 'conflict' | 'skipped'; detail: string;
  available_quantity: number; in_stock: boolean;
};
export type OEMPendingPreview = {
  tier: OEMApplyTier; mode: OEMPendingMode; size: number; eligible: number; selected: number; would_apply: number; conflicts: number; skipped: Record<string, number>;
  excluded: Record<string, number>; rows: OEMPendingPreviewRow[]; canary_done: boolean; halt: { reason: string; created_at: string } | null;
  suffix_table_version: string; seconds: number;
};
export type OEMPendingApplyResult = { run: OEMRun; replayed: boolean };
export type OEMSendToReviewResult = {
  sent: { part_id: string; sku: string; case: number }[]; already: { part_id: string; sku: string }[]; skipped: { part_id: string; sku: string; reason: string; label: string }[];
};

export const tierLabels: Record<string, string> = {
  AUTO_FLAG_CURRENT: 'SKU YA ES EL OEM', AUTO_RENAME_BASE: 'RENOMBRAR A LA BASE OEM', STRONG_PENDING_OWNER_TAGS: 'RENOMBRAR (ETIQUETAS CONFIRMADAS)',
};
export const stoppedLabels: Record<string, string> = {
  halted: 'DETENIDA POR LA REGLA DE DETENCIÓN', catalog_import: 'DETENIDA: IMPORTACIÓN DEL CATÁLOGO EN CURSO', errors: 'DETENIDA POR ERRORES INESPERADOS',
  cancelled: 'DETENIDA POR EL USUARIO',
};
export const skipLabels: Record<string, string> = {
  snapshot_changed: 'CAMBIÓ DESDE EL ANÁLISIS', new_claimant: 'OTRO SKU RECLAMA EL NÚMERO', missing: 'YA NO EXISTE', retired: 'RETIRADO O AGRUPADO',
  canonical_differs: 'EL NÚMERO NO ES EL SKU', previously_reverted: 'REVERTIDO ANTES', open_conflict: 'CONFLICTO ABIERTO', sent_to_review: 'ENVIADO A REVISIÓN',
};
export const autoTierLabels: Record<OEMAutoTier, string> = { AUTO_FLAG_CURRENT: 'SKU YA ES EL OEM', AUTO_RENAME_BASE: 'RENOMBRAR A BASE OEM' };
// A tab's apply tiers, each with its own canary credit: renames resting on owner-confirmed tags (G, NP) apply apart from seed tags (MOBIS, HMC).
export const applyTiersOf: Record<OEMAutoTier, OEMApplyTier[]> = { AUTO_FLAG_CURRENT: ['AUTO_FLAG_CURRENT'], AUTO_RENAME_BASE: ['STRONG_PENDING_OWNER_TAGS', 'AUTO_RENAME_BASE'] };
export const pendingExcludedLabels: Record<string, string> = {
  sent_to_review: 'se enviaron a la revisión OEM', open_conflict: 'tienen un conflicto de aplicación abierto', previously_reverted: 'se revirtieron antes con el mismo OEM',
};
