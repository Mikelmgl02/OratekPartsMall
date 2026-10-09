import type { CatalogImage, ManagedPart } from './types';

// Separar SKU (mall.catalog_split): a canonical SKU, the SKUs a grouping retired into it and where each row most likely came from.
export type SplitPart = { id: string; sku: string; name: string; description: string; category: string; subcategory: string; is_OEM: boolean; active: boolean };
export type SplitChild = SplitPart & {
  merged_at: string | null; merged_by: string; automatic: boolean; method: string;
  /** The alternos it had when it was grouped; null when nothing recorded them. */
  codes: { brand: string; code: string }[] | null;
};
/** suggested: the grouped SKU a row most likely came from ('' = it stays). alias_of: the grouped SKU whose own code this alterno is. */
export type SplitCode = { id: number; brand: string; code: string; ref_type: 'unknown' | 'oem' | 'company'; reference_source: string; suggested: string; alias_of: string; alias_returns: boolean };
export type SplitItem = { id: string; supplier_name: string; supplier_invent_id: string; codigo: string; brand: string; description: string; available_quantity: number; suggested: string };
export type SplitImage = CatalogImage & { suggested: string };
export type SplitPlan = { target: SplitPart & { created_by_merge: boolean }; children: SplitChild[]; codes: SplitCode[]; items: SplitItem[]; images: SplitImage[]; warnings: string[] };
export type SplitRestore = { id: string; codes: number[]; items: string[]; images: string[] };
export type SplitResult = {
  split_id: string; target: ManagedPart; restored: ManagedPart[];
  summary: { restored_skus: number; codes_moved: number; aliases_removed: number; items_moved: number; images_moved: number; links_moved: number; links_shared: number; target_retired: boolean };
};
export type MergedSku = { id: string; sku: string; description: string; active: boolean; merged_count: number; last_merged_at: string | null; automatic: boolean; merged: { sku: string; description: string }[] };
