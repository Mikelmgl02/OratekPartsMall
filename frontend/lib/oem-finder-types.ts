export type OEMApplyTier = 'AUTO_FLAG_CURRENT' | 'AUTO_RENAME_BASE' | 'STRONG_PENDING_OWNER_TAGS';
export type OEMRunMode = 'dry_run' | 'apply_auto' | 'ai';
export type OEMBatch = { index: number; applied: number; conflicts: number; skipped: number; errors: number };
export type OEMApplySummary = {
  applied?: number; conflicts?: number; skipped?: Record<string, number>; errors?: number; tiers?: Partial<Record<OEMApplyTier, number>>;
  batches?: OEMBatch[]; stopped?: 'halted' | 'catalog_import' | 'errors' | null; excluded?: Record<string, number>; selected?: number; spot_check?: number[];
};
export type OEMRun = {
  id: number; mode: OEMRunMode; status: 'running' | 'completed' | 'failed'; rules_version: string; suffix_table_version: string;
  scope: { tier?: string | null; limit?: number | null; canary?: number | null; in_stock_first?: boolean; batch_size?: number; stage?: string };
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

export const tierLabels: Record<string, string> = {
  AUTO_FLAG_CURRENT: 'SKU YA ES EL OEM', AUTO_RENAME_BASE: 'RENOMBRAR A LA BASE OEM', STRONG_PENDING_OWNER_TAGS: 'RENOMBRAR (ETIQUETAS CONFIRMADAS)',
};
export const stoppedLabels: Record<string, string> = {
  halted: 'DETENIDA POR LA REGLA DE DETENCIÓN', catalog_import: 'DETENIDA: IMPORTACIÓN DEL CATÁLOGO EN CURSO', errors: 'DETENIDA POR ERRORES INESPERADOS',
};
export const skipLabels: Record<string, string> = {
  snapshot_changed: 'CAMBIÓ DESDE EL ANÁLISIS', new_claimant: 'OTRO SKU RECLAMA EL NÚMERO', missing: 'YA NO EXISTE', retired: 'RETIRADO O AGRUPADO',
  canonical_differs: 'EL NÚMERO NO ES EL SKU', previously_reverted: 'REVERTIDO ANTES', open_conflict: 'CONFLICTO ABIERTO',
};
